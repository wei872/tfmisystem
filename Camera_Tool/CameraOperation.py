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
import socket
import re
import struct
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

# GigE Vision 的最小可用包长。低于这个值没有意义（一个 2440x2048 的帧会被
# 切成上万个包），而且它恰好是以太网标准 MTU，任何链路都保证能过。
_MIN_SAFE_PACKET_SIZE = 1500


def _netmask_to_int(netmask) -> int:
    """把 psutil 给出的掩码转成整数。

    不同平台/版本给的形式不一样：点分十进制 '255.255.255.0'，
    也可能是前缀长度的字符串 '24'。两种都要认。认不出来返回 0，
    让调用方当作"掩码未知"处理，**绝不抛异常**——这个函数在相机启动
    路径上，抛出去就会把相机启动一起带崩。
    """
    try:
        s = str(netmask).strip()
        if not s:
            return 0
        if "." in s:
            return struct.unpack("!I", socket.inet_aton(s))[0]
        # 前缀长度形式
        prefix = int(s)
        if prefix <= 0:
            return 0
        if prefix >= 32:
            return 0xFFFFFFFF
        return ((1 << prefix) - 1) << (32 - prefix)
    except Exception:
        return 0


try:
    from General_Tool.TriggerBurstProbe import record_arrival as _tbp_record
except Exception:                      # 探针不可用时退化成空操作
    def _tbp_record(camera_name, ts=None):
        return None


# TriggerDelay 硬上限（微秒）。见 __init__ 里的说明：算错的后果是相机永远不出图。
# 单台 TriggerDelay 的硬上限（微秒）。
#
# 这个数不是随便定的，它由"要真正错开到达时刻"反推出来：
#   单帧 4.766 MiB @ 1GbE = 40.0 ms 传输时间。
#   8 台总跨度必须 **> 40 ms**，各台的到达区间才不重叠 —— 否则削的只是
#   "同时开始曝光"，8 帧仍会在网线上/回调里撞在一起。
#   取 60 ms 上限 → 最大步长 60000 // 7 = 8571us → 最大跨度 60 ms，留了余量。
#
# 上限同时受触发周期约束（跨度 + 40ms 传输 < 触发周期，否则与下一次触发的
# 帧相撞）。触发是位置触发、每 80.224 mm 一次：
#   10 m/min -> 周期 481 ms；20 -> 241；30 -> 160。
#   跨度 60 ms 要求周期 > 100 ms，即车速 < 48 m/min。产线跑 10 m/min，余量很大。
#
# 它同时也是安全网：TriggerDelay 算错（名字解析异常、配置写了离谱的值）的后果
# 是相机触发后一直等、永远不出图 —— 这个故障现场极难查。
_MAX_TRIGGER_DELAY_US = 60000
# 最大相机序号（camera1..camera8）。错峰跨度 = 7 × 步长，所以步长上限是
# _MAX_TRIGGER_DELAY_US // 7。见 __init__ 里对步长（而不是对单台延时）
# 做截断的说明。
_MAX_STAGGER_INDEX = 7


def _camera_index_from_name(name) -> int:
    """从相机业务名里取序号，用于触发错峰：camera1 -> 0, camera8 -> 7。

    **解析不出来时返回 0**（即"不错峰"），不猜、不报错。
       错峰的失败模式必须是"回到原来的同时触发"，绝不能因为一个名字
       解析不出来就把相机启动搞挂。

    为什么用名字而不是枚举顺序：枚举顺序取决于 SDK 发现设备的先后，
    每次开机可能不同；那样 camera3 这次拿 500us、下次拿 3000us，
    现场就完全没法复现问题。名字是稳定的。
    """
    if not name:
        return 0
    # **必须整名匹配 cameraN，不能用 re.search 抓第一串数字**。
    #    camera_name 缺省时会退化成序列号（`camera_name or st_serial_number`），
    #    而 "KBA26070032" 用 search 会解析出 26070032 → TriggerDelay 变成
    #    天文数字，相机等于永远不出图。实测过这个坑。
    m = re.fullmatch(r"\s*camera[_\-]?(\d+)\s*", str(name), re.IGNORECASE)
    if not m:
        return 0
    try:
        idx = int(m.group(1))
    except Exception:
        return 0
    return max(0, idx - 1)      # camera1 -> 0


def _ip_to_int(ip: str) -> int:
    return struct.unpack("!I", socket.inet_aton(ip))[0]


def _same_subnet(local_ip: str, netmask, peer_ip: str) -> bool:
    try:
        mask = _netmask_to_int(netmask)
        if mask == 0:
            return False
        return (_ip_to_int(local_ip) & mask) == (_ip_to_int(peer_ip) & mask)
    except Exception:
        return False


def probe_link_mtu(ip_address: str) -> int:
    """探测与给定相机 IP 处于同一网段的本机网卡 MTU。

    **返回 0 表示探测不到**，调用方必须把 0 当作"不要开巨型帧"处理，
    绝不能当成"MTU 是 0"或"随便用一个大值"。

    为什么必须实测、不能只信 config.yaml 里写的数：

    现场踩过的坑是——交换机巨型帧开着，配置里也写了 8164，但相机实际发出
    的包超过了链路上某一跳能承载的大小，包被丢弃，表现为"相机启动不起来"
    或者只出不完整帧。关键在于 ``GevSCPSPacketSize`` 是**相机端**的设置，
    SDK 的 range_check 只校验它是否落在相机自己支持的范围里，**它完全不知道
    链路上能过多大的包**。所以这里以本机网卡实测 MTU 作为硬上限。
    """
    if not ip_address:
        return 0
    target = str(ip_address).strip()
    if not target:
        return 0

    try:
        import psutil
    except Exception:
        return 0

    try:
        stats = psutil.net_if_stats()
        addrs_all = psutil.net_if_addrs()
    except Exception:
        return 0

    for name, addrs in addrs_all.items():
        st = stats.get(name)
        if st is None or not getattr(st, "isup", False):
            continue
        mtu = int(getattr(st, "mtu", 0) or 0)
        if mtu <= 0:
            continue
        for a in addrs:
            try:
                if getattr(a, "family", None) != socket.AF_INET:
                    continue
                addr = getattr(a, "address", "") or ""
                # 相机 IP 就配在本机网卡上（同机直连/回环）：直接用这张网卡
                if addr == target:
                    return mtu
                if _same_subnet(addr, getattr(a, "netmask", None), target):
                    return mtu
            except Exception:
                continue
    return 0


def clamp_packet_size(desired: int, link_mtu: int,
                      cam_min: int, cam_max: int, cam_inc: int) -> int:
    """把期望包长收敛成"链路和相机都能接受"的值。返回 0 表示"别改，用相机默认"。

    这是纯函数，所有边界都在这里处理，便于单测：

    * **绝不超过链路实测 MTU**（这是防止"包超过设置值导致起不来"的核心）；
    * 绝不超过相机支持的最大值，绝不小于相机支持的最小值；
    * 按相机的步进 ``inc`` **向下**对齐（超一点就退回上一档，不四舍五入）；
    * 结果低于 1500 时直接返回 0 —— 宁可放弃巨型帧也不要一个没意义的小值。

    ``link_mtu=0`` 表示 MTU 未知，此时**保守退回 1500**，不冒险开巨型帧。
    """
    if cam_max <= 0:
        return 0
    inc = cam_inc if cam_inc and cam_inc > 0 else 1
    lo = cam_min if cam_min > 0 else _MIN_SAFE_PACKET_SIZE

    # MTU 未知 → 只允许标准帧
    cap = link_mtu if link_mtu > 0 else _MIN_SAFE_PACKET_SIZE
    cap = min(cap, cam_max)

    target = min(int(desired), cap) if desired and desired > 0 else cap

    # 向下对齐到步进
    if inc > 1:
        target -= target % inc

    if target < lo or target < _MIN_SAFE_PACKET_SIZE:
        return 0
    return target


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
                 heartbeat_timeout_ms: int = 0, trigger_source: str = "Line0",
                 trigger_stagger_us: int = 0):
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
        :param trigger_stagger_us: **错峰步长**（微秒）。0 = 不错峰（保持原行为）。
                               >0 时本机按相机序号错开 TriggerDelay：
                               camera1=0、camera2=1×步长、…、camera8=7×步长。

                               为什么需要：8 台相机接同一根 Line0 触发线，一次触发
                               8 台**同时**开始推流。按现场分辨率算，一次突发是
                               38.1 MB 上线 + 114.4 MB 在 8 条 SDK 回调线程上同时
                               分配，1GbE 单链路要 305 ms 才传得完。这种瞬时尖峰
                               是电源过流/整机卡死的高频诱因。

                               **有几何代价**：错峰后各相机看到的布面纵向位置
                               不再对齐。10 m/min = 166.7 mm/s，所以
                               ``7 × 步长(µs) × 0.0001667 mm/µs`` 就是 camera8
                               相对 camera1 的布面偏移。步长 500 µs ≈ 0.58 mm，
                               2000 µs ≈ 2.33 mm。取值要现场按检测要求定。
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

        # ---- 触发错峰（TriggerDelay）----
        self.trigger_stagger_us = max(0, int(trigger_stagger_us or 0))
        self._stagger_index = _camera_index_from_name(self.camera_name)

        # 索引必须落在 0.._MAX_STAGGER_INDEX。名字解析不出来时
        # _camera_index_from_name 已经返回 0（= 这台不错峰），这里再兜一层，
        # 防止将来解析逻辑改动后算出离谱的索引 —— 那个故障是"相机永远不出图"。
        if not 0 <= self._stagger_index <= _MAX_STAGGER_INDEX:
            warning(f"设备 {self.st_serial_number} 解析出的相机序号 "
                    f"{self._stagger_index} 超出 0..{_MAX_STAGGER_INDEX}"
                    f"（camera_name={self.camera_name!r}），本台按 0 处理（不错峰）。")
            self._stagger_index = 0

        # 截断的是**步长**，不是单台延时。
        #    之前按单台截断（delay > 20000 就压到 20000），后果是顶部几台被压成
        #    同一个值：实测 trigger_stagger_us=4000 时
        #      camera6/7/8 = 20000/20000/20000  →  间距 0，又变回同时触发。
        #    这恰好是错峰要防的那件事，而且是**静默**失效。
        #    按步长截断则间距始终均匀，只是跨度到顶。
        if self.trigger_stagger_us > 0:
            _max_step = _MAX_TRIGGER_DELAY_US // _MAX_STAGGER_INDEX
            if self.trigger_stagger_us > _max_step:
                warning(f"设备 {self.st_serial_number} trigger_stagger_us="
                        f"{self.trigger_stagger_us}us 过大：8 台总跨度会超过上限 "
                        f"{_MAX_TRIGGER_DELAY_US}us，导致顶部几台延时相同、"
                        f"错峰失效。已降到 {_max_step}us（跨度 "
                        f"{_max_step * _MAX_STAGGER_INDEX}us）。")
                self.trigger_stagger_us = _max_step

        self.trigger_delay_us = self._stagger_index * self.trigger_stagger_us
        # 是否**真的写进相机了**。和 trigger_delay_us 是两回事：
        # 后者是"我们想设多少"，前者是"相机收下了没有"。
        self.trigger_delay_applied = False
        # 硬上限：TriggerDelay 算错（名字解析异常、配置写了离谱的值）的后果是
        # 相机在触发后一直等、永远不出图 —— 这个故障现场极难查。
        # 20 ms 已经远超任何合理的错峰需求，超过就截断并告警。
        if self.trigger_delay_us > _MAX_TRIGGER_DELAY_US:
            warning(f"设备 {self.st_serial_number} 计算出的 TriggerDelay="
                    f"{self.trigger_delay_us}us 超过上限 "
                    f"{_MAX_TRIGGER_DELAY_US}us，已截断。"
                    f"请检查 trigger_stagger_us 配置和相机名 {self.camera_name!r}")
            self.trigger_delay_us = _MAX_TRIGGER_DELAY_US

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
        # ---- GigE 包长实测结果（get_diag_info() 可查）----
        self._diag_link_mtu = 0            # 探测到的本机网卡 MTU，0=未知
        self._diag_packet_size_wanted = 0  # 配置里期望的值
        self._diag_packet_size_applied = 0 # 实际写进相机并回读确认的值
        self._diag_packet_size_note = ""   # 被降级/放弃的原因

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

    def _set_float(self, name: str, value: float,
                   readback: bool = True) -> int:
        """设置浮点特征（特征不存在时只告警，不致命）。

        和 :meth:`_set_int` 同一条纪律：**绝不抛异常、绝不挡住相机启动**。

        :param readback: 设置后回读确认。GigE 相机的特征写入存在"返回成功但
                         实际被相机夹到合法区间"的情况（``_apply_packet_size``
                         就是为这个才回读的），浮点特征同样要验。
        :return: 0 成功；-1 失败或不支持
        """
        try:
            if not self._feature_control.is_implemented(name):
                warning(f"设备 {self.st_serial_number} 不支持特征 {name}，跳过")
                return -1
            feat = self._feature_control.get_float_feature(name)
            feat.set(float(value))
            if readback:
                try:
                    got = float(feat.get())
                    # 浮点比较留 1% 容差：相机可能按内部步进取整
                    if abs(got - float(value)) > max(abs(float(value)) * 0.01, 1.0):
                        warning(f"设备 {self.st_serial_number} {name} 设为 "
                                f"{value} 但回读为 {got}（被相机夹到合法区间），"
                                f"错峰效果可能与预期不同")
                except Exception as e:
                    warning(f"设备 {self.st_serial_number} 回读 {name} 失败: {e}")
            return 0
        except Exception as e:
            warning(f"设备 {self.st_serial_number} 设置 {name}={value} 失败: {e}")
            return -1

    def _apply_trigger_delay(self) -> bool:
        """下发本机算好的 TriggerDelay。**绝不抛异常、绝不挡住启动/切流。**

        为什么要在 Open_device **和** Set_trigger_source 两处都调：
          TriggerDelay 按 GenICam 是挂在 TriggerSelector=FrameStart 上的持久
          设置，切 TriggerSource 理论上不该丢。但"理论上不该丢"这件事在真机上
          验不了 —— 部分机型在 StopGrabbing/StartGrabbing 或改触发源时会把
          触发相关参数复位。而这里重下发一次的成本是零。

          更关键的是**失效方向**：软触发是织机**停机预览**时才切过去的。如果
          切过去时 TriggerDelay 丢了，8 台又变回同时推流，而这恰好是最不容易
          被注意到的一条路径（停机时没人盯着检测节拍）。

        trigger_delay_us == 0 时直接返回 False（不错峰是默认行为，不该去碰相机）。

        :return: True = 确实写进相机了；False = 没写（不错峰，或相机不支持）。
                 调用方**必须**用这个返回值决定日志怎么写。之前不看返回值，
                 结果相机不支持该特征、下发被跳过，启动日志却照样打
                 "TriggerDelay=3500us" —— 现场会以为错峰生效了，其实没有。
        """
        if self.trigger_delay_us <= 0:
            return False
        ok = self._set_float("TriggerDelay", float(self.trigger_delay_us)) == 0
        self.trigger_delay_applied = ok
        return ok

    def _apply_packet_size(self) -> None:
        """设置 GigE 包长。**契约：绝不抛异常、绝不挡住相机启动。**

        相机启动路径上的任何一步失败都只能降级，不能失败——这正是现场
        "相机启动不起来"要避免的。所以这里套一层兜底，真正逻辑在
        :meth:`_apply_packet_size_impl` 里。
        """
        try:
            self._apply_packet_size_impl()
        except Exception as e:
            warning(
                f"设备 {self.st_serial_number} 设置 GigE 包长时发生未预期异常: {e!r}。"
                f"**已放弃修改包长、沿用相机当前值**，相机照常启动。"
                f"若随后取不到流，请把 config.yaml 的 packet_size 设为 -1。")
            self._diag_packet_size_note = f"未预期异常，已放弃修改: {e!r}"

    def _apply_packet_size_impl(self) -> None:
        """把 ``GevSCPSPacketSize`` 设成链路和相机都能接受的值，并**回读校验**。

        这一整套流程的设计目标是：

        1. **写进相机的包长永远不会超过本机网卡实测 MTU** —— 现场出现过
           "巨型帧明明开着、但包超过设置值导致相机启动不起来"，根因就是包长
           只在相机侧校验范围、没人管链路。
        2. **任何一步失败都不能挡住相机启动**。探测失败、特征不存在、设置
           失败、回读不一致，一律告警后放弃修改、沿用相机当前值。
        3. **设置后回读确认**。``set()`` 不抛异常不代表相机真的采纳了，
           回读值不一致就当作失败并降级。

        config.yaml 里 ``packet_size`` 的三种取值：

        * ``0``  —— 自动：按实测 MTU 取最大安全值（交换机/网卡开了巨型帧
          就自动用上，没开就自动退回 1500）。
        * ``>0`` —— 指定值，但**仍然会被实测 MTU 夹紧**，配置写错也不会
          把链路打挂。
        * ``-1`` —— 完全不碰这个特征，保留相机出厂/上次保存的值。
        """
        self._diag_packet_size_wanted = self.packet_size
        self._diag_packet_size_applied = 0
        self._diag_packet_size_note = ""

        if self.packet_size < 0:
            self._diag_packet_size_note = "config 指定 -1，不修改包长"
            return

        try:
            if not self._feature_control.is_implemented("GevSCPSPacketSize"):
                self._diag_packet_size_note = "设备不支持 GevSCPSPacketSize"
                return
            feature = self._feature_control.get_int_feature("GevSCPSPacketSize")
        except Exception as e:
            warning(f"设备 {self.st_serial_number} 读取 GevSCPSPacketSize 失败: {e}")
            self._diag_packet_size_note = f"读取特征失败: {e}"
            return

        # ---- 相机侧支持范围 ----
        cam_min, cam_max, cam_inc = 0, 0, 1
        try:
            rng = feature.get_range() or {}
            cam_min = int(rng.get("min", 0) or 0)
            cam_max = int(rng.get("max", 0) or 0)
            cam_inc = int(rng.get("inc", 1) or 1)
        except Exception as e:
            # 拿不到范围就不猜，保守退回标准帧
            cam_min, cam_max, cam_inc = _MIN_SAFE_PACKET_SIZE, _MIN_SAFE_PACKET_SIZE, 1
            self._diag_packet_size_note = f"读取包长范围失败，保守用 1500: {e}"

        # ---- 链路侧实测 MTU（硬上限）----
        # probe_link_mtu 内部已经兜了异常，但这里再兜一层：它处在相机启动
        # 路径上，任何逃逸的异常都会让 Open_device 整体失败。
        try:
            link_mtu = probe_link_mtu(self.st_ip_address)
        except Exception as e:
            warning(f"设备 {self.st_serial_number} 探测链路 MTU 失败: {e!r}，"
                    f"保守退回标准帧。")
            link_mtu = 0
        self._diag_link_mtu = link_mtu
        if link_mtu <= 0:
            warning(
                f"设备 {self.st_serial_number} 探测不到网卡 MTU"
                f"(camera_ip={self.st_ip_address or '未知'})，"
                f"保守退回标准帧 {_MIN_SAFE_PACKET_SIZE}，不开巨型帧。"
                f"若现场确实已开巨型帧，请检查相机 IP 与网卡是否同网段。")

        desired = self.packet_size if self.packet_size > 0 else (
            link_mtu if link_mtu > 0 else _MIN_SAFE_PACKET_SIZE)

        target = clamp_packet_size(desired, link_mtu, cam_min, cam_max, cam_inc)
        if target <= 0:
            warning(
                f"设备 {self.st_serial_number} 无法确定安全的 GigE 包长"
                f"(期望={desired}, MTU={link_mtu}, 相机范围={cam_min}~{cam_max}"
                f" 步进 {cam_inc})，沿用相机当前值，不修改。")
            self._diag_packet_size_note = "无安全包长可用，未修改"
            return

        if self.packet_size > 0 and target < self.packet_size:
            warning(
                f"设备 {self.st_serial_number} 配置要求包长 {self.packet_size}，"
                f"但链路实测 MTU={link_mtu}、相机支持上限={cam_max}，"
                f"**已下调到 {target}** 以避免包超过链路承载导致取不到流。")
            self._diag_packet_size_note = (
                f"配置 {self.packet_size} 被下调到 {target}"
                f"(MTU={link_mtu}, 相机上限={cam_max})")

        # ---- 写入 ----
        try:
            feature.set(int(target))
        except Exception as e:
            warning(f"设备 {self.st_serial_number} 设置 GevSCPSPacketSize="
                    f"{target} 失败: {e}，沿用相机当前值。")
            self._diag_packet_size_note = f"设置 {target} 失败: {e}"
            return

        # ---- 回读校验：set 不抛异常不等于相机真的采纳了 ----
        try:
            actual = int(feature.get())
        except Exception as e:
            warning(f"设备 {self.st_serial_number} 回读 GevSCPSPacketSize 失败: {e}，"
                    f"无法确认是否生效，沿用当前值继续。")
            self._diag_packet_size_note = f"回读失败，无法确认 {target} 是否生效"
            return

        if actual != target:
            # 相机没采纳（可能被固件夹到别的档位）。这是最危险的情况：
            # 继续往下走可能取不到流，所以明确告警并把实际值记下来。
            warning(
                f"设备 {self.st_serial_number} GevSCPSPacketSize 回读不一致："
                f"期望 {target}，实际 {actual}。**若随后取不到流，"
                f"请把 config.yaml 的 packet_size 改成 -1 或 {actual}。**")
            self._diag_packet_size_note = f"回读不一致：期望 {target}，实际 {actual}"
            self._diag_packet_size_applied = actual
            return

        self._diag_packet_size_applied = actual
        info(f"设备 {self.st_serial_number} GigE 包长设为 {actual} "
             f"(链路 MTU={link_mtu}, 相机范围={cam_min}~{cam_max} 步进 {cam_inc})")

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
            # 包长必须走 _apply_packet_size()，不能直接 set：
            # 它会用本机网卡实测 MTU 把值夹紧、并回读确认相机真的采纳了，
            # 避免"包长超过链路承载 → 相机启动不起来 / 只出不完整帧"。
            self._apply_packet_size()
            if self.heartbeat_timeout_ms > 0:
                self._set_int("GevHeartbeatTimeout", self.heartbeat_timeout_ms)

            # ---- 触发体系：TriggerSelector → TriggerMode → TriggerSource ----
            # 三层都要显式设置。TriggerSelector 没设对的话，后面对
            # TriggerMode/TriggerSource 的写入可能作用在别的触发通道上，
            # 表现为"设置返回成功但拍照行为完全不受影响"。
            self._set_enum("TriggerSelector", "FrameStart", warn_only=True)
            self._set_enum("TriggerMode", "On")
            self._set_enum("TriggerSource", self.trigger_source)

            # ---- 触发错峰：TriggerDelay 按相机序号递增 ----
            # 必须在 TriggerSelector=FrameStart 之后、stream_on 之前设，否则会
            # 写到别的触发通道上（和上面 TriggerMode/TriggerSource 是同一个坑）。
            # 失败只告警不影响启动 —— 错峰的失败模式是"回到不错峰"，
            # 也就是原来的行为，不是"相机起不来"。
            self._apply_trigger_delay()

            # ---- 采集缓冲节点数：必须在 stream_on 之前设置 ----
            try:
                self._data_stream.set_acquisition_buffer_number(self.buffer_count)
            except Exception as e:
                warning(f"设备 {self.st_serial_number} 设置缓冲节点数失败: {e}")

            self.b_open_device = True

            # 日志必须反映**实际下发结果**，不能反映"我们打算设多少"。
            #    相机不支持 TriggerDelay 时下发会被跳过，此时若照打
            #    "TriggerDelay=3500us"，现场会以为错峰生效了 —— 而实际 8 台
            #    仍在同时推流。这个谎报比不打印危险得多。
            if self.trigger_delay_us <= 0:
                _delay_tag = ""
            elif self.trigger_delay_applied:
                _delay_tag = f", TriggerDelay={self.trigger_delay_us}us"
            else:
                _delay_tag = (f", TriggerDelay={self.trigger_delay_us}us "
                              f"[未生效·相机不支持或下发失败，错峰未启用]")
            info(f"设备 [{self.st_serial_number}] ({self.st_mode_name} @ {self.st_ip_address}) "
                 f"启动成功！(TriggerSelector=FrameStart, TriggerMode=On, "
                 f"TriggerSource={self.trigger_source}, BufferCount={self.buffer_count}"
                 f"{_delay_tag}")

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
                         image 为 BGR、HxWx3、uint8、**只读**；生命周期不依赖
                         SDK 环形缓冲（gxipy 构造时已 string_at 拷成 Python
                         bytes），但要在上面原地画框/改像素必须先 copy；
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

        运行在大恒 SDK 的 C 线程上：
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

            # ---- 触发突发探针：只记一次到达时间，热路径上必须极便宜 ----
            # 放在 _to_bgr **之前**：要测的是"相机什么时候把帧送上来"，
            # 不是"我们什么时候转完色"。探针没启用时是一次属性判断，零成本。
            try:
                _tbp_record(self.camera_name)
            except Exception:
                pass

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
        """把 SDK 原始帧转成 OpenCV 的 BGR uint8 数组。

        关于"要不要再 copy 一次"（这里原来写错了，实测后更正）：

        * **不需要为了生命周期而 copy。** gxipy 的 ``RawImage``/``RGBImage``
          构造时执行 ``string_at(image_buf, image_size)``
          （lib/gxipy/ImageProc.py:28 与 :172），那**已经是一次 Python bytes
          拷贝**，内存归 Python 所有；``get_numpy_array()`` 只是
          ``numpy.frombuffer`` 出的视图，会持有那个 bytes 的引用。
          所以 SDK 环形缓冲被复用也不会让这块内存失效。

        * **``np.ascontiguousarray`` 在这里不产生拷贝。** 输入本来就是
          C 连续的，``ascontiguousarray`` 原样返回同一个对象（实测
          ``out is arr`` 为真）。这里的注释原先声称做了真正的拷贝、
          并让下游拿到可写数组 —— 两句都不成立，已更正。

        * 因此返回的数组是**只读的**（``frombuffer`` over ``bytes``）。
          下游全部是只读用法（YOLO / cvtColor / imencode / 经纬线分析），
          标注绘制都先 ``image.copy()``，所以现状是安全的。
          **若将来要在帧上原地画框或改像素，必须先显式 copy**，
          否则会抛 "assignment destination is read-only"。
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
            # 只保证连续，不保证拷贝（对已连续输入它是 no-op）——
            # 见 _to_bgr 的文档字符串。生命周期由 gxipy 那次 string_at 兜住。
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

            # 切完触发源**必须重下发 TriggerDelay**。见 _apply_trigger_delay
            #    的说明：丢了的后果是软触发（停机预览）路径悄悄退回同时推流。
            if ret == 0:
                self._apply_trigger_delay()
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
            # GigE 包长实测结果：packet_size_applied 与 link_mtu 不匹配、
            # 或 packet_size_note 非空，就是"包长没设对"的直接证据。
            "link_mtu": self._diag_link_mtu,
            "packet_size_wanted": self._diag_packet_size_wanted,
            "packet_size_applied": self._diag_packet_size_applied,
            "packet_size_note": self._diag_packet_size_note,
            "stream": stream_stats,
        }
