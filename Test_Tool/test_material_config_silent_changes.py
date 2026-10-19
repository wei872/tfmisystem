# -*- coding: utf-8 -*-
"""「前端改了但日志没动静」的四种情形。

起因：现场反馈前端修改保存后，Python 日志不打变更日志。逐一压前端可能的
改法，发现四类**真实变更**当时检测不出来 —— 覆盖层是整份替换
（remote_config_store.apply 明确不做增量合并），所以"后端这次没下发的
条目"会真的从生效值里消失、悄悄退回 config.yaml，而旧代码只比对
"新下发里有什么"，正好把这半边漏掉。

四种漏掉的变更：
  1. 停机规则的某个字段被清空（键从 payload 里消失）
  2. 类别被停用（status=0）
  3. 类别被整条删除
  4. 只改了卷号/材质等元信息，阈值一个没动
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import Communication_Tool.material_config_client as M  # noqa: E402
from AnomalyDetection_Tool.config import remote_config_store as RCS  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    RCS.reset_remote_config_store()
    M._LOG_SEEN.clear()
    M._LABEL_WARNED.clear()
    yield
    RCS.reset_remote_config_store()
    M._LOG_SEEN.clear()
    M._LABEL_WARNED.clear()


def _collect(monkeypatch):
    logs = []
    for lvl in ("info", "warning", "error"):
        monkeypatch.setattr(M, lvl, (lambda L: lambda m: L.append(m))(logs))
    monkeypatch.setattr(M, "_warn_model_mismatch", lambda meta: None)
    return logs


def _base():
    return {"clothNumber": "1500D/2 100145-2610-2", "roller": "2610-2/11-0",
            "material": "1500D/2", "specification": "100145",
            "cameras": [{"cameraId": "camera%d" % i,
                         "thresholds": [
                             {"defectName": "多股线", "defectLabel": "multi_line",
                              "status": 1, "thresholdValue": 0.30},
                             {"defectName": "包边不良", "defectLabel": "edge_bad",
                              "status": 1, "thresholdValue": 0.50}],
                         "stopSetting": [
                             {"name": "单根包不进", "enabled": True,
                              "minConfidence": 0.30, "triggerMode": "dimension",
                              "minWidthMm": 50, "minHeightMm": 47}]}
                        for i in range(1, 3)]}


def _apply_then(monkeypatch, mutate):
    """先建立基线，再按 mutate 改一份重新下发，返回变更日志。"""
    M.apply_config(_base())
    logs = _collect(monkeypatch)
    p = _base()
    mutate(p)
    assert M.apply_config(p) is True
    return "\n".join(logs)


class TestSilentChangesAreNowReported:
    def test_disabled_class_is_reported(self, monkeypatch):
        """status=0：该类别退回本地阈值，这是真实的行为变化。"""
        def m(p):
            for c in p["cameras"]:
                for t in c["thresholds"]:
                    if t["defectName"] == "包边不良":
                        t["status"] = 0
        out = _apply_then(monkeypatch, m)
        assert "本次未下发" in out, out
        assert "包边不良" in out
        # 停用前是 0.5，不是"无" —— 旧值必须来自应用前的快照
        assert "0.5->退回本地" in out, out

    def test_deleted_class_is_reported(self, monkeypatch):
        """整条删掉，和停用同样要让现场看得见。"""
        def m(p):
            for c in p["cameras"]:
                c["thresholds"] = [t for t in c["thresholds"]
                                   if t["defectName"] != "包边不良"]
        out = _apply_then(monkeypatch, m)
        assert "本次未下发" in out and "包边不良" in out, out

    def test_cleared_stop_field_is_reported(self, monkeypatch):
        """字段被清空 = 键从 payload 消失。按并集比才抓得到。"""
        def m(p):
            for c in p["cameras"]:
                for st in c["stopSetting"]:
                    st.pop("minWidthMm", None)
        out = _apply_then(monkeypatch, m)
        assert "min_width_mm 50->无" in out, out

    def test_meta_only_change_is_reported(self, monkeypatch):
        """只换布卷/材质、阈值一个没动 —— 也是一次真实变更。"""
        def m(p):
            p["roller"] = "2610-3/05-0"
            p["material"] = "900D/1"
            p["specification"] = "100200"
        out = _apply_then(monkeypatch, m)
        assert "布卷/材质变更" in out, out
        assert "卷号 2610-2/11-0->2610-3/05-0" in out
        assert "材质 1500D/2->900D/1" in out

    def test_meta_only_change_has_no_empty_sections(self, monkeypatch):
        """没有相机变化时，不能留下"变化相机（0 台）："这种空标题。"""
        def m(p):
            p["roller"] = "R2"
        out = _apply_then(monkeypatch, m)
        assert "变化相机（0 台）" not in out, out
        assert "变更明细（按相机）：" not in out, out

    def test_genuinely_unchanged_stays_silent(self, monkeypatch):
        """什么都没改仍然必须静音 —— 修漏报不能变成刷屏。"""
        M.apply_config(_base())
        logs = _collect(monkeypatch)
        for _ in range(5):
            M.apply_config(_base())
        assert logs == [], logs


class TestStopRuleDiffSemantics:
    def test_diff_covers_the_union_of_keys(self):
        """旧版只比新规则里有的键，删掉的键永远发现不了。"""
        d = M._stop_rule_diff({"enabled": True, "min_width_mm": 50.0},
                              {"enabled": True})
        assert "min_width_mm 50->无" in d, d

    def test_added_key_is_reported(self):
        d = M._stop_rule_diff({"enabled": True},
                              {"enabled": True, "min_width_mm": 50.0})
        assert "min_width_mm 无->50" in d, d

    def test_float_noise_is_not_a_change(self):
        """后端回传的浮点可能带尾巴，不能因此判定"变了"。"""
        assert M._same(0.3, 0.30000000000000004) is True
        assert M._stop_rule_diff({"min_confidence": 0.3},
                                 {"min_confidence": 0.30000000000000004}) == []

    def test_bool_is_not_compared_as_number(self):
        """True == 1 在 Python 里成立，enabled 必须按布尔比。"""
        assert M._same(True, 1) is True
        assert M._same(True, False) is False
        assert M._same(False, 0) is True

    def test_identical_rules_produce_no_diff(self):
        r = {"enabled": True, "min_confidence": 0.5, "trigger_mode": "dimension",
             "min_width_mm": 50.0, "min_height_mm": 47.0}
        assert M._stop_rule_diff(dict(r), dict(r)) == []
