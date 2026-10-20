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
