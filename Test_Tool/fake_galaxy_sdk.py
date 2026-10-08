#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大恒 Galaxy SDK 的**假 C 边界**，仅用于没有相机硬件时跑单元测试。

设计原则（很重要）：
    这里只替换 ``gxipy.gxwrapper`` / ``gxipy.dxwrapper`` 这两个直接 ctypes
    调用 GxIAPI.dll / DxImageProc.dll 的模块。
    ``gxipy`` 的其余部分（DeviceManager / Device / DataStream / FeatureControl /
    Feature_s / StatusProcessor / ImageProc …）**全部跑真实代码**。

    也就是说被测对象是真的 gxipy + 真的 Camera_Tool.CameraOperation，
    被替换掉的只是"那块必须插着相机才存在的原生库"。

用法::

    import fake_galaxy_sdk
    fake_galaxy_sdk.install()      # 必须在 import gxipy 之前
    import gxipy as gx
    fake = fake_galaxy_sdk.BACKEND
    fake.add_camera("SN1", "MER2-500-14GM", "192.168.1.10", 64, 48)
    ...
    fake.deliver_frame("SN1")    # 模拟相机吐一帧
"""

import os
import sys
import types
from ctypes import (CFUNCTYPE, POINTER, Structure, Union, addressof, byref,
                    c_char, c_char_p, c_int, c_int32, c_int64, c_ubyte,
                    c_uint, c_uint16, c_ulonglong, c_void_p, py_object)

import numpy as np

# gxipy 随仓库放在 lib/ 下，内部是绝对导入，需要 lib/ 在 sys.path 上
_LIB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

# gxidef 是纯常量模块（整个文件 0 个 import、不碰任何 DLL）。
# 这里用 importlib 按文件路径单独加载它，**不走** `import gxipy`：
# 后者会执行 gxipy/__init__.py，连带把 gxwrapper 拉起来去 load GxIAPI.dll。
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location(
    "_gxidef_standalone", os.path.join(_LIB_DIR, "gxipy", "gxidef.py"))
_gxidef = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_gxidef)
GxPixelFormatEntry = _gxidef.GxPixelFormatEntry

# ======================================================================
# 结构体定义：与真实 gxwrapper.py 的 ABI 布局保持一致
# （这些只是内存布局声明，不含逻辑，必须原样复刻）
# ======================================================================
NODE_FEATURE_RESERVED_16 = 16


class GxDeviceIPInfo(Structure):
    _fields_ = [
        ('device_id', c_char * 68),
        ('mac', c_char * 32),
        ('ip', c_char * 32),
        ('subnet_mask', c_char * 32),
        ('gateway', c_char * 32),
        ('nic_mac', c_char * 32),
        ('nic_ip', c_char * 32),
        ('nic_subnet_mask', c_char * 32),
        ('nic_gateWay', c_char * 32),
        ('nic_description', c_char * 132),
        ('reserved', c_char * 512),
    ]


class GxDeviceBaseInfo(Structure):
    _fields_ = [
        ('vendor_name', c_char * 32),
        ('model_name', c_char * 32),
        ('serial_number', c_char * 32),
        ('display_name', c_char * 132),
        ('device_id', c_char * 68),
        ('user_id', c_char * 68),
        ('access_status', c_int),
        ('device_class', c_int),
        ('reserved', c_char * 300),
    ]


class GxOpenParam(Structure):
    _fields_ = [
        ('content', c_char_p),
        ('open_mode', c_uint),
        ('access_mode', c_uint),
    ]


class GXCxpInterfaceInfo(Structure):
    _fields_ = [
        ('interface_id', c_char * 64),
        ('display_name', c_char * 64),
        ('serial_number', c_char * 64),
        ('init_flag', c_uint),
        ('reserved', c_uint * 65),
    ]


class GXGevInterfaceInfo(Structure):
    _fields_ = [
        ('interface_id', c_char * 64),
        ('display_name', c_char * 64),
        ('serial_number', c_char * 64),
        ('description', c_char * 256),
        ('init_flag', c_uint),
        ('reserved', c_uint * 63),
    ]


class GXU3vInterfaceInfo(Structure):
    _fields_ = [
        ('interface_id', c_char * 64),
        ('display_name', c_char * 64),
        ('serial_number', c_char * 64),
        ('description', c_char * 256),
        ('init_flag', c_uint),
        ('reserved', c_uint * 63),
    ]


class GXUsbInterfaceInfo(Structure):
    _fields_ = [
        ('interface_id', c_char * 64),
        ('display_name', c_char * 64),
        ('serial_number', c_char * 64),
        ('description', c_char * 256),
        ('init_flag', c_uint),
        ('reserved', c_uint * 63),
    ]


class GXInterfacSpecialInfo(Union):
    _fields_ = [
        ("CXP_interface_info", GXCxpInterfaceInfo),
        ("GEV_interface_info", GXGevInterfaceInfo),
        ("U3V_interface_info", GXU3vInterfaceInfo),
        ("USB_interface_info", GXUsbInterfaceInfo),
        ("reserved", c_uint * 64),
    ]


class GXInterfaceInfo(Structure):
    _fields_ = [
        ('TLayer_type', c_int),
        ('reserved', c_int * 4),
        ('IF_info', GXInterfacSpecialInfo),
    ]


class GxIntFeatrue(Structure):
    _fields_ = [
        ('value', c_int64),
        ('min', c_int64),
        ('max', c_int64),
        ('inc', c_int64),
        ('reserved', c_int32 * NODE_FEATURE_RESERVED_16),
    ]


class GxEnumValue(Structure):
    _fields_ = [
        ('cur_value', c_int64),
        ('cur_symbolic', c_char * 128),
        ('reserved', c_int32 * 4),
    ]


class GxEnumFeatrue(Structure):
    _fields_ = [
        ('cur_value', GxEnumValue),
        ('supported_number', c_int64),
        ('supported_value', GxEnumValue * 128),
        ('reserved', c_int32 * 16),
    ]


class GxFrameCallbackParam(Structure):
    """回调参数结构。

    真实 gxwrapper 在 Windows / Linux 下给的是两套不同布局
    （Linux 多 offset_x/offset_y，Windows 多 chunk_data_handle）。
    假实现把字段取并集，这样同一份测试在两个平台都能跑。
    """
    _fields_ = [
        ('user_param_index', c_void_p),
        ('status', c_int),
        ('image_buf', c_void_p),
        ('image_size', c_int),
        ('width', c_int),
        ('height', c_int),
        ('pixel_format', c_int),
        ('frame_id', c_ulonglong),
        ('timestamp', c_ulonglong),
        ('chunk_data_handle', c_void_p),
        ('offset_x', c_int),
        ('offset_y', c_int),
        ('reserved', c_int),
    ]


class GxFrameData(Structure):
    _fields_ = [
        ('status', c_int),
        ('image_buf', c_void_p),
        ('width', c_int),
        ('height', c_int),
        ('pixel_format', c_int),
        ('image_size', c_int),
        ('frame_id', c_ulonglong),
        ('timestamp', c_ulonglong),
        ('user_param', c_void_p),
        ('chunk_data_handle', c_void_p),
        ('offset_x', c_int),
        ('offset_y', c_int),
        ('reserved', c_int),
        ('buf_id', c_ulonglong),
    ]


CAP_CALL = CFUNCTYPE(None, POINTER(GxFrameCallbackParam))
OFF_LINE_CALL = CFUNCTYPE(None, c_void_p)
RECONNECT_CALL = CFUNCTYPE(None, c_void_p)
DISCONNECT_CALL = CFUNCTYPE(None, c_void_p)
FEATURE_CALL = CFUNCTYPE(None, c_uint, py_object)
FEATURE_CALL_CHAR = CFUNCTYPE(None, c_char_p, py_object)


# ======================================================================
# 常量表
# ======================================================================
class GxStatusList:
    SUCCESS = 0
    ERROR = -1
    NOT_FOUND_TL = -2
    NOT_FOUND_DEVICE = -3
    OFFLINE = -4
    INVALID_PARAMETER = -5
    INVALID_HANDLE = -6
    INVALID_CALL = -7
    INVALID_ACCESS = -8
    NEED_MORE_BUFFER = -9
    ERROR_TYPE = -10
    OUT_OF_RANGE = -11
    NOT_IMPLEMENTED = -12
    NOT_INIT_API = -13
    TIMEOUT = -14
    REPEAT_OPENED = -1004


class GxNodeAccessMode:
    MODE_NI = 0
    MODE_NA = 1
    MODE_WO = 2
    MODE_RO = 3
    MODE_RW = 4
    MODE_UNDEF = 5


class GxOpenMode:
    SN = 0
    USER_ID = 1
    IP = 2
    MAC = 3
    INDEX = 4


class GxAccessMode:
    READONLY = 2
    CONTROL = 3
    EXCLUSIVE = 4


class GxDeviceClassList:
    UNKNOWN = 0
    USB2 = 1
    GEV = 2
    U3V = 3
    CXP = 4


class GxTLClassList:
    TL_TYPE_UNKNOWN = 0
    TL_TYPE_GEV = 1
    TL_TYPE_U3V = 4
    TL_TYPE_USB = 2
    TL_TYPE_CXP = 8


class DxStatus:
    OK = 0
    ERROR = -1


class _FeatureIdMeta(type):
    """GxFeatureID 在真实 SDK 里有几百个常量，这里按需自动生成。

    假 C 层一律用**特征名字符串**寻址，数字 ID 只在 Device.__init__ 里被
    用来构造那些遗留的 Feature 对象，具体取值无关紧要，只要稳定唯一。
    """
    _cache = {}

    def __getattr__(cls, name):
        if name.startswith('__'):
            raise AttributeError(name)
        value = cls._cache.get(name)
        if value is None:
            value = 0x10000000 + len(cls._cache) * 16
            cls._cache[name] = value
        return value


class GxFeatureID(metaclass=_FeatureIdMeta):
    COMMAND_ACQUISITION_START = 0x30000001
    COMMAND_ACQUISITION_STOP = 0x30000002


UNSIGNED_INT_MAX = 0xFFFFFFFF
UNSIGNED_LONG_LONG_MAX = 0xFFFFFFFFFFFFFFFF
INT_TYPE = int


def string_encoding(s):
    return s.encode() if isinstance(s, str) else s


def string_decoding(s):
    if isinstance(s, bytes):
        return s.decode(errors='replace')
    return s


def array_decoding(int_array_c):
    return [int(x) for x in int_array_c]


# ======================================================================
# 假相机模型
# ======================================================================
class EnumSpec:
    def __init__(self, current, options):
        self.current = current
        self.options = dict(options)   # {symbolic: value}


class IntSpec:
    def __init__(self, value, lo=0, hi=0xFFFFFFFF):
        self.value = int(value)
        self.lo = lo
        self.hi = hi


class FakeCamera:
    """一台假的大恒 GigE 相机"""

    def __init__(self, sn, model, ip, width=64, height=48,
                 pixel_format=0x02080000):   # 默认在 add_camera 里覆盖
        self.sn = sn
        self.model = model
        self.ip = ip
        self.width = width
        self.height = height
        self.pixel_format = pixel_format

        self.opened = False
        self.streaming = False
        self.closed = False
        self.acquisition_buffer_number = None
        self.capture_callback = None       # ctypes CAP_CALL 实例
        self.software_trigger_count = 0
        self.acquisition_start_count = 0
        self.acquisition_stop_count = 0

        # 特征表：默认覆盖本项目真正会写的那几个
        self.features = {
            'TriggerSelector': EnumSpec('FrameStart', {'FrameStart': 1}),
            'TriggerMode': EnumSpec('Off', {'Off': 0, 'On': 1}),
            'TriggerSource': EnumSpec('Line0',
                                      {'Software': 0, 'Line0': 1, 'Line1': 2}),
            'GevSCPSPacketSize': IntSpec(1500),
            'GevHeartbeatTimeout': IntSpec(3000),
            'ExposureTime': IntSpec(10000),
        }
        # 命令型特征（send_command 用），不是可读写特征
        self.commands = {'TriggerSoftware', 'AcquisitionStart', 'AcquisitionStop'}

        # 每像素字节数由像素格式里的位深字段决定（0x??NN???? 的 NN 是 bit 数）
        bits = (pixel_format >> 16) & 0xFF
        self._dtype = np.uint16 if bits > 8 else np.uint8

        # 图像缓冲：模拟 SDK 的环形缓冲，回调返回后会被复用
        self._raw = np.zeros((height, width), dtype=self._dtype)
        self._buf = (c_ubyte * self._raw.nbytes).from_buffer(self._raw)
        self._frame_id = 0
        self._force_incomplete = False

    # ---------------- 测试用的注入接口 ----------------
    def set_pattern(self, array):
        """设置下一帧的原始（Bayer/Mono）内容"""
        arr = np.asarray(array)
        assert arr.shape == (self.height, self.width), arr.shape
        if self._dtype == np.uint16 and arr.dtype == np.uint8:
            # 10/12/16bit 格式下，有效位在高 8 位（valid_bits=BIT8_15），
            # 左移 8 位存进去，转换回来应当无损还原成原始 uint8 图案
            arr = arr.astype(np.uint16) << 8
        self._raw[...] = arr

    def force_next_frame_incomplete(self, value=True):
        self._force_incomplete = value

    def snapshot_buffer(self):
        return self._raw.copy()

    def mutate_buffer(self):
        """模拟 SDK 在回调返回后复用/覆写缓冲区"""
        self._raw[...] = np.iinfo(self._dtype).max


# 全局后端
class _Backend:
    def __init__(self):
        self.reset()

    def reset(self):
        self.cameras = []                 # List[FakeCamera]
        self.by_sn = {}
        self.interfaces = [1]             # 一个 GigE 网卡
        self.lib_init_count = 0
        self.lib_close_count = 0
        self._handles = {}                # handle(int) -> obj
        self._next_handle = 0x1000
        self.last_error = b''
        self.enum_calls = 0
        # dx 侧记录
        self.dx_convert_broken = False    # True 时让 DxImageProc 转换失败
        self.dx_alpha_values = []         # 记录 set_alpha_value 收到的通道序

    def add_camera(self, sn, model, ip, width=64, height=48, pixel_format=None):
        if pixel_format is None:
            pixel_format = PIXEL.BAYER_GB8
        cam = FakeCamera(sn, model, ip, width, height, pixel_format)
        self.cameras.append(cam)
        self.by_sn[sn] = cam
        return cam

    def _new_handle(self, obj):
        self._next_handle += 1
        self._handles[self._next_handle] = obj
        return self._next_handle


BACKEND = _Backend()


# 像素格式常量：直接复用真实 gxidef.GxPixelFormatEntry（gxidef 是纯常量模块，
# 不碰 DLL，可以独立导入）。手抄一份数值迟早抄错 —— Bayer 系列用的是
# GX_PIXEL_MONO(0x01000000) 而不是 GX_PIXEL_COLOR，第一版就写错过。
PIXEL = GxPixelFormatEntry


# ======================================================================
# 假 gxwrapper 的 C 函数
# ======================================================================
def _cam_by_handle(handle):
    obj = BACKEND._handles.get(handle)
    if not isinstance(obj, FakeCamera):
        raise AssertionError(f"handle {handle} 不是设备句柄")
    return obj


def gx_init_lib():
    BACKEND.lib_init_count += 1
    return GxStatusList.SUCCESS


def gx_close_lib():
    BACKEND.lib_close_count += 1
    return GxStatusList.SUCCESS


def gx_set_log_type(log_type):
    return GxStatusList.SUCCESS


def gx_get_log_type():
    return GxStatusList.SUCCESS, 0


def gx_update_all_device_list(timeout=200):
    BACKEND.enum_calls += 1
    return GxStatusList.SUCCESS, len(BACKEND.cameras)


def gx_update_device_list(timeout=200):
    return gx_update_all_device_list(timeout)


def gx_update_device_list_ex(tl_type, timeout=2000):
    return gx_update_all_device_list(timeout)


def gx_get_all_device_base_info(num):
    arr = (GxDeviceBaseInfo * num)()
    for i, cam in enumerate(BACKEND.cameras[:num]):
        arr[i].vendor_name = string_encoding("Daheng Imaging")
        arr[i].model_name = string_encoding(cam.model)
        arr[i].serial_number = string_encoding(cam.sn)
        arr[i].display_name = string_encoding(f"{cam.model}({cam.sn})")
        arr[i].device_id = string_encoding(f"gev-{cam.sn}")
        arr[i].user_id = string_encoding("")
        arr[i].access_status = 3
        arr[i].device_class = GxDeviceClassList.GEV
    return GxStatusList.SUCCESS, arr


def gx_get_interface_number():
    return GxStatusList.SUCCESS, len(BACKEND.interfaces)


def gx_get_interface_info(index):
    info = GXInterfaceInfo()
    info.TLayer_type = GxTLClassList.TL_TYPE_GEV
    info.IF_info.GEV_interface_info.interface_id = string_encoding("gev-iface-0")
    info.IF_info.GEV_interface_info.display_name = string_encoding("Fake GigE NIC")
    info.IF_info.GEV_interface_info.serial_number = string_encoding("NIC0")
    info.IF_info.GEV_interface_info.description = string_encoding("fake")
    info.IF_info.GEV_interface_info.init_flag = 1
    return GxStatusList.SUCCESS, info


def gx_get_interface_handle(index):
    return GxStatusList.SUCCESS, BACKEND.interfaces[index - 1]


def gx_get_device_ip_info(index):
    cam = BACKEND.cameras[index - 1]
    info = GxDeviceIPInfo()
    info.device_id = string_encoding(f"gev-{cam.sn}")
    info.mac = string_encoding("00-11-22-33-44-55")
    info.ip = string_encoding(cam.ip)
    info.subnet_mask = string_encoding("255.255.255.0")
    info.gateway = string_encoding("0.0.0.0")
    info.nic_mac = string_encoding("AA-BB-CC-DD-EE-FF")
    info.nic_ip = string_encoding("192.168.1.1")
    info.nic_subnet_mask = string_encoding("255.255.255.0")
    info.nic_gateWay = string_encoding("0.0.0.0")
    info.nic_description = string_encoding("Fake GigE NIC")
    return GxStatusList.SUCCESS, info


def gx_open_device(open_param):
    sn = string_decoding(open_param.content)
    cam = BACKEND.by_sn.get(sn)
    if cam is None:
        BACKEND.last_error = b"device not found"
        return GxStatusList.NOT_FOUND_DEVICE, 0
    if cam.opened:
        BACKEND.last_error = b"device already opened"
        return GxStatusList.REPEAT_OPENED, 0
    cam.opened = True
    return GxStatusList.SUCCESS, BACKEND._new_handle(cam)


def gx_close_device(handle):
    cam = _cam_by_handle(handle)
    cam.opened = False
    cam.streaming = False
    cam.closed = True
    return GxStatusList.SUCCESS


def gx_get_parent_interface_from_device(handle):
    _cam_by_handle(handle)
    return GxStatusList.SUCCESS, BACKEND.interfaces[0]


def gx_get_feature_name(handle, feature):
    return GxStatusList.SUCCESS, string_encoding(f"feature_{feature:#x}")


def gx_data_stream_number_from_device(handle):
    _cam_by_handle(handle)
    return GxStatusList.SUCCESS, 1


def gx_get_data_stream_handle_from_device(handle, index):
    _cam_by_handle(handle)
    return GxStatusList.SUCCESS, BACKEND._new_handle(("stream", handle))


def gx_get_node_access_mode(handle, feature_name):
    obj = BACKEND._handles.get(handle)
    cam = obj if isinstance(obj, FakeCamera) else BACKEND._handles.get(obj[1])
    if not isinstance(cam, FakeCamera):
        return GxStatusList.SUCCESS, GxNodeAccessMode.MODE_NI
    implemented = feature_name in cam.features or feature_name in cam.commands
    return (GxStatusList.SUCCESS,
            GxNodeAccessMode.MODE_RW if implemented else GxNodeAccessMode.MODE_NI)


def _enum_spec(handle, feature_name):
    obj = BACKEND._handles.get(handle)
    cam = obj if isinstance(obj, FakeCamera) else BACKEND._handles.get(obj[1])
    return cam.features[feature_name]


def gx_get_enum_feature(handle, feature_name):
    spec = _enum_spec(handle, feature_name)
    info = GxEnumFeatrue()
    info.cur_value.cur_value = spec.options[spec.current]
    info.cur_value.cur_symbolic = string_encoding(spec.current)
    info.supported_number = len(spec.options)
    for i, (symbolic, value) in enumerate(spec.options.items()):
        info.supported_value[i].cur_value = value
        info.supported_value[i].cur_symbolic = string_encoding(symbolic)
    return GxStatusList.SUCCESS, info


def gx_get_enum_detail_feature(handle, feature_name):
    return gx_get_enum_feature(handle, feature_name)


def gx_set_enum_feature_value_string(handle, feature_name, value):
    spec = _enum_spec(handle, feature_name)
    if value not in spec.options:
        BACKEND.last_error = b"enum value out of range"
        return GxStatusList.OUT_OF_RANGE
    spec.current = value
    return GxStatusList.SUCCESS


def gx_set_enum_feature_value(handle, feature_name, value):
    spec = _enum_spec(handle, feature_name)
    for symbolic, v in spec.options.items():
        if v == value:
            spec.current = symbolic
            return GxStatusList.SUCCESS
    BACKEND.last_error = b"enum value out of range"
    return GxStatusList.OUT_OF_RANGE


def gx_get_int_feature(handle, feature_name):
    spec = _enum_spec(handle, feature_name)
    info = GxIntFeatrue()
    info.value = spec.value
    info.min = spec.lo
    info.max = spec.hi
    info.inc = 1
    return GxStatusList.SUCCESS, info


def gx_set_int_feature_value(handle, feature_name, value):
    spec = _enum_spec(handle, feature_name)
    if not (spec.lo <= value <= spec.hi):
        BACKEND.last_error = b"int value out of range"
        return GxStatusList.OUT_OF_RANGE
    spec.value = int(value)
    return GxStatusList.SUCCESS


def gx_get_payload_size(stream_handle):
    obj = BACKEND._handles.get(stream_handle)
    cam = BACKEND._handles.get(obj[1])
    return GxStatusList.SUCCESS, cam.width * cam.height


def gx_send_command(handle, feature_id):
    cam = _cam_by_handle(handle)
    if feature_id == GxFeatureID.COMMAND_ACQUISITION_START:
        cam.streaming = True
        cam.acquisition_start_count += 1
    elif feature_id == GxFeatureID.COMMAND_ACQUISITION_STOP:
        cam.streaming = False
        cam.acquisition_stop_count += 1
    return GxStatusList.SUCCESS


def gx_feature_send_command(handle, feature_name):
    cam = _cam_by_handle(handle)
    if feature_name == "TriggerSoftware":
        if not cam.streaming:
            BACKEND.last_error = b"acquisition not started"
            return GxStatusList.INVALID_CALL
        if cam.features['TriggerSource'].current != 'Software':
            # 真实相机：触发源不是 Software 时软触发命令会被忽略
            return GxStatusList.SUCCESS
        cam.software_trigger_count += 1
        return GxStatusList.SUCCESS
    return GxStatusList.SUCCESS


def gx_set_acquisition_buffer_number(handle, buf_num):
    cam = _cam_by_handle(handle)
    cam.acquisition_buffer_number = int(buf_num)
    return GxStatusList.SUCCESS


def gx_register_capture_callback(handle, callback):
    cam = _cam_by_handle(handle)
    if cam.capture_callback is not None:
        BACKEND.last_error = b"callback already registered"
        return GxStatusList.INVALID_CALL
    cam.capture_callback = callback
    return GxStatusList.SUCCESS


def gx_unregister_capture_callback(handle):
    cam = _cam_by_handle(handle)
    cam.capture_callback = None
    return GxStatusList.SUCCESS


def gx_get_last_error(size=1024):
    return GxStatusList.SUCCESS, GxStatusList.ERROR, BACKEND.last_error


# ======================================================================
# 假 dxwrapper（DxImageProc.dll）
# ======================================================================
class _ConvertCtx:
    _next = 0

    def __init__(self):
        _ConvertCtx._next += 1
        self.id = _ConvertCtx._next
        self.out_format = None
        self.valid_bits = None
        self.channel_order = None
        self.interpolation = None


_CONVERT_CTX = {}


def dx_image_format_convert_create():
    if BACKEND.dx_convert_broken:
        return DxStatus.ERROR, 0
    ctx = _ConvertCtx()
    _CONVERT_CTX[ctx.id] = ctx
    return DxStatus.OK, ctx.id


def dx_image_format_convert_set_output_pixel_format(handle, fmt):
    _CONVERT_CTX[handle].out_format = fmt
    return DxStatus.OK


def dx_image_format_convert_set_valid_bits(handle, bits):
    _CONVERT_CTX[handle].valid_bits = bits
    return DxStatus.OK


def dx_image_format_convert_set_alpha_value(handle, channel_order):
    ctx = _CONVERT_CTX[handle]
    ctx.channel_order = channel_order
    BACKEND.dx_alpha_values.append(channel_order)
    return DxStatus.OK


def dx_image_format_convert_set_interpolation_type(handle, interp):
    _CONVERT_CTX[handle].interpolation = interp
    return DxStatus.OK


def dx_image_format_convert_get_buffer_size_for_conversion(handle, fmt, width, height):
    if fmt in (PIXEL.RGB8, 0x02180015):   # RGB8 / BGR8
        return DxStatus.OK, width * height * 3
    return DxStatus.OK, width * height


def dx_image_format_convert(handle, in_buf, in_size, out_buf, out_size,
                            src_fmt, width, height, flip):
    """
    假的像素格式转换。

    真实 DxImageProc 会做 Bayer 去马赛克；这里不做（那是 C 库的职责，不是
    被测代码的职责），只保证**形状、缓冲区大小、位深降级、字节序参数传递**
    是对的：输出每个像素的 R=G=B=源像素值。测试断言据此写，既确定又与实现无关。
    """
    _CONVERT_CTX[handle]
    n_px = width * height

    if in_size == n_px * 2:
        # 16bit 源：按 valid_bits=BIT8_15 取高 8 位降到 8bit
        src16 = np.ctypeslib.as_array(
            (c_uint16 * n_px).from_address(in_buf)).reshape(height, width)
        src = (src16 >> 8).astype(np.uint8)
    else:
        src = np.ctypeslib.as_array(
            (c_ubyte * n_px).from_address(in_buf)).reshape(height, width)

    dst = np.ctypeslib.as_array((c_ubyte * out_size).from_address(out_buf))
    if out_size == n_px * 3:
        dst[...] = np.dstack([src, src, src]).reshape(-1)
    else:
        dst[...] = src.reshape(-1)
    return DxStatus.OK


def dx_image_format_convert_destroy(handle):
    _CONVERT_CTX.pop(handle, None)
    return DxStatus.OK


# ======================================================================
# 安装
# ======================================================================
def _build_module(name, mapping):
    mod = types.ModuleType(name)
    for key, value in mapping.items():
        setattr(mod, key, value)
    # 真实的 gxwrapper/dxwrapper 头部有 `import sys/os/locale` 和
    # `from ctypes import *`，而下游模块（Feature_s / ImageProc …）是靠
    # `from gxipy.gxwrapper import *` 间接拿到 sys 这些名字的。
    # 假模块必须同样再导出，否则下游一 import 就 NameError。
    import ctypes as _ct
    for source in (_ct, sys, os, types):
        for key in dir(source):
            if not key.startswith('_') and not hasattr(mod, key):
                setattr(mod, key, getattr(source, key))
    return mod


_GX_NAMES = [
    # 结构体 / 回调类型
    'GxDeviceBaseInfo', 'GxDeviceIPInfo', 'GxOpenParam', 'GXInterfaceInfo',
    'GXInterfacSpecialInfo', 'GXGevInterfaceInfo', 'GXCxpInterfaceInfo',
    'GXU3vInterfaceInfo', 'GXUsbInterfaceInfo', 'GxIntFeatrue', 'GxEnumFeatrue',
    'GxEnumValue', 'GxFrameCallbackParam', 'GxFrameData',
    'CAP_CALL', 'OFF_LINE_CALL',
    'RECONNECT_CALL', 'DISCONNECT_CALL', 'FEATURE_CALL', 'FEATURE_CALL_CHAR',
    # 常量
    'GxStatusList', 'GxNodeAccessMode', 'GxOpenMode', 'GxAccessMode',
    'GxDeviceClassList', 'GxTLClassList', 'GxFeatureID',
    'UNSIGNED_INT_MAX', 'UNSIGNED_LONG_LONG_MAX', 'INT_TYPE',
    'NODE_FEATURE_RESERVED_16',
    # 工具
    'string_encoding', 'string_decoding', 'array_decoding',
    # C 接口
    'gx_init_lib', 'gx_close_lib', 'gx_set_log_type', 'gx_get_log_type',
    'gx_update_all_device_list', 'gx_update_device_list',
    'gx_update_device_list_ex', 'gx_get_all_device_base_info',
    'gx_get_interface_number', 'gx_get_interface_info', 'gx_get_interface_handle',
    'gx_get_device_ip_info', 'gx_open_device', 'gx_close_device',
    'gx_get_parent_interface_from_device', 'gx_get_feature_name',
    'gx_data_stream_number_from_device', 'gx_get_data_stream_handle_from_device',
    'gx_get_node_access_mode', 'gx_get_enum_feature', 'gx_get_enum_detail_feature',
    'gx_set_enum_feature_value_string', 'gx_set_enum_feature_value',
    'gx_get_int_feature', 'gx_set_int_feature_value', 'gx_get_payload_size',
    'gx_send_command', 'gx_feature_send_command',
    'gx_set_acquisition_buffer_number', 'gx_register_capture_callback',
    'gx_unregister_capture_callback', 'gx_get_last_error',
]

_DX_NAMES = [
    'DxStatus',
    'dx_image_format_convert_create',
    'dx_image_format_convert_set_output_pixel_format',
    'dx_image_format_convert_set_valid_bits',
    'dx_image_format_convert_set_alpha_value',
    'dx_image_format_convert_set_interpolation_type',
    'dx_image_format_convert_get_buffer_size_for_conversion',
    'dx_image_format_convert',
    'dx_image_format_convert_destroy',
]


def install():
    """把假 C 边界塞进 sys.modules。必须在 import gxipy **之前**调用。"""
    for name in _GX_NAMES:
        assert name in globals(), f"fake gxwrapper 缺少 {name}"
    for name in _DX_NAMES:
        assert name in globals(), f"fake dxwrapper 缺少 {name}"

    sys.modules['gxipy.gxwrapper'] = _build_module(
        'gxipy.gxwrapper', {n: globals()[n] for n in _GX_NAMES})
    sys.modules['gxipy.dxwrapper'] = _build_module(
        'gxipy.dxwrapper', {n: globals()[n] for n in _DX_NAMES})
    return BACKEND


def deliver_frame(sn, incomplete=False):
    """模拟相机向已注册的回调投递一帧。"""
    cam = BACKEND.by_sn[sn]
    assert cam.capture_callback is not None, "回调未注册"
    assert cam.streaming, "未开流"

    cam._frame_id += 1
    param = GxFrameCallbackParam()
    param.user_param_index = 0
    param.status = GxStatusList.ERROR if (incomplete or cam._force_incomplete) else 0
    param.image_buf = addressof(cam._buf)
    param.image_size = cam._raw.nbytes
    param.width = cam.width
    param.height = cam.height
    param.pixel_format = cam.pixel_format
    param.frame_id = cam._frame_id
    param.timestamp = cam._frame_id * 1000
    param.chunk_data_handle = None
    param.offset_x = 0
    param.offset_y = 0

    cam.capture_callback(byref(param))
    return cam._frame_id
