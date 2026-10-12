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


# ---------------------------------------------------------------- §7 禁用编排
class TestPlanDisable:
    """plan_disable 是纯函数，但它决定了会去动操作系统的哪些东西。
    这几条不变量是整个禁用功能的安全边界。"""

    def _plan(self, reg=None, **kw):
        rep = nfc.analyse_nic_filters(nfc.enumerate_nic_filters(reg=reg))
        return nfc.plan_disable(report=rep, **kw)

    def test_never_disables_daheng_driver(self):
        """最重要的一条：大恒自家的过滤驱动绝对不能出现在禁用列表里。
        把它关掉等于让本项目自己收不到图。"""
        plan = self._plan(reg=reg_with_two_gige_filters())
        targets = [a["service"] for a in plan["actions"]]
        assert "GXGigEFilter" not in targets, "禁用列表里出现了大恒的驱动"
        assert targets == ["MVGIgEFilter"], targets

    def test_daheng_is_recorded_as_skipped_with_reason(self):
        plan = self._plan(reg=reg_with_two_gige_filters())
        sk = {s["service"]: s["reason"] for s in plan["skipped"]}
        assert "GXGigEFilter" in sk
        assert "大恒" in sk["GXGigEFilter"]

    def test_disable_command_names_the_hikvision_component(self):
        plan = self._plan(reg=reg_with_two_gige_filters())
        cmd = plan["actions"][0]["disable_cmd"]
        assert "Disable-NetAdapterBinding" in cmd
        assert "MVGIgEFilter" in cmd
        assert "GXGigEFilter" not in cmd
        # 用 InterfaceDescription 定位网卡，而不是靠猜连接名
        assert "Intel(R) I219-LM Gigabit Network Connection" in cmd

    def test_never_disables_the_last_gige_filter_on_a_nic(self):
        """两个都不是大恒的过滤驱动时，工具不能把两个都禁掉——
        那张网卡上就一个 GigE 过滤驱动都不剩了。"""
        reg = FakeReg(
            subkeys={NIC_KEY: ["0001"]},
            values={
                NIC_KEY + r"\0001": {
                    "DriverDesc": "NIC A",
                    "UpperBindings": ["FilterX", "FilterY"],
                },
                SVC + r"\FilterX": {"DisplayName": "GigE Filter X"},
                SVC + r"\FilterY": {"DisplayName": "GigE Filter Y"},
            })
        plan = self._plan(reg=reg)
        assert len(plan["actions"]) == 1, [a["service"] for a in plan["actions"]]
        assert len(plan["skipped"]) == 1
        assert "收不到图" in plan["skipped"][0]["reason"]

    def test_two_filters_on_two_nics_produces_no_actions(self):
        """分挂两张网卡不是冲突，默认（only_conflicting_nics=True）什么都不该动。"""
        reg = FakeReg(
            subkeys={NIC_KEY: ["0001", "0002"]},
            values={
                NIC_KEY + r"\0001": {"DriverDesc": "NIC A",
                                     "UpperBindings": ["MVGIgEFilter"]},
                NIC_KEY + r"\0002": {"DriverDesc": "NIC B",
                                     "UpperBindings": ["GXGigEFilter"]},
                SVC + r"\MVGIgEFilter": {"DisplayName": "Hikvision MVS GigE Filter"},
                SVC + r"\GXGigEFilter": {"DisplayName": "Galaxy GigE Filter"},
            })
        assert self._plan(reg=reg)["actions"] == []

    def test_empty_service_name_is_ignored_entirely(self):
        """空服务名连 GigE 判定都过不了（haystack 为空），所以正确行为是
        **整个忽略**，不进 action、不进 blocked、也不崩。
        ⚠️ 必须造出 ≥2 个 GigE 过滤驱动才构成冲突，否则整张网卡会被跳过，
        这条测试就什么都测不到了。"""
        reg = FakeReg(
            subkeys={NIC_KEY: ["0001"]},
            values={
                NIC_KEY + r"\0001": {"DriverDesc": "NIC A",
                                      "UpperBindings": ["FilterX", "FilterY", ""]},
                SVC + r"\FilterX": {"DisplayName": "GigE Filter X"},
                SVC + r"\FilterY": {"DisplayName": "GigE Filter Y"},
            })
        plan = self._plan(reg=reg)
        assert plan["blocked"] == []                       # 空名字压根没进判定
        assert [a["service"] for a in plan["actions"]] == ["FilterY"]
        assert [x["service"] for x in plan["skipped"]] == ["FilterX"]
        assert "" not in [a["service"] for a in plan["actions"]]

    def test_missing_driver_desc_is_blocked_not_silently_ignored(self):
        """DriverDesc 为空时**必须拒绝**，不能生成命令。
        生成的命令会是 `-eq ''`，PowerShell 里匹配不到任何网卡，于是
        "执行成功"却什么都没改——正是要避免的静默失效。"""
        reg = FakeReg(
            subkeys={NIC_KEY: ["0001"]},
            values={
                NIC_KEY + r"\0001": {          # 故意不给 DriverDesc
                    "UpperBindings": ["MVGIgEFilter", "GXGigEFilter"],
                },
                SVC + r"\MVGIgEFilter": {"DisplayName": "Hikvision MVS GigE Filter"},
                SVC + r"\GXGigEFilter": {"DisplayName": "Galaxy GigE Filter"},
            })
        plan = self._plan(reg=reg)
        assert plan["actions"] == []
        assert any("DriverDesc 为空" in b["reason"] for b in plan["blocked"]), \
            plan["blocked"]

    def test_plan_is_pure_never_touches_powershell(self, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("plan_disable 执行了系统命令")
        monkeypatch.setattr(nfc, "_run_powershell", boom)
        self._plan(reg=reg_with_two_gige_filters())


class TestDisableExecution:
    def test_dry_run_is_the_default_and_runs_nothing(self, monkeypatch):
        real = nfc.enumerate_nic_filters
        monkeypatch.setattr(nfc, "enumerate_nic_filters",
                            lambda reg=None, **k: real(
                                reg=reg_with_two_gige_filters()))
        calls = []
        res = nfc.disable_filter_bindings(
            runner=lambda c: calls.append(c) or (0, "", ""))
        assert calls == [], "没加 --yes 就执行了命令"
        assert res["dry_run"] is True
        assert len(res["plan"]["actions"]) == 1   # 计划仍然算出来了

    def test_confirm_without_admin_refuses_and_runs_nothing(self, monkeypatch):
        monkeypatch.setattr(nfc, "is_admin", lambda: False)
        calls = []
        res = nfc.disable_filter_bindings(
            confirm=True, runner=lambda c: calls.append(c) or (0, "", ""))
        assert res.get("error") == "not_admin"
        assert calls == [], "不是管理员却执行了命令"
        assert res["dry_run"] is False

    def test_admin_path_probes_before_disabling(self, monkeypatch):
        monkeypatch.setattr(nfc, "is_admin", lambda: True)
        calls = []

        def runner(cmd):
            calls.append(cmd)
            if cmd.startswith("Get-"):
                return 0, "Enabled : True", ""
            return 0, "", ""

        # 用真实注册表读不到，这里让 plan 走注入的假注册表
        real = nfc.enumerate_nic_filters          # ⚠️ 先抓住原函数，
        monkeypatch.setattr(nfc, "enumerate_nic_filters",   # 否则 lambda 里
                            lambda reg=None, **k: real(     # 调自己会无限递归
                                reg=reg_with_two_gige_filters()))
        res = nfc.disable_filter_bindings(confirm=True, runner=runner)
        assert len(calls) == 2, calls
        assert calls[0].startswith("Get-NetAdapterBinding"), "先探测后禁用"
        assert calls[1].startswith("Get-NetAdapterBinding")
        assert "Disable-NetAdapterBinding" in calls[1]
        assert res["results"][0]["service"] == "MVGIgEFilter"

    def test_already_disabled_component_is_not_touched(self, monkeypatch):
        monkeypatch.setattr(nfc, "is_admin", lambda: True)
        calls = []

        def runner(cmd):
            calls.append(cmd)
            return 0, "Enabled : False", ""     # 本来就是关的

        real = nfc.enumerate_nic_filters          # ⚠️ 先抓住原函数，
        monkeypatch.setattr(nfc, "enumerate_nic_filters",   # 否则 lambda 里
                            lambda reg=None, **k: real(     # 调自己会无限递归
                                reg=reg_with_two_gige_filters()))
        res = nfc.disable_filter_bindings(confirm=True, runner=runner)
        assert len(calls) == 1, calls          # 只探测，没执行禁用
        assert res["results"][0]["skipped"] is True


class TestNotWiredIntoStartup:
    def test_main_yolo_never_calls_the_disable_path(self):
        """禁用能力**只能**由人在命令行显式触发。
        启动流程里出现任何一个禁用入口都是缺陷。"""
        import inspect
        import fake_galaxy_sdk
        fake_galaxy_sdk.install()
        import main_yolo
        src = inspect.getsource(main_yolo)
        for banned in ("disable_filter_bindings", "plan_disable",
                       "NicFilterCheck.main", "subprocess"):
            assert banned not in src, f"main_yolo 里出现了 {banned}"

    def test_module_itself_is_not_runnable_from_import(self):
        """import 时绝不能执行禁用。只有 python -m 才会走 main()。"""
        import importlib
        src = open(nfc.__file__, encoding="utf-8").read()
        assert 'if __name__ == "__main__":' in src
        # main() 只在 __main__ 分支里被调用
        after = src.split('if __name__ == "__main__":')[1]
        assert "main()" in after
        assert src.count("sys.exit(main())") == 1
        assert importlib.util.find_spec("General_Tool.NicFilterCheck") is not None
