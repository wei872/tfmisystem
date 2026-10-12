#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GigE 包长安全设置的回归测试。

现场事故：用海康 SDK 时，实际发出的包**超过了设置的包长**，导致相机启动
不起来，而巨型帧模式明明是开着的。根因是 ``GevSCPSPacketSize`` 是相机端
设置，SDK 只校验它是否落在相机自己支持的范围里，**完全不知道链路上能过
多大的包**。

本文件测的是 `CameraOperation` 为此加的三道防线：

  1. ``clamp_packet_size()`` —— 纯函数，把期望包长收敛到"链路和相机都能
     接受"的值。**保证结果永不超过链路实测 MTU。**
  2. ``probe_link_mtu()`` —— 实测相机所在网卡的 MTU，探测不到返回 0，
     调用方必须把 0 当"不开巨型帧"处理。
  3. ``_apply_packet_size()`` —— 设置后**回读校验**，任何一步失败都只告警、
     不抛异常、不挡相机启动。

跑法（不需要插相机，也不需要装大恒驱动）::

    python -m pytest Test_Tool/test_packet_size_safety.py -v
"""

import os
import socket
import sys
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import fake_galaxy_sdk  # noqa: E402

BACKEND = fake_galaxy_sdk.install()  # 必须在 import gxipy 之前

import gxipy as gx  # noqa: E402
from Camera_Tool import CameraRegistry  # noqa: E402
import Camera_Tool.CameraOperation as co  # noqa: E402
from Camera_Tool.CameraOperation import (  # noqa: E402
    CameraOperation,
    clamp_packet_size,
    probe_link_mtu,
    _netmask_to_int,
    _same_subnet,
    _MIN_SAFE_PACKET_SIZE,
)

SN = "FAKESN0001"
CAM_IP = "192.168.1.10"


# ======================================================================
#  夹具
# ======================================================================

@pytest.fixture(autouse=True)
def backend():
    BACKEND.reset()
    CameraRegistry.clear_all()
    BACKEND.add_camera(SN, "MER2-500-14GM", CAM_IP, 32, 24,
                       fake_galaxy_sdk.PIXEL.BAYER_GB8)
    yield BACKEND
    CameraRegistry.clear_all()


def make_cam(packet_size=0):
    mgr = gx.DeviceManager()
    return CameraOperation(
        mgr, {"index": 1, "sn": SN, "model_name": "MER2-500-14GM", "ip": CAM_IP},
        packet_size=packet_size,
    )


# ======================================================================
#  1. clamp_packet_size —— 核心保证：永不超过链路 MTU
# ======================================================================

def test_jumbo_used_when_link_supports_it():
    """交换机/网卡都开了巨型帧：应当用上，不能白白退回 1500"""
    assert clamp_packet_size(8164, 9000, 576, 9000, 4) == 8164


def test_config_9000_clamped_to_real_mtu_1500():
    """
    **这就是现场那次事故**：配置写了 9000，但链路只有 1500。
    绝不能把 9000 写进相机——那正是"包超过链路承载 → 起不来"的成因。
    """
    assert clamp_packet_size(9000, 1500, 576, 9000, 4) == 1500


def test_never_exceeds_link_mtu_across_sweep():
    """性质测试：任意组合下结果都不得超过链路 MTU"""
    for mtu in (1500, 4000, 8192, 9000, 9216):
        for desired in (0, 1500, 1600, 8164, 9000, 9216, 65535):
            for cam_max in (1500, 1600, 9000, 4294967295):
                got = clamp_packet_size(desired, mtu, 576, cam_max, 4)
                assert got <= mtu, (
                    f"包长 {got} 超过链路 MTU {mtu} "
                    f"(desired={desired}, cam_max={cam_max})")


def test_camera_max_wins_when_smaller_than_mtu():
    """链路能过 9000，但相机最多只支持 1600 → 取 1600"""
    assert clamp_packet_size(8164, 9000, 576, 1600, 4) == 1600


def test_unknown_mtu_falls_back_to_standard_frame():
    """
    探测不到 MTU 时**保守退回 1500**，绝不冒险开巨型帧。
    这是"探测失败也不能把相机弄到起不来"的关键。
    """
    assert clamp_packet_size(8164, 0, 576, 9000, 4) == _MIN_SAFE_PACKET_SIZE
    assert clamp_packet_size(0, 0, 576, 9000, 4) == _MIN_SAFE_PACKET_SIZE


def test_auto_mode_uses_full_mtu():
    """desired=0 表示自动：链路允许就取满 MTU"""
    assert clamp_packet_size(0, 9000, 576, 9000, 4) == 9000
    assert clamp_packet_size(0, 1500, 576, 9000, 4) == 1500


def test_aligned_down_to_camera_increment():
    """按相机步进**向下**对齐，不四舍五入——向上会超限"""
    assert clamp_packet_size(8166, 9000, 576, 9000, 4) == 8164
    assert clamp_packet_size(8163, 9000, 576, 9000, 4) == 8160
    # inc 缺失/为 0 时按 1 处理，不应崩
    assert clamp_packet_size(8164, 9000, 576, 9000, 0) == 8164


def test_returns_zero_when_nothing_safe():
    """算不出安全值就返回 0，让调用方"不修改"而不是硬写一个坏值"""
    assert clamp_packet_size(8164, 9000, 576, 0, 4) == 0        # 相机上限无效
    assert clamp_packet_size(1400, 1400, 576, 9000, 4) == 0     # 低于 1500


def test_never_below_camera_min_or_1500():
    """下限保护：既不低于相机最小值，也不低于 1500"""
    # 相机最小 1000，MTU 1200 → 低于 1500，放弃
    assert clamp_packet_size(1200, 1200, 1000, 9000, 4) == 0
    # 相机最小 2000，MTU 9000 → 至少 2000
    assert clamp_packet_size(0, 9000, 2000, 9000, 4) == 9000


# ======================================================================
#  2. 子网掩码解析（不同平台给的格式不一样）
# ======================================================================

def test_netmask_dotted_quad():
    assert _netmask_to_int("255.255.255.0") == 0xFFFFFF00
    assert _netmask_to_int("255.255.0.0") == 0xFFFF0000
    assert _netmask_to_int("255.255.255.255") == 0xFFFFFFFF


def test_netmask_prefix_length_string():
    """有些平台/版本给的是前缀长度字符串而不是点分十进制"""
    assert _netmask_to_int("24") == 0xFFFFFF00
    assert _netmask_to_int("16") == 0xFFFF0000
    assert _netmask_to_int("32") == 0xFFFFFFFF


def test_netmask_garbage_returns_zero():
    assert _netmask_to_int("") == 0
    assert _netmask_to_int(None) == 0
    assert _netmask_to_int("not-a-mask") == 0


def test_same_subnet_true_and_false():
    assert _same_subnet("192.168.1.5", "255.255.255.0", "192.168.1.10") is True
    assert _same_subnet("192.168.1.5", "255.255.255.0", "192.168.2.10") is False
    assert _same_subnet("192.168.1.5", "255.255.0.0", "192.168.7.10") is True
    # 掩码无效时不能瞎认
    assert _same_subnet("192.168.1.5", None, "192.168.1.10") is False
    assert _same_subnet("bad-ip", "255.255.255.0", "192.168.1.10") is False


# ======================================================================
#  3. probe_link_mtu
# ======================================================================

def test_probe_returns_zero_for_empty_ip():
    assert probe_link_mtu("") == 0
    assert probe_link_mtu(None) == 0
    assert probe_link_mtu("   ") == 0


def _fake_addr(family, address, netmask):
    return types.SimpleNamespace(family=family, address=address, netmask=netmask)


def test_probe_finds_mtu_of_same_subnet_nic(monkeypatch):
    """相机在 192.168.1.0/24，就该取那张网卡的 MTU"""
    fake_psutil = types.SimpleNamespace(
        net_if_stats=lambda: {
            "eth0": types.SimpleNamespace(isup=True, mtu=1500),
            "eth1": types.SimpleNamespace(isup=True, mtu=9000),
        },
        net_if_addrs=lambda: {
            "eth0": [_fake_addr(socket.AF_INET, "10.0.0.5", "255.255.255.0")],
            "eth1": [_fake_addr(socket.AF_INET, "192.168.1.5", "255.255.255.0")],
        },
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    assert probe_link_mtu("192.168.1.10") == 9000
    assert probe_link_mtu("10.0.0.99") == 1500


def test_probe_skips_down_interfaces(monkeypatch):
    """网卡 down 着就不能拿它的 MTU，否则会按一个不通的链路去设包长"""
    fake_psutil = types.SimpleNamespace(
        net_if_stats=lambda: {
            "eth1": types.SimpleNamespace(isup=False, mtu=9000),
        },
        net_if_addrs=lambda: {
            "eth1": [_fake_addr(socket.AF_INET, "192.168.1.5", "255.255.255.0")],
        },
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    assert probe_link_mtu("192.168.1.10") == 0


def test_probe_matches_when_camera_ip_on_local_nic(monkeypatch):
    """相机 IP 就配在本机网卡上（同机直连）时也要认出来"""
    fake_psutil = types.SimpleNamespace(
        net_if_stats=lambda: {"lo": types.SimpleNamespace(isup=True, mtu=65536)},
        net_if_addrs=lambda: {
            "lo": [_fake_addr(socket.AF_INET, CAM_IP, "255.255.255.0")],
        },
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    assert probe_link_mtu(CAM_IP) == 65536


def test_probe_returns_zero_when_psutil_raises(monkeypatch):
    """psutil 炸了也不能抛出去——返回 0 让调用方走保守路径"""
    def _boom():
        raise OSError("psutil unavailable")

    fake_psutil = types.SimpleNamespace(net_if_stats=_boom, net_if_addrs=_boom)
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    assert probe_link_mtu(CAM_IP) == 0


def test_probe_returns_zero_when_psutil_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    assert probe_link_mtu(CAM_IP) == 0


# ======================================================================
#  4. _apply_packet_size 端到端（真实 CameraOperation + 假 C 层）
# ======================================================================

def test_apply_uses_conservative_1500_when_mtu_unknown(monkeypatch):
    """沙箱里探测不到相机网段 → 必须落到 1500，且回读一致"""
    monkeypatch.setattr(co, "probe_link_mtu", lambda ip: 0)
    cam = make_cam(packet_size=8164)
    assert cam.Open_device() == 0

    assert cam._diag_packet_size_applied == _MIN_SAFE_PACKET_SIZE
    assert cam._diag_link_mtu == 0
    # 配置要 8164 但被下调，note 必须留下痕迹
    assert "下调" in cam._diag_packet_size_note


def test_apply_uses_jumbo_when_link_supports_it(monkeypatch):
    monkeypatch.setattr(co, "probe_link_mtu", lambda ip: 9000)
    cam = make_cam(packet_size=8164)
    assert cam.Open_device() == 0

    assert cam._diag_packet_size_applied == 8164
    assert cam._diag_link_mtu == 9000
    assert cam._diag_packet_size_note == ""


def test_apply_clamps_config_larger_than_mtu(monkeypatch):
    """**现场事故场景**：配置 9000、链路 1500 → 必须降到 1500"""
    monkeypatch.setattr(co, "probe_link_mtu", lambda ip: 1500)
    cam = make_cam(packet_size=9000)
    assert cam.Open_device() == 0

    assert cam._diag_packet_size_applied == 1500
    assert cam._diag_packet_size_applied <= 1500


def test_apply_auto_mode_follows_mtu(monkeypatch):
    """packet_size=0 走自动：链路多大就用多大"""
    monkeypatch.setattr(co, "probe_link_mtu", lambda ip: 9000)
    cam = make_cam(packet_size=0)
    assert cam.Open_device() == 0
    assert cam._diag_packet_size_applied == 9000


def test_apply_minus_one_does_not_touch_feature(monkeypatch):
    """packet_size=-1 是完全不碰这个特征的逃生口"""
    monkeypatch.setattr(co, "probe_link_mtu", lambda ip: 9000)
    cam = make_cam(packet_size=-1)
    assert cam.Open_device() == 0

    assert cam._diag_packet_size_applied == 0
    assert cam._diag_link_mtu == 0, "-1 时连 MTU 都不该去探测"
    assert cam._diag_packet_size_note


def test_apply_never_raises_when_probe_blowups(monkeypatch):
    """探测函数炸了也必须让相机正常启动——这是"不能死机/不能起不来"的底线"""
    def _boom(ip):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(co, "probe_link_mtu", _boom)
    cam = make_cam(packet_size=8164)
    # Open_device 内部 try/except 兜着；这里要确认相机仍然启动成功
    assert cam.Open_device() == 0
    assert cam.b_open_device is True


def test_apply_records_readback_mismatch(monkeypatch):
    """set() 不抛异常不代表相机采纳了。回读不一致必须留证据并告警。"""
    monkeypatch.setattr(co, "probe_link_mtu", lambda ip: 9000)
    cam = make_cam(packet_size=8164)
    assert cam.Open_device() == 0

    # 篡改回读值，模拟相机把包长夹到了别的档位
    real_feature = cam._feature_control.get_int_feature("GevSCPSPacketSize")

    class _LyingFeature:
        def get_range(self):
            return real_feature.get_range()

        def get(self):
            return 1600          # 相机实际采纳的值和我们要求的不一样

        def set(self, v):
            real_feature.set(v)

    monkeypatch.setattr(
        cam._feature_control, "get_int_feature",
        lambda name: _LyingFeature() if name == "GevSCPSPacketSize"
        else real_feature,
    )
    cam._apply_packet_size()

    assert cam._diag_packet_size_applied == 1600
    assert "回读不一致" in cam._diag_packet_size_note


def test_diag_exposes_packet_size_fields(monkeypatch):
    """健康快照要能读到这些字段，现场才能判断包长有没有设对"""
    monkeypatch.setattr(co, "probe_link_mtu", lambda ip: 9000)
    cam = make_cam(packet_size=8164)
    cam.Open_device()

    d = cam.get_diag_info()
    for key in ("link_mtu", "packet_size_wanted",
                "packet_size_applied", "packet_size_note"):
        assert key in d, f"get_diag_info() 缺少 {key}"
    assert d["link_mtu"] == 9000
    assert d["packet_size_wanted"] == 8164
    assert d["packet_size_applied"] == 8164
