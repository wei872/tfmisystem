# -*- coding: utf-8 -*-
"""按类别覆盖学习长度 / 采样门槛（多股线 5 m / >5，短废纱线 50 m / >3）。

现场需求：加一个 短废纱线，要求"50 米内检测到 3 个就触发永久过滤"。
和现有多股线的 count_threshold 语义保持一致 —— 那是**严格大于**，
所以 3 意味着采到 4 个才生成生效区（已与现场确认）。

旧实现只有一个全局 window_length_mm，谁短谁先到点，然后 _settle 把
**所有类别**一起结算。短废纱线会在 5 m 就被拿去判定，而它要求 50 m
内凑够 3 个 —— 等于永远学不成。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from AnomalyDetection_Tool.filter.edge_region_learner import EdgeRegionLearner

W = 2440
BAND = 500
RIGHT_EDGE = W - BAND          # camera2 学习区左界：x2 >= 1940
TARGETS = {"camera2": {"side": "right", "band_px": BAND},
           "camera7": {"side": "left", "band_px": BAND}}
CLASSES = ["多股线", "短废纱线"]
RULES = {"短废纱线": {"window_length_mm": 50000, "count_threshold": 3}}


class _Dist:
    def __init__(self, mm=0.0):
        self.mm = mm

    def __call__(self):
        return self.mm


def _mk(dist=None, **kw):
    kw.setdefault("targets", dict(TARGETS))
    kw.setdefault("target_classes", list(CLASSES))
    kw.setdefault("count_threshold", 5)
    kw.setdefault("window_length_mm", 5000)
    kw.setdefault("region_padding_px", 50)
    kw.setdefault("image_width_px", W)
    kw.setdefault("class_rules", {k: dict(v) for k, v in RULES.items()})
    return EdgeRegionLearner(distance_provider=dist or _Dist(), **kw)


def _feed(m, cam, n, cls, x1=None, x2=None):
    """在学习区里塞 n 个框。"""
    if x1 is None:
        x1 = RIGHT_EDGE + 10 if cam == "camera2" else 10
    if x2 is None:
        x2 = x1 + 40
    for _ in range(n):
        m.observe(cam, cls, x1, x2)


# ======================================================================
# §1  参数按类别取，没覆盖的走全局
# ======================================================================
class TestPerClassParams:

    def test_overridden_class_uses_its_own_values(self):
        m = _mk()
        assert m.window_length_for("短废纱线") == 50000
        assert m.count_threshold_for("短废纱线") == 3

    def test_unlisted_class_keeps_global_values(self):
        m = _mk()
        assert m.window_length_for("多股线") == 5000
        assert m.count_threshold_for("多股线") == 5

    def test_unknown_class_falls_back_to_global(self):
        m = _mk()
        assert m.window_length_for("不存在的类") == 5000
        assert m.count_threshold_for("不存在的类") == 5

    def test_partial_override_keeps_the_other_field_global(self):
        m = _mk(class_rules={"短废纱线": {"count_threshold": 3}})
        assert m.count_threshold_for("短废纱线") == 3
        assert m.window_length_for("短废纱线") == 5000    # 走全局

    def test_no_class_rules_means_all_global(self):
        m = _mk(class_rules=None)
        assert m.class_rules == {}
        for c in CLASSES:
            assert m.window_length_for(c) == 5000
            assert m.count_threshold_for(c) == 5

    def test_bad_class_rules_is_ignored_not_fatal(self):
        m = _mk(class_rules="短废纱线")          # 配成字符串
        assert m.class_rules == {}
        assert m.window_length_for("短废纱线") == 5000

    def test_bad_field_falls_back_per_field(self):
        m = _mk(class_rules={"短废纱线": {"window_length_mm": "abc",
                                          "count_threshold": 3}})
        assert m.window_length_for("短废纱线") == 5000    # 坏的那项回落
        assert m.count_threshold_for("短废纱线") == 3     # 好的那项生效

    def test_explicit_override_equal_to_global_changes_nothing(self):
        """把类别显式列进 class_rules、但值和全局一样 => 行为必须完全不变。

        config.yaml 现在就是这么写 多股线 的，图的是配置自解释。
        """
        plain = _mk(class_rules=None)
        listed = _mk(class_rules={"多股线": {"window_length_mm": 5000,
                                             "count_threshold": 5}})
        for m in (plain, listed):
            assert m.window_length_for("多股线") == 5000
            assert m.count_threshold_for("多股线") == 5

        for m in (plain, listed):
            d = m._distance_provider
            d.mm = 0.0
            m.begin_window()
            _feed(m, "camera2", 6, "多股线")
            d.mm = 5000.0
            m.check_distance()
        assert plain.region_of("camera2", "多股线") == \
            listed.region_of("camera2", "多股线")

    def test_many_classes_can_each_have_their_own_rule(self):
        """现场以后加类别，直接往 class_rules 里补一段就行。"""
        d = _Dist(0.0)
        m = EdgeRegionLearner(
            targets=dict(TARGETS),
            target_classes=["多股线", "短废纱线", "小尾巴"],
            window_length_mm=5000, count_threshold=5,
            region_padding_px=50, image_width_px=W,
            class_rules={"多股线": {"window_length_mm": 8000,
                                    "count_threshold": 7},
                         "短废纱线": {"window_length_mm": 50000,
                                      "count_threshold": 3},
                         "小尾巴": {"window_length_mm": 20000}},
            distance_provider=d)
        assert (m.window_length_for("多股线"), m.count_threshold_for("多股线")) == (8000, 7)
        assert (m.window_length_for("短废纱线"), m.count_threshold_for("短废纱线")) == (50000, 3)
        # 只覆盖窗口，门槛走全局
        assert (m.window_length_for("小尾巴"), m.count_threshold_for("小尾巴")) == (20000, 5)

        m.begin_window()
        _feed(m, "camera2", 8, "多股线")
        _feed(m, "camera2", 4, "短废纱线")
        d.mm = 8000.0
        m.check_distance()
        assert m.region_of("camera2", "多股线") is not None
        assert m.region_of("camera2", "短废纱线") is None
        d.mm = 50000.0
        m.check_distance()
        assert m.region_of("camera2", "短废纱线") is not None

    def test_rule_for_class_not_in_target_classes_is_kept_but_inert(self):
        """写了覆盖但类别没进 target_classes：留着不生效，别炸。"""
        m = _mk(class_rules={"手工接头": {"count_threshold": 2}})
        assert "手工接头" in m.class_rules
        assert m.should_filter("camera2", "手工接头", 2000, 2100) is False


# ======================================================================
# §2  两个类别各算各的窗口 —— 这是本次改动的核心
# ======================================================================
class TestWindowsArePerClass:

    def test_multiline_settles_at_5m_without_dragging_the_other_class(self):
        """回归点：多股线 5 m 到点结算时，短废纱线不能被一起结算。"""
        d = _Dist(0.0)
        m = _mk(d)
        m.begin_window()
        _feed(m, "camera2", 6, "多股线")           # > 5，够
        _feed(m, "camera2", 10, "短废纱线")        # > 3，也够 —— 但窗口没到
        d.mm = 5000.0
        m.check_distance()

        assert m.region_of("camera2", "多股线") is not None, "多股线该结算了"
        assert m.region_of("camera2", "短废纱线") is None, \
            "短废纱线的窗口是 50 m，5 m 就结算等于永远学不成"

    def test_short_waste_settles_at_50m(self):
        d = _Dist(0.0)
        m = _mk(d)
        m.begin_window()
        _feed(m, "camera2", 10, "短废纱线")
        d.mm = 49999.0
        m.check_distance()
        assert m.region_of("camera2", "短废纱线") is None

        d.mm = 50000.0
        m.check_distance()
        assert m.region_of("camera2", "短废纱线") is not None

    def test_short_waste_needs_more_than_three(self):
        """>3 即 ≥4（与多股线 >5 即 ≥6 的语义一致，已与现场确认）。"""
        for n, expect in ((3, None), (4, "有生效区")):
            d = _Dist(0.0)
            m = _mk(d)
            m.begin_window()
            _feed(m, "camera2", n, "短废纱线")
            d.mm = 50000.0
            m.check_distance()
            got = m.region_of("camera2", "短废纱线")
            if expect is None:
                assert got is None, f"采到 {n} 个不该生成生效区"
            else:
                assert got is not None, f"采到 {n} 个该生成生效区"

    def test_multiline_still_needs_more_than_five(self):
        """别改坏现有多股线的门槛。"""
        for n, expect in ((5, None), (6, "有生效区")):
            d = _Dist(0.0)
            m = _mk(d)
            m.begin_window()
            _feed(m, "camera2", n, "多股线")
            d.mm = 5000.0
            m.check_distance()
            got = m.region_of("camera2", "多股线")
            assert (got is not None) == (expect is not None), n

    def test_each_camera_settles_independently_within_a_class(self):
        d = _Dist(0.0)
        m = _mk(d)
        m.begin_window()
        _feed(m, "camera2", 4, "短废纱线")     # 够
        _feed(m, "camera7", 3, "短废纱线")     # 不够
        d.mm = 50000.0
        m.check_distance()
        assert m.region_of("camera2", "短废纱线") is not None
        assert m.region_of("camera7", "短废纱线") is None


# ======================================================================
# §3  学习期（降阈值）也是按类别的
# ======================================================================
class TestLearningPeriodIsPerClass:

    def test_after_multiline_settles_its_threshold_is_no_longer_lowered(self):
        d = _Dist(0.0)
        m = _mk(d)
        m.begin_window()
        x = RIGHT_EDGE + 10
        assert m.learning_threshold_for("camera2", "多股线", x, x + 40) == 0.1
        assert m.learning_threshold_for("camera2", "短废纱线", x, x + 40) == 0.1

        d.mm = 5000.0
        m.check_distance()                       # 只有多股线结算
        assert m.learning_threshold_for("camera2", "多股线", x, x + 40) is None, \
            "多股线学完了还降阈值，会一直把区外噪声捞进来"
        assert m.learning_threshold_for("camera2", "短废纱线", x, x + 40) == 0.1

    def test_is_learning_takes_the_class(self):
        d = _Dist(0.0)
        m = _mk(d)
        m.begin_window()
        d.mm = 5000.0
        m.check_distance()
        assert m.is_learning("camera2", "多股线") is False
        assert m.is_learning("camera2", "短废纱线") is True
        # 不传类别 = 还有任何一个类在学
        assert m.is_learning("camera2") is True

        d.mm = 50000.0
        m.check_distance()
        assert m.is_learning("camera2") is False

    def test_settled_class_stops_sampling(self):
        d = _Dist(0.0)
        m = _mk(d)
        m.begin_window()
        _feed(m, "camera2", 6, "多股线")
        d.mm = 5000.0
        m.check_distance()
        before = len(m._samples[("camera2", "多股线")])
        _feed(m, "camera2", 5, "多股线")
        assert len(m._samples[("camera2", "多股线")]) == before


# ======================================================================
# §3.6  每个类别自己的学习区（targets 也可按类别调）
# ======================================================================
class TestPerClassTargets:
    """不同类别的学习区宽度/相机可以不同。

    合并语义是**按相机**的：某类的 targets 只覆盖它写了的相机，
    没写的相机回落到全局那份。
    """

    @staticmethod
    def _mk(dist=None, rules=None, classes=None, gtargets=None):
        return EdgeRegionLearner(
            targets=dict(gtargets or TARGETS),
            target_classes=list(classes or CLASSES),
            window_length_mm=5000, count_threshold=5,
            region_padding_px=50, image_width_px=W,
            class_rules=rules or {}, distance_provider=dist or _Dist())

    def test_class_uses_its_own_band_width(self):
        narrow = {"短废纱线": {"targets": {
            "camera2": {"side": "right", "band_px": 200},
            "camera7": {"side": "left", "band_px": 200}}}}
        m = self._mk(rules=narrow)
        # 短废纱线 的学习区左界是 2440-200=2240，多股线 仍是 2440-500=1940
        x = W - 300                      # 落在多股线区内、短废纱线区外
        assert m.in_learning_zone("camera2", x, x + 10, "多股线") is True
        assert m.in_learning_zone("camera2", x, x + 10, "短废纱线") is False

        x2 = W - 100                     # 两个类的区内
        assert m.in_learning_zone("camera2", x2, x2 + 10, "多股线") is True
        assert m.in_learning_zone("camera2", x2, x2 + 10, "短废纱线") is True

    def test_unnamed_camera_falls_back_to_global(self):
        """只写了 camera2 => camera7 回落全局，不能被弄丢。"""
        m = self._mk(rules={"短废纱线": {"targets": {
            "camera2": {"side": "right", "band_px": 200}}}})
        assert m.targets_for("短废纱线")["camera2"]["band_px"] == 200
        assert m.targets_for("短废纱线")["camera7"] == TARGETS["camera7"]
        # camera7 左带 500 px 仍然生效
        assert m.in_learning_zone("camera7", 10, 50, "短废纱线") is True

    def test_class_can_learn_on_a_camera_the_global_lacks(self):
        m = self._mk(rules={"短废纱线": {"targets": {
            "camera3": {"side": "left", "band_px": 300}}}})
        assert "camera3" in m.targets_for("短废纱线")
        assert m.in_learning_zone("camera3", 10, 50, "短废纱线") is True
        # 别的类别没这台相机的学习区
        assert m.in_learning_zone("camera3", 10, 50, "多股线") is False

    def test_omitting_a_camera_falls_back_not_drops(self):
        """合并语义：不写就是回落全局，**不是**去掉。想去掉要写 null。"""
        m = self._mk(rules={"短废纱线": {"targets": {
            "camera7": {"side": "left", "band_px": 500}}}})
        m.begin_window()
        assert "camera2" in m.targets_for("短废纱线"), \
            "省略 camera2 应该回落到全局，而不是把它去掉"
        assert m.observe("camera2", "短废纱线", W - 100, W - 60) is True

    def test_explicit_null_drops_that_camera(self):
        """要把某类从某台相机上摘掉，显式写 null。"""
        m = self._mk(rules={"短废纱线": {"targets": {
            "camera2": None,
            "camera7": {"side": "left", "band_px": 500}}}})
        m.begin_window()
        t = m.targets_for("短废纱线")
        assert "camera2" not in t
        assert "camera7" in t
        # camera2 上不再采样（照常按正常阈值报出）
        assert m.observe("camera2", "短废纱线", W - 100, W - 60) is False
        assert m.observe("camera7", "短废纱线", 10, 50) is True
        # 别的类别不受影响
        assert m.observe("camera2", "多股线", W - 100, W - 60) is True

    def test_dropping_every_camera_leaves_the_class_without_a_zone(self):
        m = self._mk(rules={"短废纱线": {"targets": {
            "camera2": None, "camera7": None}}})
        assert m.targets_for("短废纱线") == {}
        assert m.is_learning("camera2", "短废纱线") is False
        assert m.observe("camera2", "短废纱线", W - 100, W - 60) is False

    def test_class_without_targets_uses_global(self):
        m = self._mk(rules={"短废纱线": {"count_threshold": 3}})
        assert m.targets_for("短废纱线") == m.targets
        assert m.targets_for("多股线") == m.targets

    def test_side_can_differ_per_class(self):
        m = self._mk(rules={"短废纱线": {"targets": {
            "camera2": {"side": "left", "band_px": 400}}}})
        assert m.in_learning_zone("camera2", 10, 50, "短废纱线") is True
        assert m.in_learning_zone("camera2", 10, 50, "多股线") is False
        assert m.in_learning_zone("camera2", W - 100, W - 60, "短废纱线") is False

    def test_bad_per_class_targets_are_rejected(self):
        m = self._mk(rules={"短废纱线": {"targets": {
            "camera2": {"side": "up", "band_px": 200},     # side 非法
            "camera7": {"side": "left", "band_px": 0}}}})  # band 非法
        t = m.targets_for("短废纱线")
        # 两条都无效 -> 合并后退回全局那两台
        assert t["camera2"] == TARGETS["camera2"]
        assert t["camera7"] == TARGETS["camera7"]

    def test_is_learning_respects_per_class_targets(self):
        m = self._mk(rules={"短废纱线": {"targets": {
            "camera3": {"side": "left", "band_px": 300}}}})
        m.begin_window()
        assert m.is_learning("camera2", "多股线") is True
        # 加了 camera3，但 camera2 仍从全局回落 => 两台都在学
        assert m.is_learning("camera2", "短废纱线") is True
        assert m.is_learning("camera3", "短废纱线") is True

    def test_is_learning_false_when_camera_dropped_by_null(self):
        m = self._mk(rules={"短废纱线": {"targets": {"camera2": None}}})
        m.begin_window()
        assert m.is_learning("camera2", "短废纱线") is False
        assert m.is_learning("camera7", "短废纱线") is True

    def test_learning_threshold_follows_per_class_zone(self):
        m = self._mk(rules={"短废纱线": {"targets": {
            "camera2": {"side": "right", "band_px": 200},
            "camera7": {"side": "left", "band_px": 200}}}})
        m.begin_window()
        x = W - 300
        assert m.learning_threshold_for("camera2", "多股线", x, x + 10) == 0.1
        assert m.learning_threshold_for("camera2", "短废纱线", x, x + 10) is None

    def test_each_class_generates_its_own_region_from_its_own_zone(self):
        d = _Dist(0.0)
        m = self._mk(dist=d, rules={"短废纱线": {
            "count_threshold": 3,
            "targets": {"camera2": {"side": "right", "band_px": 200},
                        "camera7": {"side": "left", "band_px": 200}}}})
        m.begin_window()
        # 多股线：在 500 px 带的内侧（短废纱线区外）
        _feed(m, "camera2", 6, "多股线", x1=W - 400, x2=W - 360)
        # 短废纱线：在 200 px 带内
        _feed(m, "camera2", 4, "短废纱线", x1=W - 150, x2=W - 110)
        d.mm = 5000.0
        m.check_distance()
        r1 = m.region_of("camera2", "多股线")
        assert r1 is not None
        d.mm = 50000.0
        m.check_distance()
        r2 = m.region_of("camera2", "短废纱线")
        assert r2 is not None
        # 两个生效区来自不同的采样带，不该重合
        assert r1 != r2
        # 多股线的生效区滤不掉短废纱线
        assert m.should_filter("camera2", "多股线", r1[0] + 5, r1[0] + 15) is True
        assert m.should_filter("camera2", "短废纱线", r1[0] + 5, r1[0] + 15) is False

    def test_snapshot_exposes_per_class_targets(self):
        m = self._mk(rules={"短废纱线": {"targets": {
            "camera2": {"side": "right", "band_px": 200}}}})
        snap = m.snapshot()
        assert snap["per_class"]["短废纱线"]["targets"]["camera2"]["band_px"] == 200
        assert snap["per_class"]["多股线"]["targets"]["camera2"]["band_px"] == BAND


# ======================================================================
# §3.5  学习期采样的框不保存、不上传（对 target_classes 里任何类别都成立）
# ======================================================================
class TestLearningSamplesAreNotUploaded:
    """detection_manager 在面积分析**之前**丢掉 observe() 返回 True 的框，
    所以它们不进停机判定、不存图、不上传。

    判定完全由 target_classes 驱动，没有任何硬编码类别 ——
    往 target_classes 加新类别，它自动享有同样待遇。这条必须锁住。
    """

    @staticmethod
    def _mk_multi(dist, classes):
        return EdgeRegionLearner(
            targets=dict(TARGETS), target_classes=list(classes),
            window_length_mm=5000, count_threshold=5,
            region_padding_px=50, image_width_px=W,
            class_rules={"短废纱线": {"window_length_mm": 50000,
                                      "count_threshold": 3}},
            distance_provider=dist)

    def test_every_target_class_is_suppressed_in_the_zone(self):
        """含一个配置里还没有的新类别 —— 证明不是硬编码。"""
        d = _Dist(0.0)
        m = self._mk_multi(d, ["多股线", "短废纱线", "小尾巴"])
        m.begin_window()
        x = RIGHT_EDGE + 10
        for cls in ("多股线", "短废纱线", "小尾巴"):
            assert m.observe("camera2", cls, x, x + 40) is True, cls

    def test_non_target_class_is_never_suppressed(self):
        d = _Dist(0.0)
        m = self._mk_multi(d, ["多股线", "短废纱线"])
        m.begin_window()
        x = RIGHT_EDGE + 10
        for cls in ("棉团", "蚊虫小黑点", "油点线"):
            assert m.observe("camera2", cls, x, x + 40) is False, cls

    def test_target_class_outside_the_zone_is_never_suppressed(self):
        d = _Dist(0.0)
        m = self._mk_multi(d, ["多股线", "短废纱线"])
        m.begin_window()
        for cls in ("多股线", "短废纱线"):
            assert m.observe("camera2", cls, 100, 140) is False, cls

    def test_both_learning_bands_are_suppressed(self):
        d = _Dist(0.0)
        m = self._mk_multi(d, ["短废纱线"])
        m.begin_window()
        assert m.observe("camera2", "短废纱线", W - BAND + 10, W - BAND + 50) is True
        assert m.observe("camera7", "短废纱线", 10, 50) is True
        # camera2 的左边不是学习区
        assert m.observe("camera2", "短废纱线", 10, 50) is False

    def test_suppression_follows_the_class_own_window(self):
        """学习期抑制的时长 = 该类自己的窗口，不是全局窗口。

        多股线 5 m 就恢复上报；短废纱线要等到 50 m。
        """
        d = _Dist(0.0)
        m = self._mk_multi(d, ["多股线", "短废纱线"])
        m.begin_window()
        x = RIGHT_EDGE + 10

        d.mm = 0.0
        assert m.observe("camera2", "多股线", x, x + 40) is True
        assert m.observe("camera2", "短废纱线", x, x + 40) is True

        d.mm = 5000.0
        m.check_distance()
        assert m.observe("camera2", "多股线", x, x + 40) is False, \
            "多股线学完了就该恢复上报"
        assert m.observe("camera2", "短废纱线", x, x + 40) is True, \
            "短废纱线的窗口是 50 m，还在攒样本"

        d.mm = 50000.0
        m.check_distance()
        assert m.observe("camera2", "短废纱线", x, x + 40) is False

    def test_manager_drops_them_before_area_analysis(self):
        """接线：observe/should_filter 必须在"面积分析"之前，
        否则被滤掉的框会进停机判定、会存图、会上传。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "AnomalyDetection_Tool", "core",
                               "detection_manager.py"), encoding="utf-8") as f:
            src = f.read()
        i_filter = src.index("_el.should_filter(")
        i_observe = src.index("_el.observe(")
        i_area = src.index("# 面积分析")
        assert i_filter < i_area and i_observe < i_area
        # 被丢掉的框不能留在 result.boxes 里
        i_keep = src.index("result.boxes = keep")
        assert i_keep < i_area
        # 丢掉后 total_boxes / has_detection 必须在**同一处**跟着改，
        # 否则下游仍按"有检出"处理。
        #
        # 这里必须锚定到 result.boxes = keep 之后的一小段，不能整文件搜
        # 子串：`result.has_detection = result.total_boxes > 0` 在本文件
        # 里出现两次（:548 和 :580），整文件搜的话改掉 :580 也照样通过，
        # 断言等于空跑。
        tail = src[i_keep:i_keep + 400]
        assert "result.total_boxes = len(keep)" in tail, tail
        assert "result.has_detection = result.total_boxes > 0" in tail, tail


# ======================================================================
# §4  换卷重新学
# ======================================================================
class TestPerRollReset:

    def test_new_roll_resets_settled_and_regions(self):
        d = _Dist(0.0)
        m = _mk(d)
        m.begin_window()
        _feed(m, "camera2", 6, "多股线")
        d.mm = 5000.0
        m.check_distance()
        assert m.region_of("camera2", "多股线") is not None

        m.begin_window("前端开始")
        assert m.region_of("camera2", "多股线") is None
        assert m.is_learning("camera2", "多股线") is True

    def test_end_roll_clears_everything(self):
        d = _Dist(0.0)
        m = _mk(d)
        m.begin_window()
        _feed(m, "camera2", 4, "短废纱线")
        d.mm = 50000.0
        m.check_distance()
        assert m.region_of("camera2", "短废纱线") is not None

        m.end_roll("前端停止")
        assert m.region_of("camera2", "短废纱线") is None
        assert m.is_learning("camera2", "短废纱线") is False

    def test_region_is_class_scoped(self):
        """短废纱线学到的区绝不能拿去屏蔽多股线。"""
        d = _Dist(0.0)
        m = _mk(d)
        m.begin_window()
        _feed(m, "camera2", 4, "短废纱线")
        d.mm = 50000.0
        m.check_distance()
        reg = m.region_of("camera2", "短废纱线")
        assert reg is not None
        x = reg[0] + 5
        assert m.should_filter("camera2", "短废纱线", x, x + 10) is True
        assert m.should_filter("camera2", "多股线", x, x + 10) is False


# ======================================================================
# §5  snapshot 要能看出每个类别的参数和状态
# ======================================================================
class TestSnapshot:

    def test_snapshot_exposes_per_class(self):
        d = _Dist(0.0)
        m = _mk(d)
        m.begin_window()
        d.mm = 5000.0
        m.check_distance()
        snap = m.snapshot()
        pc = snap["per_class"]
        assert pc["多股线"]["window_length_mm"] == 5000
        assert pc["多股线"]["count_threshold"] == 5
        assert pc["多股线"]["settled"] is True
        assert pc["短废纱线"]["window_length_mm"] == 50000
        assert pc["短废纱线"]["count_threshold"] == 3
        assert pc["短废纱线"]["settled"] is False
        assert snap["class_rules"]["短废纱线"]["window_length_mm"] == 50000
