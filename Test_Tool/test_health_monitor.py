#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HealthMonitor（General_Tool/HealthMonitor.py）的回归测试。

跑法::

    python -m pytest Test_Tool/test_health_monitor.py -v

重点：心跳线程绝不能因为某个数据源出错而挂掉 ——
它是卡死现场唯一的取证手段。
"""

import os
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from General_Tool.HealthMonitor import (          # noqa: E402
    HealthMonitor, init_health_monitor, get_health_monitor,
    shutdown_health_monitor,
)


def test_snapshot_contains_core_fields():
    m = HealthMonitor(interval=0)
    snap = m.snapshot()
    assert "tick" in snap
    assert "threads" in snap
    assert snap["threads"] > 0


def test_snapshot_reports_rss_when_psutil_available():
    pytest.importorskip("psutil")
    m = HealthMonitor(interval=0)
    snap = m.snapshot()
    assert snap.get("rss_mb", 0) > 0, "psutil 可用时应当报出 RSS"


def test_provider_values_are_merged():
    m = HealthMonitor(interval=0,
                      providers=[lambda: {"camera1.frames": 42, "infer_q": 3}])
    snap = m.snapshot()
    assert snap["camera1.frames"] == 42
    assert snap["infer_q"] == 3


def test_broken_provider_does_not_kill_snapshot():
    """单个 provider 抛异常必须被吞掉，其余数据照常"""
    def boom():
        raise RuntimeError("provider 炸了")

    m = HealthMonitor(interval=0, providers=[boom, lambda: {"ok": 1}])
    snap = m.snapshot()
    assert snap["ok"] == 1
    assert any(k.startswith("provider_err") for k in snap), snap


def test_non_dict_provider_return_is_ignored():
    m = HealthMonitor(interval=0, providers=[lambda: "不是dict"])
    snap = m.snapshot()
    assert "threads" in snap


def test_peak_rss_is_tracked(monkeypatch):
    m = HealthMonitor(interval=0)
    m.snapshot()
    assert m._peak_rss_mb >= 0


def test_loop_runs_and_stops():
    m = HealthMonitor(interval=0.05)
    assert m.start() is True
    deadline = time.time() + 3.0
    while m._tick < 2 and time.time() < deadline:
        time.sleep(0.02)
    assert m._tick >= 2, "心跳线程没有按间隔跑起来"
    m.stop()
    assert not (m._thread is not None and m._thread.is_alive())


def test_loop_survives_provider_failure():
    """provider 每拍都抛，心跳线程也必须继续跑"""
    def boom():
        raise RuntimeError("每拍都炸")

    m = HealthMonitor(interval=0.05, providers=[boom])
    m.start()
    deadline = time.time() + 3.0
    while m._tick < 3 and time.time() < deadline:
        time.sleep(0.02)
    ticks = m._tick
    m.stop()
    assert ticks >= 3, f"provider 连续失败把心跳线程带崩了（只跑了 {ticks} 拍）"


def test_zero_interval_does_not_start():
    m = HealthMonitor(interval=0)
    assert m.start() is False
    assert m._thread is None


def test_stop_is_idempotent():
    m = HealthMonitor(interval=0.05)
    m.start()
    m.stop()
    m.stop()          # 再停一次不应抛异常


def test_singleton_lifecycle():
    mon = init_health_monitor(interval=0.05)
    assert get_health_monitor() is mon
    shutdown_health_monitor()
    assert get_health_monitor() is None
    shutdown_health_monitor()   # 幂等
