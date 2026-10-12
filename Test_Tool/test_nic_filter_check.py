#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网卡 GigE 过滤驱动检查测试。

背景：本项目从海康 MVS 换到大恒 Galaxy，代码里的海康调用已清零，但**驱动
装在操作系统上、不随代码走**。两个 GigE 过滤驱动挂同一张网卡是"整机网络栈
卡死/电脑死机"的高频原因，而且从 Python 侧完全看不出异常。

本文件重点守两条：

1. **判定逻辑正确**：≥2 个疑似 GigE 过滤驱动挂同一张网卡必须报冲突，
   1 个不算，0 个也不算。方向搞反会漏掉真正的死机隐患。
2. **绝不抛异常、绝不阻塞启动**。这条路径和 CameraOperation 的包长路径
   同一条纪律：查驱动失败的失败模式必须是"日志里说没查成"，
   不能是"相机起不来"。

注册表访问被抽成 ``_Registry`` 协议，测试注入假实现，所以**在非 Windows 上
也能把所有判定分支跑满**（``enumerate_nic_filters`` 只在 reg=None 的默认
路径上判平台）。

跑法::

    python -m pytest Test_Tool/test_nic_filter_check.py -v
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402
import yaml  # noqa: E402

from General_Tool import NicFilterCheck as nfc  # noqa: E402


# ------------------------------------------------------------------
# 假注册表
# ------------------------------------------------------------------

class FakeReg(nfc._Registry):
    """按 {路径: {值名: 值}} + {路径: [子键...]} 构造的假 HKLM"""

    def __init__(self, values=None, subkeys=None):
        self.values = values or {}
        self.subkeys_map = subkeys or {}
        self.calls = []

    def subkeys(self, path):
        self.calls.append(("subkeys", path))
        return list(self.subkeys_map.get(path, []))

    def value(self, path, name):
        self.calls.append(("value", path, name))
        return self.values.get(path, {}).get(name)


class RaisingReg(nfc._Registry):
    """一读就炸的注册表：验证任何异常都不会冒出去"""

    def subkeys(self, path):
        raise PermissionError("denied")

    def value(self, path, name):
        raise OSError("boom")


NIC_KEY = nfc._NIC_CLASS_KEY
SVC = nfc._SERVICES_KEY


def reg_with_two_gige_filters():
    """一张网卡上同时挂海康和大恒两个过滤驱动——就是要抓的那个情况"""
    return FakeReg(
        subkeys={NIC_KEY: ["0001"]},
        values={
            NIC_KEY + r"\0001": {
                "DriverDesc": "Intel(R) I219-LM Gigabit Network Connection",
                "UpperBindings": ["MVGIgEFilter", "GXGigEFilter"],
            },
            SVC + r"\MVGIgEFilter": {
                "DisplayName": "MVS GigE Filter Driver",
                "Description": "Hikvision MVS GigE Vision filter",
                "ImagePath": r"\SystemRoot\system32\DRIVERS\MVGIgEFilter.sys",
            },
            SVC + r"\GXGigEFilter": {
                "DisplayName": "Galaxy GigE Filter Driver",
                "Description": "Daheng Galaxy GigE Vision filter",
                "ImagePath": r"\SystemRoot\system32\DRIVERS\GXGigEFilter.sys",
            },
        },
    )


# ==================================================================
# §1 基础工具函数
# ==================================================================

class TestHelpers:

    def test_as_str_list_variants(self):
        assert nfc._as_str_list(["a", "b"]) == ["a", "b"]
        assert nfc._as_str_list("a") == ["a"]
        assert nfc._as_str_list("") == []
        assert nfc._as_str_list("   ") == []
        assert nfc._as_str_list(None) == []
        assert nfc._as_str_list(("a", "", "b")) == ["a", "b"]
        assert nfc._as_str_list(123) == []

    def test_matches_is_case_insensitive(self):
        assert nfc._matches("mvs gige filter driver",
                            ("gige", "hikvision")) == "gige"
        assert nfc._matches("HIKVISION MVS", ("hikvision",)) == "hikvision"
        assert nfc._matches("Npcap Packet Driver", ("gige",)) is None

    def test_matches_handles_empty(self):
        assert nfc._matches("", ("gige",)) is None
        assert nfc._matches(None, ("gige",)) is None
        assert nfc._matches("gige", ()) is None
        assert nfc._matches("gige", ("", None)) is None

    def test_haystack_concatenates_all_fields(self):
        h = nfc._haystack("SvcName", "Display", None, "C:\\path\\x.sys")
        assert "svcname" in h and "display" in h and "x.sys" in h

    def test_default_patterns_cover_both_vendors(self):
        """内置表必须覆盖海康和大恒，否则这个检查等于没用"""
        p = tuple(x.lower() for x in nfc.DEFAULT_GIGE_PATTERNS)
        for needle in ("hikvision", "mvs", "daheng", "galaxy", "gige"):
            assert any(needle in x or x in needle for x in p), \
                f"内置表里没有 {needle}"


# ==================================================================
# §2 枚举
# ==================================================================

class TestEnumerate:

    def test_reads_upper_bindings(self):
        nics = nfc.enumerate_nic_filters(reg_with_two_gige_filters())
        assert nics is not None and len(nics) == 1
        assert nics[0]["nic_key"] == "0001"
        assert "I219-LM" in nics[0]["driver_desc"]
        assert [f["service"] for f in nics[0]["filters"]] == \
            ["MVGIgEFilter", "GXGigEFilter"]

    def test_classifies_vendor(self):
        nics = nfc.enumerate_nic_filters(reg_with_two_gige_filters())
        by_svc = {f["service"]: f for f in nics[0]["filters"]}
        assert by_svc["MVGIgEFilter"]["matched_gige"] == "hikvision"
        assert by_svc["GXGigEFilter"]["matched_gige"] == "daheng"

    def test_merges_upper_and_lower_bindings_without_dupes(self):
        reg = FakeReg(
            subkeys={NIC_KEY: ["0001"]},
            values={NIC_KEY + r"\0001": {
                "DriverDesc": "NIC",
                "UpperBindings": ["A", "B"],
                "LowerBindings": ["B", "C"],       # B 重复
            }})
        nics = nfc.enumerate_nic_filters(reg)
        assert [f["service"] for f in nics[0]["filters"]] == ["A", "B", "C"]

    def test_one_bad_nic_does_not_kill_others(self):
        """单张网卡读失败不能连累其他网卡——否则一张有问题就全查不到"""
        class Partial(FakeReg):
            def value(self, path, name):
                if r"\0002" in path and name == "DriverDesc":
                    raise OSError("bad key")
                return super().value(path, name)

        reg = Partial(
            subkeys={NIC_KEY: ["0001", "0002", "0003"]},
            values={
                NIC_KEY + r"\0001": {"DriverDesc": "NIC1",
                                     "UpperBindings": ["A"]},
                NIC_KEY + r"\0003": {"DriverDesc": "NIC3",
                                     "UpperBindings": ["C"]},
            })
        nics = nfc.enumerate_nic_filters(reg)
        assert nics is not None
        assert [n["nic_key"] for n in nics] == ["0001", "0003"]

    def test_no_subkeys_returns_none(self):
        """查不到就是 None，不是空列表。空列表的意思是「查到了一张都没有」，两者不能混。"""
        assert nfc.enumerate_nic_filters(FakeReg()) is None

    def test_raising_registry_returns_none(self):
        assert nfc.enumerate_nic_filters(RaisingReg()) is None

    def test_default_path_on_non_windows_returns_none(self):
        """不传 reg 时走默认路径，非 Windows 必须安静返回 None"""
        if sys.platform == "win32":
            pytest.skip("这条只在非 Windows 上有意义")
        assert nfc.enumerate_nic_filters() is None

    def test_explicit_reg_bypasses_platform_check(self):
        """显式传 reg 就无条件使用——这是判定逻辑能在 Linux 上被测的前提"""
        assert nfc.enumerate_nic_filters(
            reg_with_two_gige_filters()) is not None

    def test_custom_patterns_used(self):
        """现场补充的特征串要真的生效"""
        reg = FakeReg(
            subkeys={NIC_KEY: ["0001"]},
            values={NIC_KEY + r"\0001": {
                "DriverDesc": "NIC", "UpperBindings": ["WeirdVendor"]},
                SVC + r"\WeirdVendor": {"DisplayName": "某国产相机过滤驱动"}})
        assert nfc.enumerate_nic_filters(
            reg)[0]["filters"][0]["matched_gige"] is None
        hit = nfc.enumerate_nic_filters(
            reg, gige_patterns=("某国产相机",))
        assert hit[0]["filters"][0]["matched_gige"] == "某国产相机"


# ==================================================================
# §3 判定
# ==================================================================

class TestAnalyse:

    def test_none_means_not_checked(self):
        r = nfc.analyse_nic_filters(None)
        assert r["checked"] is False
        assert r["conflicts"] == [] and r["singles"] == []

    def test_empty_list_means_not_checked(self):
        """空列表也当「没查到」，不能当成「查到了、一切正常」。"""
        assert nfc.analyse_nic_filters([])["checked"] is False

    def test_two_gige_on_one_nic_is_a_conflict(self):
        """**这是整个模块存在的理由**"""
        nics = nfc.enumerate_nic_filters(reg_with_two_gige_filters())
        r = nfc.analyse_nic_filters(nics)
        assert r["checked"] is True
        assert len(r["conflicts"]) == 1
        assert r["singles"] == []
        assert len(r["conflicts"][0]["gige_filters"]) == 2

    def test_single_gige_is_not_a_conflict(self):
        reg = FakeReg(
            subkeys={NIC_KEY: ["0001"]},
            values={NIC_KEY + r"\0001": {
                "DriverDesc": "NIC", "UpperBindings": ["GXGigEFilter"]},
                SVC + r"\GXGigEFilter": {"DisplayName": "Daheng Galaxy"}})
        r = nfc.analyse_nic_filters(nfc.enumerate_nic_filters(reg))
        assert r["conflicts"] == []
        assert len(r["singles"]) == 1

    def test_zero_gige_is_not_a_conflict(self):
        reg = FakeReg(
            subkeys={NIC_KEY: ["0001"]},
            values={NIC_KEY + r"\0001": {
                "DriverDesc": "NIC", "UpperBindings": ["SomeOtherFilter"]},
                SVC + r"\SomeOtherFilter": {"DisplayName": "普通过滤驱动"}})
        r = nfc.analyse_nic_filters(nfc.enumerate_nic_filters(reg))
        assert r["conflicts"] == [] and r["singles"] == []
        assert len(r["all_filters"]) == 1        # 但清单里仍然列出来

    def test_two_gige_on_different_nics_is_not_a_conflict(self):
        """关键：冲突是**按网卡**判定的。两个过滤驱动分别挂两张网卡不算冲突。"""
        reg = FakeReg(
            subkeys={NIC_KEY: ["0001", "0002"]},
            values={
                NIC_KEY + r"\0001": {"DriverDesc": "NIC-A",
                                     "UpperBindings": ["MVGIgEFilter"]},
                NIC_KEY + r"\0002": {"DriverDesc": "NIC-B",
                                     "UpperBindings": ["GXGigEFilter"]},
                SVC + r"\MVGIgEFilter": {"DisplayName": "Hikvision"},
                SVC + r"\GXGigEFilter": {"DisplayName": "Daheng"},
            })
        r = nfc.analyse_nic_filters(nfc.enumerate_nic_filters(reg))
        assert r["conflicts"] == [], "分属两张网卡不该算冲突"
        assert len(r["singles"]) == 2

    def test_all_filters_always_listed(self):
        """完整清单一律列出：内置名字表不可能覆盖所有厂商"""
        nics = nfc.enumerate_nic_filters(reg_with_two_gige_filters())
        r = nfc.analyse_nic_filters(nics)
        assert len(r["all_filters"]) == 1
        assert len(r["all_filters"][0]["filters"]) == 2


# ==================================================================
# §4 日志输出
# ==================================================================

class TestReporting:

    @staticmethod
    def _collect(**kw):
        lines = []
        nfc.report_nic_filters(log_fn=lines.append, **kw)
        return lines

    def test_conflict_warns_and_names_both_drivers(self):
        txt = "\n".join(self._collect(reg=reg_with_two_gige_filters()))
        assert "⚠" in txt
        assert "MVGIgEFilter" in txt and "GXGigEFilter" in txt
        assert "Hikvision" in txt or "hikvision" in txt.lower()
        assert "卸载海康" in txt or "只留大恒" in txt

    def test_no_conflict_reports_ok(self):
        reg = FakeReg(
            subkeys={NIC_KEY: ["0001"]},
            values={NIC_KEY + r"\0001": {
                "DriverDesc": "NIC", "UpperBindings": ["GXGigEFilter"]},
                SVC + r"\GXGigEFilter": {"DisplayName": "Daheng Galaxy"}})
        txt = "\n".join(self._collect(reg=reg))
        assert "✓ 未发现重复" in txt
        assert "⚠" not in txt

    def test_not_checked_says_so_without_error(self):
        """查不到时必须安静说明原因，不能报错——失败模式是「没查成」。"""
        lines = self._collect(reg=RaisingReg())
        txt = "\n".join(lines)
        assert "未能读取" in txt or "跳过检查" in txt
        assert "⚠" not in txt

    def test_full_list_always_printed(self):
        txt = "\n".join(self._collect(reg=reg_with_two_gige_filters()))
        assert "完整清单" in txt

    def test_logger_raising_does_not_propagate(self):
        """日志器自己炸了也不能把启动带崩"""
        def boom(*a, **k):
            raise RuntimeError("logger down")
        # 不该抛异常
        r = nfc.report_nic_filters(reg=reg_with_two_gige_filters(),
                                   log_fn=boom)
        assert r["checked"] is True
        assert len(r["conflicts"]) == 1


# ==================================================================
# §5 入口永不抛异常
# ==================================================================

class TestNeverRaises:
    """这条路径在启动流程上，和 CameraOperation 的包长路径同一条纪律：
    查驱动失败绝不能让相机起不来。"""

    def test_entry_swallows_everything(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("everything is on fire")
        monkeypatch.setattr(nfc, "report_nic_filters", boom)
        assert nfc.check_nic_filter_drivers() == {"checked": False}

    def test_entry_survives_raising_registry(self, monkeypatch):
        monkeypatch.setattr(nfc, "_WinRegistry", lambda: RaisingReg())
        r = nfc.check_nic_filter_drivers()
        assert isinstance(r, dict)
        assert r["checked"] is False

    def test_patterns_are_appended_not_replaced(self, monkeypatch):
        """补充特征必须**追加**在默认表上，不能把海康/大恒的覆盖掉"""
        seen = {}

        def spy(reg=None, gige_patterns=None, **kw):
            seen["pats"] = gige_patterns
            return {"checked": True, "conflicts": [], "singles": [],
                    "all_filters": []}

        monkeypatch.setattr(nfc, "report_nic_filters", spy)
        nfc.check_nic_filter_drivers(gige_patterns=["某国产相机"])
        pats = seen["pats"]
        assert "某国产相机" in pats
        for needle in ("hikvision", "daheng", "gige"):
            assert needle in pats, f"默认表被覆盖掉了，丢了 {needle}"

    def test_empty_patterns_pass_none(self, monkeypatch):
        seen = {}

        def spy(reg=None, gige_patterns=None, **kw):
            seen["pats"] = gige_patterns
            return {"checked": False}

        monkeypatch.setattr(nfc, "report_nic_filters", spy)
        nfc.check_nic_filter_drivers(gige_patterns=[])
        assert seen["pats"] is None
        nfc.check_nic_filter_drivers(gige_patterns=["", "  ", None])
        assert seen["pats"] is None


# ==================================================================
# §6 出厂 config.yaml
# ==================================================================

@pytest.fixture(scope="module")
def shipped():
    with open(os.path.join(_ROOT, "config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)["logging"]


class TestShippedConfig:

    def test_keys_present(self, shipped):
        assert shipped["nic_filter_check"] is True
        assert shipped["nic_filter_patterns"] == []

    def test_check_enabled_by_default(self, shipped):
        """这是排查整机死机的关键观测点，出厂必须是开的"""
        assert shipped.get("nic_filter_check", True) is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
