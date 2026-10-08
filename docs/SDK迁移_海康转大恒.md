# 相机 SDK 迁移：海康 MvCamera → 大恒图像 Galaxy (gxipy)

> 背景：现场实际使用的是**大恒图像**的 GigE 工业相机，但代码一直用的是
> **海康威视 MVS SDK**（`lib/MvImport` + `MvCameraControl.dll`）。
> 海康 SDK 能靠 GigE Vision 通用协议枚举到第三方相机并勉强取图，
> 但它加载的是海康自己的网卡过滤驱动和 GVSP 收包栈，与大恒相机固件的
> 心跳 / 包重传 / 包长协商行为并不匹配。这是"工厂环境下电脑突然卡死"
> 的重要嫌疑之一。本次把 SDK 整体换成大恒官方的 `gxipy`。

---

## 一、"电脑突然卡死"的代码侧根因分析

下面每一条都对应旧代码里可以指出的具体行，**不是泛泛而谈**。
按"最可能导致整机无响应"到"次要"排序。

### 1. 用 `ctypes.py_object` 把 Python 对象地址当 `void*` 传给 C 层（已消除）

旧 `Camera_Tool/CameraOperation.py`：

```python
def Creat_IntPtr(str_value):
    user_ptr = ctypes.py_object(str_value)     # 只是把地址拿出来
    return user_ptr

def Read_IntPtr_Value(int_part_value):
    user_value = ctypes.cast(int_part_value, ctypes.py_object).value
    return user_value
```

`Registration_callback()` 里 `pUser = Creat_IntPtr(self.st_serial_number)`，
而 `MV_CC_RegisterImageCallBackEx` 的 argtypes 是 `(c_void_p, c_void_p, c_void_p)`
（见 `git show HEAD:lib/MvImport/MvCameraControl_class.py` 第 242 行），
于是 `py_object` 被**降级成一个裸地址**存进 SDK，`pUser` 这个包装对象在
函数返回后就被回收了。

回调里再用 `ctypes.cast(ptr, ctypes.py_object).value` 把这个整数
**当作一个活的 PyObject 指针**解引用：

- 不增加引用计数；
- 不做任何类型检查；
- 内存早已被 CPython 分配器回收/复用时，读到的就是垃圾对象头。

这类问题的典型表现恰恰是 **进程没有任何 Python 回溯地卡死或访问违例**
——因为崩溃发生在 C 层，`try/except` 和 `faulthandler` 都抓不到。
这与"未知原因卡死"的现场描述高度吻合。

**现在**：`gxipy` 的 `DataStream.register_capture_callback()` 直接接收一个
普通 Python 函数，并在 `DataStream.__py_capture_callback` 上自己持有引用，
完全不需要手工搬运对象地址。`Creat_IntPtr` / `Read_IntPtr_Value` 已删除。

### 2. 按"猜的长度"手工读 SDK 原始缓冲区（已消除）

旧 `main_yolo.py::image_callback`：

```python
buff_size = st_frame_info["nWidth"] * st_frame_info["nHeight"]
raw = ctypes.string_at(pData, buff_size)
```

`width * height` 只有在"每像素恰好 1 字节且不带 chunk data"时才等于真实帧长。
一旦相机输出切成 10/12bit packed、或开启了 chunk data，这里就会**欠读或越界读**
一块由 SDK 管理的内存。越界读在 Windows 上跨过页边界就是访问违例。

**现在**：统一走 `RawImage.get_numpy_array()`，长度由 SDK 自己给出的
`image_size` 决定，代码里不再出现任何手算的缓冲区长度。

### 3. 每帧白拷一份 45MB + 采集缓冲只有 1 个节点（已消除）

旧回调链每帧的内存开销：

| 步骤 | 额外分配 |
|---|---|
| `ctypes.string_at(pData, w*h)` | ~5 MB（2440×2048） |
| `np.frombuffer(raw).copy()` | ~5 MB |
| `cv2.cvtColor(BAYER_GB2RGB)` | ~15 MB |
| `cv2.resize(..., (nWidth, nHeight))` | ~15 MB（**尺寸完全没变，纯浪费**） |

最后那次 `cv2.resize` 目标尺寸和源尺寸一模一样，等于每帧每相机白扔 15MB。
8 路相机 × 触发频率一上来，GC 压力和页面文件抖动足以把整机拖到假死。

再叠加旧的 `MV_CC_SetImageNodeNum(1)` —— **SDK 内部只有 1 个缓冲节点**，
回调稍慢一点就没有可用缓冲，直接丢帧甚至阻塞收包线程。

**现在**：
- 去掉那次无意义的 `resize`；
- 转换交给大恒 `DxImageProc`（SIMD 优化），Python 侧只留一次 `copy()`
  用于把数据脱离 SDK 环形缓冲；
- 缓冲节点数改为可配置，默认 **10**（`config.yaml → cameras.acquisition.buffer_count`）。

### 4. 强制杀线程的"工具函数"（已删除）

旧 `CameraOperation.py` 顶部有 `Async_raise()` / `Stop_thread()`，
用 `PyThreadState_SetAsyncExc` 从外部把一个线程掐掉。
**在 SDK 原生调用进行到一半时把线程从底下抽走，是让进程死锁的经典手法。**
这两个函数全项目没有任何调用点，属于海康示例代码残留，已删除。

### 5. 硬编码的 Bayer 相位（会导致颜色错，不是卡死，但顺手修了）

旧代码写死 `cv2.COLOR_BAYER_GB2RGB`。换一台 Bayer 相位不同的相机，
红蓝通道就反了，而且**不会报任何错**。现在按 `pixel_format` 走 SDK 转换，
并保留一条按格式查表的 OpenCV 兜底路径。

### 6. 需要在现场排查的、代码之外的因素

这些我**无法在本仓库里验证**，请现场按顺序确认：

- **网卡上是否同时装了海康和大恒两个 GigE 过滤驱动**。
  两个过滤驱动挂同一张网卡是"整机网络栈卡死"的高频原因。
  换 SDK 后请把海康 MVS 的 GigE Filter Driver 卸载或禁用，只留大恒的。
- **巨型帧（Jumbo Frame）**：交换机和网卡必须同时开（一般 9014 字节），
  只开一边会造成大量分片/丢包，CPU 被软中断打满。
- **网卡收包缓冲 / 中断亲和性**：8 路 GigE 相机建议每路独占一个 CPU 核。
- **内存与页面文件**：`buffer_count: 10` 在 8 路 2440×2048 下约占 400MB
  SDK 缓冲，内存紧张时把这个值调小。

---

## 二、改了哪些文件

| 文件 | 变更 |
|---|---|
| `lib/gxipy/` | **新增**。从 `Development/Samples/Python/gxipy` 复制过来的大恒官方 Python SDK（纯 Python，随仓库分发） |
| `lib/gxipy/DeviceManager.py` | 删掉 `from numpy.compat import long`。`numpy.compat` 在 NumPy 2.0 已被移除，而本项目钉的是 numpy 2.2.5，这行一 import 就 `ImportError`；`long` 只被 Python 2 的死分支引用 |
| `lib/MvImport/` | **删除**（海康 Python 封装） |
| `Samples/` | **删除**（4 个海康示例脚本，无人引用） |
| `Camera_Tool/CameraOperation.py` | **重写**为 gxipy 实现，对外接口保持不变 |
| `main_yolo.py` | 枚举/打开/回调/收尾全部改成 gxipy；删掉 ctypes 回调 trampoline |
| `main_yolo.spec` | PyInstaller 从收 `MvImport`+海康 DLL 改成收 `gxipy` |
| `config.yaml` / `AnomalyDetection_Tool/config/settings.py` | 新增 `cameras.acquisition` 段与 `CAMERA_ACQ_CONFIG` |
| `requirements.txt` | 运行时依赖说明从海康 MVS 改成大恒 Galaxy |
| `Test_Tool/fake_galaxy_sdk.py`<br>`Test_Tool/test_camera_operation.py` | **新增**，无硬件回归测试（25 个用例） |

**没有改**：`Camera_Tool/CameraRegistry.py`、`Communication_Tool/Modbus.py`、
`General_Tool/BackgroundTaskManager.py`。`CameraOperation` 的方法名、返回值语义
和 `st_serial_number` / `st_mode_name` / `st_ip_address` / `b_open_device` /
`b_start_grabbing` 等属性全部保留，所以调度侧一行没动。

---

## 三、新的相机层 API

```python
import gxipy as gx
from Camera_Tool.CameraOperation import CameraOperation

mgr = gx.DeviceManager()                      # 构造即 gx_init_lib()
num, dev_list = mgr.update_all_device_list(2000)

op = CameraOperation(
    mgr, dev_list[0],
    camera_name="camera1",
    buffer_count=10,            # SDK 采集缓冲节点数（旧实现硬编码 1）
    packet_size=0,              # GevSCPSPacketSize，0 = 不改
    heartbeat_timeout_ms=3000,  # GevHeartbeatTimeout，0 = 不改
    trigger_source="Line0",
)

op.Open_device()                              # 返回 0 / -1，不再抛异常
op.Registration_callback(handler)             # handler(image_bgr, frame_info)
op.Start_grabbing()
...
op.Set_trigger_source("Software")             # 停机预览
op.Trigger_once()                             # 软触发一帧
op.Stop_grabbing()
op.Close_device()
op.get_diag_info()                            # 帧计数 / 丢帧数 / 软触发计数
```

回调签名从海康的 `(pData, pFrameInfo, pUser)` 三指针变成了
`handler(image: np.ndarray, frame_info: dict)`：
`image` 已经是 **BGR / uint8 / HxWx3 的独立副本**（已脱离 SDK 环形缓冲，
可安全跨线程传递、可写），`frame_info` 含
`nWidth / nHeight / nFrameNum / timestamp / pixel_format / camera_sn / camera_name`。

### 几个有意的行为变化

1. **`Open_device()` 失败不再抛异常**，改为记日志 + 返回 `-1`。
   旧实现里一台相机没插好，`Open_all_devices()` 的裸调用就会把整个启动流程掀掉，
   其余 7 台也跟着不工作。
2. **`Stop_grabbing()` 会注销回调，`Start_grabbing()` 会重新注册**。
   这样"回调已注册但没在取流"的中间态不存在了，消掉了
   "C 线程正在回调、Python 侧回调对象已被置空"的竞态。
   `Set_trigger_source()` 走内部的停/开流，**不碰回调注册**。
3. **回调里的异常一律被吞掉并计数**（`callback_error_count`）。
   gxipy 自己的 `__on_capture_callback` 没有 try 包裹，异常穿透到 ctypes
   边界只会打印一行 "Exception ignored"，排查起来极其痛苦。

---

## 四、新增配置（`config.yaml`）

```yaml
cameras:
  sn_map:
    KBA26070032: "camera1"
    ...
  acquisition:
    buffer_count: 10          # 采集缓冲节点数（旧实现硬编码 1）
    packet_size: 0            # GevSCPSPacketSize，0=不修改；开了巨型帧可设 8164
    heartbeat_timeout_ms: 3000  # GigE 心跳超时，0=不修改
    trigger_source: "Line0"   # 开机默认触发源
    enum_timeout_ms: 2000     # 枚举设备超时
```

改完需重启（这些参数决定 SDK 对象怎么创建，热改不生效）。

---

## 五、现场部署清单

1. **卸载海康 MVS**（至少禁用它的 GigE Filter Driver），确认网卡上只剩大恒的过滤驱动。
2. **安装大恒 Galaxy 相机驱动**（含 GenICam 运行时）。装完确认环境变量
   `GALAXY_GENICAM_ROOT` 存在 —— `lib/gxipy/gxwrapper.py` 靠它定位
   `GxIAPI.dll` / `DxImageProc.dll`。缺失时启动日志会打印
   `大恒 GxIAPI 初始化失败`。
3. 用大恒自带的 **GalaxyView** 先把 8 台相机逐台连一遍，确认 SN 与
   `config.yaml → cameras.sn_map` 一致、IP 与网卡同网段。
4. 交换机 + 网卡都打开**巨型帧**；要生效就把 `packet_size` 设成 `8164`。
5. 跑一次 `python main_yolo.py`，在日志里确认每台相机都有
   `设备 [SN] ... 启动成功！(TriggerSelector=FrameStart, TriggerMode=On, TriggerSource=Line0, BufferCount=10)`。
6. 运行中随时可以调 `CameraOperation.get_diag_info()` 看
   `stream.lost`（SDK 侧丢帧数）和 `stream.incomplete`（不完整帧数）。
   这两个数持续增长 = 网络带宽/丢包问题，不是软件问题。

---

## 六、无硬件回归测试

```bash
python -m pytest Test_Tool/test_camera_operation.py -v
```

`Test_Tool/fake_galaxy_sdk.py` **只替换 ctypes 那一层**
（`gxipy.gxwrapper` / `gxipy.dxwrapper`，也就是真正会去 load
`GxIAPI.dll` / `DxImageProc.dll` 的两个模块）。`gxipy` 的
`DeviceManager` / `Device` / `DataStream` / `FeatureControl` / `Feature_s` /
`StatusProcessor` / `ImageProc`，以及被测的 `CameraOperation`，**全部跑真实代码**。

覆盖的关键行为（25 个用例）：

- 触发三层参数（TriggerSelector/TriggerMode/TriggerSource）与缓冲节点数确实写下去了
- 交到回调手上的数组是 **BGR/uint8/HxWx3、连续、可写**
- **交给下游的数组是独立副本**：回调返回后把 SDK 缓冲区改成全 255，
  下游拿到的数据不变（这条守的就是 SDK 环形缓冲复用的坑）
- 传给 SDK 转换器的通道序确实是 `ORDER_BGR`（红蓝不会反）
- 不完整帧被丢弃、回调里的异常不会穿透到 C 层
- 大恒转换器不可用时 OpenCV 兜底能出图
- 灰度 / 10bit 灰度路径，后者必须降回 uint8
- 软触发只在 `TriggerSource=Software` 时真正生效
- 切触发源会停流→改参数→重新开流，且**不注销回调**
- Stop/Start 往返、Close 幂等、打开失败不抛异常
