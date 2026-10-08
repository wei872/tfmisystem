#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
相机操作封装 —— **大恒图像 Galaxy (GxIAPI / gxipy) SDK**

本模块以前基于海康 MvCamera SDK 编写，但现场实际用的是大恒图像的相机
（GigE 接口，GenICam 协议）。海康 MVS 虽然能靠 GigE Vision 通用协议枚举到
第三方相机并勉强取图，但它加载的是海康自己的网卡过滤驱动 / GVSP 收包栈，
与大恒相机固件的心跳、包重传、包长协商行为并不匹配，是现场"整机突然卡死"
的重要嫌疑之一。详见 docs/SDK迁移_海康转大恒.md。

本次改造同时顺手消除了几处**必然踩雷**的写法：

1. 不再用 ``ctypes.py_object`` 把 Python 对象地址当 ``void*`` 塞给 C 层、
   再在回调里 ``ctypes.cast(ptr, py_object)`` 取回来（旧的 Creat_IntPtr /
   Read_IntPtr_Value）。这种写法绕过了 CPython 的引用计数，一旦那个地址上
   的对象被回收并复用，回调里就会解引用一块已经释放/被别人占用的内存 ——
   表现为**没有任何 Python 回溯的进程卡死或访问违例**，和现场症状一致。
   gxipy 的 register_capture_callback 直接收一个普通 Python 函数，
   并在 DataStream 上自己持有引用，不存在这个问题。

2. 不再用 ``ctypes.string_at(pData, width*height)`` 手工按"猜的"长度读原始
   缓冲区。旧代码假设每像素恒为 1 字节且不带 chunk data，一旦相机输出格式
   变成 10/12bit packed 或开启 chunk，就会越界/欠读。现在统一走
   ``RawImage.get_numpy_array()``，长度由 SDK 给出的 image_size 决定。

3. 不再把"强制杀线程"(PyThreadState_SetAsyncExc) 留在相机模块里。
   在 SDK 原生调用中间把一个线程从底下抽掉，是让进程死锁的经典手法。

对外接口（方法名、返回值语义、st_serial_number 等属性）保持不变，
CameraRegistry / Modbus.TriggerControlSystem / BackgroundTaskManager 无需改动。
"""

import os
import sys
import threading
import time

# gxipy 内部用的是绝对导入（from gxipy.gxwrapper import *），
# 所以必须把 lib/ 目录挂到 sys.path 上，让 gxipy 成为顶层包。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LIB_DIR = os.path.join(_REPO_ROOT, "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

import numpy as np
import cv2

import gxipy as gx

from General_Tool.EnhancedLogger import info, error, warning
from Camera_Tool import CameraRegistry


# ----------------------------------------------------------------------
# 像素格式 → OpenCV Bayer 转换码（仅在 SDK 转换器不可用时兜底使用）
# ----------------------------------------------------------------------
_BAYER8_TO_CV = {
    gx.GxPixelFormatEntry.BAYER_GR8: cv2.COLOR_BAYER_GR2BGR,
    gx.GxPixelFormatEntry.BAYER_RG8: cv2.COLOR_BAYER_RG2BGR,
    gx.GxPixelFormatEntry.BAYER_GB8: cv2.COLOR_BAYER_GB2BGR,
    gx.GxPixelFormatEntry.BAYER_BG8: cv2.COLOR_BAYER_BG2BGR,
}

# 丢帧/异常日志的节流间隔（秒），避免每帧刷屏把日志文件打爆
_LOG_THROTTLE_SEC = 5.0


class CameraOperationError(RuntimeError):
    """相机操作失败"""


class CameraOperation:
    """单台大恒相机的生命周期与取流封装。

    典型用法::

        cam_op = CameraOperation(device_manager, dev_info, camera_name="camera1")
        cam_op.Open_device()
        cam_op.Registration_callback(my_handler)   # my_handler(image, frame_info)
        cam_op.Start_grabbing()
        ...
        cam_op.Stop_grabbing()
        cam_op.Close_device()
    """

    def __init__(self, device_manager, device_info: dict, camera_name: str = None,
                 buffer_count: int = 10, packet_size: int = 0,
                 heartbeat_timeout_ms: int = 0, trigger_source: str = "Line0"):
        """
        :param device_manager: gxipy 的 DeviceManager 实例（全局共用一个）
        :param device_info:    DeviceManager.update_all_device_list() 返回的
                               单台设备信息 dict（含 'sn'/'model_name'/'ip'/'index'）
        :param camera_name:    业务侧名字（camera1..camera8），用于 CameraRegistry
        :param buffer_count:   SDK 采集缓冲节点数。旧实现硬编码成 1，
                               回调稍慢一点 SDK 内部就没缓冲可用 → 直接丢帧。
        :param packet_size:    GigE 包长(GevSCPSPacketSize)。0 = 不修改，
                               用相机/网卡协商出来的默认值。
        :param heartbeat_timeout_ms: GigE 心跳超时。0 = 不修改。
                                     链路被拔/交换机抖动时，超时太长会让
                                     SDK 长时间阻塞在读包里，看起来就是"卡死"。
        :param trigger_source: 初始触发源，"Line0"(硬触发) 或 "Software"
        """
        self.device_manager = device_manager
        self.device_info = dict(device_info or {})

        # ---- 与旧实现保持一致的属性名（Modbus / BackgroundTaskManager 在用）----
        self.st_serial_number = str(self.device_info.get("sn", "") or "")
        self.st_mode_name = str(self.device_info.get("model_name", "") or "")
        self.st_ip_address = str(self.device_info.get("ip", "") or "")
        self.n_connect_num = int(self.device_info.get("index", 0) or 0)

        self.camera_name = camera_name or self.st_serial_number or f"camera_{self.n_connect_num}"

        self.cam_object = None            # gx.Device
        self._feature_control = None      # 设备远程层（相机本体）特征控制
        self._data_stream = None          # 0 号数据流

        self.b_open_device = False
        self.b_regist_callback = False
        self.b_start_grabbing = False

        self.trigger_source = trigger_source

        self.buffer_count = max(1, int(buffer_count or 1))
        self.packet_size = int(packet_size or 0)
        self.heartbeat_timeout_ms = int(heartbeat_timeout_ms or 0)

        # 用户回调：签名 handler(image_bgr: np.ndarray, frame_info: dict) -> None
        self._user_callback = None
        # 交给 SDK 的纯函数（register_capture_callback 要求 types.FunctionType，
        # 绑定方法会被它拒掉），同时自己留一份引用防止被 GC
        self._capture_handler = self._make_capture_handler()

        self._trigger_switch_lock = threading.RLock()
        self._log_throttle: dict = {}

        # ---- 诊断计数器（get_diag_info() 可查）----
        self._diag_soft_trigger_call_count = 0
        self._diag_soft_trigger_success_count = 0
        self._diag_soft_trigger_last_error = None
        self._diag_frame_count = 0
        self._diag_bad_frame_count = 0
        self._diag_convert_error_count = 0
        self._diag_callback_error_count = 0
        self._diag_last_frame_time = 0.0

    # ==================================================================
    # 内部工具
    # ==================================================================
    def _throttled(self, key: str, level, msg: str) -> None:
        """按 key 节流的日志，避免高频错误把日志刷爆（同时拖慢回调线程）"""
        now = time.time()
        if now - self._log_throttle.get(key, 0.0) >= _LOG_THROTTLE_SEC:
            self._log_throttle[key] = now
            level(msg)

    def _set_enum(self, name: str, value, warn_only: bool = False) -> int:
        """设置枚举型特征。成功返回 0，失败返回 -1（warn_only=False 时抛异常）"""
        try:
            self._feature_control.get_enum_feature(name).set(value)
            return 0
        except Exception as e:
            if warn_only:
                warning(f"设备 {self.st_serial_number} 设置 {name}={value} 失败: {e}")
                return -1
            raise CameraOperationError(
                f"设备 {self.st_serial_number} 设置 {name}={value} 失败: {e}") from e

    def _set_int(self, name: str, value: int) -> int:
        """设置整型特征（特征不存在时只告警，不致命）"""
        try:
            if not self._feature_control.is_implemented(name):
                warning(f"设备 {self.st_serial_number} 不支持特征 {name}，跳过")
                return -1
            self._feature_control.get_int_feature(name).set(int(value))
            return 0
        except Exception as e:
            warning(f"设备 {self.st_serial_number} 设置 {name}={value} 失败: {e}")
            return -1

    # ==================================================================
    # 打开 / 关闭
    # ==================================================================
    def Open_device(self) -> int:
        """打开设备并完成触发参数初始化。

        成功返回 0；失败返回 -1（**不抛异常**）。
        旧实现在这里直接 raise，一台相机没插好就会把整个产线的启动流程掀掉，
        现在改成记录日志 + 返回错误码，让其余相机照常工作。
        """
        if self.b_open_device:
            return -1

        try:
            cam = self.device_manager.open_device_by_sn(self.st_serial_number)
            if cam is None:
                error(f"设备 {self.st_serial_number} 打开失败（open_device_by_sn 返回 None）")
                return -1

            self.cam_object = cam
            self._feature_control = cam.get_remote_device_feature_control()
            self._data_stream = cam.data_stream[0]

            # ---- GigE 链路参数（仅 GigE 设备有这些特征，不支持则自动跳过）----
            if self.packet_size > 0:
                self._set_int("GevSCPSPacketSize", self.packet_size)
            if self.heartbeat_timeout_ms > 0:
                self._set_int("GevHeartbeatTimeout", self.heartbeat_timeout_ms)

            # ---- 触发体系：TriggerSelector → TriggerMode → TriggerSource ----
            # 三层都要显式设置。TriggerSelector 没设对的话，后面对
            # TriggerMode/TriggerSource 的写入可能作用在别的触发通道上，
            # 表现为"设置返回成功但拍照行为完全不受影响"。
            self._set_enum("TriggerSelector", "FrameStart", warn_only=True)
            self._set_enum("TriggerMode", "On")
            self._set_enum("TriggerSource", self.trigger_source)

            # ---- 采集缓冲节点数：必须在 stream_on 之前设置 ----
            try:
                self._data_stream.set_acquisition_buffer_number(self.buffer_count)
            except Exception as e:
                warning(f"设备 {self.st_serial_number} 设置缓冲节点数失败: {e}")

            self.b_open_device = True

            info(f"设备 [{self.st_serial_number}] ({self.st_mode_name} @ {self.st_ip_address}) "
                 f"启动成功！(TriggerSelector=FrameStart, TriggerMode=On, "
                 f"TriggerSource={self.trigger_source}, BufferCount={self.buffer_count})")

            CameraRegistry.register_camera(self.camera_name, self)
            return 0

        except Exception as e:
            error(f"设备 {self.st_serial_number} 打开失败: {e}")
            self._safe_teardown()
            return -1

    def _safe_teardown(self) -> None:
        """打开过程中出错时尽量把已申请的资源释放掉，不留半开状态"""
        try:
            if self._data_stream is not None and self.b_regist_callback:
                self._data_stream.unregister_capture_callback()
        except Exception:
            pass
        self.b_regist_callback = False
        try:
            if self.cam_object is not None:
                self.cam_object.close_device()
        except Exception:
            pass
        self.cam_object = None
        self._feature_control = None
        self._data_stream = None
        self.b_open_device = False
        self.b_start_grabbing = False

    def Close_device(self) -> int:
        """停止取流、注销回调并关闭设备。成功返回 0。"""
        if not self.b_open_device:
            warning(f"设备 {self.st_serial_number} 未开启，无需关闭")
            return -1

        if self.b_start_grabbing:
            self.Stop_grabbing()

        try:
            self.cam_object.close_device()
        except Exception as e:
            error(f"设备 {self.st_serial_number} 关闭设备失败: {e}")
        finally:
            self.cam_object = None
            self._feature_control = None
            self._data_stream = None
            self.b_open_device = False
            self.b_start_grabbing = False
            self.b_regist_callback = False
            self._user_callback = None

        CameraRegistry.unregister_camera(self.camera_name)
        return 0

    # ==================================================================
    # 回调
    # ==================================================================
    def _make_capture_handler(self):
        """构造一个真正的 function（不是 bound method）交给 SDK。

        gxipy 的 register_capture_callback 里有
        ``isinstance(callback_func, types.FunctionType)`` 的硬校验，
        直接传 self._on_capture（MethodType）会被拒。
        """
        def _handler(raw_image):
            self._on_capture(raw_image)

        return _handler

    def Registration_callback(self, callback) -> int:
        """注册用户图像回调。

        :param callback: ``handler(image: np.ndarray, frame_info: dict) -> None``
                         image 为 BGR、HxWx3、uint8、**已脱离 SDK 缓冲区的独立副本**；
                         frame_info 含 nWidth/nHeight/nFrameNum/timestamp/
                         camera_sn/camera_name/pixel_format。
        :return: 0 成功，-1 失败
        """
        if not callable(callback):
            error(f"设备 {self.st_serial_number} 回调注册失败：callback 不可调用")
            return -1
        if not self.b_open_device:
            error(f"设备 {self.st_serial_number} 注册图像回调失败! 设备未开启。")
            return -1

        self._user_callback = callback
        try:
            self._data_stream.register_capture_callback(self._capture_handler)
        except Exception as e:
            self._user_callback = None
            error(f"设备 {self.st_serial_number} 注册图像回调失败: {e}")
            return -1

        self.b_regist_callback = True
        return 0

    def _unregister_callback_internal(self) -> None:
        if self._data_stream is None or not self.b_regist_callback:
            return
        try:
            self._data_stream.unregister_capture_callback()
        except Exception as e:
            warning(f"设备 {self.st_serial_number} 注销图像回调失败: {e}")
        self.b_regist_callback = False

    def _on_capture(self, raw_image) -> None:
        """SDK 采集线程入口。

        ⚠ 运行在大恒 SDK 的 C 线程上：
          - 这里抛出的任何异常都会穿透到 ctypes 边界（gxipy 自己没有 try 包），
            所以**整体用 try/except 兜死**；
          - 任何阻塞都会造成丢帧，所以只做"取数组 + 转 BGR + 分发"。
        """
        try:
            self._diag_frame_count += 1
            self._diag_last_frame_time = time.time()

            if self._user_callback is None:
                return

            if raw_image.get_status() != gx.GxFrameStatusList.SUCCESS:
                self._diag_bad_frame_count += 1
                self._throttled(
                    "bad_frame", warning,
                    f"设备 {self.st_serial_number} 收到不完整帧(status="
                    f"{raw_image.get_status()})，已丢弃；累计 {self._diag_bad_frame_count} 帧。"
                    f"通常是网络丢包/带宽不足，请检查网卡巨型帧与收包缓冲设置。")
                return

            frame_info = {
                "nWidth": raw_image.get_width(),
                "nHeight": raw_image.get_height(),
                "nFrameNum": raw_image.get_frame_id(),
                "timestamp": raw_image.get_timestamp(),
                "pixel_format": raw_image.get_pixel_format(),
                "camera_sn": self.st_serial_number,
                "camera_name": self.camera_name,
            }

            image = self._to_bgr(raw_image)
            if image is None:
                return

            self._user_callback(image, frame_info)

        except Exception as e:
            # 绝不能让异常跑到 ctypes 边界之外
            self._diag_callback_error_count += 1
            self._throttled(
                "cb_error", error,
                f"设备 {self.st_serial_number} 回调处理失败: {e} "
                f"(累计 {self._diag_callback_error_count} 次)")

    # ==================================================================
    # 像素格式转换
    # ==================================================================
    def _to_bgr(self, raw_image):
        """把 SDK 原始帧转成 OpenCV 的 BGR uint8 数组（**独立副本**）。

        返回的数组必须在回调返回后依然有效 —— SDK 的环形缓冲在回调返回后
        就会被复用，所以这里一定要 copy 出来。
        """
        pixel_format = raw_image.get_pixel_format()

        try:
            if gx.Utility.is_gray(pixel_format):
                arr = self._gray_to_array(raw_image)
                if arr is None:
                    return None
                if arr.ndim == 3:
                    return arr
                return cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)

            rgb_image = raw_image.convert(
                "RGB", channel_order=gx.DxRGBChannelOrder.ORDER_BGR)
            if rgb_image is None:
                return self._bayer_fallback(raw_image, pixel_format)
            arr = rgb_image.get_numpy_array()
            if arr is None:
                return self._bayer_fallback(raw_image, pixel_format)
            # copy：既脱离 SDK 缓冲区，也让下游可写（frombuffer 出来的是只读的）
            return np.ascontiguousarray(arr)

        except Exception as e:
            self._diag_convert_error_count += 1
            self._throttled(
                "convert_error", error,
                f"设备 {self.st_serial_number} 像素格式转换失败"
                f"(pixel_format=0x{pixel_format:x}): {e}")
            return self._bayer_fallback(raw_image, pixel_format)

    @staticmethod
    def _gray_to_array(raw_image):
        """灰度/Bayer 帧 → 2D **uint8** 数组。

        8bit 直接取；10/12/16bit 会拿到 uint16，必须先降到 RAW8，
        否则下游（YOLO / OpenCV Bayer 转换码）拿到的就是 uint16 数组。
        """
        try:
            arr = raw_image.get_numpy_array()
            if arr is not None and arr.dtype == np.uint8:
                return arr
        except Exception:
            pass

        raw8 = raw_image.convert("RAW8")
        if raw8 is None:
            return None
        return raw8.get_numpy_array()

    def _bayer_fallback(self, raw_image, pixel_format):
        """SDK 转换器（DxImageProc）不可用时的 OpenCV 兜底。

        注意：GenICam 与 OpenCV 的 Bayer 命名约定在个别机型上会差一个相位，
        如果现场看到红蓝通道反了，把 _BAYER8_TO_CV 里对应的转换码换一下即可。
        """
        code = _BAYER8_TO_CV.get(pixel_format)
        if code is None:
            self._throttled(
                "no_fallback", error,
                f"设备 {self.st_serial_number} 像素格式 0x{pixel_format:x} "
                f"既不能用大恒 SDK 转换、也没有 OpenCV 兜底路径，本帧丢弃")
            return None
        try:
            arr = self._gray_to_array(raw_image)
            if arr is None or arr.ndim != 2:
                return None
            self._throttled(
                "using_fallback", warning,
                f"设备 {self.st_serial_number} 正在使用 OpenCV 兜底做 Bayer 转换，"
                f"请确认大恒 Galaxy 运行时的 DxImageProc.dll 是否正常加载")
            return cv2.cvtColor(arr, code)
        except Exception as e:
            self._throttled("fallback_error", error,
                            f"设备 {self.st_serial_number} OpenCV 兜底转换失败: {e}")
            return None

    # ==================================================================
    # 取流
    # ==================================================================
    def Start_grabbing(self) -> int:
        """开始取流。成功返回 0。"""
        if not self.b_open_device:
            error(f"设备 {self.st_serial_number} 开始取流失败! 设备未开启")
            return -1
        if self._user_callback is None:
            error(f"设备 {self.st_serial_number} 开始取流失败! 尚未注册回调")
            return -1
        if self.b_start_grabbing:
            return 0

        try:
            if not self.b_regist_callback:
                self._data_stream.register_capture_callback(self._capture_handler)
                self.b_regist_callback = True
            self.cam_object.stream_on()
        except Exception as e:
            error(f"设备 {self.st_serial_number} 开始取流失败: {e}")
            self._unregister_callback_internal()
            return -1

        self.b_start_grabbing = True
        return 0

    def Stop_grabbing(self) -> int:
        """停止取流并注销回调。成功返回 0。

        先 stream_off 再注销回调：SDK 停流后不会再投递帧，
        避免"C 线程正在回调、Python 侧回调对象已被置空"的竞态。
        """
        if not (self.b_open_device and self.b_start_grabbing):
            return -1

        try:
            self.cam_object.stream_off()
        except Exception as e:
            error(f"设备 {self.st_serial_number} 停止取流失败: {e}")
            return -1
        finally:
            self.b_start_grabbing = False

        self._unregister_callback_internal()
        return 0

    # ==================================================================
    # 触发
    # ==================================================================
    def Trigger_once(self) -> int:
        """软触发一次。成功返回 0。"""
        if not self.b_open_device:
            raise CameraOperationError(
                f"设备 {self.st_serial_number} 软触发失败：设备未开启!")

        self._diag_soft_trigger_call_count += 1
        try:
            self._feature_control.get_command_feature("TriggerSoftware").send_command()
        except Exception as e:
            self._diag_soft_trigger_last_error = str(e)
            error(f"设备 {self.st_serial_number} 软触发失败! "
                  f"(第{self._diag_soft_trigger_call_count}次调用) {e}")
            return -1

        self._diag_soft_trigger_success_count += 1
        self._diag_soft_trigger_last_error = None
        self._throttled(
            "soft_trigger_ok", info,
            f"设备 {self.st_serial_number} 软触发正常 "
            f"(第{self._diag_soft_trigger_call_count}次调用, "
            f"累计成功{self._diag_soft_trigger_success_count}次)")
        return 0

    def Set_trigger_source(self, source: str) -> int:
        """动态切换触发源（"Line0" 硬触发 / "Software" 软触发）。

        取流过程中热切 TriggerSource 在部分机型上会"返回成功但不生效"，
        所以这里统一走 StopGrabbing → 改参数 → StartGrabbing。
        注意：**不注销回调**，只停/开流，切完立刻继续出图。
        """
        if not self.b_open_device:
            warning(f"设备 {self.st_serial_number} 未开启，无法切换触发源")
            return -1
        if self.trigger_source == source:
            return 0

        with self._trigger_switch_lock:
            if self.trigger_source == source:
                return 0

            was_grabbing = self.b_start_grabbing
            if was_grabbing:
                try:
                    self.cam_object.stream_off()
                except Exception as e:
                    error(f"设备 {self.st_serial_number} 切换触发源前停止取流失败: {e}，本次切换中止")
                    return -1
                self.b_start_grabbing = False
                info(f"设备 {self.st_serial_number} 已停止取流，准备切换触发源为 {source}")

            # 防御性：确保 TriggerSelector 仍是 FrameStart
            self._set_enum("TriggerSelector", "FrameStart", warn_only=True)

            ret = self._set_enum("TriggerSource", source, warn_only=True)
            if ret == 0:
                self.trigger_source = source
                info(f"设备 {self.st_serial_number} 触发源已切换为 {source}")
            else:
                error(f"设备 {self.st_serial_number} 切换触发源为 {source} 失败")

            if was_grabbing:
                try:
                    self.cam_object.stream_on()
                except Exception as e:
                    error(f"设备 {self.st_serial_number} 切换触发源后重新开流失败: {e}，"
                          f"相机将无法出图，需要人工介入排查")
                    return -1
                self.b_start_grabbing = True
                info(f"设备 {self.st_serial_number} 已重新开始取流 (触发源={self.trigger_source})")

            return ret

    # ==================================================================
    # 诊断
    # ==================================================================
    def get_diag_info(self) -> dict:
        """诊断信息：触发次数、帧计数、SDK 侧丢帧数、最近一次收帧时间。"""
        stream_stats = {}
        for attr, key in (("StreamDeliveredFrameCount", "delivered"),
                          ("StreamLostFrameCount", "lost"),
                          ("StreamIncompleteFrameCount", "incomplete")):
            try:
                feature = getattr(self._data_stream, attr, None)
                if feature is not None and self.b_open_device:
                    stream_stats[key] = feature.get()
            except Exception:
                pass

        return {
            "camera_name": self.camera_name,
            "serial": self.st_serial_number,
            "model": self.st_mode_name,
            "ip": self.st_ip_address,
            "trigger_source": self.trigger_source,
            "b_open_device": self.b_open_device,
            "b_start_grabbing": self.b_start_grabbing,
            "soft_trigger_call_count": self._diag_soft_trigger_call_count,
            "soft_trigger_success_count": self._diag_soft_trigger_success_count,
            "soft_trigger_last_error": self._diag_soft_trigger_last_error,
            "frame_count": self._diag_frame_count,
            "bad_frame_count": self._diag_bad_frame_count,
            "convert_error_count": self._diag_convert_error_count,
            "callback_error_count": self._diag_callback_error_count,
            "last_frame_time": self._diag_last_frame_time,
            "stream": stream_stats,
        }
