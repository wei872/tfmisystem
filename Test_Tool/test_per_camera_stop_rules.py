#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按相机覆盖的**停机规则**测试。

背景：检测阈值已经能按相机覆盖（见 test_per_camera_thresholds.py），但停机
规则（machine_stop_rules）一直是按类别的全局值。现场要求停机也能按相机调。

这一层比检测层风险高一个量级——配错会真的停机或真的不停机，所以本文件的
重点不是"正常路径能跑"，而是：

1. **不配覆盖时行为必须与改动前逐字节一致**（§1）。老调用点一个都不能受影响。
2. **坏配置必须安全回落**，不能静默改变停机（§4）。尤其是 T16 踩过的那个坑：
   YAML 少写一个冒号 → 整块变成字符串 → 不报错但配置完全失效。
3. **改动能被看见**（§7/§8）：启动日志必须列出所有与全局不同的项。

跑法::

    python -m pytest Test_Tool/test_per_camera_stop_rules.py -v
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402
import yaml  # noqa: E402

import AnomalyDetection_Tool.config.settings as st  # noqa: E402


# ------------------------------------------------------------------
# 公共夹具
# ------------------------------------------------------------------

# 一份最小的、贴近现场的停机规则，避免依赖 config.yaml 的具体数值
RULES = {
    "enabled": True,
    "save_to_folder": True,
    "trigger_relay": True,
    "default_rule": None,
    "rules": {
        "多股线": {
            "enabled": True,
            "trigger_mode": "dimension",
            "min_confidence": 0.70,
            "min_width_mm": 30.0,
            "min_height_mm": 30.0,
        },
        "飘絮": {
            "enabled": True,
            "min_confidence": 0.1,
            "min_area_mm2": 130,
        },
        "蚊虫小黑点": {"enabled": False},
    },
}


@pytest.fixture
def no_override(monkeypatch):
    """把按相机覆盖清空——测"没配覆盖"的路径"""
    monkeypatch.setattr(st, "CAMERA_STOP_RULE_OVERRIDES", {})


@pytest.fixture
def with_override(monkeypatch):
    """注入一份按相机覆盖，供各测试改写后使用"""
    box = {}
    monkeypatch.setattr(st, "CAMERA_STOP_RULE_OVERRIDES", box)
    return box


# ==================================================================
# §1 向后兼容：不传 camera_name / 没配覆盖时，行为必须与改动前一致
# ==================================================================

class TestBackwardCompatible:

    def test_no_camera_name_returns_global_rule(self, no_override):
        """camera_name=None 必须返回全局规则——老调用点靠这条活着"""
        r = st.effective_stop_rule("多股线", None, stop_rules=RULES)
        assert r == RULES["rules"]["多股线"]

    def test_unknown_camera_behaves_like_global(self, no_override):
        """没配过的相机名 = 全局"""
        assert st.effective_stop_rule("多股线", "camera99",
                                      stop_rules=RULES) == \
            RULES["rules"]["多股线"]

    def test_returns_copy_not_the_config_dict(self, no_override):
        """返回副本：调用方改它不能污染配置本体。

        改动前 get_stop_rule 返回的是配置里的**同一个** dict，现在返回副本。
        已确认全仓库没有任何地方写入返回的 rule（grep 过），所以这是安全的；
        但必须钉住"是副本"这个性质，否则将来有人改了返回值会静默改配置。
        """
        r = st.effective_stop_rule("多股线", None, stop_rules=RULES)
        r["min_confidence"] = 0.01
        assert RULES["rules"]["多股线"]["min_confidence"] == 0.70

    def test_disabled_class_returns_none(self, no_override):
        assert st.effective_stop_rule("蚊虫小黑点", None, stop_rules=RULES) is None

    def test_unknown_class_returns_none(self, no_override):
        assert st.effective_stop_rule("不存在的类", None, stop_rules=RULES) is None

    def test_master_switch_off_beats_everything(self, with_override):
        """总开关关着，任何按相机覆盖都不能让机器停下来"""
        with_override["camera1"] = {
            "rules": {"多股线": {"enabled": True, "min_confidence": 0.0}}}
        off = dict(RULES, enabled=False)
        assert st.effective_stop_rule("多股线", "camera1", stop_rules=off) is None
        assert st.effective_stop_rule("多股线", None, stop_rules=off) is None

    def test_missing_enabled_key_means_off(self, no_override):
        """配置里整个 machine_stop_rules 段缺失时按"不停机"处理，不能抛异常。

        改动前这里是 MACHINE_STOP_RULES["enabled"]，缺键会 KeyError。
        """
        assert st.effective_stop_rule("多股线", "camera1", stop_rules={}) is None


# ==================================================================
# §2 按相机逐键覆盖
# ==================================================================

class TestPerCameraOverride:

    def test_single_key_override_keeps_other_keys(self, with_override):
        """逐键覆盖的核心：只写 min_confidence，宽高不能丢。

        如果实现成"整块替换"，min_width_mm/min_height_mm 会消失，
        dimension 判定退化成只看置信度——这是最容易配出来的事故。
        """
        with_override["camera1"] = {"rules": {"多股线": {"min_confidence": 0.5}}}
        r = st.effective_stop_rule("多股线", "camera1", stop_rules=RULES)
        assert r["min_confidence"] == 0.5
        assert r["min_width_mm"] == 30.0
        assert r["min_height_mm"] == 30.0
        assert r["trigger_mode"] == "dimension"
        assert r["enabled"] is True

    def test_override_does_not_leak_to_other_cameras(self, with_override):
        with_override["camera1"] = {"rules": {"多股线": {"min_confidence": 0.5}}}
        assert st.effective_stop_rule(
            "多股线", "camera2", stop_rules=RULES)["min_confidence"] == 0.70
        assert st.effective_stop_rule(
            "多股线", None, stop_rules=RULES)["min_confidence"] == 0.70

    def test_override_dimension_fields(self, with_override):
        with_override["camera3"] = {"rules": {"多股线": {"min_width_mm": 45.0}}}
        r = st.effective_stop_rule("多股线", "camera3", stop_rules=RULES)
        assert r["min_width_mm"] == 45.0
        assert r["min_confidence"] == 0.70       # 没写的键沿用全局

    def test_override_area_rule(self, with_override):
        with_override["camera4"] = {
            "rules": {"飘絮": {"min_area_mm2": 200, "min_confidence": 0.3}}}
        r = st.effective_stop_rule("飘絮", "camera4", stop_rules=RULES)
        assert r == {"enabled": True, "min_confidence": 0.3, "min_area_mm2": 200}

    def test_override_trigger_mode(self, with_override):
        """trigger_mode 也能按相机改（area ↔ dimension）"""
        with_override["camera5"] = {"rules": {"飘絮": {"trigger_mode": "dimension"}}}
        r = st.effective_stop_rule("飘絮", "camera5", stop_rules=RULES)
        assert r["trigger_mode"] == "dimension"

    def test_camera_can_disable_a_class(self, with_override):
        """某台相机上关掉某个类的停机——这是最常见的需求之一"""
        with_override["camera6"] = {"rules": {"多股线": {"enabled": False}}}
        assert st.effective_stop_rule("多股线", "camera6", stop_rules=RULES) is None
        # 别的相机照旧
        assert st.effective_stop_rule("多股线", "camera1", stop_rules=RULES) is not None

    def test_camera_can_enable_a_globally_disabled_class(self, with_override):
        """反过来也行：全局关掉的类，某台相机单独打开"""
        with_override["camera7"] = {
            "rules": {"蚊虫小黑点": {"enabled": True, "min_confidence": 0.9}}}
        r = st.effective_stop_rule("蚊虫小黑点", "camera7", stop_rules=RULES)
        assert r is not None
        assert r["min_confidence"] == 0.9

    def test_camera_can_add_a_class_with_no_global_rule(self, with_override):
        """全局没有这个类的规则，也能纯按相机新增一条"""
        with_override["camera8"] = {"rules": {
            "新缺陷": {"enabled": True, "trigger_mode": "area",
                      "min_confidence": 0.6, "min_area_mm2": 50}}}
        r = st.effective_stop_rule("新缺陷", "camera8", stop_rules=RULES)
        assert r["min_confidence"] == 0.6
        # 别的相机上仍然没有
        assert st.effective_stop_rule("新缺陷", "camera1", stop_rules=RULES) is None

    def test_get_camera_stop_rule_override_shape(self, with_override):
        with_override["camera1"] = {"rules": {"多股线": {"min_confidence": 0.5}}}
        assert st.get_camera_stop_rule_override("camera1", "多股线") == \
            {"min_confidence": 0.5}
        assert st.get_camera_stop_rule_override("camera1", "飘絮") is None
        assert st.get_camera_stop_rule_override("camera2", "多股线") is None
        assert st.get_camera_stop_rule_override(None, "多股线") is None
        assert st.get_camera_stop_rule_override("camera1", None) is None
        assert st.get_camera_stop_rule_override("camera1", "") is None


# ==================================================================
# §3 有效停机门槛与检测阈值的联动
# ==================================================================

class TestGateInteraction:

    def test_gate_is_max_of_detection_and_stop(self):
        assert st.effective_stop_gate(0.65, 0.70) == 0.70
        assert st.effective_stop_gate(0.85, 0.50) == 0.85

    def test_lowering_stop_threshold_below_detection_is_a_noop(self):
        """检测阈值把关时，降停机阈值不改变有效门槛。

        这正是"两边都能按相机调"带来的新情况：光看停机阈值会以为停机变松了。
        """
        before = st.effective_stop_gate(0.85, 0.50)   # 油点线：检测把关
        after = st.effective_stop_gate(0.85, 0.20)
        assert before == after == 0.85

    def test_raising_stop_threshold_above_detection_takes_over(self):
        before = st.effective_stop_gate(0.85, 0.50)
        after = st.effective_stop_gate(0.85, 0.95)
        assert before == 0.85 and after == 0.95


# ==================================================================
# §4 坏配置必须安全回落
# ==================================================================

class TestBadConfigSafety:

    def test_string_block_falls_back_to_global(self, with_override):
        """T16 踩过的坑：YAML 少写一个冒号 → 整块变成字符串。

        必须当作"没配覆盖"回落全局，绝不能因为拿不到 dict 就变成不停机。
        """
        with_override["camera1"] = {
            "rules": {"多股线": "enabled: true min_confidence: 0.5"}}
        assert st.effective_stop_rule("多股线", "camera1", stop_rules=RULES) == \
            RULES["rules"]["多股线"]

    def test_whole_rules_section_as_string(self, with_override):
        with_override["camera1"] = "rules: 多股线: ..."
        assert st.effective_stop_rule("多股线", "camera1", stop_rules=RULES) == \
            RULES["rules"]["多股线"]

    def test_camera_block_none(self, with_override):
        with_override["camera1"] = None
        assert st.effective_stop_rule("多股线", "camera1", stop_rules=RULES) == \
            RULES["rules"]["多股线"]

    def test_non_numeric_value_is_dropped(self, with_override):
        """非法数值只丢这一项，其余覆盖照常生效。

        不能因为一个笔误让整块覆盖静默消失——那样更难查。
        """
        with_override["camera1"] = {"rules": {"多股线": {
            "min_confidence": "很高", "min_width_mm": 45.0}}}
        r = st.effective_stop_rule("多股线", "camera1", stop_rules=RULES)
        assert r["min_confidence"] == 0.70      # 回落全局
        assert r["min_width_mm"] == 45.0        # 这一项生效

    def test_unknown_key_is_ignored(self, with_override):
        """白名单外的键（笔误）一律忽略，不能悄悄进入生效规则"""
        with_override["camera1"] = {"rules": {"多股线": {
            "min_confidance": 0.1, "min_confidence": 0.5}}}
        r = st.effective_stop_rule("多股线", "camera1", stop_rules=RULES)
        assert "min_confidance" not in r
        assert r["min_confidence"] == 0.5

    def test_bad_global_rule_type_is_ignored(self, no_override):
        """全局规则写成 None/字符串时不能崩，按"没有全局规则"处理"""
        broken = {"enabled": True, "rules": {"多股线": None}}
        assert st.effective_stop_rule("多股线", "camera1",
                                      stop_rules=broken) is None
        broken2 = {"enabled": True, "rules": {"多股线": "oops"}}
        assert st.effective_stop_rule("多股线", "camera1",
                                      stop_rules=broken2) is None

    def test_stop_rules_object_raising_is_contained(self, no_override):
        """读配置抛异常也不能把检测循环带崩——按不停机处理"""
        class Boom(dict):
            def get(self, *a, **k):
                raise RuntimeError("boom")
        assert st.effective_stop_rule("多股线", "camera1",
                                      stop_rules=Boom()) is None


# ==================================================================
# §5 签名探测：两种 stop_conf_lookup 都必须能用
# ==================================================================

class TestLookupArity:

    def test_one_arg_lambda(self):
        assert st._stop_conf_lookup_takes_camera(lambda cls: 0.5) is False

    def test_two_arg_lambda(self):
        assert st._stop_conf_lookup_takes_camera(lambda cls, cam: 0.5) is True

    def test_default_second_arg_counts_as_two(self):
        assert st._stop_conf_lookup_takes_camera(lambda cls, cam=None: 0.5) is True

    def test_star_args(self):
        assert st._stop_conf_lookup_takes_camera(lambda *a: 0.5) is True

    def test_builtin_without_signature_is_treated_as_one_arg(self):
        """取不到签名时保守按老签名调，不能因为探测失败就抛异常。

        `iter` / `type` 是真正取不到签名的内建对象（`print` 不行——它有
        ``*args``，会被判成两参数）。
        """
        assert st._stop_conf_lookup_takes_camera(iter) is False
        assert st._stop_conf_lookup_takes_camera(type) is False

    def test_signature_probe_raising_is_contained(self, monkeypatch):
        """inspect.signature 自己抛异常也不能把启动流程带崩"""
        def boom(fn):
            raise RuntimeError("no signature for you")
        monkeypatch.setattr(st.inspect, "signature", boom)
        assert st._stop_conf_lookup_takes_camera(lambda cls, cam: 0.5) is False

    def test_cross_check_works_with_one_arg_lookup(self, monkeypatch):
        """T15 的老调用方式仍然正确"""
        monkeypatch.setattr(st, "CAMERA_THRESHOLD_OVERRIDES", {
            "camera1": {"class_confidence_thresholds": {"油点线": 0.3}}})
        changed = st.check_camera_threshold_stop_impact(
            ["camera1"], {"油点线": 0.85}, 0.1, lambda cls: 0.5)
        assert len(changed) == 1
        assert changed[0]["camera"] == "camera1"
        assert changed[0]["gate_before"] == 0.85
        assert changed[0]["gate_after"] == 0.5

    def test_per_camera_stop_conf_actually_used(self, monkeypatch):
        """这一条是"按相机停机值真的被用上了"的判据。

        同一份检测阈值覆盖（全局 0.6 → camera1 0.8），只改注入的停机查询：

        * 老签名 ``(cls)`` 返回全局停机值 0.5：
          门槛 max(0.6,0.5)=0.6 → max(0.8,0.5)=0.8，**会报警**。
        * 新签名 ``(cls, cam)`` 返回该相机停机值 0.9：
          门槛 max(0.6,0.9)=0.9 → max(0.8,0.9)=0.9，**不该报警**。

        两次结果必须不同——相同就说明相机名没被传进去，交叉校验会拿全局
        停机值去比，给出错误的结论。
        """
        monkeypatch.setattr(st, "CAMERA_THRESHOLD_OVERRIDES", {
            "camera1": {"class_confidence_thresholds": {"油点线": 0.8}}})
        glob_det = {"油点线": 0.6}

        old = st.check_camera_threshold_stop_impact(
            ["camera1"], glob_det, 0.1, lambda cls: 0.5)
        new = st.check_camera_threshold_stop_impact(
            ["camera1"], glob_det, 0.1,
            lambda cls, cam: 0.9 if cam == "camera1" else 0.5)

        assert len(old) == 1, f"老签名应当报警: {old}"
        assert old[0]["gate_before"] == 0.6 and old[0]["gate_after"] == 0.8
        assert new == [], f"新签名下门槛没变(0.9→0.9)，不该报警: {new}"

    def test_reported_numbers_come_from_the_camera(self, monkeypatch):
        """报警里印出来的停机值和门槛必须来自**该相机**，不是全局。

        注意这个函数的语义：它只隔离"检测阈值覆盖"的影响，两次计算用的是
        **同一个**停机值，所以停机值本身变了不会让它报警（那是
        check_camera_stop_rule_impact 的职责）。这里验证的是：当该相机停机值
        与全局不同时，报出来的 stop_conf / gate_before 必须是该相机的那份。

        检测 0.6→0.8；全局停机 0.5，camera2 停机 0.7：
          * 按相机（0.7）：门槛 0.7 → 0.8
          * 按全局（0.5）：门槛 0.6 → 0.8
        两者都会报警，但 gate_before 不同——不同才说明相机名真的传进去了。
        """
        monkeypatch.setattr(st, "CAMERA_THRESHOLD_OVERRIDES", {
            "camera2": {"class_confidence_thresholds": {"油点线": 0.8}}})
        glob_det = {"油点线": 0.6}

        per_cam = st.check_camera_threshold_stop_impact(
            ["camera2"], glob_det, 0.1,
            lambda cls, cam: 0.7 if cam == "camera2" else 0.5)
        per_glob = st.check_camera_threshold_stop_impact(
            ["camera2"], glob_det, 0.1, lambda cls: 0.5)

        assert len(per_cam) == 1 and len(per_glob) == 1
        assert per_cam[0]["stop_conf"] == 0.7
        assert per_cam[0]["gate_before"] == 0.7
        assert per_glob[0]["stop_conf"] == 0.5
        assert per_glob[0]["gate_before"] == 0.6
        assert per_cam[0]["gate_after"] == per_glob[0]["gate_after"] == 0.8

    def test_stop_change_is_not_this_functions_job(self, monkeypatch):
        """钉住语义边界：停机值本身变化不由本函数报告。

        检测阈值覆盖前后都低于该相机停机值 0.95，有效门槛恒为 0.95，
        所以这里必须返回空——停机值的变化由 check_camera_stop_rule_impact 报。
        把两件事混在一起会让启动日志给出误导性的结论。
        """
        monkeypatch.setattr(st, "CAMERA_THRESHOLD_OVERRIDES", {
            "camera2": {"class_confidence_thresholds": {"油点线": 0.85}}})
        assert st.check_camera_threshold_stop_impact(
            ["camera2"], {"油点线": 0.6}, 0.1,
            lambda cls, cam: 0.95 if cam == "camera2" else 0.5) == []


# ==================================================================
# §6 check_camera_stop_rule_impact —— 停机规则本身的差异
# ==================================================================

class TestStopRuleImpact:

    def test_no_override_no_impact(self, no_override):
        assert st.check_camera_stop_rule_impact(
            ["camera1", "camera2"], stop_rules=RULES) == []

    def test_identical_values_report_nothing(self, with_override):
        """展开成与全局相同的值（出厂配置就是这样）不算差异"""
        with_override["camera1"] = {
            "rules": {"多股线": dict(RULES["rules"]["多股线"])}}
        assert st.check_camera_stop_rule_impact(
            ["camera1"], stop_rules=RULES) == []

    def test_detects_min_confidence_change(self, with_override):
        with_override["camera1"] = {"rules": {"多股线": {"min_confidence": 0.5}}}
        out = st.check_camera_stop_rule_impact(["camera1"], stop_rules=RULES)
        assert len(out) == 1
        assert out[0]["camera"] == "camera1"
        assert out[0]["class_name"] == "多股线"
        assert out[0]["field"] == "min_confidence"
        assert out[0]["global_value"] == 0.70
        assert out[0]["camera_value"] == 0.5
        assert out[0]["effect"] == "停机会变频繁"

    def test_raising_threshold_means_less_stopping(self, with_override):
        with_override["camera1"] = {"rules": {"多股线": {"min_confidence": 0.95}}}
        out = st.check_camera_stop_rule_impact(["camera1"], stop_rules=RULES)
        assert out[0]["effect"] == "停机会变少"

    def test_detects_enabled_flip(self, with_override):
        with_override["camera1"] = {"rules": {"多股线": {"enabled": False}}}
        out = st.check_camera_stop_rule_impact(["camera1"], stop_rules=RULES)
        assert len(out) == 1
        assert out[0]["field"] == "enabled"
        assert out[0]["effect"] == "该类不再停机"

    def test_detects_size_change_direction(self, with_override):
        with_override["camera1"] = {"rules": {"多股线": {"min_width_mm": 10.0}}}
        out = st.check_camera_stop_rule_impact(["camera1"], stop_rules=RULES)
        assert out[0]["effect"] == "停机会变频繁"     # 尺寸门槛降低 → 更容易停

    def test_detects_trigger_mode_change(self, with_override):
        with_override["camera1"] = {"rules": {"飘絮": {"trigger_mode": "dimension"}}}
        out = st.check_camera_stop_rule_impact(["camera1"], stop_rules=RULES)
        assert out[0]["field"] == "trigger_mode"
        assert "area→dimension" in out[0]["effect"]

    def test_multiple_cameras_and_fields(self, with_override):
        with_override["camera1"] = {
            "rules": {"多股线": {"min_confidence": 0.5, "min_width_mm": 20.0}}}
        with_override["camera2"] = {"rules": {"飘絮": {"min_area_mm2": 999}}}
        out = st.check_camera_stop_rule_impact(
            ["camera1", "camera2"], stop_rules=RULES)
        assert len(out) == 3
        assert {o["camera"] for o in out} == {"camera1", "camera2"}

    def test_malformed_override_reported_as_no_impact_not_crash(self, with_override):
        with_override["camera1"] = "不是字典"
        assert st.check_camera_stop_rule_impact(
            ["camera1"], stop_rules=RULES) == []

    def test_effect_direction_table(self):
        """方向搞反会让人误判风险，所以逐条钉死"""
        e = st._stop_change_effect
        assert e("min_confidence", 0.7, 0.5) == "停机会变频繁"
        assert e("min_confidence", 0.7, 0.9) == "停机会变少"
        assert e("min_confidence", 0.7, 0.7) == "无变化"
        assert e("min_area_mm2", 130, 200) == "停机会变少"
        assert e("min_area_mm2", 130, 60) == "停机会变频繁"
        assert e("enabled", True, False) == "该类不再停机"
        assert e("enabled", False, True) == "该类会停机"
        assert e("trigger_mode", "area", "dimension") == \
            "停机判定方式 area→dimension"


# ==================================================================
# §6b 有效门槛换算 —— 停机字段变化 ≠ 有效门槛变化
# ==================================================================

class TestGateReporting:
    """停机字段变了不等于有效门槛变了那么多，日志必须报后者。

    这是本次实现里我自己写错过的一处：第一版只报"min_confidence 0.7→0.5，
    停机会变频繁"，但如果该相机检测阈值是 0.65，真实门槛只从 0.7 掉到 0.65。
    现场看到 0.5 会以为停机大幅放宽，实际没有。
    """

    @pytest.fixture(autouse=True)
    def _isolate_detection_overrides(self, monkeypatch):
        """把按相机**检测**覆盖清空。

        不清的话，仓库里真实 config.yaml 的 camera_threshold_overrides 会
        参与计算（camera3 的 多股线 是 0.65），把这里注入的检测阈值盖掉，
        断言就成了在测出厂配置而不是测这段逻辑。
        """
        monkeypatch.setattr(st, "CAMERA_THRESHOLD_OVERRIDES", {})

    def test_gate_shrinks_less_than_the_field(self, with_override, monkeypatch):
        """真实场景：全局 多股线 停机 0.7 / 检测 0.65，camera3 把停机降到 0.5。

        字段上是 0.7→0.5，但检测阈值 0.65 接管，真实门槛只到 0.65。
        """
        monkeypatch.setattr(st, "CLASS_CONFIDENCE_THRESHOLDS", {"多股线": 0.65})
        with_override["camera3"] = {"rules": {"多股线": {"min_confidence": 0.5}}}
        out = st.check_camera_stop_rule_impact(["camera3"], stop_rules=RULES)
        assert len(out) == 1
        c = out[0]
        assert c["effect"] == "停机会变频繁"
        assert c["det_threshold"] == 0.65
        assert c["gate_before"] == 0.70
        assert c["gate_after"] == 0.65, \
            f"门槛应被检测阈值 0.65 兜住，实际算成 {c['gate_after']}"
        assert "0.7→0.65" in c["gate_effect"]

    def test_gate_unchanged_when_detection_gates(self, with_override, monkeypatch):
        """检测阈值更高时，怎么降停机阈值都不改变有效门槛"""
        monkeypatch.setattr(st, "CLASS_CONFIDENCE_THRESHOLDS", {"多股线": 0.9})
        with_override["camera3"] = {"rules": {"多股线": {"min_confidence": 0.5}}}
        out = st.check_camera_stop_rule_impact(["camera3"], stop_rules=RULES)
        assert out[0]["gate_before"] == 0.9
        assert out[0]["gate_after"] == 0.9
        assert "不变" in out[0]["gate_effect"]

    def test_gate_reported_for_size_change_too(self, with_override, monkeypatch):
        """尺寸条件不影响门槛，但仍要把门槛现状报出来，便于对照"""
        monkeypatch.setattr(st, "CLASS_CONFIDENCE_THRESHOLDS", {"多股线": 0.65})
        with_override["camera3"] = {"rules": {"多股线": {"min_width_mm": 10.0}}}
        out = st.check_camera_stop_rule_impact(["camera3"], stop_rules=RULES)
        assert out[0]["field"] == "min_width_mm"
        assert out[0]["gate_before"] == out[0]["gate_after"] == 0.70
        assert "不变" in out[0]["gate_effect"]

    def test_gate_reported_for_enable_flip(self, with_override, monkeypatch):
        """禁用后不能报出"只剩检测阈值"的假门槛"""
        monkeypatch.setattr(st, "CLASS_CONFIDENCE_THRESHOLDS", {"多股线": 0.65})
        with_override["camera3"] = {"rules": {"多股线": {"enabled": False}}}
        out = st.check_camera_stop_rule_impact(["camera3"], stop_rules=RULES)
        assert out[0]["effect"] == "该类不再停机"
        assert out[0]["gate_before"] == 0.70
        assert out[0]["gate_after"] is None, \
            "规则没了就没有门槛，不能退化成检测阈值 0.65"
        assert "不再停机" in out[0]["gate_effect"]

    def test_gate_reported_for_reenable(self, with_override, monkeypatch):
        """反过来：全局禁用的类在某台相机上打开，要报出新门槛"""
        monkeypatch.setattr(st, "CLASS_CONFIDENCE_THRESHOLDS", {})
        with_override["camera7"] = {"rules": {
            "蚊虫小黑点": {"enabled": True, "min_confidence": 0.9}}}
        out = st.check_camera_stop_rule_impact(["camera7"], stop_rules=RULES)
        assert len(out) == 1
        assert out[0]["effect"] == "该类会停机"
        assert out[0]["gate_before"] is None
        assert out[0]["gate_after"] == 0.9
        assert "开始停机" in out[0]["gate_effect"]


class TestStopRuleMinConfidence:
    """隐式默认值必须与 StopMachineService 的兜底一致，否则算错门槛"""

    def test_explicit_value(self):
        assert st.stop_rule_min_confidence({"min_confidence": 0.56}) == 0.56

    def test_area_default_is_085(self):
        assert st.stop_rule_min_confidence({"enabled": True}) == 0.85
        assert st.stop_rule_min_confidence(
            {"trigger_mode": "area"}) == 0.85

    def test_dimension_default_is_05(self):
        assert st.stop_rule_min_confidence(
            {"trigger_mode": "dimension"}) == 0.5

    def test_non_numeric_falls_back_to_default(self):
        assert st.stop_rule_min_confidence(
            {"min_confidence": "很高", "trigger_mode": "dimension"}) == 0.5

    def test_non_dict_returns_none(self):
        assert st.stop_rule_min_confidence(None) is None
        assert st.stop_rule_min_confidence("oops") is None

    def test_empty_rule_has_no_gate(self):
        """空规则 = 该类不停机，不能凭空套出一个 0.85 的门槛。

        某台相机把某个类 enabled 设为 false 时，生效规则就是空 dict；
        若这里返回 0.85，启动日志会报出一个根本不存在的停机门槛。
        """
        assert st.stop_rule_min_confidence({}) is None
        assert st.effective_stop_gate(0.65, st.stop_rule_min_confidence({})) == 0.65

    def test_gate_effect_strings(self):
        assert st._gate_effect(0.7, 0.5) == "有效门槛 0.7→0.5，停机会变频繁"
        assert st._gate_effect(0.5, 0.9) == "有效门槛 0.5→0.9，停机会变少"
        assert st._gate_effect(0.7, 0.7) == "有效门槛不变（0.7）"
        assert "无法计算" in st._gate_effect(None, 0.5)
        assert "无法计算" in st._gate_effect(0.5, None)


# ==================================================================
# §7 describe_camera_stop_rules —— 启动打印用的生效表
# ==================================================================

class TestDescribe:

    def test_marks_overridden_classes(self, with_override):
        with_override["camera1"] = {"rules": {"多股线": {"min_confidence": 0.5}}}
        d = st.describe_camera_stop_rules("camera1", stop_rules=RULES)
        assert d["多股线"]["<camera_override>"] is True
        assert d["多股线"]["min_confidence"] == 0.5
        assert "<camera_override>" not in d["飘絮"]
        assert d["蚊虫小黑点"] is None          # 全局禁用

    def test_describe_does_not_mutate_config(self, with_override):
        with_override["camera1"] = {"rules": {"多股线": {"min_confidence": 0.5}}}
        st.describe_camera_stop_rules("camera1", stop_rules=RULES)
        assert "<camera_override>" not in RULES["rules"]["多股线"]

    def test_describe_includes_camera_only_class(self, with_override):
        with_override["camera1"] = {
            "rules": {"新缺陷": {"enabled": True, "min_confidence": 0.6}}}
        d = st.describe_camera_stop_rules("camera1", stop_rules=RULES)
        assert "新缺陷" in d and d["新缺陷"]["min_confidence"] == 0.6


# ==================================================================
# §8 真实入口 StopMachineService.get_stop_rule
# ==================================================================

class TestRealEntryPoint:
    """settings 里的逻辑对了不代表接上线了，这里测生产代码真正调的那个函数"""

    @pytest.fixture(autouse=True)
    def _import_service(self, monkeypatch):
        from AnomalyDetection_Tool.services.stop_machine_service import (
            StopMachineService,
        )
        self.svc = StopMachineService
        # 把全局停机规则换成夹具里那份，避免依赖 config.yaml 的具体数值
        monkeypatch.setattr(st, "MACHINE_STOP_RULES", RULES)
        monkeypatch.setattr(st, "CAMERA_STOP_RULE_OVERRIDES", {})
        yield
        monkeypatch.undo()

    def test_no_camera_name_unchanged(self):
        assert self.svc.get_stop_rule("多股线") == RULES["rules"]["多股线"]

    def test_camera_name_plumbed_through(self, monkeypatch):
        monkeypatch.setattr(st, "CAMERA_STOP_RULE_OVERRIDES", {
            "camera1": {"rules": {"多股线": {"min_confidence": 0.5}}}})
        assert self.svc.get_stop_rule("多股线", "camera1")["min_confidence"] == 0.5
        assert self.svc.get_stop_rule("多股线", "camera2")["min_confidence"] == 0.70

    def test_disabled_returns_none(self):
        assert self.svc.get_stop_rule("蚊虫小黑点", "camera1") is None


# ==================================================================
# §9 出厂 config.yaml 校验
# ==================================================================
# 这一节校验的是仓库里真实的 config.yaml，不是构造出来的数据。
# 目的：8 台已展开且初始值 = 全局，所以**行为必须与不配置时完全一致**。
# 改了任何一台的数，这里就会失败——这是故意的，提醒你去启动日志确认影响。

@pytest.fixture(scope="module")
def shipped():
    path = os.path.join(_ROOT, "config.yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


class TestShippedConfig:

    def test_section_exists_and_is_dict(self, shipped):
        ovr = shipped.get("camera_stop_rule_overrides")
        assert isinstance(ovr, dict), \
            f"camera_stop_rule_overrides 不是 dict（实际 {type(ovr).__name__}）——" \
            "多半是 YAML 少写了冒号，整块被解析成了字符串"
        assert sorted(ovr) == [f"camera{i}" for i in range(1, 9)]

    def test_every_rules_block_is_a_dict(self, shipped):
        """T16 那个静默 bug 的守卫：块必须是 dict，不能是 str"""
        ovr = shipped["camera_stop_rule_overrides"]
        for cam, blk in ovr.items():
            assert isinstance(blk, dict), f"{cam} 不是 dict: {type(blk).__name__}"
            rules = blk.get("rules")
            assert isinstance(rules, dict), \
                f"{cam}.rules 不是 dict: {type(rules).__name__}"
            for cls, cb in rules.items():
                assert isinstance(cb, dict), \
                    f"{cam}.rules.{cls} 不是 dict: {type(cb).__name__}"

    def test_every_camera_matches_global(self, shipped):
        g = shipped["machine_stop_rules"]["rules"]
        ovr = shipped["camera_stop_rule_overrides"]
        for cam, blk in ovr.items():
            assert sorted(blk["rules"]) == sorted(g), f"{cam} 的类集合与全局不一致"
            for cls, cb in blk["rules"].items():
                assert sorted(cb) == sorted(g[cls]), f"{cam}.{cls} 的键集合与全局不一致"
                for k, v in cb.items():
                    assert type(v) is type(g[cls][k]) and v == g[cls][k], \
                        f"{cam}.{cls}.{k}: {v!r} != 全局 {g[cls][k]!r}"

    def test_runtime_reports_zero_impact(self, shipped):
        """走真实函数：出厂配置下 8 台与全局逐项相同，差异列表必须为空"""
        cams = sorted(shipped["camera_stop_rule_overrides"])
        assert st.check_camera_stop_rule_impact(cams) == []

    def test_effective_rules_equal_global(self, shipped):
        """走真实函数：每台的生效停机规则必须与全局逐项相同"""
        g = shipped["machine_stop_rules"]["rules"]
        for cam in sorted(shipped["camera_stop_rule_overrides"]):
            for cls in g:
                base = st.effective_stop_rule(cls, None)
                eff = st.effective_stop_rule(cls, cam)
                assert base == eff, f"{cam}.{cls}: {eff} != 全局 {base}"

    def test_effective_stop_gate_identical_across_cameras(self, shipped):
        """有效停机门槛 max(检测, 停机) 必须 8 台一致——行为没变的最终判据"""
        cams = sorted(shipped["camera_stop_rule_overrides"])
        g = shipped["machine_stop_rules"]["rules"]
        for cls in g:
            gates = set()
            for cam in cams:
                det = st.describe_camera_thresholds(
                    cam, st.CLASS_CONFIDENCE_THRESHOLDS).get(
                        cls, st.get_default_class_confidence())
                stop = (st.effective_stop_rule(cls, cam) or {}).get("min_confidence")
                gates.add(st.effective_stop_gate(det, stop))
            assert len(gates) == 1, f"{cls} 的有效停机门槛 8 台不一致: {gates}"


# ==================================================================
# §10 真实启动打印路径 main_yolo._print_stop_rules()
# ==================================================================
# main_yolo 依赖大恒 SDK 的 libgxiapi.so，在沙箱里加载不了；但
# Test_Tool/fake_galaxy_sdk.py 替换掉了 ctypes 那一层，装上之后 main_yolo
# **可以**导入。所以这里测的是现场真正会执行的那个函数，不是替身。
#
# 注意导入 main_yolo 有副作用（启 ConfigWatcher 线程、建两个线程池），
# 因此放在最后一个 section，并在 fixture 里关掉线程池。

_MAIN = None
try:
    _TEST_DIR = os.path.join(_ROOT, "Test_Tool")
    if _TEST_DIR not in sys.path:
        sys.path.insert(0, _TEST_DIR)
    import fake_galaxy_sdk  # noqa: E402
    fake_galaxy_sdk.install()
    import main_yolo as _MAIN  # noqa: E402
except Exception as _e:                                  # pragma: no cover
    _MAIN = None
    _MAIN_IMPORT_ERR = _e


@pytest.fixture(scope="module")
def _main_module():
    yield _MAIN
    for ex in (_MAIN._fabric_executor, _MAIN._aux_executor):
        try:
            ex.shutdown(wait=False)
        except Exception:
            pass


@pytest.mark.skipif(_MAIN is None, reason="main_yolo 需要假 Galaxy SDK 才能导入")
class TestRealStartupPrint:

    @pytest.fixture
    def main(self, _main_module):
        return _main_module

    @pytest.fixture
    def lines(self, main, monkeypatch):
        """把 main_yolo.info 换成收集器。

        本项目的 EnhancedLogger 把 propagate 设为 False 并清掉了 handler，
        caplog / capsys 都抓不到（T13 已经踩过），只能替换模块级的 info。
        """
        buf = []
        monkeypatch.setattr(main, "info",
                            lambda *a, **k: buf.append(" ".join(str(x) for x in a)))
        return buf

    def test_shipped_config_prints_all_consistent(self, main, lines, monkeypatch):
        """出厂配置下必须打印 ✓ 一致，且不得出现 ⚠"""
        monkeypatch.setattr(st, "CAMERA_STOP_RULE_OVERRIDES", {})
        main._print_stop_rules()
        txt = "\n".join(lines)
        assert "✓ 按相机停机规则：8 台与全局完全一致" in txt
        assert "⚠ 以下相机的停机规则" not in txt
        assert "多股线: conf>=0.7 + 宽>=30.0mm + 高>=30.0mm" in txt

    def test_override_prints_warning_and_effective_rules(
            self, main, lines, monkeypatch):
        """有覆盖时必须列出差异、方向、以及**有效门槛**的换算"""
        monkeypatch.setattr(st, "CAMERA_STOP_RULE_OVERRIDES", {
            "camera3": {"rules": {"多股线": {"min_confidence": 0.5}}}})
        monkeypatch.setattr(st, "CAMERA_THRESHOLD_OVERRIDES", {})
        main._print_stop_rules()
        txt = "\n".join(lines)
        assert "⚠ 以下相机的停机规则" in txt
        assert "camera3 / 多股线 / min_confidence: 0.7 → 0.5" in txt
        assert "停机会变频繁" in txt
        # 关键：必须报有效门槛，而不是只报停机字段
        assert "↳" in txt and "有效门槛" in txt
        assert "← 已覆盖" in txt

    def test_camera_can_disable_class_in_print(self, main, lines, monkeypatch):
        monkeypatch.setattr(st, "CAMERA_STOP_RULE_OVERRIDES", {
            "camera6": {"rules": {"多股线": {"enabled": False}}}})
        main._print_stop_rules()
        txt = "\n".join(lines)
        assert "该类不再停机" in txt
        assert "不再停机，无有效停机门槛" in txt

    def test_threshold_cross_check_still_runs(self, main, lines, monkeypatch):
        """_print_camera_thresholds 的交叉校验要用**按相机**的停机值。

        camera1 多股线 检测降到 0.6、camera2 降到 0.2，而多股线停机阈值是 0.70
        —— 两台都由停机阈值把关，所以**不该**出现在改变停机行为的列表里。
        camera5 油点线 降到 0.3（停机阈值 0.5 < 检测 0.85）则**应该**出现。
        这条正是 T15 的结论，改成按相机停机后必须依然成立。
        """
        monkeypatch.setattr(st, "CAMERA_STOP_RULE_OVERRIDES", {})
        monkeypatch.setattr(st, "CAMERA_THRESHOLD_OVERRIDES", {
            "camera1": {"class_confidence_thresholds": {"多股线": 0.6}},
            "camera2": {"class_confidence_thresholds": {"多股线": 0.2}},
            "camera5": {"class_confidence_thresholds": {"油点线": 0.3}},
        })
        main._print_camera_thresholds()
        txt = "\n".join(lines)
        assert "⚠ 以下覆盖**改变了停机行为**" in txt
        assert "camera5 / 油点线" in txt
        assert "camera1 / 多股线" not in txt, "多股线由停机阈值 0.70 把关，不该报警"
        assert "camera2 / 多股线" not in txt, "多股线由停机阈值 0.70 把关，不该报警"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
