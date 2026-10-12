#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按相机覆盖的置信度阈值测试。

背景：现场 8 台相机安装位置/光照不同，同一套阈值不一定都合适。

关键约束（决定了实现方式）：**pipeline 是每 worker 线程一个，不是每相机一个**
（`detection_worker_pool._get_or_create_pipeline` 用线程本地存储，
`num_workers: 5` 而相机 8 台）。所以阈值不能在构造时取快照，必须在
`_filter_boxes()` 里按当前帧的 camera_name 现查。本文件的
`test_filter_boxes_actually_passes_camera_name` 就是守这条接线的——
纯逻辑对了但没接上线，功能等于没做。

跑法::

    python -m pytest Test_Tool/test_per_camera_thresholds.py -v
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from AnomalyDetection_Tool.models.data_types import (  # noqa: E402
    ClassThresholdConfig,
    DetectionBox,
)

GLOBAL = {"多股线": 0.65, "油点线": 0.85, "纬线不直": 0.35}
GLOBAL_DEFAULT = 0.1

def make_cfg(overrides=None, with_provider=True):
    """构造一个 ClassThresholdConfig。overrides: {camera_name: 覆盖块}"""
    table = dict(overrides or {})
    return ClassThresholdConfig(
        thresholds_by_name=dict(GLOBAL),
        thresholds_by_id={},
        default_threshold=GLOBAL_DEFAULT,
        camera_override_provider=(lambda cam: table.get(cam)) if with_provider else None,
    )

def box(class_name, conf, class_id=0):
    return DetectionBox(x1=10, y1=10, x2=60, y2=60, confidence=conf,
                        class_id=class_id, class_name=class_name)

# ======================================================================
#  1. 向后兼容：没配覆盖时行为必须和改动前完全一致
# ======================================================================

def test_no_override_behaves_exactly_as_before():
    cfg = make_cfg()
    assert cfg.get_threshold("多股线", 0) == 0.65
    assert cfg.get_threshold("油点线", 0) == 0.85
    assert cfg.get_threshold("不存在的类", 0) == GLOBAL_DEFAULT

def test_no_provider_at_all_still_works():
    """完全没传 provider（旧调用方式）不能崩"""
    cfg = ClassThresholdConfig(thresholds_by_name=dict(GLOBAL),
                               default_threshold=GLOBAL_DEFAULT)
    assert cfg.get_threshold("多股线", 0, "camera3") == 0.65
    assert cfg.get_threshold("多股线", 0) == 0.65

def test_camera_without_override_uses_global():
    cfg = make_cfg({"camera3": {"class_confidence_thresholds": {"多股线": 0.3}}})
    # camera7 没配覆盖 → 走全局
    assert cfg.get_threshold("多股线", 0, "camera7") == 0.65
    assert cfg.get_threshold("多股线", 0, "camera3") == 0.3

def test_empty_camera_name_uses_global():
    cfg = make_cfg({"camera3": {"class_confidence_thresholds": {"多股线": 0.3}}})
    assert cfg.get_threshold("多股线", 0, "") == 0.65
    assert cfg.get_threshold("多股线", 0, None) == 0.65

# ======================================================================
#  2. 优先级
# ======================================================================

def test_camera_class_beats_global_class():
    cfg = make_cfg({"camera3": {"class_confidence_thresholds": {"多股线": 0.4}}})
    assert cfg.get_threshold("多股线", 0, "camera3") == 0.4
    # 其他类不受影响
    assert cfg.get_threshold("油点线", 0, "camera3") == 0.85

def test_camera_class_beats_camera_default():
    """同类同时配了按类和 default 时，按类更具体，应当胜出"""
    cfg = make_cfg({"camera3": {
        "default_class_confidence": 0.9,
        "class_confidence_thresholds": {"多股线": 0.4},
    }})
    assert cfg.get_threshold("多股线", 0, "camera3") == 0.4
    # 没显式写的类走相机级 default
    assert cfg.get_threshold("油点线", 0, "camera3") == 0.9

def test_camera_default_shadows_global_class():
    """
    ⚠ 这是最容易配错的一条，所以专门钉一个测试。

    相机级 default 会盖掉全局的按类阈值：camera3 配了 0.15，
    那么全局精心调过的「油点线: 0.85」在 camera3 上也变成 0.15。
    这是"覆盖块遮蔽全局块"的标准语义，但必须让使用者知道。
    """
    cfg = make_cfg({"camera3": {"default_class_confidence": 0.15}})
    assert cfg.get_threshold("油点线", 0, "camera3") == 0.15, (
        "相机级 default 应当遮蔽全局按类阈值；若此断言失败说明优先级变了，"
        "必须同步更新 config.yaml 注释和 data_types.py 的文档字符串")
    # 其他相机不受影响
    assert cfg.get_threshold("油点线", 0, "camera7") == 0.85

def test_camera_id_override():
    cfg = make_cfg({"camera3": {"class_confidence_thresholds_by_id": {7: 0.42}}})
    assert cfg.get_threshold("某类", 7, "camera3") == 0.42
    assert cfg.get_threshold("某类", 8, "camera3") == GLOBAL_DEFAULT

def test_full_precedence_chain():
    """7 级优先级从上到下逐级验证"""
    cfg = make_cfg({"camera3": {
        "class_confidence_thresholds": {"甲": 0.11},
        "class_confidence_thresholds_by_id": {5: 0.22},
        "default_class_confidence": 0.33,
    }})
    cfg.thresholds_by_id = {9: 0.44}

    # 1 相机+类名
    assert cfg.get_threshold("甲", 5, "camera3") == 0.11
    # 2 相机+类id
    assert cfg.get_threshold("乙", 5, "camera3") == 0.22
    # 3 相机 default
    assert cfg.get_threshold("乙", 1, "camera3") == 0.33
    # 4 全局类名（换一台没配覆盖的相机）
    cfg2 = make_cfg({})
    cfg2.thresholds_by_id = {9: 0.44}
    assert cfg2.get_threshold("油点线", 9, "camera9") == 0.85
    # 5 全局类id
    assert cfg2.get_threshold("乙", 9, "camera9") == 0.44
    # 7 全局 default
    assert cfg2.get_threshold("乙", 1, "camera9") == GLOBAL_DEFAULT

def test_global_default_provider_used_when_no_override():
    cfg = make_cfg()
    cfg.default_threshold_provider = lambda: 0.77
    assert cfg.get_threshold("未配置的类", 0, "camera3") == 0.77

def test_camera_default_beats_global_provider():
    cfg = make_cfg({"camera3": {"default_class_confidence": 0.05}})
    cfg.default_threshold_provider = lambda: 0.77
    assert cfg.get_threshold("未配置的类", 0, "camera3") == 0.05
    assert cfg.get_threshold("未配置的类", 0, "camera7") == 0.77

# ======================================================================
#  3. 健壮性：配错不能崩、不能影响检测
# ======================================================================

def test_bad_value_falls_through_to_global():
    """阈值写成非数字时当作没配，继续往下走，不能抛"""
    cfg = make_cfg({"camera3": {"class_confidence_thresholds": {"多股线": "很高"}}})
    assert cfg.get_threshold("多股线", 0, "camera3") == 0.65

def test_bad_camera_default_falls_through():
    cfg = make_cfg({"camera3": {"default_class_confidence": "低"}})
    assert cfg.get_threshold("油点线", 0, "camera3") == 0.85

def test_non_dict_blocks_are_ignored():
    """覆盖块写成字符串/列表时忽略，不能崩"""
    for bad in ("camera3", ["a"], 42, None):
        cfg = make_cfg({"camera3": bad} if bad is not None else {})
        assert cfg.get_threshold("多股线", 0, "camera3") == 0.65

def test_provider_raising_is_treated_as_no_override():
    """provider 抛异常时退化成"没有覆盖"，绝不能让检测崩掉"""
    def _boom(cam):
        raise RuntimeError("config exploded")

    cfg = ClassThresholdConfig(
        thresholds_by_name=dict(GLOBAL),
        default_threshold=GLOBAL_DEFAULT,
        camera_override_provider=_boom,
    )
    assert cfg.get_threshold("多股线", 0, "camera3") == 0.65

def test_check_confidence_honours_camera():
    cfg = make_cfg({"camera3": {"class_confidence_thresholds": {"多股线": 0.3}}})
    # 0.5 的置信度：全局阈值 0.65 下不通过，camera3 阈值 0.3 下通过
    assert cfg.check_confidence("多股线", 0, 0.5, "camera3") is True
    assert cfg.check_confidence("多股线", 0, 0.5, "camera7") is False

# ======================================================================
#  4. 接线：_filter_boxes 必须真的把 camera_name 传下去
# ======================================================================

class _PipelineStub:
    """只提供 _filter_boxes 实际访问的属性"""

    def __init__(self, threshold_config):
        self.filter_classes = set()
        self.threshold_config = threshold_config

def _filter(stub, boxes, camera_name=""):
    """调用真实的 DetectionPipeline._filter_boxes"""
    from AnomalyDetection_Tool.core.detection_pipeline import DetectionPipeline
    return DetectionPipeline._filter_boxes(
        stub, boxes, None, "", camera_name)

def test_filter_boxes_actually_passes_camera_name():
    """
    **这条是接线测试**：纯逻辑对了但 _filter_boxes 没把 camera_name 传下去，
    功能等于没做（而且不会报错，只会静默地所有相机都用全局阈值）。
    """
    cfg = make_cfg({"camera3": {"class_confidence_thresholds": {"纬线不直": 0.30}}})
    stub = _PipelineStub(cfg)

    # 置信度 0.32：全局阈值 0.35 下应被过滤掉，camera3 阈值 0.30 下应保留
    b = box("纬线不直", 0.32)

    filtered, _, f_thr, _, _, _ = _filter(stub, [b], "camera3")
    assert len(filtered) == 1, f"camera3 应保留这个框，实际过滤掉了 {f_thr} 个"
    assert f_thr == 0

    filtered7, _, f_thr7, _, _, _ = _filter(stub, [b], "camera7")
    assert len(filtered7) == 0, "camera7 用全局阈值 0.35，应当过滤掉"
    assert f_thr7 == 1

def test_filter_boxes_default_camera_is_global():
    """不传 camera_name（旧调用方式）必须走全局阈值"""
    cfg = make_cfg({"camera3": {"class_confidence_thresholds": {"纬线不直": 0.30}}})
    stub = _PipelineStub(cfg)
    b = box("纬线不直", 0.32)

    filtered, _, f_thr, _, _, _ = _filter(stub, [b])
    assert len(filtered) == 0 and f_thr == 1

def test_filter_boxes_mixed_cameras_same_pipeline():
    """
    同一个 pipeline（= 同一个 worker）先后处理不同相机的帧，
    阈值必须跟着帧走，不能被上一帧的相机"粘住"。
    这正是"pipeline 是每 worker 而非每相机"带来的风险。
    """
    cfg = make_cfg({
        "camera3": {"class_confidence_thresholds": {"纬线不直": 0.30}},
        "camera5": {"class_confidence_thresholds": {"纬线不直": 0.90}},
    })
    stub = _PipelineStub(cfg)
    b = box("纬线不直", 0.32)

    assert len(_filter(stub, [b], "camera3")[0]) == 1   # 0.30 → 保留
    assert len(_filter(stub, [b], "camera5")[0]) == 0   # 0.90 → 过滤
    assert len(_filter(stub, [b], "camera7")[0]) == 0   # 全局 0.35 → 过滤
    assert len(_filter(stub, [b], "camera3")[0]) == 1   # 再回来仍然正确

# ======================================================================
#  5. settings 层：读配置 / 算生效值
# ======================================================================

def test_settings_override_lookup(monkeypatch):
    import AnomalyDetection_Tool.config.settings as st

    table = {
        "camera3": {"class_confidence_thresholds": {"多股线": 0.55}},
        "camera7": {"default_class_confidence": 0.2},
        "bad": "不是字典",
    }
    monkeypatch.setattr(st, "CAMERA_THRESHOLD_OVERRIDES", table)

    assert st.get_camera_threshold_override("camera3") == table["camera3"]
    # 没配的相机返回 None，让调用方能区分"没配"和"配了空块"
    assert st.get_camera_threshold_override("camera1") is None
    assert st.get_camera_threshold_override("") is None
    assert st.get_camera_threshold_override(None) is None
    # 配成非字典的当作没配
    assert st.get_camera_threshold_override("bad") is None

def test_settings_camera_default_distinguishes_unset_from_zero(monkeypatch):
    """没配必须返回 None，不能返回 0——0 是个合法阈值，含义完全不同"""
    import AnomalyDetection_Tool.config.settings as st

    monkeypatch.setattr(st, "CAMERA_THRESHOLD_OVERRIDES", {
        "camera3": {"default_class_confidence": 0},
        "camera5": {"class_confidence_thresholds": {"多股线": 0.5}},
        "camera7": {"default_class_confidence": "不是数字"},
    })

    assert st.get_camera_default_confidence("camera3") == 0.0   # 配了 0
    assert st.get_camera_default_confidence("camera5") is None  # 没配
    assert st.get_camera_default_confidence("camera7") is None  # 配错了
    assert st.get_camera_default_confidence("camera1") is None  # 整台没配

def test_describe_camera_thresholds_shows_effective_values(monkeypatch):
    """启动时打印用的生效值表：覆盖的要替换，没覆盖的要保留全局"""
    import AnomalyDetection_Tool.config.settings as st

    monkeypatch.setattr(st, "CAMERA_THRESHOLD_OVERRIDES", {
        "camera3": {"class_confidence_thresholds": {"多股线": 0.55},
                    "default_class_confidence": 0.15},
    })

    eff = st.describe_camera_thresholds("camera3", GLOBAL)
    assert eff["多股线"] == 0.55          # 被覆盖
    assert eff["油点线"] == 0.85          # 沿用全局
    assert eff["<default>"] == 0.15       # 相机级默认值单独标出来

    # 没配覆盖的相机：生效值就等于全局，且不出现 <default> 键
    eff7 = st.describe_camera_thresholds("camera7", GLOBAL)
    assert eff7 == GLOBAL
    assert "<default>" not in eff7

def test_describe_skips_bad_values(monkeypatch):
    import AnomalyDetection_Tool.config.settings as st

    monkeypatch.setattr(st, "CAMERA_THRESHOLD_OVERRIDES", {
        "camera3": {"class_confidence_thresholds": {"多股线": "很高",
                                                    "纬线不直": 0.25}},
    })
    eff = st.describe_camera_thresholds("camera3", GLOBAL)
    assert eff["多股线"] == 0.65    # 配错的忽略，保留全局
    assert eff["纬线不直"] == 0.25  # 配对的生效


# ======================================================================
#  6. 停机交叉校验：降检测阈值会不会连带改变停机
# ======================================================================
#
# 停机判定吃的是**过滤后**的框（detection_manager 里 for box in result.boxes，
# 而 result.boxes 是 _filter_boxes 的输出）。所以一个框要停机必须同时满足
# conf >= 检测阈值 且 conf >= 停机阈值 —— 真正把关的是 max(两者)。

def test_effective_stop_gate_is_max():
    import AnomalyDetection_Tool.config.settings as st

    assert st.effective_stop_gate(0.65, 0.70) == 0.70
    assert st.effective_stop_gate(0.85, 0.50) == 0.85
    assert st.effective_stop_gate(0.50, 0.50) == 0.50


def test_effective_stop_gate_handles_missing():
    import AnomalyDetection_Tool.config.settings as st

    assert st.effective_stop_gate(None, 0.7) == 0.7
    assert st.effective_stop_gate(0.6, None) == 0.6
    assert st.effective_stop_gate(None, None) is None
    assert st.effective_stop_gate("坏值", 0.7) == 0.7


def test_user_scenario_duoguoxian_stop_unchanged(monkeypatch):
    """
    **用户的确切场景**：camera1 多股线检测阈值改 0.6、camera2 改 0.2，
    要求"停机阈值一样"。

    多股线的停机 min_confidence 是 0.70，高于全局检测阈值 0.65，所以有效
    停机门槛本来就是 0.70。把检测阈值降到 0.6 或 0.2，有效门槛仍是 0.70
    —— **停机行为完全不变**。这条测试就是把用户的诉求钉住。
    """
    import AnomalyDetection_Tool.config.settings as st

    monkeypatch.setattr(st, "CAMERA_THRESHOLD_OVERRIDES", {
        "camera1": {"class_confidence_thresholds": {"多股线": 0.6}},
        "camera2": {"class_confidence_thresholds": {"多股线": 0.2}},
    })
    stop = {"多股线": 0.70}          # 取自 config.yaml machine_stop_rules

    changed = st.check_camera_threshold_stop_impact(
        ["camera1", "camera2", "camera3"], GLOBAL, GLOBAL_DEFAULT,
        lambda cls: stop.get(cls))

    assert changed == [], (
        f"多股线检测阈值降到 0.6/0.2 不应改变停机行为，但报告了变化: {changed}")


def test_lowering_detection_below_stop_conf_changes_stop(monkeypatch):
    """
    反例：油点线 检测 0.85 / 停机 0.50。把检测降到 0.3，有效门槛从 0.85
    掉到 0.50 —— 停机会变频繁。这正是用户说"停机阈值一样"时要避免的，
    所以必须被检测出来并告警。
    """
    import AnomalyDetection_Tool.config.settings as st

    monkeypatch.setattr(st, "CAMERA_THRESHOLD_OVERRIDES", {
        "camera5": {"class_confidence_thresholds": {"油点线": 0.3}},
    })
    changed = st.check_camera_threshold_stop_impact(
        ["camera5"], GLOBAL, GLOBAL_DEFAULT,
        lambda cls: {"油点线": 0.50}.get(cls))

    assert len(changed) == 1
    c = changed[0]
    assert c["camera"] == "camera5" and c["class_name"] == "油点线"
    assert c["gate_before"] == 0.85 and c["gate_after"] == 0.50
    assert c["effect"] == "停机会变频繁"


def test_raising_detection_above_stop_conf_changes_stop(monkeypatch):
    """反向：把检测阈值提到停机阈值之上，停机会变少"""
    import AnomalyDetection_Tool.config.settings as st

    monkeypatch.setattr(st, "CAMERA_THRESHOLD_OVERRIDES", {
        "camera3": {"class_confidence_thresholds": {"多股线": 0.95}},
    })
    changed = st.check_camera_threshold_stop_impact(
        ["camera3"], GLOBAL, GLOBAL_DEFAULT,
        lambda cls: {"多股线": 0.70}.get(cls))

    assert len(changed) == 1
    assert changed[0]["gate_before"] == 0.70
    assert changed[0]["gate_after"] == 0.95
    assert changed[0]["effect"] == "停机会变少"


def test_class_without_stop_rule_never_reported(monkeypatch):
    """没有停机规则的类（如蚊虫小黑点）怎么改都不影响停机"""
    import AnomalyDetection_Tool.config.settings as st

    monkeypatch.setattr(st, "CAMERA_THRESHOLD_OVERRIDES", {
        "camera3": {"class_confidence_thresholds": {"蚊虫小黑点": 0.01}},
    })
    changed = st.check_camera_threshold_stop_impact(
        ["camera3"], GLOBAL, GLOBAL_DEFAULT, lambda cls: None)
    assert changed == []


def test_camera_without_override_never_reported(monkeypatch):
    import AnomalyDetection_Tool.config.settings as st

    monkeypatch.setattr(st, "CAMERA_THRESHOLD_OVERRIDES", {
        "camera1": {"class_confidence_thresholds": {"多股线": 0.6}},
    })
    changed = st.check_camera_threshold_stop_impact(
        ["camera2", "camera7"], GLOBAL, GLOBAL_DEFAULT,
        lambda cls: {"多股线": 0.70}.get(cls))
    assert changed == []


def test_stop_lookup_raising_is_tolerated(monkeypatch):
    """停机规则查询抛异常时按"没有停机规则"处理，不能把启动打印搞崩"""
    import AnomalyDetection_Tool.config.settings as st

    monkeypatch.setattr(st, "CAMERA_THRESHOLD_OVERRIDES", {
        "camera3": {"class_confidence_thresholds": {"多股线": 0.6}},
    })

    def _boom(cls):
        raise RuntimeError("rules exploded")

    assert st.check_camera_threshold_stop_impact(
        ["camera3"], GLOBAL, GLOBAL_DEFAULT, _boom) == []


# ======================================================================
#  7. 校验真实 config.yaml（守住已发布的配置本身）
# ======================================================================

CONFIG_PATH = os.path.join(_ROOT, "config.yaml")


def _load_config():
    import yaml
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_shipped_config_override_blocks_are_dicts():
    """
    **这条守的是一个真实踩过的坑**：生成配置时漏写冒号，
    ``class_confidence_thresholds`` 整块被 YAML 解析成一个字符串。

    危险之处在于它**不会报错**——代码对"非字典覆盖块"是按"没配覆盖"
    静默处理的，所以程序照常跑，但按相机配置完全不生效。
    """
    cfg = _load_config()
    ov = cfg.get("camera_threshold_overrides")
    if not ov:
        return                      # 未配置也是合法状态
    assert isinstance(ov, dict), "camera_threshold_overrides 必须是映射"
    for cam, block in ov.items():
        assert isinstance(block, dict), (
            f"{cam} 的覆盖块必须是映射，实际是 {type(block).__name__}。"
            f"常见原因是漏写冒号导致整块被解析成字符串。")
        pc = block.get("class_confidence_thresholds")
        if pc is not None:
            assert isinstance(pc, dict), (
                f"{cam}.class_confidence_thresholds 必须是映射，"
                f"实际是 {type(pc).__name__}")
            for cls, v in pc.items():
                assert isinstance(v, (int, float)) and not isinstance(v, bool), (
                    f"{cam}.{cls} = {v!r} 必须是数字")


def test_shipped_config_covers_all_cameras_or_none():
    """要么一台都不配（全走全局），要么 8 台都列全——避免只配一半造成混乱"""
    cfg = _load_config()
    ov = cfg.get("camera_threshold_overrides")
    if not ov:
        return
    expected = sorted(set(cfg["cameras"]["sn_map"].values()))
    assert sorted(ov) == expected, (
        f"覆盖块应列全 {expected}，实际只有 {sorted(ov)}")


def test_shipped_config_matches_global_so_behaviour_is_unchanged():
    """
    已发布的 8 台展开配置，每台的值都等于全局值，所以**行为必须与不配置
    时完全一致**。若有人改了某台的数，这条会失败——那是有意的，提醒同时
    去看启动日志的停机交叉校验。
    """
    cfg = _load_config()
    ov = cfg.get("camera_threshold_overrides")
    if not ov:
        return
    gdet = dict(cfg["class_confidence_thresholds"])
    for cam, block in ov.items():
        pc = block.get("class_confidence_thresholds") or {}
        for cls, v in pc.items():
            assert cls in gdet, (
                f"{cam} 覆盖了一个全局里没有的类 {cls!r}，"
                f"请确认类名没有写错（写错的类名不会报错，只会静默无效）")
            assert float(v) == float(gdet[cls]), (
                f"{cam}.{cls}={v} 与全局 {gdet[cls]} 不同，"
                f"说明这台相机的检测行为已被改动")


def test_shipped_config_does_not_shadow_global_default():
    """
    展开的 8 台里**不应该**出现 default_class_confidence。

    写了它会让该相机上所有"没显式列出的类"都用这个默认值，把全局的按类
    阈值全部遮蔽。目前有 4 个类只在 machine_stop_rules 里（棉团、捻接器
    接头、小尾巴、稀缝），它们靠回落到全局 default_class_confidence 工作。
    """
    cfg = _load_config()
    ov = cfg.get("camera_threshold_overrides")
    if not ov:
        return
    for cam, block in ov.items():
        assert "default_class_confidence" not in block, (
            f"{cam} 不应配置 default_class_confidence：它会遮蔽全局的按类"
            f"阈值，使只在 machine_stop_rules 里的类拿不到正确的回落值")


def test_shipped_config_classes_are_a_subset_of_global():
    """覆盖里出现的每个类名都必须在全局里存在，否则是拼写错误"""
    cfg = _load_config()
    ov = cfg.get("camera_threshold_overrides")
    if not ov:
        return
    gdet = cfg["class_confidence_thresholds"]
    for cam, block in ov.items():
        for cls in (block.get("class_confidence_thresholds") or {}):
            assert cls in gdet, f"{cam} 里的类名 {cls!r} 在全局配置中不存在"

