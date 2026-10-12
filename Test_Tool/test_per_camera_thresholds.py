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

