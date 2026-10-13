#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【三】停机 · 重复触发（同一台相机上同类疵点在时间窗口内反复出现）。

需求原话：「在一台相机上，连续几秒检测到相同类型的疵点，就停机」，
例如 camera3 上 3 秒内出现 3 次「包边不良」就触发停机。

本文件重点不是"正常路径能跑"，而是这几条**配错就会误停或漏停**的边界：

1. **按帧计数**：同一帧同类出现 5 个框只算 1 次（§2）。
   按框算的话单帧误检 3 个就立刻误停 —— 这是最容易被写错的地方。
2. **滑动窗口**：过期要真的滑掉（§2），窗口用单调时钟（§5）。
3. **触发后清空**：否则之后每一帧都会重复触发，停机信号刷爆（§2）。
4. **计数门槛独立于单次停机门槛**：复用 0.9 的话本功能对「包边不良」
   永远不生效（§3）。
5. **没配的相机行为完全不变**（§3）。
6. **坏配置安全回落**，不能静默改变停机（§4）。
7. **并发安全**：post-worker 是多线程的（§6）。

跑法::

    python -m pytest Test_Tool/test_repeat_stop.py -v
"""

import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402

import AnomalyDetection_Tool.config.settings as st  # noqa: E402
from AnomalyDetection_Tool.services.repeat_stop_tracker import (  # noqa: E402
    RepeatStopTracker,
)


# ------------------------------------------------------------------
# 公共夹具
# ------------------------------------------------------------------

# 现场那条规则：camera3 上 3 秒内 3 次「包边不良」
CAM3_RULE = {"enabled": True, "count": 3, "window_sec": 3.0,
             "min_confidence": 0.5}

REPEAT_RULES = {
    "enabled": True,
    "rules": {},                      # 全局故意留空：只按相机配
}


class FakeClock:
    """可控时钟。时间窗口逻辑必须能在测试里精确推进。"""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@pytest.fixture
def overrides(monkeypatch):
    """把按相机覆盖注入 settings，测完自动还原。"""
    monkeypatch.setattr(st, "CAMERA_REPEAT_STOP_OVERRIDES",
                        {"camera3": {"rules": {"包边不良": dict(CAM3_RULE)}}})
    return st.CAMERA_REPEAT_STOP_OVERRIDES


def make_tracker(rules=None, clock=None):
    """rules: {(class, camera): rule|None}，或用 effective_repeat_rule。"""
    clock = clock or FakeClock()
    if rules is None:
        provider = lambda c, cam: st.effective_repeat_rule(c, cam, REPEAT_RULES)
    else:
        provider = lambda c, cam: rules.get((c, cam))
    return RepeatStopTracker(provider, clock=clock), clock


# ==================================================================
# §1 规则合并：全局 → 按相机
# ==================================================================
class TestEffectiveRepeatRule:
    def test_camera_override_is_used(self, overrides):
        r = st.effective_repeat_rule("包边不良", "camera3", REPEAT_RULES)
        assert r == CAM3_RULE

    def test_unconfigured_camera_gets_none(self, overrides):
        """全局 rules 是空的，所以除 camera3 外都不做重复判定。"""
        for cam in ("camera1", "camera2", "camera4", "camera8", None):
            assert st.effective_repeat_rule("包边不良", cam, REPEAT_RULES) is None

    def test_unconfigured_class_gets_none(self, overrides):
        assert st.effective_repeat_rule("棉团", "camera3", REPEAT_RULES) is None

    def test_master_switch_beats_everything(self, overrides):
        off = {"enabled": False, "rules": {"包边不良": dict(CAM3_RULE)}}
        assert st.effective_repeat_rule("包边不良", "camera3", off) is None

    def test_per_class_disabled(self):
        rules = {"enabled": True,
                 "rules": {"包边不良": {"enabled": False, "count": 3,
                                        "window_sec": 3.0}}}
        assert st.effective_repeat_rule("包边不良", None, rules) is None

    def test_count_below_two_is_rejected(self):
        """count<2 就是"出现即停"，那是单次停机规则的职责。

        在这里放行会导致同一个疵点被两条路径各停一次。
        """
        for bad in ({"count": 1, "window_sec": 3.0},
                    {"count": 0, "window_sec": 3.0},
                    {"count": 3, "window_sec": 0},
                    {"count": 3, "window_sec": -1}):
            rules = {"enabled": True, "rules": {"包边不良": dict(bad)}}
            assert st.effective_repeat_rule("包边不良", None, rules) is None, bad

    def test_returns_a_copy(self, overrides):
        r = st.effective_repeat_rule("包边不良", "camera3", REPEAT_RULES)
        r["count"] = 999
        again = st.effective_repeat_rule("包边不良", "camera3", REPEAT_RULES)
        assert again["count"] == 3, "返回的必须是副本，改它不能污染配置本体"

    def test_garbage_override_falls_back(self, overrides):
        """YAML 少写冒号会让整块变成字符串（T16 的坑），必须当没有覆盖。"""
        overrides["camera3"] = "rules: 包边不良: 3"          # 字符串，不是 dict
        assert st.effective_repeat_rule("包边不良", "camera3", REPEAT_RULES) is None

    def test_bad_key_in_override_is_dropped_not_fatal(self, overrides):
        overrides["camera3"]["rules"]["包边不良"] = {
            "count": 5, "window_sec": 2.0, "min_confidence": "不是数字",
            "写错的键": 1}
        r = st.effective_repeat_rule("包边不良", "camera3", REPEAT_RULES)
        assert r["count"] == 5 and r["window_sec"] == 2.0
        assert "min_confidence" not in r, "非法值应被丢弃，回落全局"
        assert "写错的键" not in r


# ==================================================================
# §2 滑动窗口语义（核心）
# ==================================================================
class TestSlidingWindow:
    def _t(self):
        return make_tracker({("包边不良", "camera3"): dict(CAM3_RULE)})

    def test_three_within_window_triggers(self):
        tr, clk = self._t()
        assert tr.record_frame("camera3", ["包边不良"], {"包边不良": 0.6}) == []
        clk.advance(1.0)
        assert tr.record_frame("camera3", ["包边不良"], {"包边不良": 0.6}) == []
        clk.advance(1.0)
        hits = tr.record_frame("camera3", ["包边不良"], {"包边不良": 0.6})
        assert len(hits) == 1
        assert hits[0]["count"] == 3 and hits[0]["class_name"] == "包边不良"
        assert hits[0]["span_sec"] == pytest.approx(2.0)

    def test_two_within_window_does_not_trigger(self):
        tr, clk = self._t()
        for _ in range(2):
            assert tr.record_frame("camera3", ["包边不良"], {"包边不良": 0.6}) == []
            clk.advance(1.0)
        assert tr.get_stats()["triggered"] == 0

    def test_expired_occurrences_slide_out(self):
        """第 1 次过期后再来 2 次，只算 2 次，不该触发。"""
        tr, clk = self._t()
        tr.record_frame("camera3", ["包边不良"], {"包边不良": 0.6})
        clk.advance(3.1)                       # 第 1 次已滑出窗口
        tr.record_frame("camera3", ["包边不良"], {"包边不良": 0.6})
        clk.advance(1.0)
        assert tr.record_frame("camera3", ["包边不良"], {"包边不良": 0.6}) == []
        assert tr.get_stats()["triggered"] == 0

    def test_window_is_sliding_not_tumbling(self):
        """滑动窗口：0.0/2.0/2.5 这三次里，后两次 + 新一次仍应能凑满。"""
        tr, clk = self._t()
        for dt in (0.0, 2.0, 0.5, 0.5):        # t = 0, 2.0, 2.5, 3.0
            tr.record_frame("camera3", ["包边不良"], {"包边不良": 0.6})
            clk.advance(dt)
        # t=2.5 时窗口 [0,2.5] 内有 3 次 -> 应已触发
        assert tr.get_stats()["triggered"] == 1

    def test_trigger_clears_the_window(self):
        """⚠️ 关键：触发后必须清空，否则之后**每一帧**都会重复触发。"""
        tr, clk = self._t()
        for _ in range(3):
            tr.record_frame("camera3", ["包边不良"], {"包边不良": 0.6})
            clk.advance(0.5)
        assert tr.get_stats()["triggered"] == 1
        # 再来 1 帧：窗口已清空，只有 1 次，不该再触发
        clk.advance(0.5)
        assert tr.record_frame("camera3", ["包边不良"], {"包边不良": 0.6}) == []
        assert tr.get_stats()["triggered"] == 1

    def test_rearm_needs_full_count_again(self):
        tr, clk = self._t()
        for _ in range(3):
            tr.record_frame("camera3", ["包边不良"], {"包边不良": 0.6})
            clk.advance(0.5)
        for _ in range(2):                      # 重新攒，只攒 2 次
            clk.advance(0.5)
            assert tr.record_frame("camera3", ["包边不良"], {"包边不良": 0.6}) == []
        clk.advance(0.5)
        assert len(tr.record_frame("camera3", ["包边不良"], {"包边不良": 0.6})) == 1
        assert tr.get_stats()["triggered"] == 2

    def test_per_frame_dedup_same_class_many_boxes(self):
        """⚠️ 按帧计数：同一帧同类出现 5 个框也只算 1 次。

        按框算的话，单帧误检 3 个就会立刻误停 —— 这是本功能最容易写错的地方。
        """
        tr, clk = self._t()
        # 同一帧传 5 个「包边不良」
        assert tr.record_frame("camera3", ["包边不良"] * 5,
                               {"包边不良": 0.6}) == []
        assert tr.get_stats()["occurrences"] == 1, "同一帧同类只应记 1 次"

    def test_different_classes_do_not_share_a_window(self):
        rules = {("包边不良", "camera3"): dict(CAM3_RULE),
                 ("棉团", "camera3"): dict(CAM3_RULE)}
        tr, clk = make_tracker(rules)
        fired = []
        for cls in ("包边不良", "棉团", "包边不良", "棉团", "包边不良"):
            fired += tr.record_frame("camera3", [cls], {cls: 0.6})
            clk.advance(0.5)
        # 包边不良 3 次、棉团 2 次 —— 棉团不够，包边不良够
        assert [h["class_name"] for h in fired] == ["包边不良"], fired
        assert tr.get_stats()["triggered"] == 1

    def test_cameras_do_not_share_a_window(self):
        rules = {("包边不良", "camera3"): dict(CAM3_RULE),
                 ("包边不良", "camera5"): dict(CAM3_RULE)}
        tr, clk = make_tracker(rules)
        fired = []
        for cam in ("camera3", "camera5", "camera3", "camera5", "camera3"):
            for h in tr.record_frame(cam, ["包边不良"], {"包边不良": 0.6}):
                fired.append((cam, h["class_name"]))
            clk.advance(0.5)
        assert fired == [("camera3", "包边不良")], fired   # 只有 camera3 攒够
        assert tr.get_stats()["triggered"] == 1

    def test_empty_and_blank_input_is_ignored(self):
        tr, _ = self._t()
        assert tr.record_frame("camera3", [], {}) == []
        assert tr.record_frame("camera3", ["", None], {}) == []
        assert tr.record_frame("", ["包边不良"], {}) == []
        assert tr.get_stats()["occurrences"] == 0


# ==================================================================
# §3 计数门槛：必须独立于单次停机门槛
# ==================================================================
class TestCountingGate:
    def test_below_min_confidence_is_not_counted(self):
        tr, clk = make_tracker({("包边不良", "camera3"): dict(CAM3_RULE)})
        for _ in range(5):
            assert tr.record_frame("camera3", ["包边不良"],
                                   {"包边不良": 0.4}) == []
            clk.advance(0.5)
        assert tr.get_stats()["triggered"] == 0
        assert tr.get_stats()["occurrences"] == 0, "不够格的一次都不该计入"

    def test_mixed_confidence_only_counts_qualified(self):
        tr, clk = make_tracker({("包边不良", "camera3"): dict(CAM3_RULE)})
        for conf in (0.4, 0.6, 0.4, 0.7, 0.4, 0.8):
            tr.record_frame("camera3", ["包边不良"], {"包边不良": conf})
            clk.advance(0.4)
        assert tr.get_stats()["occurrences"] == 3
        assert tr.get_stats()["triggered"] == 1

    def test_gate_uses_the_best_box_of_the_frame(self, overrides):
        """代表框取本帧置信度最高的那个 —— 这是 StopMachineService 的约定。"""
        r = st.effective_repeat_rule("包边不良", "camera3", REPEAT_RULES)
        tr, clk = make_tracker({("包边不良", "camera3"): r})
        # 本帧有 0.4 和 0.7 两个框，代表框是 0.7，应该计入
        tr.record_frame("camera3", ["包边不良"], {"包边不良": 0.7})
        assert tr.get_stats()["occurrences"] == 1


# ==================================================================
# §4 坏配置安全回落
# ==================================================================
class TestBadConfig:
    def test_provider_raising_does_not_break_the_frame(self):
        def boom(cls, cam):
            raise RuntimeError("配置读坏了")
        tr = RepeatStopTracker(boom, clock=FakeClock())
        assert tr.record_frame("camera3", ["包边不良"], {"包边不良": 0.9}) == []

    def test_non_numeric_count_or_window_is_ignored(self):
        tr, _ = make_tracker({("包边不良", "camera3"):
                              {"count": "三", "window_sec": 3.0}})
        assert tr.record_frame("camera3", ["包边不良"], {"包边不良": 0.9}) == []

    def test_count_one_is_ignored_by_the_tracker_too(self):
        """双重保险：settings 已经拦了，tracker 自己也要拦。"""
        tr, _ = make_tracker({("包边不良", "camera3"):
                              {"count": 1, "window_sec": 3.0}})
        for _ in range(3):
            assert tr.record_frame("camera3", ["包边不良"],
                                   {"包边不良": 0.9}) == []
        assert tr.get_stats()["triggered"] == 0


# ==================================================================
# §5 时钟
# ==================================================================
class TestClock:
    def test_default_clock_is_monotonic(self):
        """必须是 time.monotonic 而不是 time.time —— 系统对时/NTP 校时
        会让时间窗口突然清空或突然塞满。"""
        import inspect
        import time as _time
        sig = inspect.signature(RepeatStopTracker.__init__)
        assert sig.parameters["clock"].default is _time.monotonic

    def test_wall_clock_jump_would_not_matter(self):
        """用注入的单调时钟验证：时间只往前走，窗口按真实间隔滑动。"""
        tr, clk = make_tracker({("包边不良", "camera3"): dict(CAM3_RULE)})
        tr.record_frame("camera3", ["包边不良"], {"包边不良": 0.6})
        clk.advance(1.0)
        tr.record_frame("camera3", ["包边不良"], {"包边不良": 0.6})
        clk.advance(1.0)
        assert len(tr.record_frame("camera3", ["包边不良"],
                                   {"包边不良": 0.6})) == 1


# ==================================================================
# §6 并发（post-worker 是多线程的）
# ==================================================================
class TestConcurrency:
    def test_concurrent_recording_does_not_lose_or_double_count(self):
        rule = {"enabled": True, "count": 100, "window_sec": 60.0}
        tr = RepeatStopTracker(lambda c, cam: rule, clock=FakeClock())
        n_threads, per = 8, 50
        barrier = threading.Barrier(n_threads)

        def work(i):
            barrier.wait()
            for _ in range(per):
                tr.record_frame(f"camera{i % 8 + 1}", ["包边不良"],
                                {"包边不良": 0.9})

        ts = [threading.Thread(target=work, args=(i,)) for i in range(n_threads)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert tr.get_stats()["occurrences"] == n_threads * per

    def test_snapshot_is_consistent_under_lock(self):
        tr, clk = make_tracker({("包边不良", "camera3"): dict(CAM3_RULE)})
        tr.record_frame("camera3", ["包边不良"], {"包边不良": 0.6})
        snap = tr.snapshot("camera3", "包边不良")
        assert len(snap) == 1 and snap[0]["pending"] == 1


# ==================================================================
# §7 现场那条规则的端到端语义
# ==================================================================
class TestSiteScenario:
    """camera3 上 3 秒内 3 次「包边不良」→ 停机。用真实配置结构走一遍。"""

    def test_shipped_config_matches_the_request(self):
        import yaml
        with open(os.path.join(_ROOT, "config.yaml"), encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        rr = cfg["repeat_stop_rules"]
        assert rr["enabled"] is True
        # 规则配在全局 -> 8 台相机都按它计数
        r = rr["rules"]["包边不良"]
        assert r["count"] == 3
        assert r["window_sec"] == pytest.approx(3.0)
        assert r["enabled"] is True
        # 但只有这两台有停机权
        assert rr["stop_allowed_cameras"] == ["camera1", "camera8"]

    def test_rule_resolves_for_all_but_only_two_participate(self):
        """规则写在 globals，所以 8 台都**能解析出**规则；
        但只有 stop_allowed_cameras 里的相机**真的参与**。

        这两件事必须分清：解析出规则 ≠ 参与。参与与否由白名单决定，
        判定在计数之前就短路（见 §10）。
        """
        import yaml
        with open(os.path.join(_ROOT, "config.yaml"), encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        assert len(cfg["cameras"]["sn_map"]) == 8
        rr = cfg["repeat_stop_rules"]
        for i in range(1, 9):
            cam = f"camera{i}"
            r = st.effective_repeat_rule("包边不良", cam, rr)
            assert r is not None, f"{cam} 解析不出规则"
            assert r["count"] == 3
            participates = st.repeat_stop_allowed(cam, rr)
            assert participates is (i in (1, 8)), \
                f"{cam} 参与状态应为 {i in (1, 8)}，实得 {participates}"

    def test_counting_gate_is_looser_than_the_single_shot_gate(self):
        """⚠️ 这条是本功能有没有用的关键。

        「包边不良」的单次停机门槛是 min_confidence=0.9。如果计数门槛也取 0.9，
        第 1 次就单次停机了，永远攒不到第 3 次 —— 本功能对这个类等于没开。
        """
        import yaml
        with open(os.path.join(_ROOT, "config.yaml"), encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        single = cfg["machine_stop_rules"]["rules"]["包边不良"]["min_confidence"]
        repeat = cfg["repeat_stop_rules"]["rules"]["包边不良"]["min_confidence"]
        assert repeat < single, (
            f"计数门槛 {repeat} 必须 < 单次停机门槛 {single}，"
            f"否则重复规则永远不会先于单次规则触发")

    def test_realistic_frame_cadence(self):
        """现场节拍：10 m/min 下每台约 2.08 帧/秒 → 每帧间隔约 481 ms。
        连续 6 帧都检出，应该在第 3 帧就触发（约 0.96 s）。"""
        tr, clk = make_tracker({("包边不良", "camera3"): dict(CAM3_RULE)})
        fired_at = None
        for i in range(6):
            hits = tr.record_frame("camera3", ["包边不良"], {"包边不良": 0.6})
            if hits and fired_at is None:
                fired_at = i
            clk.advance(0.481)
        assert fired_at == 2, f"应在第 3 帧触发，实际第 {fired_at + 1} 帧"


# ==================================================================
# §8 集成：StopMachineService.check_repeat_stops → _handle_stop
# ==================================================================
import numpy as np  # noqa: E402

from AnomalyDetection_Tool.models.data_types import DetectionBox  # noqa: E402
from AnomalyDetection_Tool.services.stop_machine_service import (  # noqa: E402
    StopMachineService,
)


def _box(cls="包边不良", conf=0.6):
    return DetectionBox(x1=10, y1=10, x2=60, y2=60,
                        confidence=conf, class_id=1, class_name=cls)


@pytest.fixture
def service(monkeypatch, overrides):
    """一个不碰继电器/磁盘的 StopMachineService，_handle_stop 被记录。"""
    monkeypatch.setattr(st, "REPEAT_STOP_RULES", REPEAT_RULES)
    svc = StopMachineService(machine_stop_path="/tmp/_rs_test",
                             launch_tag="test", analysis_mode="none")
    calls = []
    monkeypatch.setattr(
        svc, "_handle_stop",
        lambda image, box, cam, fn, result, rule, **kw:
        (calls.append({"camera": cam, "class": box.class_name,
                       "conf": box.confidence,
                       "reason": result.debug_info.get("stop_reason"),
                       "count": result.debug_info.get("repeat_count")}),
         "")[1])
    return svc, calls


class TestServiceIntegration:
    IMG = np.zeros((64, 64, 3), dtype=np.uint8)

    def test_third_occurrence_calls_handle_stop(self, service):
        svc, calls = service
        for i in range(2):
            assert svc.check_repeat_stops(
                self.IMG, "camera3", f"f{i}.png", {"包边不良": _box()}) is False
        assert calls == []
        assert svc.check_repeat_stops(
            self.IMG, "camera3", "f2.png", {"包边不良": _box()}) is True
        assert len(calls) == 1
        assert calls[0]["camera"] == "camera3"
        assert calls[0]["class"] == "包边不良"
        assert calls[0]["reason"] == "repeat"
        assert calls[0]["count"] == 3

    def test_other_camera_never_stops(self, service):
        """只有 camera3 配了规则，其余相机行为必须完全不变。"""
        svc, calls = service
        for i in range(6):
            assert svc.check_repeat_stops(
                self.IMG, "camera1", f"f{i}.png", {"包边不良": _box()}) is False
        assert calls == []

    def test_frame_already_stopped_counts_but_does_not_re_stop(self, service):
        """本帧已由单次规则停过：历史照记，但不再发第二次停机动作。

        否则继电器和落盘会被同一帧重复触发。
        """
        svc, calls = service
        svc.check_repeat_stops(self.IMG, "camera3", "f0.png",
                               {"包边不良": _box()}, frame_already_stopped=True)
        svc.check_repeat_stops(self.IMG, "camera3", "f1.png",
                               {"包边不良": _box()}, frame_already_stopped=True)
        assert calls == []
        # 第 3 帧攒够了，但因为 frame_already_stopped 不动作
        assert svc.check_repeat_stops(
            self.IMG, "camera3", "f2.png", {"包边不良": _box()},
            frame_already_stopped=True) is False
        assert calls == [], "本帧已停机时不该再发停机动作"
        # 但计数确实记了：窗口已被消费，下一轮要重新攒
        snap = svc.repeat_tracker.snapshot("camera3", "包边不良")
        assert (snap[0]["pending"] if snap else 0) == 0

    def test_only_one_stop_per_frame_even_if_two_classes_fire(self, monkeypatch,
                                                              overrides):
        rules = {"enabled": True, "rules": {
            "包边不良": {"enabled": True, "count": 2, "window_sec": 3.0},
            "棉团": {"enabled": True, "count": 2, "window_sec": 3.0}}}
        monkeypatch.setattr(st, "REPEAT_STOP_RULES", rules)
        svc = StopMachineService(machine_stop_path="/tmp/_rs_test2",
                                 launch_tag="test", analysis_mode="none")
        calls = []
        monkeypatch.setattr(svc, "_handle_stop",
                            lambda *a, **kw: (calls.append(a[2]), "")[1])
        for i in range(2):
            svc.check_repeat_stops(self.IMG, "camera1", f"f{i}.png",
                                   {"包边不良": _box("包边不良"),
                                    "棉团": _box("棉团", 0.9)})
        assert len(calls) == 1, f"同一帧两个类都攒够，也只该停一次: {calls}"

    def test_low_confidence_box_never_stops(self, service):
        svc, calls = service
        for i in range(6):
            svc.check_repeat_stops(self.IMG, "camera3", f"f{i}.png",
                                   {"包边不良": _box(conf=0.4)})
        assert calls == [], "低于计数门槛 0.5 的框不该被计入"

    def test_empty_frame_is_a_noop(self, service):
        svc, calls = service
        assert svc.check_repeat_stops(self.IMG, "camera3", "f.png", {}) is False
        assert calls == []


# ==================================================================
# §9 真·端到端：走 YOLODetectionManager._process_result
# ==================================================================
# 前面几节都是打桩验证。这一节跑**真实的** _process_result，
# 证明接线确实在主流程里执行 —— 打桩测不到"忘了接"这种错。
from AnomalyDetection_Tool.core.detection_manager import (  # noqa: E402
    YOLODetectionManager,
)


class _FakeResult:
    def __init__(self, boxes):
        self.boxes = boxes
        self.total_boxes = len(boxes)
        self.has_detection = len(boxes) > 0


class _NoopPerf:
    def record(self, *a, **kw):
        pass


@pytest.fixture
def manager(monkeypatch, overrides):
    monkeypatch.setattr(st, "REPEAT_STOP_RULES", REPEAT_RULES)
    dm = YOLODetectionManager({"yolo": {"queue_size": 4,
                                        "max_inflight_frames": 4,
                                        "num_workers": 1,
                                        "post_workers": 1,
                                        "image_size": 640}},
                              "/tmp/_rs_mgr", None,
                              machine_stop_path="/tmp/_rs_mgr_stop")
    dm._stop_service = StopMachineService(machine_stop_path="/tmp/_rs_mgr_stop",
                                          launch_tag="t", analysis_mode="none")
    calls = []
    monkeypatch.setattr(
        dm._stop_service, "_handle_stop",
        lambda image, box, cam, fn, result, rule, **kw:
        (calls.append((cam, box.class_name,
                       result.debug_info.get("stop_reason"))), "")[1])
    monkeypatch.setattr(dm, "_submit_async", lambda fn, *a: None)
    monkeypatch.setattr(dm, "_perf_tracker", _NoopPerf())
    return dm, calls


class TestRealPipeline:
    IMG = np.zeros((64, 64, 3), dtype=np.uint8)

    def _frame(self, dm, i, boxes, camera="camera3"):
        dm._process_result(_FakeResult(boxes), self.IMG, camera, "SN",
                           f"f{i}.png", f"2026-01-01 00:00:0{i}.000",
                           {}, time.time())

    def test_five_boxes_in_one_frame_count_once(self, manager):
        """⚠️ 主流程里的按帧去重：单帧 5 个同类框只算 1 次。"""
        dm, calls = manager
        self._frame(dm, 0, [_box()] * 5)
        stats = dm._stop_service.repeat_tracker.get_stats()
        assert stats["occurrences"] == 1, stats
        assert calls == []

    def test_three_frames_trigger_the_real_stop(self, manager):
        dm, calls = manager
        self._frame(dm, 0, [_box()])
        self._frame(dm, 1, [_box()])
        assert calls == []
        self._frame(dm, 2, [_box()])
        assert calls == [("camera3", "包边不良", "repeat")], calls

    def test_other_camera_is_untouched(self, manager):
        dm, calls = manager
        for i in range(6):
            self._frame(dm, i, [_box()], camera="camera1")
        assert calls == []
        assert dm._stop_service.repeat_tracker.get_stats()["occurrences"] == 0

    def test_mixed_classes_pick_the_best_box_per_class(self, manager):
        """代表框必须是本帧该类置信度最高的那个（停机落盘/继电器用它）。"""
        dm, calls = manager
        self._frame(dm, 0, [_box(conf=0.55), _box(conf=0.88), _box(conf=0.6)])
        self._frame(dm, 1, [_box(conf=0.9)])
        self._frame(dm, 2, [_box(conf=0.7)])
        assert len(calls) == 1

    def test_broken_rule_provider_does_not_break_the_frame(self, manager,
                                                           monkeypatch):
        """重复停机这条路径出任何问题，都不能影响检测主流程。"""
        dm, calls = manager

        def boom(cls, cam):
            raise RuntimeError("配置读坏了")
        monkeypatch.setattr(dm._stop_service._repeat_tracker,
                            "_rule_provider", boom)
        self._frame(dm, 0, [_box()])            # 不该抛
        assert calls == []


# ==================================================================
# §10 停机授权白名单：8 台都计数，只有 camera1 / camera8 能停机
# ==================================================================
class TestStopAuthority:
    def test_key_absent_means_all_allowed(self):
        """向后兼容：没配这个键 -> 不限制。"""
        assert st.repeat_stop_allowed("camera5", {"enabled": True}) is True

    def test_empty_list_means_none_allowed(self):
        """⚠️ [] 必须是"谁都不许停"，不能被当成"没配"。

        用 rules.get(k) or 默认 会把 [] 当成没配，于是"谁都不许停"
        被静默变成"全都许停"，方向正好反了。
        """
        rules = {"enabled": True, "stop_allowed_cameras": []}
        for i in range(1, 9):
            assert st.repeat_stop_allowed(f"camera{i}", rules) is False

    def test_only_listed_cameras_allowed(self):
        rules = {"enabled": True,
                 "stop_allowed_cameras": ["camera1", "camera8"]}
        expect = {1: True, 2: False, 3: False, 4: False,
                  5: False, 6: False, 7: False, 8: True}
        for i, want in expect.items():
            got = st.repeat_stop_allowed(f"camera{i}", rules)
            assert got is want, f"camera{i}: 期望 {want} 实得 {got}"

    def test_unknown_camera_is_not_allowed(self):
        rules = {"enabled": True,
                 "stop_allowed_cameras": ["camera1", "camera8"]}
        assert st.repeat_stop_allowed("camera99", rules) is False
        assert st.repeat_stop_allowed("", rules) is False
        assert st.repeat_stop_allowed(None, rules) is False

    def test_serial_number_is_not_mistaken_for_a_name(self):
        """相机名退化成了序列号时，不该碰巧匹配上白名单。"""
        rules = {"enabled": True,
                 "stop_allowed_cameras": ["camera1", "camera8"]}
        assert st.repeat_stop_allowed("KBA26070032", rules) is False

    def test_none_value_means_all_allowed(self):
        """写了键但没给值（YAML 里 `stop_allowed_cameras:` 空着）。"""
        assert st.repeat_stop_allowed(
            "camera5", {"enabled": True, "stop_allowed_cameras": None}) is True

    def test_garbage_value_fails_closed(self):
        """不是列表就**不许停** —— 宁可漏停，不可误停。"""
        for bad in ("camera1", 3, {"camera1": True}):
            assert st.repeat_stop_allowed(
                "camera1",
                {"enabled": True, "stop_allowed_cameras": bad}) is False, bad

    def test_shipped_config_authorises_exactly_camera1_and_8(self):
        import yaml
        with open(os.path.join(_ROOT, "config.yaml"), encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        rr = cfg["repeat_stop_rules"]
        got = {i: st.repeat_stop_allowed(f"camera{i}", rr) for i in range(1, 9)}
        assert got == {1: True, 2: False, 3: False, 4: False,
                       5: False, 6: False, 7: False, 8: True}, got


@pytest.fixture
def auth_service(monkeypatch):
    """8 台都配规则，但只有 camera1/camera8 有停机权。"""
    rules = {"enabled": True,
             "stop_allowed_cameras": ["camera1", "camera8"],
             "rules": {"包边不良": {"enabled": True, "count": 3,
                                    "window_sec": 3.0,
                                    "min_confidence": 0.5}}}
    monkeypatch.setattr(st, "REPEAT_STOP_RULES", rules)
    monkeypatch.setattr(st, "CAMERA_REPEAT_STOP_OVERRIDES", {})
    svc = StopMachineService(machine_stop_path="/tmp/_rs_auth",
                             launch_tag="t", analysis_mode="none")
    calls = []
    monkeypatch.setattr(
        svc, "_handle_stop",
        lambda image, box, cam, fn, result, rule, **kw:
        (calls.append(cam), "")[1])
    return svc, calls


class TestStopAuthorityIntegration:
    IMG = np.zeros((64, 64, 3), dtype=np.uint8)

    def _hit(self, svc, cam, n=3):
        out = None
        for i in range(n):
            out = svc.check_repeat_stops(self.IMG, cam, f"f{i}.png",
                                         {"包边不良": _box()})
        return out

    def test_camera1_can_stop(self, auth_service):
        svc, calls = auth_service
        assert self._hit(svc, "camera1") is True
        assert calls == ["camera1"]

    def test_camera8_can_stop(self, auth_service):
        svc, calls = auth_service
        assert self._hit(svc, "camera8") is True
        assert calls == ["camera8"]

    def test_middle_cameras_do_not_count_at_all(self, auth_service):
        """⚠️ 核心：没停机权的相机**连计数都不做**。

        攒够了也不能停，计数本身没有意义 —— 白占一份滑动窗口、
        白刷日志。所以判定在计数之前就短路了。
        """
        svc, calls = auth_service
        for i in (2, 3, 4, 5, 6, 7):
            cam = f"camera{i}"
            assert self._hit(svc, cam) is False, f"{cam} 不该返回已停机"
        assert calls == [], f"中间相机不该停机: {calls}"
        stats = svc.get_stats()
        assert stats["repeat_stop_triggered"] == 0
        # 关键：一次都没计过
        tr_stats = svc.repeat_tracker.get_stats()
        assert tr_stats["occurrences"] == 0, tr_stats
        assert tr_stats["frames_seen"] == 0, tr_stats

    def test_unauthorized_camera_creates_no_window(self, auth_service):
        """不给没权限的相机建滑动窗口 —— 否则 6 台 × N 类的 deque 白白常驻。"""
        svc, _ = auth_service
        self._hit(svc, "camera5")
        assert svc.repeat_tracker.snapshot("camera5") == []
        assert svc.repeat_tracker.snapshot() == [], "整个 tracker 应该是空的"

    def test_frames_skipped_is_counted_for_diagnosis(self, auth_service):
        """跳过的帧数还是要能看见，否则"为什么 camera5 不停机"无从查起。
        这是个纯计数器，不建窗口、不判定。"""
        svc, _ = auth_service
        self._hit(svc, "camera5", n=3)
        assert svc.get_stats()["repeat_stop_frames_skipped"] == 3

    def test_authorized_cameras_still_count_and_stop(self, auth_service):
        svc, calls = auth_service
        assert self._hit(svc, "camera1") is True
        assert calls == ["camera1"]
        assert svc.repeat_tracker.get_stats()["occurrences"] == 3

    def test_whitelist_does_not_touch_single_shot_stops(self, monkeypatch):
        """⚠️ 白名单**只管【三】**。【二】单次停机在 8 台上的行为必须没变。

        这条是防止有人把授权判断误加到 analyze_defect_area /
        analyze_defect_dimension 里 —— 那会让中间 6 台突然失去单次停机能力。
        """
        import inspect
        from AnomalyDetection_Tool.services import stop_machine_service as sms
        for fn in (sms.StopMachineService.analyze_defect_area,
                   sms.StopMachineService.analyze_defect_dimension):
            src = inspect.getsource(fn)
            assert "repeat_stop_allowed" not in src, \
                f"{fn.__name__} 不该受重复停机白名单影响"

    def test_real_pipeline_camera3_does_not_stop(self, monkeypatch):
        """走真实 _process_result：camera3 攒够 3 次也不停机。"""
        rules = {"enabled": True,
                 "stop_allowed_cameras": ["camera1", "camera8"],
                 "rules": {"包边不良": {"enabled": True, "count": 3,
                                        "window_sec": 3.0,
                                        "min_confidence": 0.5}}}
        monkeypatch.setattr(st, "REPEAT_STOP_RULES", rules)
        monkeypatch.setattr(st, "CAMERA_REPEAT_STOP_OVERRIDES", {})
        dm = YOLODetectionManager({"yolo": {"queue_size": 4,
                                            "max_inflight_frames": 4,
                                            "num_workers": 1,
                                            "post_workers": 1,
                                            "image_size": 640}},
                                  "/tmp/_rs_auth2", None,
                                  machine_stop_path="/tmp/_rs_auth2_stop")
        dm._stop_service = StopMachineService(
            machine_stop_path="/tmp/_rs_auth2_stop", launch_tag="t",
            analysis_mode="none")
        calls = []
        monkeypatch.setattr(dm._stop_service, "_handle_stop",
                            lambda *a, **kw: (calls.append(a[2]), "")[1])
        monkeypatch.setattr(dm, "_submit_async", lambda fn, *a: None)
        monkeypatch.setattr(dm, "_perf_tracker", _NoopPerf())

        for cam, want in (("camera3", 0), ("camera5", 0), ("camera1", 1)):
            calls.clear()
            for i in range(3):
                dm._process_result(
                    _FakeResult([_box()]), self.IMG, cam, "SN",
                    f"{cam}_{i}.png", f"2026-01-01 00:00:0{i}.000",
                    {}, time.time())
            assert len(calls) == want, f"{cam}: 期望停机 {want} 次，实得 {calls}"


# ==================================================================
# §11 新增类别：单根包不进
# ==================================================================
class TestSingleYarnClass:
    """单根包不进 的门槛结构和包边不良不同，必须单独验。

    · 检测阈值 = 0.87，单次停机门槛 min_confidence = 0.5
      → 0.5 那条**永远不是瓶颈**（有效门槛 = max = 0.87）
    · 单次规则真正的闸是 min_height_mm = 47（= 498.6 px）
    → 重复规则不看高度，抓的正是"反复出现但每次都不够 47 mm"的那些
    """

    CLS = "单根包不进"

    @staticmethod
    def _cfg():
        import yaml
        with open(os.path.join(_ROOT, "config.yaml"), encoding="utf-8") as f:
            return yaml.safe_load(f)

    def test_shipped_config(self):
        r = self._cfg()["repeat_stop_rules"]["rules"][self.CLS]
        assert r["enabled"] is True
        assert r["count"] == 3
        assert r["window_sec"] == pytest.approx(3.0)

    def test_min_confidence_deliberately_absent(self):
        """不写 min_confidence = 跟随检测阈值。

        写一个 <= 0.87 的数等于没写；写死了反而会在将来调低检测阈值时
        悄悄变成新瓶颈。这条测试是防止有人"顺手补上"一个数。
        """
        r = self._cfg()["repeat_stop_rules"]["rules"][self.CLS]
        assert "min_confidence" not in r, (
            "单根包不进 故意不写计数门槛，见 config.yaml 里的说明")

    def test_single_shot_confidence_gate_is_never_binding(self):
        """证明 0.5 那条确实是死条款：检测阈值 0.87 比它高。"""
        cfg = self._cfg()
        det = cfg["class_confidence_thresholds"][self.CLS]
        stop = cfg["machine_stop_rules"]["rules"][self.CLS]["min_confidence"]
        assert det > stop, (
            f"检测阈值 {det} 未高于停机门槛 {stop}，"
            f"上面的分析不再成立，请重新评估计数门槛")

    def test_rule_resolves_for_authorised_cameras_only(self):
        cfg = self._cfg()
        rr = cfg["repeat_stop_rules"]
        for i in range(1, 9):
            cam = f"camera{i}"
            assert st.effective_repeat_rule(self.CLS, cam, rr) is not None
            assert st.repeat_stop_allowed(cam, rr) is (i in (1, 8))

    def test_repeat_rule_catches_what_single_shot_lets_through(self, monkeypatch):
        """⚠️ 本类新增有没有意义，全看这一条。

        造一个 conf=0.88（过检测阈值 0.87）、高 100px（= 9.4mm，远小于
        单次要求的 47mm）的框：
          · 单次维度规则**不停机**（高度不够）
          · 重复规则连出 3 帧 → **停机**
        如果单次规则也停，说明这个框本来就会被停，新增等于没用。
        """
        monkeypatch.setattr(st, "REPEAT_STOP_RULES",
                            self._cfg()["repeat_stop_rules"])
        monkeypatch.setattr(st, "CAMERA_REPEAT_STOP_OVERRIDES", {})
        svc = StopMachineService(machine_stop_path="/tmp/_rs_yarn",
                                 launch_tag="t", analysis_mode="none")
        stops = []
        monkeypatch.setattr(svc, "_handle_stop",
                            lambda *a, **kw: (stops.append(a[2]), "")[1])

        box = DetectionBox(x1=10, y1=10, x2=60, y2=110,     # 高 100 px
                           confidence=0.88, class_id=2, class_name=self.CLS)
        px_to_mm = st.PIXEL_TO_MM
        assert box.height * px_to_mm < 47.0, "这个框必须小于单次要求的高度"

        # 1) 单次维度规则：不该停
        single = svc.analyze_defect_dimension(
            np.zeros((200, 200, 3), np.uint8), box, "camera1", "f.png")
        assert single.triggered is False, "单次规则不该停这个框，否则本测试无意义"

        # 2) 重复规则：连出 3 帧应该停
        img = np.zeros((200, 200, 3), np.uint8)
        assert svc.check_repeat_stops(img, "camera1", "f0.png",
                                      {self.CLS: box}) is False
        assert svc.check_repeat_stops(img, "camera1", "f1.png",
                                      {self.CLS: box}) is False
        assert svc.check_repeat_stops(img, "camera1", "f2.png",
                                      {self.CLS: box}) is True
        assert stops == ["camera1"]

    def test_middle_camera_does_not_count_this_class_either(self, monkeypatch):
        monkeypatch.setattr(st, "REPEAT_STOP_RULES",
                            self._cfg()["repeat_stop_rules"])
        monkeypatch.setattr(st, "CAMERA_REPEAT_STOP_OVERRIDES", {})
        svc = StopMachineService(machine_stop_path="/tmp/_rs_yarn2",
                                 launch_tag="t", analysis_mode="none")
        stops = []
        monkeypatch.setattr(svc, "_handle_stop",
                            lambda *a, **kw: (stops.append(a[2]), "")[1])
        box = DetectionBox(10, 10, 60, 110, 0.88, 2, self.CLS)
        for i in range(5):
            assert svc.check_repeat_stops(np.zeros((200, 200, 3), np.uint8),
                                          "camera5", f"f{i}.png",
                                          {self.CLS: box}) is False
        assert stops == []
        assert svc.repeat_tracker.get_stats()["occurrences"] == 0

    def test_two_classes_do_not_share_a_window(self, monkeypatch):
        """包边不良 和 单根包不进 必须各自独立计数。"""
        monkeypatch.setattr(st, "REPEAT_STOP_RULES",
                            self._cfg()["repeat_stop_rules"])
        monkeypatch.setattr(st, "CAMERA_REPEAT_STOP_OVERRIDES", {})
        svc = StopMachineService(machine_stop_path="/tmp/_rs_yarn3",
                                 launch_tag="t", analysis_mode="none")
        stops = []
        monkeypatch.setattr(svc, "_handle_stop",
                            lambda *a, **kw: (stops.append((a[2], a[1].class_name)), "")[1])
        img = np.zeros((200, 200, 3), np.uint8)
        a = DetectionBox(10, 10, 60, 60, 0.6, 1, "包边不良")
        b = DetectionBox(10, 10, 60, 110, 0.88, 2, self.CLS)
        # 交替出现：各自只攒到 2 次，都不该停
        for i, bx in enumerate((a, b, a, b)):
            assert svc.check_repeat_stops(img, "camera1", f"f{i}.png",
                                          {bx.class_name: bx}) is False
        assert stops == [], stops
        snap = {(d["camera_name"], d["class_name"]): d["pending"]
                for d in svc.repeat_tracker.snapshot()}
        assert snap == {("camera1", "包边不良"): 2,
                        ("camera1", self.CLS): 2}, snap


# ==================================================================
# §12 按相机覆盖 camera_repeat_stop_overrides
# ==================================================================
class TestPerCameraRepeatOverride:
    """逐键覆盖全局规则。⚠️ 只对白名单里的相机有意义。"""

    CLS = "包边不良"

    @staticmethod
    def _base_rules():
        return {"enabled": True,
                "stop_allowed_cameras": ["camera1", "camera8"],
                "rules": {"包边不良": {"enabled": True, "count": 3,
                                       "window_sec": 3.0,
                                       "min_confidence": 0.5}}}

    def _svc(self, monkeypatch, ovr):
        monkeypatch.setattr(st, "REPEAT_STOP_RULES", self._base_rules())
        monkeypatch.setattr(st, "CAMERA_REPEAT_STOP_OVERRIDES", ovr)
        svc = StopMachineService(machine_stop_path="/tmp/_rs_ovr",
                                 launch_tag="t", analysis_mode="none")
        stops = []
        monkeypatch.setattr(svc, "_handle_stop",
                            lambda *a, **kw: (stops.append(a[2]), "")[1])
        return svc, stops

    def test_override_applies_to_one_camera_only(self, monkeypatch):
        svc, stops = self._svc(monkeypatch, {
            "camera1": {"rules": {self.CLS: {"count": 2}}}})
        img = np.zeros((64, 64, 3), np.uint8)
        box = _box()
        svc.check_repeat_stops(img, "camera1", "f0.png", {self.CLS: box})
        assert stops == []
        svc.check_repeat_stops(img, "camera1", "f1.png", {self.CLS: box})
        assert stops == ["camera1"], "camera1 应 2 次就停"

    def test_unoverridden_camera_keeps_global(self, monkeypatch):
        """覆盖一台不能影响另一台。"""
        svc, stops = self._svc(monkeypatch, {
            "camera1": {"rules": {self.CLS: {"count": 2}}}})
        img = np.zeros((64, 64, 3), np.uint8)
        box = _box()
        for i in range(2):                       # camera8 全局是 3 次
            assert svc.check_repeat_stops(img, "camera8", f"f{i}.png",
                                          {self.CLS: box}) is False
        assert stops == [], "camera8 不该被 camera1 的覆盖影响"
        assert svc.check_repeat_stops(img, "camera8", "f2.png",
                                      {self.CLS: box}) is True

    def test_override_can_disable_a_class_on_one_camera(self, monkeypatch):
        svc, stops = self._svc(monkeypatch, {
            "camera8": {"rules": {self.CLS: {"enabled": False}}}})
        img = np.zeros((64, 64, 3), np.uint8)
        box = _box()
        for i in range(5):
            assert svc.check_repeat_stops(img, "camera8", f"f{i}.png",
                                          {self.CLS: box}) is False
        assert stops == []
        # camera1 不受影响
        for i in range(3):
            svc.check_repeat_stops(img, "camera1", f"g{i}.png", {self.CLS: box})
        assert stops == ["camera1"]

    def test_override_for_unlisted_camera_is_dead_config(self, monkeypatch):
        """⚠️ 钉住 config.yaml 注释里那句警告。

        给白名单外的相机配覆盖，规则能正确解析出来，但**完全不起作用**
        —— 因为计数之前就被短路了。这条测试防止有人照着旧注释
        去给 camera5 配参数，然后疑惑为什么没反应。
        """
        svc, stops = self._svc(monkeypatch, {
            "camera5": {"rules": {self.CLS: {"count": 2}}}})
        # 规则确实被覆盖了
        r = st.effective_repeat_rule(self.CLS, "camera5")
        assert r is not None and r["count"] == 2
        # 但一点用都没有
        img = np.zeros((64, 64, 3), np.uint8)
        box = _box()
        for i in range(4):
            assert svc.check_repeat_stops(img, "camera5", f"f{i}.png",
                                          {self.CLS: box}) is False
        assert stops == []
        assert svc.repeat_tracker.get_stats()["occurrences"] == 0, \
            "白名单外的相机连计数都不该做"

    def test_shipped_config_has_no_active_override(self):
        """出厂状态：没有任何按相机覆盖在生效。

        这个键**允许整个不写** —— settings 里
        ``CAMERA_REPEAT_STOP_OVERRIDES = _LiveDict(..., {})`` 缺键就取 {}。
        所以"键不存在"和"键为 {}"必须都算通过。
        """
        import yaml
        with open(os.path.join(_ROOT, "config.yaml"), encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        assert cfg.get("camera_repeat_stop_overrides", {}) == {}

    def test_missing_override_key_behaves_like_empty(self, monkeypatch):
        """⚠️ 钉住"这个键可以省略"：键不存在时，行为必须和 {} 完全一致。

        实测过：删掉这个键后 CAMERA_REPEAT_STOP_OVERRIDES 取默认 {}，
        camera1/camera8 照常停机、白名单外相机照常不参与。
        这条测试防止将来有人把缺键当成配置错误。
        """
        # settings 里声明的默认值就是 {}，所以缺键 == 空字典
        assert st.CAMERA_REPEAT_STOP_OVERRIDES._default == {}
        # 且缺键时 get 返回 None（= 没有覆盖，回落全局）
        monkeypatch.setattr(st, "CAMERA_REPEAT_STOP_OVERRIDES", {})
        assert st.get_camera_repeat_rule_override("camera1", "包边不良") is None
        assert st.get_camera_repeat_rule_override(None, "包边不良") is None

    def test_config_comment_does_not_suggest_a_dead_example(self):
        """注释里的例子必须用白名单里的相机，否则照着配就是死配置。

        扫的是整个【三】段（用法说明现在并在 repeat_stop_rules 的注释里，
        不再有独立的 camera_repeat_stop_overrides 段）。
        """
        import re
        import yaml
        with open(os.path.join(_ROOT, "config.yaml"), encoding="utf-8") as f:
            text = f.read()
        seg = text[text.index("# 【三】停机 · 重复触发"):
                   text.index("heatmap:")]
        allowed = set(yaml.safe_load(text)["repeat_stop_rules"]
                      ["stop_allowed_cameras"])
        mentioned = set(re.findall(r"camera\d", seg))
        bad = mentioned - allowed
        assert not bad, (
            f"注释里举了白名单外的相机 {sorted(bad)} 作为覆盖示例，"
            f"照着配是死配置。白名单={sorted(allowed)}")
