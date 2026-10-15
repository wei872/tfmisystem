#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【六】边缘区自学习的回归测试。

三条被现场纠正过的语义，每条都单独钉死：
  1. **学习期只由米长决定** —— 前端「停止」是"这批布结束了"，只清场，
     绝不能用半截数据凑出生效区；
  2. **降阈值只在学习区内** —— 区外照旧用正常阈值，所以区外根本不会
     产生低置信度噪声框，也就不需要任何补救性的抑制规则；
  3. **生效区按 (相机, 类别) 分别存** —— 只针对目标类，绝不波及别的疵点，
     且结构上支持以后往 target_classes 里加类别。
"""

import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from AnomalyDetection_Tool.filter.edge_region_learner import (  # noqa: E402
    EdgeRegionLearner, init_edge_learner, get_edge_learner)

W = 2440          # 图像宽
BAND = 350
RIGHT_EDGE = W - BAND      # camera2 学习区左界：x2 >= 2090


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
    return EdgeRegionLearner(distance_provider=dist or _Dist(), **kw)


def _feed(m, cam, boxes, cls="多股线"):
    for x1, x2 in boxes:
        m.observe(cam, cls, x1, x2)


# ======================================================================
# §1  修正一：学习期只由米长决定，「停止」只清场不结算
# ======================================================================
class TestLearningEndsOnlyByDistance:

    def test_learning_active_before_ten_metres(self):
        d = _Dist(0.0)
        m = _mk(d)
        m.begin_window("程序启动")
        d.mm = 9999
        m.check_distance()
        assert m.is_learning("camera2") is True

    def test_learning_ends_at_ten_metres(self):
        d = _Dist(0.0)
        m = _mk(d)
        m.begin_window()
        d.mm = 10000
        m.check_distance()
        assert m.is_learning("camera2") is False
        assert m.snapshot()["stats"]["settled_by_distance"] == 1

    def test_growth_measured_from_window_start(self):
        """程序启动时 PLC 会恢复上次累计值，必须按增量算。"""
        d = _Dist(500000.0)
        m = _mk(d)
        m.begin_window("程序启动")
        d.mm = 509999
        m.check_distance()
        assert m.is_learning("camera2") is True
        d.mm = 510000
        m.check_distance()
        assert m.is_learning("camera2") is False

    def test_stop_before_ten_metres_does_not_create_a_region(self):
        """**关键**：没走到 10 m 就「停止」，是"这批布结束了"，
        不是学习期结算。绝不能拿 4 个样本凑一个生效区出来。"""
        d = _Dist(0.0)
        m = _mk(d)
        m.begin_window()
        _feed(m, "camera2", [(2100, 2200)] * 8)      # 样本够 8 个
        d.mm = 4000                                   # 但只走了 4 m
        m.end_roll("前端停止")
        assert m.region_of("camera2", "多股线") is None
        assert m.snapshot()["stats"]["regions_learned"] == 0
        assert m.snapshot()["stats"]["settled_by_distance"] == 0

    def test_stop_clears_an_already_learned_region(self):
        """生效区是本卷的，布下机了就该失效。"""
        d = _Dist(0.0)
        m = _mk(d)
        m.begin_window()
        _feed(m, "camera2", [(2100, 2200)] * 6)
        d.mm = 10000
        m.check_distance()
        assert m.region_of("camera2", "多股线") is not None
        m.end_roll("前端停止")
        assert m.region_of("camera2", "多股线") is None
        assert m.should_filter("camera2", "多股线", 2100, 2200) is False

    def test_stop_clears_samples(self):
        m = _mk()
        m.begin_window()
        _feed(m, "camera2", [(2100, 2200)] * 3)
        assert m.snapshot()["samples"]["camera2/多股线"] == 3
        m.end_roll()
        assert m.snapshot()["samples"]["camera2/多股线"] == 0

    def test_next_roll_starts_clean(self):
        d = _Dist(0.0)
        m = _mk(d)
        m.begin_window()
        _feed(m, "camera2", [(2100, 2200)] * 6)
        d.mm = 10000
        m.check_distance()
        assert m.region_of("camera2", "多股线") is not None
        m.begin_window("下一卷")
        assert m.region_of("camera2", "多股线") is None
        assert m.is_learning("camera2") is True

    def test_stop_is_idempotent(self):
        m = _mk()
        m.begin_window()
        m.end_roll()
        m.end_roll()
        assert m.snapshot()["stats"]["rolls_ended"] == 2


# ======================================================================
# §2  修正二：降阈值只在学习区内
# ======================================================================
class TestThresholdScopedToLearningZone:

    def test_in_zone_gets_low_threshold(self):
        m = _mk()
        m.begin_window()
        assert m.learning_threshold_for("camera2", "多股线",
                                        2100, 2300) == 0.1

    def test_out_of_zone_keeps_normal_threshold(self):
        """区外不降阈值 —— 这是第一版做错的地方。"""
        m = _mk()
        m.begin_window()
        assert m.learning_threshold_for("camera2", "多股线",
                                        500, 600) is None

    def test_out_of_zone_box_is_not_suppressed(self):
        """区外的框用的是正常阈值，是真检出，必须照常报出。
        第一版在这里额外加了一条"区外低置信度也丢掉"的补救规则，
        那是把两个问题搅在一起；区外不降阈值就不需要它。"""
        m = _mk()
        m.begin_window()
        assert m.observe("camera2", "多股线", 500, 600) is False
        assert m.snapshot()["samples"]["camera2/多股线"] == 0

    def test_camera2_zone_is_the_right_side(self):
        m = _mk()
        m.begin_window()
        assert m.in_learning_zone("camera2", 2089, 2089) is False
        assert m.in_learning_zone("camera2", 2090, 2090) is True

    def test_camera7_zone_is_the_left_side(self):
        m = _mk()
        m.begin_window()
        assert m.in_learning_zone("camera7", 350, 900) is True
        assert m.in_learning_zone("camera7", 351, 900) is False

    def test_zone_does_not_apply_to_other_cameras(self):
        m = _mk()
        m.begin_window()
        assert m.in_learning_zone("camera3", 2100, 2300) is False
        assert m.learning_threshold_for("camera3", "多股线",
                                        2100, 2300) is None

    def test_no_threshold_before_window_opens(self):
        m = _mk()
        assert m.learning_threshold_for("camera2", "多股线",
                                        2100, 2300) is None


# ======================================================================
# §3  修正三：生效区按 (相机, 类别) 分别存
# ======================================================================
class TestPerCameraPerClassRegion:

    def _learn(self, cam="camera2", cls="多股线",
               boxes=None, dist=None):
        d = dist or _Dist(0.0)
        m = _mk(d)
        m.begin_window()
        _feed(m, cam, boxes or [(2100, 2200)] * 6, cls)
        d.mm = 10000
        m.check_distance()
        return m

    def test_region_keyed_by_camera_and_class(self):
        m = self._learn()
        assert m.region_of("camera2", "多股线") is not None
        assert m.region_of("camera7", "多股线") is None
        assert m.region_of("camera2", "包边不良") is None

    def test_filter_only_hits_the_learned_class(self):
        """生效区只对学它的那个类生效，绝不波及别的疵点。"""
        m = self._learn()
        assert m.should_filter("camera2", "多股线", 2100, 2200) is True
        for cls in ("包边不良", "油点线", "单根包不进", "飘絮", "纬线杂质"):
            assert m.should_filter("camera2", cls, 2100, 2200) is False, cls

    def test_filter_does_not_cross_cameras(self):
        m = self._learn("camera2")
        assert m.should_filter("camera7", "多股线", 2100, 2200) is False

    def test_two_classes_get_separate_regions(self):
        """结构上支持多类别：各学各的，互不干扰。"""
        d = _Dist(0.0)
        m = EdgeRegionLearner(
            target_classes=["多股线", "包边不良"],
            count_threshold=5, region_padding_px=50,
            image_width_px=W, distance_provider=d)
        m.begin_window()
        _feed(m, "camera2", [(2100, 2200)] * 6, "多股线")
        _feed(m, "camera2", [(2150, 2400)] * 6, "包边不良")
        d.mm = 10000
        m.check_distance()
        assert m.region_of("camera2", "多股线") == (2050, 2250)
        # 2400+50=2450 被夹到图像右边界 2440
        assert m.region_of("camera2", "包边不良") == (2100, 2440)
        # 各自的区互不越界（注意判定是"有重叠即命中"，取真正不相交的框）
        assert m.should_filter("camera2", "多股线", 2300, 2400) is False
        assert m.should_filter("camera2", "包边不良", 1000, 2000) is False

    def test_one_class_can_fail_while_another_succeeds(self):
        d = _Dist(0.0)
        m = EdgeRegionLearner(
            target_classes=["多股线", "包边不良"],
            count_threshold=5, image_width_px=W, distance_provider=d)
        m.begin_window()
        _feed(m, "camera2", [(2100, 2200)] * 6, "多股线")
        _feed(m, "camera2", [(2100, 2200)] * 2, "包边不良")   # 不够
        d.mm = 10000
        m.check_distance()
        assert m.region_of("camera2", "多股线") is not None
        assert m.region_of("camera2", "包边不良") is None

    def test_class_without_a_region_is_not_filtered_by_another_class(self):
        """**生效区的类别维度必须真的生效。**

        两个类都是目标类、都在同一台相机上，只有多股线学成了区域。
        这时包边不良落在同一片横向位置也**必须照常报出** —— 它没学出
        自己的区，就不能借用别人的。

        这条是专门用来抓"生效区只按相机存、丢了类别维度"的：
        那样实现的话这里会误报 True。（第一版测试漏了它，注入该 bug
        时 53 个测试全绿 —— 因为 should_filter 先用 target_classes
        挡掉了非目标类，把类别维度的错误掩盖了。所以必须让两个类
        **都在** target_classes 里，才真的走到区域查找那一步。）
        """
        d = _Dist(0.0)
        m = EdgeRegionLearner(
            target_classes=["多股线", "包边不良"],
            count_threshold=5, image_width_px=W, distance_provider=d)
        m.begin_window()
        _feed(m, "camera2", [(2100, 2200)] * 6, "多股线")
        _feed(m, "camera2", [(2100, 2200)] * 2, "包边不良")   # 不够，不生成
        d.mm = 10000
        m.check_distance()
        assert m.should_filter("camera2", "多股线", 2100, 2200) is True
        assert m.should_filter("camera2", "包边不良", 2100, 2200) is False

    def test_target_classes_accepts_a_plain_string(self):
        m = EdgeRegionLearner(target_classes="多股线")
        assert m.target_classes == ("多股线",)

    def test_empty_target_classes_disables(self):
        m = EdgeRegionLearner(target_classes=[])
        m.begin_window()
        assert m.is_learning("camera2") is False


# ======================================================================
# §4  生效区的算法
# ======================================================================
class TestRegionMath:

    def test_exactly_five_does_not_create_a_region(self):
        """> 5 才生成 —— 正好 5 个不行。"""
        d = _Dist(0.0)
        m = _mk(d)
        m.begin_window()
        _feed(m, "camera2", [(2100, 2200)] * 5)
        d.mm = 10000
        m.check_distance()
        assert m.region_of("camera2", "多股线") is None
        assert m.snapshot()["stats"]["not_enough_samples"] == 1

    def test_region_is_min_max_expanded(self):
        d = _Dist(0.0)
        m = _mk(d)
        m.begin_window()
        _feed(m, "camera2", [(2150, 2250), (2100, 2180), (2200, 2320),
                             (2120, 2260), (2140, 2300), (2110, 2210)])
        d.mm = 10000
        m.check_distance()
        # min(x1)=2100, max(x2)=2320 -> 外扩 50 => [2050, 2370]
        assert m.region_of("camera2", "多股线") == (2050, 2370)

    def test_region_can_be_narrower_than_the_learning_zone(self):
        """生效区不等于学习区：布边只占一小段时它就窄。"""
        d = _Dist(0.0)
        m = _mk(d)
        m.begin_window()
        # 别用靠图像右边的框：2420+50 会被夹到 2440，宽度就不是 120 了
        _feed(m, "camera2", [(2100, 2120)] * 6)
        d.mm = 10000
        m.check_distance()
        left, right = m.region_of("camera2", "多股线")
        assert (left, right) == (2050, 2170)
        assert (right - left) == 20 + 100          # 20 px 布边 + 两侧各 50
        assert right - left < BAND

    def test_region_clamped_to_image_bounds(self):
        d = _Dist(0.0)
        m = _mk(d)
        m.begin_window()
        _feed(m, "camera7", [(0, 100), (5, 120), (10, 90),
                             (0, 110), (20, 130), (0, 95)])
        d.mm = 10000
        m.check_distance()
        left, right = m.region_of("camera7", "多股线")
        assert left == 0
        assert right == 180

    def test_partial_overlap_counts_as_hit(self):
        """这类疵点是横向长线，有一部分在生效区里就是同一条布边。"""
        d = _Dist(0.0)
        m = _mk(d)
        m.begin_window()
        _feed(m, "camera2", [(2100, 2200)] * 6)     # 区 [2050, 2250]
        d.mm = 10000
        m.check_distance()
        assert m.should_filter("camera2", "多股线", 2000, 2100) is True
        assert m.should_filter("camera2", "多股线", 2200, 2400) is True
        assert m.should_filter("camera2", "多股线", 1000, 2000) is False


# ======================================================================
# §5  健壮性 / 并发
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
        m = EdgeRegionLearner(targets={"camera2": spec})
        assert m.targets == {}
        m.begin_window()
        assert m.is_learning("camera2") is False

    def test_explicit_empty_targets_disables(self):
        """{} 是显式禁用，绝不能回落到默认 camera2/camera7。"""
        m = EdgeRegionLearner(targets={})
        assert m.targets == {}
        m.begin_window()
        assert m.is_learning("camera2") is False

    def test_none_targets_uses_default(self):
        m = EdgeRegionLearner(targets=None)
        assert set(m.targets) == {"camera2", "camera7"}

    def test_broken_distance_provider_does_not_raise(self):
        def boom():
            raise RuntimeError("编码器没连上")
        m = EdgeRegionLearner(distance_provider=boom)
        m.begin_window()
        m.check_distance()
        assert m.is_learning("camera2") is True

    def test_concurrent_use_is_safe(self):
        m = _mk()
        m.begin_window()
        errs = []

        def worker():
            try:
                for k in range(200):
                    m.observe("camera2", "多股线",
                              2100 + (k % 50), 2200 + (k % 50))
                    m.should_filter("camera2", "多股线", 2100, 2200)
                    m.learning_threshold_for("camera2", "多股线", 2100, 2200)
            except Exception as e:              # pragma: no cover
                errs.append(e)

        ths = [threading.Thread(target=worker) for _ in range(8)]
        [t.start() for t in ths]
        [t.join() for t in ths]
        assert errs == []
        assert m.snapshot()["samples"]["camera2/多股线"] == 8 * 200


# ======================================================================
# §6  集成：真的走 detection_pipeline._filter_boxes 这段代码
# ======================================================================
class TestFilterBoxesIntegration:
    """用桩对象绑定**真实的**未绑定方法，测的是改过的那段代码本身。"""

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

        _Stub._filter_boxes = dp.DetectionPipeline._filter_boxes
        return _Stub(), dp

    def _box(self, cls, x1, x2, conf, cid=0):
        from AnomalyDetection_Tool.models.data_types import DetectionBox
        return DetectionBox(x1=x1, y1=100, x2=x2, y2=200,
                            confidence=conf, class_id=cid, class_name=cls)

    def test_low_conf_in_zone_passes_during_learning(self, pipe):
        stub, dp = pipe
        m = init_edge_learner(image_width_px=W)
        m.begin_window()
        dp.get_edge_learner = lambda: m
        try:
            out, *_ = stub._filter_boxes(
                [self._box("多股线", 2100, 2300, 0.30)], None,
                camera_name="camera2")
            assert len(out) == 1
            assert out[0].threshold_used == 0.65, "threshold_used 记正常阈值"
        finally:
            dp.get_edge_learner = lambda: None

    def test_low_conf_out_of_zone_is_blocked(self, pipe):
        """**修正二的核心**：区外不降阈值，0.30 的框被正常阈值挡掉。"""
        stub, dp = pipe
        m = init_edge_learner(image_width_px=W)
        m.begin_window()
        dp.get_edge_learner = lambda: m
        try:
            out, *_ = stub._filter_boxes(
                [self._box("多股线", 500, 600, 0.30)], None,
                camera_name="camera2")
            assert out == []
        finally:
            dp.get_edge_learner = lambda: None

    def test_high_conf_out_of_zone_still_reported(self, pipe):
        stub, dp = pipe
        m = init_edge_learner(image_width_px=W)
        m.begin_window()
        dp.get_edge_learner = lambda: m
        try:
            out, *_ = stub._filter_boxes(
                [self._box("多股线", 500, 600, 0.90)], None,
                camera_name="camera2")
            assert len(out) == 1
        finally:
            dp.get_edge_learner = lambda: None

    def test_other_class_not_lowered(self, pipe):
        stub, dp = pipe
        m = init_edge_learner(image_width_px=W)
        m.begin_window()
        dp.get_edge_learner = lambda: m
        try:
            out, *_ = stub._filter_boxes(
                [self._box("包边不良", 2100, 2300, 0.30)], None,
                camera_name="camera2")
            assert out == []
        finally:
            dp.get_edge_learner = lambda: None

    def test_learner_crash_falls_back(self, pipe):
        stub, dp = pipe

        class _Boom:
            def learning_threshold_for(self, *a, **k):
                raise RuntimeError("学习器炸了")

        dp.get_edge_learner = lambda: _Boom()
        try:
            out, *_ = stub._filter_boxes(
                [self._box("多股线", 2100, 2300, 0.30),
                 self._box("多股线", 2100, 2300, 0.90)], None,
                camera_name="camera2")
            assert len(out) == 1 and out[0].confidence == 0.90
        finally:
            dp.get_edge_learner = lambda: None


# ======================================================================
# §7  端到端：一整卷
# ======================================================================
class TestFullRoll:

    def test_full_roll(self):
        d = _Dist(0.0)
        m = _mk(d)
        m.begin_window("程序启动")

        # 走了 6 m：右边带稳定出现 7 个多股线
        d.mm = 6000
        for x1, x2 in [(2120, 2260), (2100, 2240), (2140, 2280),
                       (2110, 2250), (2130, 2270), (2105, 2245),
                       (2125, 2265)]:
            assert m.observe("camera2", "多股线", x1, x2) is True
        # 区外的框不受影响
        assert m.observe("camera2", "多股线", 600, 700) is False
        # camera7 左边带也攒够
        _feed(m, "camera7", [(20, 150)] * 6)

        # 到 10 m，结算
        d.mm = 10000
        m.check_distance()
        assert m.is_learning("camera2") is False
        assert m.region_of("camera2", "多股线") == (2050, 2330)
        assert m.region_of("camera7", "多股线") == (0, 200)

        # 本卷后续：区内滤掉，区外照常，别的类不受影响
        assert m.should_filter("camera2", "多股线", 2100, 2250) is True
        assert m.should_filter("camera2", "多股线", 600, 700) is False
        assert m.should_filter("camera2", "包边不良", 2100, 2250) is False
        assert m.should_filter("camera7", "多股线", 2100, 2250) is False

        # 布下机
        m.end_roll("前端停止")
        assert m.region_of("camera2", "多股线") is None

        # 下一卷
        m.begin_window("前端开始")
        assert m.is_learning("camera2") is True
        assert m.snapshot()["samples"]["camera2/多股线"] == 0


# ======================================================================
# §8  配置与接线
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
        assert float(c["learning_threshold"]) == 0.1
        assert float(c["window_length_mm"]) == 10000
        assert int(c["count_threshold"]) == 5
        assert int(c["region_padding_px"]) == 50

    def test_target_classes_is_a_list(self, cfg):
        """必须可扩展：以后加类别只改配置。"""
        tc = cfg["multiline_edge_learning"]["target_classes"]
        assert isinstance(tc, list)
        assert tc == ["多股线"]

    def test_config_targets(self, cfg):
        t = cfg["multiline_edge_learning"]["targets"]
        assert set(t) == {"camera2", "camera7"}
        assert t["camera2"]["side"] == "right"
        assert t["camera7"]["side"] == "left"
        assert t["camera2"]["band_px"] == 350
        assert t["camera7"]["band_px"] == 350

    def test_image_width_matches_size_conversion(self, cfg):
        assert int(cfg["multiline_edge_learning"]["image_width_px"]) == \
            int(cfg["size_conversion"]["image_width_px"])

    def test_learning_threshold_is_below_normal(self, cfg):
        lt = float(cfg["multiline_edge_learning"]["learning_threshold"])
        normal = float(cfg["class_confidence_thresholds"]["多股线"])
        assert lt < normal

    def test_settings_exports_it(self):
        from AnomalyDetection_Tool.config.settings import MULTILINE_EDGE_LEARNING
        assert MULTILINE_EDGE_LEARNING.get("target_classes") == ["多股线"]

    def test_pipeline_scopes_threshold_to_zone(self):
        with open(os.path.join(ROOT, "AnomalyDetection_Tool", "core",
                               "detection_pipeline.py"), encoding="utf-8") as f:
            src = f.read()
        body = src[src.index("def _filter_boxes"):]
        # 必须把坐标传进去，否则无法判断是否在学习区内
        assert "learning_threshold_for(\n" \
               "                        camera_name, box.class_name, " \
               "box.x1, box.x2)" in body
        assert "box.threshold_used = thr" in body

    def test_manager_hooks_before_area_analysis(self):
        with open(os.path.join(ROOT, "AnomalyDetection_Tool", "core",
                               "detection_manager.py"), encoding="utf-8") as f:
            src = f.read()
        assert src.index("get_edge_learner()") < src.index("# 面积分析")

    def test_main_yolo_stop_hook_calls_end_roll_not_settle(self):
        """「停止」必须走 end_roll（清场），不能走任何结算路径。"""
        with open(os.path.join(ROOT, "main_yolo.py"), encoding="utf-8") as f:
            src = f.read()
        assert 'end_roll("前端停止")' in src
        assert "end_window" not in src, "旧的结算式收尾不该再出现"

    def test_old_module_is_gone(self):
        old = os.path.join(ROOT, "AnomalyDetection_Tool", "filter",
                           "multiline_edge_learner.py")
        assert not os.path.exists(old)

    def test_singleton_roundtrip(self):
        m = init_edge_learner(count_threshold=7)
        assert get_edge_learner() is m
        assert m.count_threshold == 7


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
