# -*- coding: utf-8 -*-
"""缺陷停机标志卡死 -> 硬触发帧被静默丢弃。

现场现象：Modbus 速度正常（10.8 m/min）、[触发] 正常、总米长一直涨，
前端却一张图都收不到。每行状态末尾都挂着 [缺陷逻辑停机]。

根因不在 image_control()，在 Modbus._check_auto_resume()：
旧实现把"观测停稳"和"恢复动作"绑在冷却期之后 —— 冷却期里强制
_has_stopped_after_defect = False 并直接 return，等于这 2 秒里发生的一切
都不看。现场织机常在 1 秒内停完又重启，等冷却结束再去等停稳，那个"停"
早就过去了，状态机永远卡在"等待停稳"，标志再也不复位。

冷却期挡的应该是"恢复动作"，不是"观测"。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_galaxy_sdk
fake_galaxy_sdk.install()

import Communication_Tool.ThriftControl as TC
from Communication_Tool import Modbus
from Communication_Tool.Modbus import TriggerControlSystem

MIN_COOL_DOWN = 2.0
STOP_SPEED = 1.0
RESUME_SPEED = 5.0
SUSTAINED_RESUME = 10.0


class Shim:
    """只带状态机需要的字段，方法用真实实现。"""
    _has_stopped_after_defect = False
    _resume_speed_since = None
    _resume_log_at = 0.0
    _check_auto_resume = TriggerControlSystem._check_auto_resume


@pytest.fixture
def shim():
    s = Shim()
    s._has_stopped_after_defect = False
    s._resume_speed_since = None
    s._resume_log_at = 0.0
    TC.setIsStop(False, "")
    TC.setIsStop(True, "缺陷停机")
    yield s
    TC.setIsStop(False, "")


def drive(shim, script):
    """script: [(dt, speed)]。返回 (恢复时刻, 日志列表)。

    注入 now 而不是真的睡 —— 兜底判定要"连续高速 10 秒"，用真实时钟的话
    单测得睡 10 秒。_defect_stop_time 固定不动，用 now 推进时间轴。
    """
    logs = []
    old = Modbus.info
    Modbus.info = lambda m: logs.append(m)
    t0 = TC.getDefectStopTime()
    elapsed = 0.0
    recovered_at = None
    try:
        for dt, sp in script:
            elapsed += dt
            shim._check_auto_resume({'current_speed': sp}, now=t0 + elapsed)
            if recovered_at is None and not TC.getIsStop():
                recovered_at = elapsed
    finally:
        Modbus.info = old
    return recovered_at, logs


class TestAutoResumeDeadlock:
    def test_normal_stop_then_restart_recovers(self, shim):
        """基线：停机持续超过冷却期，旧实现也能恢复，别改坏它。"""
        at, _ = drive(shim, [(0.5, 0.0)] * 8 + [(0.5, 10.0)] * 8)
        assert at is not None
        assert at == pytest.approx(4.5)
        assert TC.getIsStop() is False

    def test_stop_and_restart_inside_cooldown_recovers(self, shim):
        """回归点：停机和重启都落在 2 秒冷却期内 —— 旧实现永久卡死。"""
        at, logs = drive(
            shim,
            [(0.3, 9.0)] * 2 + [(0.3, 0.0)] * 2 + [(0.3, 10.8)] * 20)
        assert at is not None, "冷却期内停机被忽略，状态机永久卡死"
        assert at == pytest.approx(2.1, abs=0.05)   # 冷却一结束就恢复
        assert TC.getIsStop() is False
        assert any("已确认停稳" in l for l in logs), logs

    def test_never_observed_stop_falls_back(self, shim):
        """完全没观测到停稳：靠"连续高速 10 秒"兜底，否则永久卡死。"""
        at, _ = drive(shim, [(0.114, 10.8)] * 120)
        assert at is not None, "没观测到停稳就永久卡死"
        assert at == pytest.approx(SUSTAINED_RESUME + MIN_COOL_DOWN, abs=0.3)

    def test_interrupted_high_speed_resets_the_timer(self, shim):
        """高速被打断要重新计时 —— 兜底不能被零星高速凑出来。"""
        at, _ = drive(shim, [(0.114, 10.8)] * 60 +
                      [(0.114, 2.0)] * 5 + [(0.114, 10.8)] * 60)
        assert at is None
        assert TC.getIsStop() is True

    def test_cooldown_alone_does_not_recover(self, shim):
        """冷却期内即使速度很高也不能恢复（挡惯性速度的原意要保住）。"""
        at, _ = drive(shim, [(0.1, 30.0)] * 15)      # 1.5s < 2s
        assert at is None
        assert TC.getIsStop() is True

    def test_inertial_speed_after_stop_does_not_recover_immediately(self, shim):
        """停稳后速度还没回到 5 m/min 以上，不能恢复。"""
        at, _ = drive(shim, [(0.5, 0.0)] * 8 + [(0.5, 3.0)] * 10)
        assert at is None

    def test_manual_pause_never_auto_recovers(self, shim):
        """手动停止优先级最高，状态机不许碰。"""
        TC.setIsStop(True, "停止")
        at, _ = drive(shim, [(0.5, 0.0)] * 8 + [(0.5, 10.0)] * 30)
        assert at is None
        assert TC.getIsStop() is True
        assert TC.isManualPaused() is True


class TestSpeedSnapshot:
    """image_control 在相机回调线程，拿不到 main() 里的 controller 实例。"""

    def test_get_current_speed_reads_snapshot(self):
        old = Modbus._LAST_SPEED
        try:
            Modbus._LAST_SPEED = 10.86
            assert Modbus.get_current_speed() == 10.86
        finally:
            Modbus._LAST_SPEED = old

    def test_main_yolo_speed_helper_never_raises(self):
        import main_yolo
        assert isinstance(main_yolo._current_loom_speed(), float)


class TestDroppedFramesAreVisible:
    """缺陷停机丢帧必须出声，否则现场分不清"相机没出图"和"图被丢了"。"""

    @pytest.fixture
    def frames(self, monkeypatch):
        import numpy as np
        import main_yolo as MY
        from AnomalyDetection_Tool.config.settings import CAMERA_SN_MAP
        sn = next(iter(CAMERA_SN_MAP))
        logs = []
        monkeypatch.setattr(MY, "info", lambda m: logs.append(m))
        monkeypatch.setattr(MY, "debug", lambda m: None)
        monkeypatch.setattr(MY, "get_running_system", lambda: None)
        MY._defect_stop_log_ts.clear()
        MY._manual_stop_log_ts.clear()
        return MY, sn, np.zeros((64, 64, 3), dtype=np.uint8), logs

    def _drop(self, frames, n, status):
        MY, sn, img, logs = frames
        TC.setIsStop(False, "")
        TC.setIsStop(True, status)
        logs.clear()
        MY._defect_stop_log_ts.clear()
        MY._manual_stop_log_ts.clear()
        for _ in range(n):
            MY.image_control(img, {"nFrameNum": 1, "nHeight": 64,
                                   "nWidth": 64}, camera_sn=sn)
        return logs

    def test_defect_stop_drop_is_logged(self, frames):
        MY, sn, img, _ = frames
        logs = self._drop(frames, 25, "缺陷停机")
        assert len(logs) == 1, logs
        assert "缺陷停机" in logs[0]
        assert "被丢弃" in logs[0]
        assert "m/min" in logs[0]          # 带上织机速度，现场才看得懂

    def test_defect_stop_drop_is_throttled(self, frames):
        logs = self._drop(frames, 500, "缺陷停机")
        assert len(logs) == 1, "500 帧只该打 1 条（10s 节流窗口）"

    def test_manual_stop_uses_its_own_message(self, frames):
        logs = self._drop(frames, 25, "停止")
        assert len(logs) == 1, logs
        assert "[停止]" in logs[0]
        assert "缺陷停机" not in logs[0]

    def test_unknown_camera_still_returns_early(self, frames):
        """SN 没配的相机走的是另一条 return，不该打丢帧日志。"""
        MY, _, img, _ = frames
        TC.setIsStop(False, "")
        TC.setIsStop(True, "缺陷停机")
        logs = []
        MY.info = lambda m: logs.append(m)
        MY._defect_stop_log_ts.clear()
        MY.image_control(img, {"nFrameNum": 1, "nHeight": 64, "nWidth": 64},
                         camera_sn="NOT_A_REAL_SN")
        assert logs == []
