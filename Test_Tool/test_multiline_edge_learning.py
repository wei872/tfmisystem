#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【六】多股线边缘区自学习的回归测试。

重点盯：
  1. **区域按相机分开** —— camera2 学右侧、camera7 学左侧，共用区间会
     拿一台的布边去屏蔽另一台的真缺陷；
  2. **count > 5 才生成区域** —— 正好 5 个不行；
  3. **只有低置信度的框被抑制** —— 达到正常阈值的是真检出，必须照常放行；
  4. **区域是本卷的** —— 下一卷「开始」必须清空重学。
"""

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from AnomalyDetection_Tool.filter.multiline_edge_learner import (  # noqa: E402
    MultiLineEdgeLearner, init_multiline_learner, get_multiline_learner)

W = 2440          # 图像宽
NORMAL = 0.65     # 多股线正常阈值
BAND = 350


class _Dist:
    """可控的米长源（mm）。"""

    def __init__(self, mm=0.0):
        self.mm = mm

    def __call__(self):
        return self.mm


def _mk(dist=None, **kw):
    kw.setdefault("count_threshold", 5)
    kw.setdefault("region_padding_px", 50)
    kw.setdefault("image_width_px", W)
    return MultiLineEdgeLearner(distance_provider=dist or _Dist(), **kw)


# ======================================================================
# §1  窗口生命周期
# ======================================================================
class TestWindowLifecycle:

    def test_not_learning_before_begin(self):
        m = _mk()
        assert m.is_learning("camera2") is False
        assert m.learning_threshold_for("camera2", "多股线") is None

    def test_learning_after_begin(self):
        m = _mk()
        m.begin_window("程序启动")
        assert m.is_learning("camera2") is True
        assert m.is_learning("camera7") is True
        assert m.learning_threshold_for("camera2", "多股线") == 0.1

    def test_not_learning_for_other_cameras(self):
        m = _mk()
        m.begin_window()
        for cam in ("camera1", "camera3", "camera8"):
            assert m.is_learning(cam) is False
            assert m.learning_threshold_for(cam, "多股线") is None

    def test_not_learning_for_other_classes(self):
        m = _mk()
        m.begin_window()
        assert m.learning_threshold_for("camera2", "多股线") == 0.1
        for cls in ("包边不良", "油点线", "单根包不进"):
            assert m.learning_threshold_for("camera2", cls) is None

    def test_learning_stops_after_end(self):
        m = _mk()
        m.begin_window()
        m.end_window("前端停止")
        assert m.is_learning("camera2") is False

    def test_window_closes_at_ten_metres(self):
        d = _Dist(0.0)
        m = _mk(d)
        m.begin_window()
        d.mm = 9999
        m.check_distance()
        assert m.is_learning("camera2") is True, "9.999 m 还没到"
        d.mm = 10000
        m.check_distance()
        assert m.is_learning("camera2") is False, "10 m 必须结算"

    def test_growth_is_measured_from_window_start(self):
        """程序启动时累计米长可能已经很大（PLC 恢复），必须按增量算。"""
        d = _Dist(500000.0)          # 已经跑了 500 m
        m = _mk(d)
        m.begin_window("程序启动")
        d.mm = 509000                # 又走了 9 m
        m.check_distance()
        assert m.is_learning("camera2") is True
        d.mm = 510000                # 增量到 10 m
        m.check_distance()
        assert m.is_learning("camera2") is False

    def test_end_is_idempotent(self):
        m = _mk()
        m.begin_window()
        m.end_window()
        m.end_window()               # 不该炸，也不该重复结算
        assert m.snapshot()["stats"]["closed_by_stop"] == 1


# ======================================================================
# §2  学习期：记录与抑制
# ======================================================================
class TestLearningPhase:

    def test_camera2_right_band_is_recorded(self):
        m = _mk()
        m.begin_window()
        # 右边带 = x2 >= 2440-350 = 2090
        assert m.observe("camera2", "多股线", 2100, 2300, 0.3, NORMAL) is True
        assert m.snapshot()["samples"]["camera2"] == 1

    def test_camera7_left_band_is_recorded(self):
        m = _mk()
        m.begin_window()
        # 左边带 = x1 <= 350
        assert m.observe("camera7", "多股线", 10, 200, 0.3, NORMAL) is True
        assert m.snapshot()["samples"]["camera7"] == 1

    def test_camera2_left_side_is_not_in_band(self):
        """camera2 只看右侧，左侧的框不属于边带。"""
        m = _mk()
        m.begin_window()
        assert m.observe("camera2", "多股线", 10, 200, 0.3, NORMAL) is True
        assert m.snapshot()["samples"]["camera2"] == 0
        assert m.snapshot()["stats"]["suppressed_out_of_band"] == 1

    def test_camera7_right_side_is_not_in_band(self):
        m = _mk()
        m.begin_window()
        assert m.observe("camera7", "多股线", 2100, 2300, 0.3, NORMAL) is True
        assert m.snapshot()["samples"]["camera7"] == 0

    def test_out_of_band_low_conf_is_suppressed_but_not_recorded(self):
        """阈值降到 0.1 后边带外会冒出噪声，不能当真缺陷报出去。"""
        m = _mk()
        m.begin_window()
        assert m.observe("camera2", "多股线", 800, 900, 0.2, NORMAL) is True
        assert m.snapshot()["samples"]["camera2"] == 0

    def test_genuine_detection_passes_through(self):
        """置信度达到正常阈值的是真检出，学习期也必须放行。"""
        m = _mk()
        m.begin_window()
        assert m.observe("camera2", "多股线", 2100, 2300, 0.65, NORMAL) is False
        assert m.observe("camera2", "多股线", 800, 900, 0.9, NORMAL) is False

    def test_other_class_never_suppressed(self):
        m = _mk()
        m.begin_window()
        assert m.observe("camera2", "包边不良", 2100, 2300, 0.2, NORMAL) is False

    def test_other_camera_never_suppressed(self):
        m = _mk()
        m.begin_window()
        assert m.observe("camera3", "多股线", 2100, 2300, 0.2, NORMAL) is False

    def test_observe_is_noop_when_window_closed(self):
        m = _mk()
        m.begin_window()
        m.end_window()
        assert m.observe("camera2", "多股线", 2100, 2300, 0.3, NORMAL) is False


# ======================================================================
# §3  区域生成
# ======================================================================
class TestRegionLearning:

    def _feed(self, m, cam, boxes):
        m.begin_window()
        for x1, x2, c in boxes:
            m.observe(cam, "多股线", x1, x2, c, NORMAL)
        m.end_window()

    def test_exactly_five_does_not_create_a_region(self):
        """> 5 才生成 —— 正好 5 个不行。"""
        m = _mk()
        self._feed(m, "camera2", [(2100, 2200, 0.3)] * 5)
        assert m.region_of("camera2") is None
        assert m.snapshot()["stats"]["not_enough_samples"] == 1

    def test_six_creates_a_region(self):
        m = _mk()
        self._feed(m, "camera2", [(2100, 2200, 0.3)] * 6)
        assert m.region_of("camera2") is not None

    def test_region_is_min_max_expanded_by_padding(self):
        m = _mk()
        self._feed(m, "camera2", [
            (2150, 2250, 0.3), (2100, 2180, 0.4), (2200, 2320, 0.2),
            (2120, 2260, 0.3), (2140, 2300, 0.3), (2110, 2210, 0.5),
        ])
        # min(x1)=2100, max(x2)=2320 -> 外扩 50 => [2050, 2370]
        assert m.region_of("camera2") == (2050, 2370)

    def test_region_is_clamped_to_image_bounds(self):
        m = _mk()
        self._feed(m, "camera7", [
            (0, 100, 0.3), (5, 120, 0.3), (10, 90, 0.3),
            (0, 110, 0.3), (20, 130, 0.3), (0, 95, 0.3),
        ])
        left, right = m.region_of("camera7")
        assert left == 0, "左边不能扩成负数"
        assert right == 130 + 50

    def test_regions_are_per_camera(self):
        """camera2 学右侧、camera7 学左侧，必须各存各的。"""
        m = _mk()
        m.begin_window()
        for _ in range(6):
            m.observe("camera2", "多股线", 2100, 2200, 0.3, NORMAL)
            m.observe("camera7", "多股线", 10, 120, 0.3, NORMAL)
        m.end_window()
        assert m.region_of("camera2")[0] > 2000
        assert m.region_of("camera7")[1] < 300

    def test_region_cleared_on_next_roll(self):
        m = _mk()
        self._feed(m, "camera2", [(2100, 2200, 0.3)] * 6)
        assert m.region_of("camera2") is not None
        m.begin_window("下一卷")
        assert m.region_of("camera2") is None, "区域是本卷的，必须清空重学"


# ======================================================================
# §4  永久过滤区生效
# ======================================================================
class TestPermanentFilter:

    def _learned(self, cam="camera2", region_boxes=None):
        m = _mk()
        m.begin_window()
        for x1, x2 in (region_boxes or [(2100, 2200)] * 6):
            m.observe(cam, "多股线", x1, x2, 0.3, NORMAL)
        m.end_window()
        return m

    def test_box_inside_region_is_filtered(self):
        m = self._learned()
        assert m.should_filter("camera2", "多股线", 2120, 2180) is True

    def test_box_outside_region_is_not_filtered(self):
        m = self._learned()
        assert m.should_filter("camera2", "多股线", 500, 600) is False

    def test_partial_overlap_counts(self):
        """多股线是横向长线，只要有一部分在区里就是同一条布边。"""
        m = self._learned()          # 区域 [2050, 2250]
        assert m.should_filter("camera2", "多股线", 2000, 2100) is True
        assert m.should_filter("camera2", "多股线", 2200, 2400) is True

    def test_filter_is_per_camera(self):
        m = self._learned("camera2")
        assert m.should_filter("camera7", "多股线", 2120, 2180) is False

    def test_other_class_not_filtered(self):
        m = self._learned()
        assert m.should_filter("camera2", "包边不良", 2120, 2180) is False

    def test_no_filter_before_learning(self):
        m = _mk()
        m.begin_window()
        assert m.should_filter("camera2", "多股线", 2120, 2180) is False


# ======================================================================
# §5  配置健壮性 / 并发
# ======================================================================
class TestRobustness:

    @pytest.mark.parametrize("spec", [
        {"side": "up", "band_px": 350},
        {"side": "", "band_px": 350},
        {"side": "right", "band_px": 0},
        {"side": "right", "band_px": -10},
        {"side": "right", "band_px": "abc"},
    ])
    def test_bad_target_spec_is_skipped(self, spec):
        m = MultiLineEdgeLearner(targets={"camera2": spec})
        assert m.targets == {}
        m.begin_window()
        assert m.is_learning("camera2") is False

    def test_empty_targets_disables_everything(self):
        m = MultiLineEdgeLearner(targets={})
        m.begin_window()
        assert m.is_learning("camera2") is False
        assert m.observe("camera2", "多股线", 2100, 2200, 0.3, 0.65) is False

    def test_broken_distance_provider_does_not_raise(self):
        def boom():
            raise RuntimeError("编码器没连上")
        m = MultiLineEdgeLearner(distance_provider=boom)
        m.begin_window()
        m.check_distance()            # 不该抛
        assert m.is_learning("camera2") is True

    def test_concurrent_observe_is_safe(self):
        m = _mk()
        m.begin_window()
        errs = []

        def worker(i):
            try:
                for k in range(200):
                    m.observe("camera2", "多股线",
                              2100 + (k % 50), 2200 + (k % 50), 0.3, NORMAL)
                    m.should_filter("camera2", "多股线", 2100, 2200)
            except Exception as e:              # pragma: no cover
                errs.append(e)

        ths = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        assert errs == []
        assert m.snapshot()["samples"]["camera2"] == 8 * 200


# ======================================================================
# §6  接线
# ======================================================================
class TestWiring:

    @pytest.fixture(scope="class")
    def cfg(self):
        import yaml
        with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
            return yaml.safe_load(f)

    def test_config_section(self, cfg):
        c = cfg["multiline_edge_learning"]
        assert c["enabled"] is True
        assert c["target_class"] == "多股线"
        assert float(c["learning_threshold"]) == 0.1
        assert float(c["window_length_mm"]) == 10000
        assert int(c["count_threshold"]) == 5
        assert int(c["region_padding_px"]) == 50

    def test_config_targets(self, cfg):
        t = cfg["multiline_edge_learning"]["targets"]
        assert set(t) == {"camera2", "camera7"}
        assert t["camera2"]["side"] == "right"
        assert t["camera7"]["side"] == "left"
        assert t["camera2"]["band_px"] == 350
        assert t["camera7"]["band_px"] == 350

    def test_image_width_matches_size_section(self, cfg):
        assert int(cfg["multiline_edge_learning"]["image_width_px"]) == \
            int(cfg["size_conversion"]["image_width_px"])

    def test_learning_threshold_is_below_normal(self, cfg):
        """学习阈值必须低于正常阈值，否则降阈值毫无意义。"""
        lt = float(cfg["multiline_edge_learning"]["learning_threshold"])
        normal = float(cfg["class_confidence_thresholds"]["多股线"])
        assert lt < normal

    def test_settings_exports_it(self):
        from AnomalyDetection_Tool.config.settings import MULTILINE_EDGE_LEARNING
        assert MULTILINE_EDGE_LEARNING.get("target_class") == "多股线"

    def test_pipeline_lowers_threshold_during_learning(self):
        with open(os.path.join(ROOT, "AnomalyDetection_Tool", "core",
                               "detection_pipeline.py"), encoding="utf-8") as f:
            src = f.read()
        body = src[src.index("def _filter_boxes"):]
        assert "learning_threshold_for(" in body
        # threshold_used 必须仍是正常阈值，下游靠它区分真检出
        assert "box.threshold_used = thr" in body

    def test_manager_hooks_before_area_analysis(self):
        """被滤掉的框不该进停机判定/存图/上传，所以必须在面积分析之前。"""
        with open(os.path.join(ROOT, "AnomalyDetection_Tool", "core",
                               "detection_manager.py"), encoding="utf-8") as f:
            src = f.read()
        assert src.index("get_multiline_learner()") < src.index("# 面积分析")

    def test_thrift_has_stop_hook(self):
        with open(os.path.join(ROOT, "Communication_Tool", "ThriftControl.py"),
                  encoding="utf-8") as f:
            src = f.read()
        assert "def register_on_stop" in src
        assert "_fire_stop_callbacks()" in src

    def test_main_yolo_registers_both_hooks(self):
        with open(os.path.join(ROOT, "main_yolo.py"), encoding="utf-8") as f:
            src = f.read()
        assert "register_on_start(" in src
        assert "register_on_stop(" in src
        assert 'begin_window("程序启动")' in src

    def test_singleton_roundtrip(self):
        m = init_multiline_learner(count_threshold=7)
        assert get_multiline_learner() is m
        assert m.count_threshold == 7


# ======================================================================
# §7  集成：真的走 detection_pipeline._filter_boxes 这段代码
# ======================================================================
class TestFilterBoxesIntegration:
    """用桩对象绑定**真实的**未绑定方法，测的是改过的那段代码本身，
    不是重新实现一份。"""

    @pytest.fixture()
    def pipe(self):
        from AnomalyDetection_Tool.core import detection_pipeline as dp

        class _ThrCfg:
            def get_threshold(self, class_name, class_id, camera_name):
                return 0.65

        class _Stub:
            filter_classes = set()
            threshold_config = _ThrCfg()

            def _gap_to_fabric(self, box, bbox):
                return 0

        # 直接取真实实现，绑到桩上
        _Stub._filter_boxes = dp.DetectionPipeline._filter_boxes
        return _Stub(), dp

    def _box(self, cls, x1, x2, conf, cid=0):
        from AnomalyDetection_Tool.models.data_types import DetectionBox
        return DetectionBox(x1=x1, y1=100, x2=x2, y2=200,
                            confidence=conf, class_id=cid,
                            class_name=cls)

    def test_low_conf_multiline_passes_filter_during_learning(self, pipe):
        """学习期：0.3 的多股线（低于 0.65）必须能穿过阈值过滤，
        否则学习器根本拿不到框。"""
        stub, dp = pipe
        m = init_multiline_learner(image_width_px=W, count_threshold=5)
        m.begin_window()
        dp.get_multiline_learner = lambda: m
        try:
            out, *_ = stub._filter_boxes(
                [self._box("多股线", 2100, 2300, 0.30)], None,
                camera_name="camera2")
            assert len(out) == 1
            # threshold_used 必须仍是**正常**阈值 0.65，
            # 下游靠它判定这是"只因降阈值才存在的框"
            assert out[0].threshold_used == 0.65
        finally:
            dp.get_multiline_learner = lambda: None

    def test_low_conf_multiline_blocked_when_not_learning(self, pipe):
        stub, dp = pipe
        m = init_multiline_learner(image_width_px=W)
        dp.get_multiline_learner = lambda: m        # 未 begin_window
        try:
            out, *_ = stub._filter_boxes(
                [self._box("多股线", 2100, 2300, 0.30)], None,
                camera_name="camera2")
            assert out == []
        finally:
            dp.get_multiline_learner = lambda: None

    def test_other_camera_not_affected(self, pipe):
        stub, dp = pipe
        m = init_multiline_learner(image_width_px=W)
        m.begin_window()
        dp.get_multiline_learner = lambda: m
        try:
            out, *_ = stub._filter_boxes(
                [self._box("多股线", 2100, 2300, 0.30)], None,
                camera_name="camera3")
            assert out == [], "camera3 不在目标里，0.3 该被正常阈值挡掉"
        finally:
            dp.get_multiline_learner = lambda: None

    def test_other_class_not_affected(self, pipe):
        stub, dp = pipe
        m = init_multiline_learner(image_width_px=W)
        m.begin_window()
        dp.get_multiline_learner = lambda: m
        try:
            out, *_ = stub._filter_boxes(
                [self._box("包边不良", 2100, 2300, 0.30)], None,
                camera_name="camera2")
            assert out == [], "别的类不该被降阈值"
        finally:
            dp.get_multiline_learner = lambda: None

    def test_learner_crash_falls_back_to_normal_threshold(self, pipe):
        """学习器抛异常时必须退回正常阈值，不能把整个过滤搞崩。"""
        stub, dp = pipe

        class _Boom:
            def learning_threshold_for(self, *a, **k):
                raise RuntimeError("学习器炸了")

        dp.get_multiline_learner = lambda: _Boom()
        try:
            out, *_ = stub._filter_boxes(
                [self._box("多股线", 2100, 2300, 0.30),
                 self._box("多股线", 2100, 2300, 0.90)], None,
                camera_name="camera2")
            assert len(out) == 1, "只有 0.90 那个该留下"
            assert out[0].confidence == 0.90
        finally:
            dp.get_multiline_learner = lambda: None


# ======================================================================
# §8  端到端：一整卷的完整流程
# ======================================================================
class TestFullRoll:

    def test_full_roll_lifecycle(self):
        d = _Dist(0.0)
        m = _mk(d)

        # --- 程序启动，开窗 ---
        m.begin_window("程序启动")
        assert m.is_learning("camera2")

        # --- 走了 6 m，右边带稳定出现 7 个低置信度多股线 ---
        d.mm = 6000
        for x1, x2 in [(2120, 2260), (2100, 2240), (2140, 2280),
                       (2110, 2250), (2130, 2270), (2105, 2245),
                       (2125, 2265)]:
            assert m.observe("camera2", "多股线", x1, x2, 0.35, NORMAL)
        # 同时有一个真检出（>=0.65），必须放行
        assert m.observe("camera2", "多股线", 2150, 2200, 0.80,
                         NORMAL) is False
        # camera7 左边带也攒够
        for _ in range(6):
            m.observe("camera7", "多股线", 20, 150, 0.30, NORMAL)

        # --- 到 10 m，自动结算 ---
        d.mm = 10000
        m.check_distance()
        assert m.is_learning("camera2") is False

        # 区域 = [min(2100), max(2280)] 外扩 50 => [2050, 2330]
        assert m.region_of("camera2") == (2050, 2330)
        assert m.region_of("camera7") == (0, 200)

        # --- 本卷后续：区内滤掉，区外照常 ---
        assert m.should_filter("camera2", "多股线", 2100, 2250) is True
        assert m.should_filter("camera2", "多股线", 600, 700) is False
        assert m.should_filter("camera2", "包边不良", 2100, 2250) is False
        # camera7 的区域不影响 camera2 的坐标
        assert m.should_filter("camera7", "多股线", 2100, 2250) is False

        # --- 下一卷 ---
        d.mm = 10000
        m.begin_window("前端开始")
        assert m.region_of("camera2") is None
        assert m.snapshot()["samples"]["camera2"] == 0

    def test_roll_with_too_few_samples_stays_normal(self):
        d = _Dist(0.0)
        m = _mk(d)
        m.begin_window()
        for _ in range(4):
            m.observe("camera2", "多股线", 2100, 2200, 0.30, NORMAL)
        m.end_window("前端停止")
        assert m.region_of("camera2") is None
        assert m.should_filter("camera2", "多股线", 2100, 2200) is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
