#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
健康心跳里的**内核非分页池**监控测试。

为什么要有这一项：网卡驱动和 GigE 过滤驱动是从 Windows 内核**非分页池**分配
内存的，那是独立于物理内存的另一套资源。收包路径一堵，驱动持续申请，池子见底
时整个 OS 失稳、只能硬重启 —— **而此时物理内存可能才用 60%**，
`health_rss_warn_mb` 和 `MemoryGuard` 两道基于物理内存的防线都不会触发。

这里能离线验证的部分：
  * PERFORMANCE_INFORMATION 的**结构体布局**（偏移与 sizeof）。布局错了会读到
    相邻字段的垃圾值，而且不会报错——所以必须钉死。
  * 取不到数据时返回 None，且**绝不在快照里写成 0**。
  * 基线/峰值/增长的记账逻辑，以及告警升级。

真实 Windows 上的 `GetPerformanceInfo` 返回值无法在这里验证（沙箱是 Linux），
函数里另外加了一道自证：`PhysicalTotal × PageSize` 必须与 psutil 报告的物理
总内存吻合，否则返回 None。

跑法::

    python -m pytest Test_Tool/test_health_pool_monitor.py -v
"""

import ctypes
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import General_Tool.HealthMonitor as hm  # noqa: E402
from General_Tool.HealthMonitor import (  # noqa: E402
    HealthMonitor,
    get_kernel_pool_info,
    _define_perf_struct,
)


# ======================================================================
#  1. 结构体布局：错了会静默读到垃圾值，必须钉死
# ======================================================================

# Win32 文档里 PERFORMANCE_INFORMATION 在 x64 上的布局
_EXPECTED_X64 = {
    "cb":                 (0, 4),
    "CommitTotal":        (8, 8),
    "CommitLimit":        (16, 8),
    "CommitPeak":         (24, 8),
    "PhysicalTotal":      (32, 8),
    "PhysicalAvailable":  (40, 8),
    "SystemCache":        (48, 8),
    "KernelTotal":        (56, 8),
    "KernelPaged":        (64, 8),
    "KernelNonPaged":     (72, 8),
    "PageSize":           (80, 8),
    "HandleCount":        (88, 4),
    "ProcessCount":       (92, 4),
    "ThreadCount":        (96, 4),
}


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8,
                    reason="只在 64 位上校验 x64 布局")
def test_struct_layout_matches_win32_x64():
    """每个字段的偏移和宽度都必须与 Win32 文档一致"""
    _, PERF = _define_perf_struct()
    for name, (off, size) in _EXPECTED_X64.items():
        f = getattr(PERF, name)
        assert f.offset == off, f"{name} 偏移应为 {off}，实际 {f.offset}"
        assert f.size == size, f"{name} 宽度应为 {size}，实际 {f.size}"


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8,
                    reason="只在 64 位上校验 x64 布局")
def test_struct_total_size_is_104():
    """
    sizeof 必须正好 104。这个值同时被当作 cb 传给 GetPerformanceInfo，
    传错了系统调用会直接失败（失败模式是"没有数据"，不是"错误数据"，
    所以宁可让它失败）。
    """
    _, PERF = _define_perf_struct()
    assert ctypes.sizeof(PERF) == 104


def test_kernel_nonpaged_offset_is_72():
    """单独钉一下最关心的那个字段——它是这整套监控存在的理由"""
    _, PERF = _define_perf_struct()
    assert getattr(PERF, "KernelNonPaged").offset == 72


# ======================================================================
#  2. 取不到数据时的行为
# ======================================================================

def test_returns_none_on_non_windows(monkeypatch):
    """非 Windows 平台必须返回 None，不能抛、也不能返回假数据"""
    # get_kernel_pool_info 内部是 `import sys`，拿到的是同一个模块对象
    monkeypatch.setattr(sys, "platform", "linux")
    assert get_kernel_pool_info() is None


def test_none_is_never_written_as_zero():
    """
    快照里绝不能出现 pool_nonpaged_mb=0。0 会被读成"池子是空的、很健康"，
    而真相是"这项数据没取到"。取不到就该完全没有这个键。
    """
    mon = HealthMonitor(interval=0)
    snap = mon.snapshot()
    if sys.platform != "win32":
        assert "pool_nonpaged_mb" not in snap
        assert "pool_nonpaged_growth_mb" not in snap


def test_probe_failure_does_not_break_snapshot(monkeypatch):
    """探测函数炸了，心跳的其余部分必须照常出数据"""
    def _boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(hm, "get_kernel_pool_info", _boom)
    mon = HealthMonitor(interval=0)
    snap = mon.snapshot()

    assert "pool_err" in snap
    # 其余字段不受影响
    assert "threads" in snap
    assert "gc" in snap


# ======================================================================
#  3. 基线 / 峰值 / 增长记账
# ======================================================================

def test_baseline_peak_growth_accounting(monkeypatch):
    """耗尽是缓慢爬升，只有增长量才能证明它"""
    values = iter([300.0, 420.0, 900.0, 750.0])

    monkeypatch.setattr(hm, "get_kernel_pool_info", lambda: {
        "pool_nonpaged_mb": next(values),
        "pool_paged_mb": 100.0,
        "pool_kernel_mb": 0.0,
        "handles_system": 0,
    })

    mon = HealthMonitor(interval=0)

    s1 = mon.snapshot()
    assert s1["pool_nonpaged_mb"] == 300.0
    assert s1["pool_nonpaged_base_mb"] == 300.0   # 首次即基线
    assert s1["pool_nonpaged_growth_mb"] == 0.0

    s2 = mon.snapshot()
    assert s2["pool_nonpaged_base_mb"] == 300.0   # 基线不随时间漂移
    assert s2["pool_nonpaged_growth_mb"] == 120.0

    s3 = mon.snapshot()
    assert s3["pool_nonpaged_peak_mb"] == 900.0
    assert s3["pool_nonpaged_growth_mb"] == 600.0

    s4 = mon.snapshot()
    assert s4["pool_nonpaged_mb"] == 750.0
    assert s4["pool_nonpaged_peak_mb"] == 900.0   # 峰值不因回落而下降
    assert s4["pool_nonpaged_growth_mb"] == 450.0


def test_baseline_captured_only_once(monkeypatch):
    """基线必须是启动后的第一次读数，否则增长量会被稀释成 0"""
    monkeypatch.setattr(hm, "get_kernel_pool_info",
                        lambda: {"pool_nonpaged_mb": 500.0})
    mon = HealthMonitor(interval=0)
    for _ in range(5):
        snap = mon.snapshot()
    assert snap["pool_nonpaged_base_mb"] == 500.0
    assert snap["pool_nonpaged_growth_mb"] == 0.0


def test_missing_nonpaged_key_does_not_crash(monkeypatch):
    """探测只返回部分字段时不能 KeyError"""
    monkeypatch.setattr(hm, "get_kernel_pool_info",
                        lambda: {"pool_paged_mb": 100.0})
    mon = HealthMonitor(interval=0)
    snap = mon.snapshot()
    assert "pool_paged_mb" in snap
    assert "pool_nonpaged_growth_mb" not in snap


# ======================================================================
#  4. 告警升级
# ======================================================================

def test_warning_threshold_configurable():
    mon = HealthMonitor(interval=0, pool_growth_warn_mb=512)
    assert mon.pool_growth_warn_mb == 512.0
    # 0 = 关闭告警
    assert HealthMonitor(interval=0, pool_growth_warn_mb=0).pool_growth_warn_mb == 0.0


def _heartbeat_lines(monkeypatch):
    """把 HealthMonitor 导入的 info/warning 换成录音桩，返回收集列表。

    不用 caplog 也不用 capsys：项目的 EnhancedLogger 在 __init__ 里
    _clear_handlers()、并用自定义的 _log_with_caller_info 出日志，
    既不经过标准 handler 链（挂 handler 抓不到），写 stdout 的时机也不受
    pytest 捕获控制。直接替换 HealthMonitor 模块里的 info/warning 最确定。
    """
    calls = []
    monkeypatch.setattr(hm, "info", lambda m, *a, **k: calls.append(("INFO", m)))
    monkeypatch.setattr(hm, "warning",
                        lambda m, *a, **k: calls.append(("WARNING", m)))
    return calls


def _run_one_loop(mon):
    """让 _loop 跑完一轮快照后退出。

    停止用的 provider 必须在基线快照**之后**才装上，否则第一次快照就把它
    置位，_loop 一进 while 就退出，什么都测不到。
    """
    mon.add_provider(lambda: (mon._stop.set(), {})[1])
    mon._loop()


def test_over_pool_triggers_warning_branch(monkeypatch):
    """增长超过阈值时，心跳必须升级成 WARNING 并说明是内核池"""
    calls = _heartbeat_lines(monkeypatch)
    mon = HealthMonitor(interval=0.001, pool_growth_warn_mb=512)

    monkeypatch.setattr(hm, "get_kernel_pool_info",
                        lambda: {"pool_nonpaged_mb": 100.0})
    base = mon.snapshot()            # 建立基线 100
    assert base["pool_nonpaged_base_mb"] == 100.0

    monkeypatch.setattr(hm, "get_kernel_pool_info",
                        lambda: {"pool_nonpaged_mb": 900.0})

    _run_one_loop(mon)

    hb = [m for lvl, m in calls if "tick=1" in m]
    assert hb, f"心跳这一轮根本没跑，实际调用: {calls}"
    assert any("内核非分页池" in m for m in hb), f"没有看到内核池告警: {hb}"
    assert any("800" in m for m in hb)          # 增长量 900-100
    assert any(lvl == "WARNING" and "tick=1" in m for lvl, m in calls), (
        f"应升级为 WARNING，实际级别: {[l for l, _ in calls]}")


def test_under_pool_threshold_stays_info(monkeypatch):
    """没超阈值时不该误报，否则现场会把这个告警当噪音忽略掉"""
    calls = _heartbeat_lines(monkeypatch)
    mon = HealthMonitor(interval=0.001, pool_growth_warn_mb=512)

    monkeypatch.setattr(hm, "get_kernel_pool_info",
                        lambda: {"pool_nonpaged_mb": 300.0})
    base = mon.snapshot()            # 基线 300
    assert base["pool_nonpaged_base_mb"] == 300.0

    _run_one_loop(mon)

    hb = [m for lvl, m in calls if "tick=1" in m]
    assert hb, f"心跳这一轮根本没跑，实际调用: {calls}"
    assert not any("内核非分页池" in m for m in hb), f"不该告警却告警了: {hb}"
    # 心跳本体仍然要发（INFO 级）
    assert any(lvl == "INFO" and "tick=1" in m for lvl, m in calls)


def test_pool_warning_disabled_when_threshold_zero(monkeypatch):
    """阈值设 0 表示关闭告警，此时即使涨很多也不该报"""
    calls = _heartbeat_lines(monkeypatch)
    mon = HealthMonitor(interval=0.001, pool_growth_warn_mb=0)

    monkeypatch.setattr(hm, "get_kernel_pool_info",
                        lambda: {"pool_nonpaged_mb": 100.0})
    mon.snapshot()

    monkeypatch.setattr(hm, "get_kernel_pool_info",
                        lambda: {"pool_nonpaged_mb": 9999.0})

    _run_one_loop(mon)

    hb = [m for lvl, m in calls if "tick=1" in m]
    assert hb, f"心跳这一轮根本没跑，实际调用: {calls}"
    assert not any("内核非分页池" in m for m in hb)
    # 但数据本身仍然要记录，关闭的只是告警
    assert any("pool_nonpaged_mb=9999.0" in m for m in hb)


def test_init_health_monitor_accepts_pool_param():
    """init_health_monitor 必须把这个参数透下去，否则 config 里的值无效"""
    import inspect
    sig = inspect.signature(hm.init_health_monitor)
    assert "pool_growth_warn_mb" in sig.parameters
