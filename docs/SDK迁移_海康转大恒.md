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

  > ✅ **这一条现在程序会自动查，并且查到了就拒绝启动**：启动时（在
  > `gx.DeviceManager()` 之前）读注册表枚举每张网卡绑定的 NDIS 过滤驱动，
  > **发现同一张网卡上挂了 ≥2 个疑似 GigE 过滤驱动就直接拒绝启动**
  > （`run_production_mode()` 返回 1，不会走到开相机那一步），并打印出
  > 是哪张网卡、哪几个驱动、以及修复命令。
  > 实现在 `General_Tool/NicFilterCheck.py`，接线在 `main_yolo.py` 的
  > `run_production_mode()` 开头。
  >
  > **判定门槛**：只在"确实查到了、且确认有冲突"时才拦。读不到注册表
  > （权限不足、非 Windows 开发机）时**放行**，只留一条"没查成"的日志 ——
  > 读不到跟"装了两个"是两回事，不能混。
  >
  > **没有运行时绕过方式**（环境变量、命令行参数都不认）。唯一的关闭开关是
  > `config.yaml` 里的 `logging.nic_filter_check: false`，需要能改到那台机器上
  > 的配置文件。这是整个检查功能的总开关，关掉之后连日志都不再打印。
  >
  > 日志里还会**无条件打印每张网卡的完整过滤驱动清单**——因为内置的厂商
  > 名字表不可能覆盖所有驱动。如果日志说"未识别到任何疑似 GigE 过滤驱动"
  > 但现场确实在用 GigE 相机，把清单里的驱动名补进
  > `logging.nic_filter_patterns` 即可。
  >
  > 注意：查不到（权限不足等）时只是日志里说一声，**不会影响相机启动**。
  >
  > **也可以让程序代劳**，但只能在停产时由人工执行，不在启动路径上：
  >
  > ```
  > python -m General_Tool.NicFilterCheck          # 预演，只看计划不动任何东西
  > python -m General_Tool.NicFilterCheck --yes    # 以**管理员身份**执行
  > ```
  >
  > 三道闸：**非管理员直接拒绝**、**不加 `--yes` 只预演**、**绝不禁用大恒自家的
  > 过滤驱动**（`KEEP_PATTERNS` 硬编码，不是配置项）。另外还会保证**不会把某张
  > 网卡上最后一个 GigE 过滤驱动也禁掉**。
  >
  > ⚠️ 执行后网卡会短暂 reset，**8 路相机会全部断线重连**，务必停产时做。
  > ⚠️ 这一步在 Windows 上**未经验证**，现场第一次请先跑预演、核对打印出来的
  > PowerShell 命令，再执行。
- **巨型帧（Jumbo Frame）**：交换机和网卡必须同时开（一般 9014 字节），
  只开一边会造成大量分片/丢包，CPU 被软中断打满。
- **网卡收包缓冲 / 中断亲和性**：8 路 GigE 相机建议每路独占一个 CPU 核。
- **内存与页面文件**：`buffer_count: 10` 在 8 路 2440×2048 下约占 400MB
  SDK 缓冲，内存紧张时把这个值调小。

### 7. GigE 包长超过链路承载，导致相机启动不起来（已加防护）

**现场事故**：用海康 SDK 时，实际发出的包超过了设置的包长，相机启动不起来，
而巨型帧模式明明是开着的。

根因：`GevSCPSPacketSize` 是**相机端**的设置。SDK 的 `range_check` 只校验
它是否落在**相机自己**支持的范围里（`lib/gxipy/Feature.py::IntFeature.set`），
**它完全不知道链路上能过多大的包**。所以"交换机开了巨型帧 + 配置写了 8164"
并不构成任何保证——链路上任何一跳的 MTU 更小，包就被丢弃，表现为相机启动
不起来或者只出不完整帧。

现在的三道防线（`Camera_Tool/CameraOperation.py`）：

| 防线 | 实现 | 保证 |
|---|---|---|
| 实测链路 MTU | `probe_link_mtu()` 用 `psutil` 找到与相机 IP **同网段**的本机网卡，取它的 MTU | 以实测值为硬上限；探测不到返回 0 |
| 收敛包长 | `clamp_packet_size()` 纯函数 | **结果永不超过链路 MTU**；同时受相机 min/max 约束；按步进**向下**对齐；低于 1500 直接放弃 |
| 回读校验 | `_apply_packet_size_impl()` 设置后再 `feature.get()` | `set()` 不抛异常 ≠ 相机采纳了；回读不一致就告警并记录实际值 |

外加一条**契约**：`_apply_packet_size()` 外层套了兜底 `try/except`，
任何未预期异常都只告警、放弃修改、沿用相机当前值——**绝不挡住相机启动**。

`config.yaml → cameras.acquisition.packet_size` 的三种取值：

| 值 | 行为 |
|---|---|
| `0`（推荐，当前默认） | 自动：按实测 MTU 取最大安全值。巨型帧开了自动用上，没开自动退回 1500 |
| `>0` | 指定值，但**仍被实测 MTU 夹紧**并告警。配置写错也不会把链路打挂 |
| `-1` | 完全不碰这个特征，沿用相机出厂/上次保存的值 |

保守性是有意的：**MTU 探测不到时一律退回 1500，不冒险开巨型帧**。
宁可少拿一点带宽，也不要一台起不来的相机。

现场判读（`get_diag_info()` 与健康心跳都有）：

| 读数 | 含义 |
|---|---|
| `packet_size_applied` ≈ `link_mtu` | 正常，巨型帧已生效 |
| `packet_size_applied` = 1500 但现场开了巨型帧 | MTU 没探测到（通常是相机 IP 与网卡不同网段），看 `packet_size_note` |
| `packet_size_note` 含"回读不一致" | 相机把包长夹到了别的档位，按 note 里给的值改配置 |
| `stream.incomplete` 持续增长 | 包被丢，帧收不全 —— 包长或带宽问题 |

回归测试：`Test_Tool/test_packet_size_safety.py`，27 例。其中
`test_never_exceeds_link_mtu_across_sweep` 是性质测试，遍历
MTU × 期望值 × 相机上限的组合，逐一断言结果不超过 MTU。

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
4. 交换机 + 网卡都打开**巨型帧**（两边都要开，只开一边会造成大量分片/丢包）。
   **`config.yaml → cameras.acquisition.packet_size` 保持 `0` 不要改**：
   `0` 是自动模式，代码会实测本机网卡 MTU 后取最大安全值，巨型帧开了就自动
   用上。
   > 不要手工写死成 `8164` 或 `9000`。现场出过"巨型帧明明开着、但相机启动
   > 不起来"，根因就是包长只在相机侧校验范围、没人管链路能过多大的包。
   > 现在即使写死了，代码也会用实测 MTU 夹紧并告警；但保持 `0` 最稳。
   > 想完全不动这个特征就设 `-1`。
5. 跑一次 `python main_yolo.py`，在日志里确认每台相机都有
   `设备 [SN] ... 启动成功！(TriggerSelector=FrameStart, TriggerMode=On, TriggerSource=Line0, BufferCount=10)`，
   以及一行 `设备 [SN] GigE 包长设为 N (链路 MTU=..., 相机范围=...)`。
   **如果这行里的 N 是 1500 而现场确实开了巨型帧，说明 MTU 没探测到**
   （通常是相机 IP 与网卡不同网段），日志里会有对应的 WARNING。
6. 运行中随时可以调 `CameraOperation.get_diag_info()` 看
   `stream.lost`（SDK 侧丢帧数）、`stream.incomplete`（不完整帧数），
   以及 `link_mtu` / `packet_size_applied` / `packet_size_note`。
   这几个数持续增长或 note 非空 = 网络/包长问题，不是软件问题。
   健康心跳里也导出了同样的字段（`{相机名}.lost` / `.incomplete` / `.mtu` / `.pkt`）。

---

## 六、无硬件回归测试

```bash
python -m pytest Test_Tool/test_camera_operation.py -v      # 25 passed
python -m pytest Test_Tool/test_packet_size_safety.py -v    # 27 passed
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
