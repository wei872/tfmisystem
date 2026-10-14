#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【五】超速守卫的回归测试。

重点盯四件事：
  1. **钳帧间隔算得对**（10 m/min + 80.224 mm ⇒ 481.3 ms / 2.0775 Hz）；
  2. **限流按相机独立**（8 台不能共用一个时间戳互相挤占）；
  3. **中值滤波挡住测速尖峰**（current_speed 在 dt 极小时 1 个脉冲
     就能算出 1413 m/min）；
  4. **闸门在 _to_bgr 之前** —— 否则省不下那 15 MB 分配和色彩转换。
"""

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from General_Tool.SpeedGuard import (  # noqa: E402
    SpeedGuard, init_speed_guard, get_speed_guard,
    DEFAULT_WARN_MPM, DEFAULT_CAP_SPEED_MPM, DEFAULT_TRIGGER_DISTANCE_MM,
    DEFAULT_FRAME_HEIGHT_MM)


class _Clock:
    """可手动推进的假时钟。"""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def adv(self, dt):
        self.t += dt


def _g(clock=None, **kw):
    kw.setdefault("window", 1)
    kw.setdefault("min_samples", 1)
    return SpeedGuard(clock=clock or _Clock(), **kw)


# ======================================================================
# §1  钳帧间隔与临界车速
# ======================================================================
class TestCapMath:

    def test_cap_interval_matches_the_hand_calculation(self):
        """10 m/min + 80.224 mm ⇒ 2.0775 Hz ⇒ 481.3 ms。"""
        g = _g(cap_speed_mpm=10.0, trigger_distance_mm=80.224)
        assert g.cap_fps == pytest.approx(2.0775, abs=1e-3)
        assert g.cap_interval_sec * 1000 == pytest.approx(481.3, abs=0.5)

    def test_trigger_distance_default_matches_plc_config(self):
        """默认值必须等于 plc.trigger_distance_mm × scaling_factor。"""
        import yaml
        with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
            plc = yaml.safe_load(f)["plc"]
        expect = float(plc["trigger_distance_mm"]) * float(plc["scaling_factor"])
        assert DEFAULT_TRIGGER_DISTANCE_MM == pytest.approx(expect, abs=1e-6)

    def test_frame_height_default_matches_pixel_math(self):
        """2048 px × (23 cm / 2440 px) × 10 = 193.05 mm。"""
        assert DEFAULT_FRAME_HEIGHT_MM == pytest.approx(
            2048 * (23 / 2440) * 10, abs=1e-6)
        assert DEFAULT_FRAME_HEIGHT_MM == pytest.approx(193.05, abs=0.01)

    def test_blind_spot_speed_accounts_for_trigger_quantisation(self):
        """临界车速 = floor(帧高/触发距离) × cap_speed = 2 × 10 = 20.0。

        **不是** 帧高/cap_interval（那会算出 24.06，是错的）。钳帧只能落在
        触发栅格上，实际接受间隔被量化成 k × 触发距离（k 为整数），
        间距 = k × 触发距离与车速无关；完整覆盖要求
        k <= floor(193.05/80.224) = 2。
        """
        g = _g(cap_speed_mpm=10.0, trigger_distance_mm=80.224,
               frame_height_mm=193.05)
        assert g._max_k == 2
        assert g.blind_spot_mpm == pytest.approx(20.0, abs=1e-6)

    def test_the_wrong_formula_would_give_24(self):
        """钉住这个坑：帧高/cap_interval 是错的算法，别改回去。"""
        g = _g(cap_speed_mpm=10.0, trigger_distance_mm=80.224,
               frame_height_mm=193.05)
        wrong = g.frame_height_mm / g.cap_interval_sec * 60 / 1000
        assert wrong == pytest.approx(24.06, abs=0.02)
        assert g.blind_spot_mpm < wrong

    def test_blind_spot_limit_scales_with_cap_speed(self):
        """临界车速 = 帧高/触发距离 × cap_speed = 2.4064 × cap_speed。

        注意方向：钳得越**狠**（cap_speed 越小），盲区来得越**早**。
        这是个无法两全的权衡 —— 想同时改善只能缩短触发距离。
        """
        for cap, expect in ((5.0, 10.0), (10.0, 20.0), (20.0, 40.0)):
            g = _g(cap_speed_mpm=cap, trigger_distance_mm=80.224,
                   frame_height_mm=193.05)
            assert g.blind_spot_mpm == pytest.approx(expect, abs=0.02), cap

    def test_shorter_trigger_distance_raises_the_limit(self):
        """唯一能同时改善覆盖和保护的手段：触发更密。

        触发距离减半 ⇒ floor(193.05/40.112)=4 ⇒ 临界 4 × 10 = 40 m/min。
        """
        g = _g(cap_speed_mpm=10.0, trigger_distance_mm=40.112,
               frame_height_mm=193.05)
        assert g._max_k == 4
        assert g.blind_spot_mpm == pytest.approx(40.0, abs=1e-6)


# ======================================================================
# §2  什么时候钳、什么时候不钳
# ======================================================================
class TestWhenToCap:

    def test_below_threshold_nothing_is_dropped(self):
        c = _Clock()
        g = _g(c, warn_mpm=15.0)
        g.update(10.0)
        assert g.snapshot()["capping"] is False
        for i in range(20):                    # 疯狂喂帧也不该丢
            c.adv(0.001)
            assert g.should_drop("camera1") is False

    def test_above_threshold_starts_capping(self):
        c = _Clock()
        g = _g(c, warn_mpm=15.0)
        g.update(20.0)
        assert g.snapshot()["capping"] is True

    def test_first_frame_after_entering_cap_is_accepted(self):
        c = _Clock()
        g = _g(c, warn_mpm=15.0)
        g.update(20.0)
        assert g.should_drop("camera1") is False, "进入钳帧后第一帧必须收"

    def test_second_frame_too_soon_is_dropped(self):
        c = _Clock()
        g = _g(c, warn_mpm=15.0, cap_speed_mpm=10.0,
               trigger_distance_mm=80.224)
        g.update(20.0)
        assert g.should_drop("camera1") is False
        c.adv(0.100)                            # 100 ms < 481 ms
        assert g.should_drop("camera1") is True

    def test_frame_after_the_interval_is_accepted(self):
        c = _Clock()
        g = _g(c, warn_mpm=15.0, cap_speed_mpm=10.0,
               trigger_distance_mm=80.224)
        g.update(20.0)
        assert g.should_drop("camera1") is False
        c.adv(0.482)                            # > 481.3 ms
        assert g.should_drop("camera1") is False

    def test_sustained_overspeed_yields_the_capped_rate(self):
        """42 m/min 持续 10 秒：不钳会收 87 帧，钳后只收约 17 帧。

        实收率必然**低于**理论 cap_fps —— 只能在触发边界上收，
        481.3 ms 的间隔落在 114.7 ms 的触发栅格上会量化成 5×114.7=573.5 ms。
        所以断言的是"落在 [0.75, 1.0] × 理论值"，并且远低于不钳的帧数。
        """
        c = _Clock()
        g = _g(c, warn_mpm=15.0, cap_speed_mpm=10.0,
               trigger_distance_mm=80.224)
        g.update(42.0)
        accepted = 0
        itv = 0.1147                            # 42 m/min => 每台 8.72 帧/秒
        n = int(10 / itv)
        for _ in range(n):
            c.adv(itv)
            if not g.should_drop("camera1"):
                accepted += 1
        ideal = 2.0775 * 10
        assert accepted <= ideal, "钳帧后不可能比理论值收得更多"
        assert accepted >= ideal * 0.75, f"收得太少: {accepted}"
        assert accepted < n * 0.35, f"几乎没钳住: {accepted}/{n}"

    def test_hysteresis_prevents_flapping(self):
        c = _Clock()
        g = _g(c, warn_mpm=15.0, hysteresis=0.9)
        g.update(15.0)
        assert g.snapshot()["capping"] is True
        g.update(14.0)                          # 15×0.9=13.5，还没降够
        assert g.snapshot()["capping"] is True
        g.update(13.0)
        assert g.snapshot()["capping"] is False


# ======================================================================
# §2b  端到端：模拟真实帧流，验证覆盖边界
# ======================================================================
class TestCoverageBoundary:
    """把 Modbus 喂速度 + 8 台相机出帧整个跑一遍，钉住实测边界。

    理论临界是 20.0 m/min，但那是刀刃值：k = ceil(481.3/240.67) = 2，
    浮点累积会让第二帧落在 481.34 ms 刚好超阈值被拒，实际落到 k=3。
    所以实测是 <=19.5 完整、>=20.0 有盲区。
    """

    TD = 102 * 0.78651
    FH = 193.05

    def _simulate(self, speed, secs=10.0):
        c = _Clock(0.0)
        g = SpeedGuard(warn_mpm=15.0, cap_speed_mpm=10.0,
                       trigger_distance_mm=self.TD, frame_height_mm=self.FH,
                       window=5, min_samples=3, hysteresis=0.9, clock=c)
        itv = self.TD / (speed * 1000 / 60)
        n = int(secs / itv)
        nxt_modbus = 0.0
        accepted = 0
        for _ in range(n):
            c.adv(itv)
            while c() >= nxt_modbus:        # Modbus 线程按自己的节奏喂速度
                g.update(speed)
                nxt_modbus += 0.1
            if not g.should_drop("camera1"):
                accepted += 1
        return g, n, accepted

    def test_19_5_is_fully_covered(self):
        g, n, acc = self._simulate(19.5)
        assert g.snapshot()["accept_gap_mm"] <= self.FH
        assert g.snapshot()["stats"]["blind_warned"] == 0
        assert acc < n, "应该确实在钳帧"

    def test_20_has_a_blind_spot(self):
        g, n, acc = self._simulate(20.0)
        assert g.snapshot()["accept_gap_mm"] > self.FH, "刀刃值实测已落到 k=3"
        assert g.snapshot()["stats"]["blind_warned"] >= 1

    def test_capping_actually_reduces_the_frame_count(self):
        """42 m/min：不钳 87 帧/台，钳后约 19 帧 —— 内存分配降到约 1/4.6。"""
        g, n, acc = self._simulate(42.0)
        assert n == 87
        assert acc < n * 0.30
        assert g.snapshot()["stats"]["frames_capped"] > 0

    def test_below_warn_threshold_nothing_is_capped(self):
        g, n, acc = self._simulate(14.0)
        assert acc == n
        assert g.snapshot()["capping"] is False


# ======================================================================
# §3  按相机独立限流
# ======================================================================
class TestPerCameraIndependence:

    def test_cameras_do_not_share_the_quota(self):
        """8 台各自被 Line0 触发，共用时间戳会互相挤占。"""
        c = _Clock()
        g = _g(c, warn_mpm=15.0)
        g.update(20.0)
        for cam in [f"camera{i}" for i in range(1, 9)]:
            assert g.should_drop(cam) is False, f"{cam} 第一帧就被丢了"

    def test_one_camera_being_capped_does_not_affect_another(self):
        c = _Clock()
        g = _g(c, warn_mpm=15.0)
        g.update(20.0)
        assert g.should_drop("camera1") is False
        c.adv(0.05)
        assert g.should_drop("camera1") is True     # camera1 被钳
        assert g.should_drop("camera2") is False    # camera2 不受影响

    def test_counters_reset_when_cap_is_entered_and_exited(self):
        c = _Clock()
        g = _g(c, warn_mpm=15.0, hysteresis=0.9)
        g.update(20.0)
        assert g.should_drop("camera1") is False
        c.adv(0.05)
        assert g.should_drop("camera1") is True
        g.update(5.0)                            # 退出钳帧
        assert g.should_drop("camera1") is False   # 立刻恢复，不受旧时间戳影响


# ======================================================================
# §4  中值滤波挡尖峰
# ======================================================================
class TestMedianFilter:

    def test_single_spike_does_not_start_capping(self):
        """正常 10 m/min 里蹦一个 1413 m/min，中值窗口 5 ⇒ 中值仍是 10。"""
        g = SpeedGuard(warn_mpm=15.0, window=5, min_samples=3)
        for v in (10.0, 10.0, 1413.0, 10.0, 10.0):
            g.update(v)
            assert g.should_drop("camera1") is False
        assert g.snapshot()["filtered_mpm"] == 10.0
        assert g.snapshot()["capping"] is False

    def test_sustained_high_speed_does_start_capping(self):
        g = SpeedGuard(warn_mpm=15.0, window=5, min_samples=3)
        for _ in range(5):
            g.update(20.0)
        assert g.snapshot()["capping"] is True

    def test_spike_is_counted_for_diagnosis(self):
        g = SpeedGuard(warn_mpm=15.0, window=5, min_samples=3)
        for v in (10.0, 10.0, 1413.0, 10.0, 10.0):
            g.update(v)
        assert g.snapshot()["stats"]["spikes_filtered"] >= 1

    def test_no_decision_before_min_samples(self):
        g = SpeedGuard(warn_mpm=15.0, window=5, min_samples=3)
        g.update(999.0)
        g.update(999.0)
        assert g.snapshot()["capping"] is False, "样本不足就判定会误报"
        g.update(999.0)
        assert g.snapshot()["capping"] is True


# ======================================================================
# §5  盲区告警
# ======================================================================
class TestBlindSpotWarning:

    def test_no_warning_below_the_critical_speed(self):
        g = _g(warn_mpm=15.0, cap_speed_mpm=10.0, trigger_distance_mm=80.224,
               frame_height_mm=193.05)
        g.update(20.0)
        assert g.snapshot()["stats"]["blind_warned"] == 0

    def test_warning_above_the_critical_speed(self):
        g = _g(warn_mpm=15.0, cap_speed_mpm=10.0, trigger_distance_mm=80.224,
               frame_height_mm=193.05)
        g.update(42.0)
        assert g.snapshot()["stats"]["blind_warned"] == 1
        assert g.snapshot()["accept_gap_mm"] > 193.05

    def test_warning_is_not_repeated_every_sample(self):
        g = _g(warn_mpm=15.0, cap_speed_mpm=10.0, trigger_distance_mm=80.224,
               frame_height_mm=193.05)
        for _ in range(20):
            g.update(42.0)
        assert g.snapshot()["stats"]["blind_warned"] == 1

    def test_warning_rearms_after_speed_falls(self):
        g = _g(warn_mpm=15.0, cap_speed_mpm=10.0, trigger_distance_mm=80.224,
               frame_height_mm=193.05)
        g.update(42.0)
        g.update(16.0)                          # 仍在钳帧但没盲区了
        assert g.snapshot()["stats"]["blind_warned"] == 1
        g.update(42.0)
        assert g.snapshot()["stats"]["blind_warned"] == 2


# ======================================================================
# §6  失败开放
# ======================================================================
class TestFailOpen:

    @pytest.mark.parametrize("bad", [None, float("nan"), float("inf"),
                                     float("-inf"), -5.0])
    def test_bad_samples_are_ignored(self, bad):
        g = _g(warn_mpm=15.0)
        g.update(bad)
        assert g.snapshot()["capping"] is False
        assert g.should_drop("camera1") is False

    def test_no_sample_means_no_cap(self):
        g = _g(warn_mpm=15.0)
        assert g.should_drop("camera1") is False

    def test_empty_camera_name_still_works(self):
        c = _Clock()
        g = _g(c, warn_mpm=15.0)
        g.update(20.0)
        assert g.should_drop("") is False
        c.adv(0.01)
        assert g.should_drop("") is True

    def test_broken_clock_does_not_raise(self):
        def boom():
            raise RuntimeError("时钟炸了")
        g = SpeedGuard(warn_mpm=15.0, window=1, min_samples=1, clock=boom)
        g.update(99.0)                          # 不该抛
        assert g.should_drop("camera1") is False, "出错必须收图"

    def test_clear_resets(self):
        c = _Clock()
        g = _g(c, warn_mpm=15.0)
        g.update(99.0)
        assert g.snapshot()["capping"] is True
        g.clear()
        assert g.snapshot()["capping"] is False
        assert g.should_drop("camera1") is False

    def test_concurrent_update_and_drop_is_safe(self):
        """update 在 Modbus 线程、should_drop 在 8 个 SDK 回调线程。"""
        g = SpeedGuard(warn_mpm=15.0, window=5, min_samples=3)
        g.update(42.0)
        stop = threading.Event()
        errs = []

        def feeder():
            try:
                while not stop.is_set():
                    g.update(42.0)
            except Exception as e:              # pragma: no cover
                errs.append(e)

        def reader(i):
            try:
                while not stop.is_set():
                    g.should_drop(f"camera{i}")
            except Exception as e:              # pragma: no cover
                errs.append(e)

        ths = [threading.Thread(target=feeder)] + \
              [threading.Thread(target=reader, args=(i,)) for i in range(1, 9)]
        [t.start() for t in ths]
        threading.Event().wait(0.3)
        stop.set()
        [t.join() for t in ths]
        assert errs == []


# ======================================================================
# §7  配置与接线
# ======================================================================
class TestConfigAndWiring:

    @pytest.fixture(scope="class")
    def cfg(self):
        import yaml
        with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
            return yaml.safe_load(f)

    def test_config_values(self, cfg):
        sg = cfg["speed_guard"]
        assert sg["enabled"] is True
        assert float(sg["warn_mpm"]) == 15.0
        assert float(sg["cap_speed_mpm"]) == 10.0
        assert int(sg["window"]) >= 3

    def test_config_trigger_distance_matches_plc(self, cfg):
        """两处触发距离必须一致，否则钳出来的帧率是错的。"""
        sg = cfg["speed_guard"]
        plc = cfg["plc"]
        expect = float(plc["trigger_distance_mm"]) * float(plc["scaling_factor"])
        assert float(sg["trigger_distance_mm"]) == pytest.approx(expect, abs=0.01)

    def test_config_frame_height_matches_pixel_math(self, cfg):
        sz = cfg.get("size", {})
        real_cm = float(sz.get("real_width_cm", 23))
        width_px = float(sz.get("image_width_px", 2440))
        expect = 2048 * (real_cm / width_px) * 10
        assert float(cfg["speed_guard"]["frame_height_mm"]) == \
            pytest.approx(expect, abs=0.05)

    def test_settings_exports_the_section(self):
        from AnomalyDetection_Tool.config.settings import SPEED_GUARD
        assert SPEED_GUARD.get("warn_mpm") == 15.0
        assert SPEED_GUARD.get("cap_speed_mpm") == 10.0

    @pytest.fixture(scope="class")
    def cam_src(self):
        with open(os.path.join(ROOT, "Camera_Tool", "CameraOperation.py"),
                  encoding="utf-8") as f:
            return f.read()

    def test_gate_is_before_to_bgr(self, cam_src):
        """核心不变量：闸门必须在 _to_bgr 之前，否则省不下那 15 MB。"""
        body = cam_src[cam_src.index("def _on_capture"):]
        assert body.index("_sg.should_drop(") < \
            body.index("image = self._to_bgr(raw_image)")

    def test_gate_passes_the_camera_name(self, cam_src):
        """限流按相机独立，必须把 camera_name 传进去。"""
        assert "_sg.should_drop(self.camera_name)" in cam_src

    def test_gate_is_wrapped_in_try(self, cam_src):
        body = cam_src[cam_src.index("def _on_capture"):]
        seg = body[body.index("_sg.should_drop(") - 250:
                   body.index("_sg.should_drop(") + 250]
        assert "try:" in seg and "except Exception" in seg

    def test_drop_counter_is_initialised(self, cam_src):
        """回归：这个计数器曾经只被 += 和读取、从未初始化 ⇒ AttributeError。"""
        assert "self._diag_speed_dropped = 0" in cam_src

    def test_modbus_feeds_the_guard(self):
        with open(os.path.join(ROOT, "Communication_Tool", "Modbus.py"),
                  encoding="utf-8") as f:
            src = f.read()
        assert "_sg.update(self.current_speed)" in src
        assert "from General_Tool.SpeedGuard import get_speed_guard" in src

    def test_main_yolo_initialises_it(self):
        with open(os.path.join(ROOT, "main_yolo.py"), encoding="utf-8") as f:
            src = f.read()
        assert "init_speed_guard(" in src
        assert "cap_speed_mpm=" in src
        assert "drop_enabled" not in src, "旧的丢帧开关应该已经移除"

    def test_defaults(self):
        assert DEFAULT_WARN_MPM == 15.0
        assert DEFAULT_CAP_SPEED_MPM == 10.0

    def test_singleton_roundtrip(self):
        g1 = init_speed_guard(warn_mpm=12.0)
        assert get_speed_guard() is g1
        assert g1.warn_mpm == 12.0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
