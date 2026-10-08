#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Camera_Tool.CameraOperation（大恒 Galaxy SDK 版）的回归测试。

跑法（不需要插相机，也不需要装大恒驱动）::

    python -m pytest Test_Tool/test_camera_operation.py -v

原理见 Test_Tool/fake_galaxy_sdk.py：只有 ctypes 那一层被替换成假实现，
gxipy 的 DeviceManager / Device / DataStream / FeatureControl / ImageProc
以及被测的 CameraOperation 全部是真实代码。
"""

import os
import sys
import types

import numpy as np
import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import fake_galaxy_sdk                     # noqa: E402

BACKEND = fake_galaxy_sdk.install()      # 必须在 import gxipy 之前

import gxipy as gx                        # noqa: E402  (真实 gxipy + 假 C 层)
from Camera_Tool import CameraRegistry    # noqa: E402
from Camera_Tool.CameraOperation import CameraOperation  # noqa: E402

SN = "FAKESN0001"
WIDTH, HEIGHT = 32, 24


@pytest.fixture(autouse=True)
def backend():
    BACKEND.reset()
    CameraRegistry.clear_all()
    BACKEND.add_camera(SN, "MER2-500-14GM", "192.168.1.10",
                       WIDTH, HEIGHT, fake_galaxy_sdk.PIXEL.BAYER_GB8)
    yield BACKEND
    CameraRegistry.clear_all()


def make_manager():
    return gx.DeviceManager()


def device_info(sn=SN):
    return {"index": 1, "sn": sn, "model_name": "MER2-500-14GM",
            "ip": "192.168.1.10"}


def make_cam(mgr, **kwargs):
    kwargs.setdefault("buffer_count", 7)
    kwargs.setdefault("packet_size", 8164)
    kwargs.setdefault("heartbeat_timeout_ms", 2500)
    return CameraOperation(mgr, device_info(), camera_name="camera1", **kwargs)


@pytest.fixture
def cam(backend):
    mgr = make_manager()
    op = make_cam(mgr)
    assert op.Open_device() == 0
    yield op
    if op.b_open_device:
        op.Close_device()


def gradient(w=WIDTH, h=HEIGHT):
    """一张值可预测的图：pixel(r, c) = (r * 4 + c) % 256"""
    r = np.arange(h, dtype=np.uint32).reshape(-1, 1) * 4
    c = np.arange(w, dtype=np.uint32).reshape(1, -1)
    return ((r + c) % 256).astype(np.uint8)


# ======================================================================
# 打开 / 参数初始化
# ======================================================================
def test_open_device_sets_trigger_chain_and_buffer(cam, backend):
    fake = backend.by_sn[SN]
    assert cam.b_open_device is True
    assert fake.features['TriggerSelector'].current == 'FrameStart'
    assert fake.features['TriggerMode'].current == 'On'
    assert fake.features['TriggerSource'].current == 'Line0'
    assert fake.acquisition_buffer_number == 7
    assert fake.features['GevSCPSPacketSize'].value == 8164
    assert fake.features['GevHeartbeatTimeout'].value == 2500


def test_open_device_registers_into_camera_registry(cam):
    assert CameraRegistry.get_camera("camera1") is cam
    assert CameraRegistry.get_camera_count() == 1


def test_device_info_fields_are_populated(cam):
    assert cam.st_serial_number == SN
    assert cam.st_mode_name == "MER2-500-14GM"
    assert cam.st_ip_address == "192.168.1.10"
    assert cam.camera_name == "camera1"


def test_open_device_with_unknown_sn_returns_error_without_raising():
    mgr = make_manager()
    op = CameraOperation(mgr, device_info(sn="NOSUCHCAM"), camera_name="ghost")
    assert op.Open_device() == -1
    assert op.b_open_device is False
    assert CameraRegistry.get_camera_count() == 0


# ======================================================================
# 回调注册
# ======================================================================
def test_capture_handler_is_a_plain_function(cam):
    """gxipy 的 register_capture_callback 只收 types.FunctionType，
    直接传绑定方法会被它拒掉 —— 这条断言守着 _make_capture_handler 的设计。"""
    assert isinstance(cam._capture_handler, types.FunctionType)
    assert not isinstance(cam._on_capture, types.FunctionType)


def test_registration_callback_requires_open_device(backend):
    mgr = make_manager()
    op = make_cam(mgr)
    assert op.Registration_callback(lambda img, info: None) == -1


def test_full_pipeline_registers_callback_on_sdk(cam, backend):
    assert cam.Registration_callback(lambda img, info: None) == 0
    assert cam.b_regist_callback is True
    assert backend.by_sn[SN].capture_callback is not None


# ======================================================================
# 取帧 → BGR
# ======================================================================
def _run_one_frame(cam, backend, pattern=None):
    received = []
    assert cam.Registration_callback(
        lambda img, info: received.append((img, info))) == 0
    assert cam.Start_grabbing() == 0
    if pattern is not None:
        backend.by_sn[SN].set_pattern(pattern)
    fake_galaxy_sdk.deliver_frame(SN)
    assert len(received) == 1, "回调没有被调用"
    return received[0]


def test_frame_is_delivered_as_bgr_uint8(cam, backend):
    backend.by_sn[SN].set_pattern(gradient())
    image, frame_info = _run_one_frame(cam, backend)

    assert image.shape == (HEIGHT, WIDTH, 3)
    assert image.dtype == np.uint8
    assert image.flags['C_CONTIGUOUS']
    assert image.flags['WRITEABLE']       # 下游要画框，必须可写

    assert frame_info["nWidth"] == WIDTH
    assert frame_info["nHeight"] == HEIGHT
    assert frame_info["nFrameNum"] == 1
    assert frame_info["camera_sn"] == SN
    assert frame_info["camera_name"] == "camera1"


def test_sdk_converter_is_asked_for_bgr_channel_order(cam, backend):
    """DxRGBChannelOrder.ORDER_BGR 必须传给 SDK，否则 OpenCV 拿到的红蓝是反的"""
    backend.dx_alpha_values.clear()
    _run_one_frame(cam, backend, gradient())
    assert backend.dx_alpha_values == [gx.DxRGBChannelOrder.ORDER_BGR]


def test_delivered_array_is_a_copy_not_a_view_of_sdk_buffer(cam, backend):
    """
    最关键的一条：SDK 的环形缓冲在回调返回后会被复用。
    回调交出去的数组必须是独立副本，否则下游线程会读到下一帧的数据。
    """
    pattern = gradient()
    backend.by_sn[SN].set_pattern(pattern)
    image, _ = _run_one_frame(cam, backend, pattern)

    before = image.copy()
    backend.by_sn[SN].mutate_buffer()     # 模拟 SDK 复用缓冲区
    assert np.array_equal(image, before), "交出去的数组仍然指向 SDK 缓冲区"


def test_incomplete_frame_is_dropped(cam, backend):
    received = []
    assert cam.Registration_callback(
        lambda img, info: received.append(img)) == 0
    assert cam.Start_grabbing() == 0

    fake_galaxy_sdk.deliver_frame(SN, incomplete=True)
    assert received == []
    assert cam.get_diag_info()["bad_frame_count"] == 1


def test_exception_in_user_callback_does_not_escape(cam, backend):
    """回调运行在 SDK 的 C 线程上，异常穿透到 ctypes 边界是灾难"""
    def boom(img, info):
        raise RuntimeError("下游炸了")

    assert cam.Registration_callback(boom) == 0
    assert cam.Start_grabbing() == 0
    fake_galaxy_sdk.deliver_frame(SN)      # 不应抛出
    assert cam.get_diag_info()["callback_error_count"] == 1


def test_opencv_fallback_when_sdk_converter_unavailable(cam, backend):
    backend.dx_convert_broken = True
    image, _ = _run_one_frame(cam, backend, gradient())
    assert image.shape == (HEIGHT, WIDTH, 3)
    assert image.dtype == np.uint8
    assert cam.get_diag_info()["convert_error_count"] == 1


def test_mono_camera_path(backend):
    backend.by_sn[SN].pixel_format = fake_galaxy_sdk.PIXEL.MONO8
    mgr = make_manager()
    op = make_cam(mgr)
    assert op.Open_device() == 0

    pattern = gradient()
    backend.by_sn[SN].set_pattern(pattern)
    image, _ = _run_one_frame(op, backend, pattern)

    assert image.shape == (HEIGHT, WIDTH, 3)
    assert image.dtype == np.uint8
    # 灰度转 BGR：三个通道相同且等于原始灰度值
    assert np.array_equal(image[..., 0], pattern)
    assert np.array_equal(image[..., 1], pattern)
    assert np.array_equal(image[..., 2], pattern)
    op.Close_device()


def test_mono16_is_downscaled_to_uint8(backend):
    """10/12/16bit 灰度不能直接把 uint16 数组丢给下游"""
    backend.by_sn[SN].pixel_format = fake_galaxy_sdk.PIXEL.MONO10
    mgr = make_manager()
    op = make_cam(mgr)
    assert op.Open_device() == 0

    pattern = gradient()
    backend.by_sn[SN].set_pattern(pattern)
    image, _ = _run_one_frame(op, backend)
    assert image.dtype == np.uint8
    assert image.shape == (HEIGHT, WIDTH, 3)
    # 16bit 按高 8 位有效降级回 8bit，图案应当无损还原
    assert np.array_equal(image[..., 0], pattern)
    op.Close_device()


# ======================================================================
# 触发
# ======================================================================
def test_trigger_once_sends_software_command(cam, backend):
    fake = backend.by_sn[SN]
    assert cam.Registration_callback(lambda img, info: None) == 0
    assert cam.Start_grabbing() == 0

    # 触发源还是 Line0，软触发命令被相机忽略（真实行为）
    assert cam.Trigger_once() == 0
    assert fake.software_trigger_count == 0

    assert cam.Set_trigger_source("Software") == 0
    assert fake.features['TriggerSource'].current == 'Software'

    assert cam.Trigger_once() == 0
    assert fake.software_trigger_count == 1
    diag = cam.get_diag_info()
    assert diag["soft_trigger_call_count"] == 2
    assert diag["soft_trigger_success_count"] == 2


def test_set_trigger_source_restarts_the_stream(cam, backend):
    fake = backend.by_sn[SN]
    assert cam.Registration_callback(lambda img, info: None) == 0
    assert cam.Start_grabbing() == 0
    assert fake.streaming is True

    assert cam.Set_trigger_source("Software") == 0
    assert fake.acquisition_stop_count == 1
    assert fake.acquisition_start_count == 2   # Start_grabbing 1 次 + 切换后 1 次
    assert fake.streaming is True
    assert cam.b_start_grabbing is True

    # 切换触发源不应该把回调注销掉
    assert fake.capture_callback is not None


def test_set_trigger_source_is_idempotent(cam, backend):
    assert cam.Registration_callback(lambda img, info: None) == 0
    assert cam.Start_grabbing() == 0
    before = backend.by_sn[SN].acquisition_stop_count
    assert cam.Set_trigger_source("Line0") == 0
    assert backend.by_sn[SN].acquisition_stop_count == before


def test_set_trigger_source_without_open_device():
    mgr = make_manager()
    op = make_cam(mgr)
    assert op.Set_trigger_source("Software") == -1


def test_trigger_once_raises_when_device_closed():
    mgr = make_manager()
    op = make_cam(mgr)
    with pytest.raises(Exception):
        op.Trigger_once()


# ======================================================================
# 取流 / 关闭
# ======================================================================
def test_start_grabbing_requires_callback(cam, backend):
    assert cam.Start_grabbing() == -1
    assert backend.by_sn[SN].streaming is False


def test_stop_grabbing_unregisters_and_start_reregisters(cam, backend):
    assert cam.Registration_callback(lambda img, info: None) == 0
    assert cam.Start_grabbing() == 0
    assert backend.by_sn[SN].capture_callback is not None

    assert cam.Stop_grabbing() == 0
    assert cam.b_start_grabbing is False
    assert backend.by_sn[SN].capture_callback is None

    assert cam.Start_grabbing() == 0
    assert backend.by_sn[SN].capture_callback is not None


def test_close_device_cleans_up(cam, backend):
    assert cam.Registration_callback(lambda img, info: None) == 0
    assert cam.Start_grabbing() == 0

    assert cam.Close_device() == 0
    assert cam.b_open_device is False
    assert cam.b_start_grabbing is False
    assert cam.b_regist_callback is False
    assert backend.by_sn[SN].closed is True
    assert CameraRegistry.get_camera_count() == 0


def test_close_device_twice_is_safe(cam, backend):
    assert cam.Close_device() == 0
    assert cam.Close_device() == -1


# ======================================================================
# 诊断
# ======================================================================
def test_diag_info_reports_stream_statistics(cam, backend):
    _run_one_frame(cam, backend, gradient())
    diag = cam.get_diag_info()
    assert diag["camera_name"] == "camera1"
    assert diag["serial"] == SN
    assert diag["trigger_source"] == "Line0"
    assert diag["frame_count"] == 1
    assert diag["b_start_grabbing"] is True
    assert isinstance(diag["stream"], dict)
