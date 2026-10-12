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
        # 建议文案改过：真机实证**取消勾选就够了**（Enabled=False 即不在收包
        # 路径上），不必卸载。原来写"卸载海康 MVS"是针对海康 MVS 的措辞，
        # 现场漏掉的是 HikRobot 的 HKR_neuGEVFilter，措辞得通用一点。
        assert "取消勾选" in txt
        assert "GigE Vision LightWeight Filter" in txt      # 点名要保留的那个
        assert "ncpa.cpl" in txt                            # 给出可操作的路径

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


# ------------------------------------------------------- §8 启动拦截（硬闸门）
class TestStartupGate:
    """main_yolo.run_production_mode() 里的硬性拦截。

    判定门槛：**只有"确实查到了、且确认有冲突"才拦**。读不到注册表必须放过，
    否则非 Windows 开发机和权限不足的机器会永远起不来。"""

    def _setup(self, monkeypatch, reg):
        import fake_galaxy_sdk
        fake_galaxy_sdk.install()
        import main_yolo

        real = nfc.enumerate_nic_filters
        monkeypatch.setattr(nfc, "enumerate_nic_filters",
                            lambda reg_=None, **k: real(reg=reg))

        # 记录有没有真的走到开相机那一步
        built = []
        orig_dm = main_yolo.gx.DeviceManager

        class _Spy(orig_dm):
            def __init__(self, *a, **k):
                built.append(1)
                super().__init__(*a, **k)
        monkeypatch.setattr(main_yolo.gx, "DeviceManager", _Spy)
        # 让后面的枚举步骤立刻失败，避免测试真去连相机
        monkeypatch.setattr(main_yolo.gx.DeviceManager, "update_all_device_list",
                            lambda self, t=0: (0, []), raising=False)
        monkeypatch.setitem(main_yolo.LOGGING_CONFIG, "nic_filter_check", True)
        errors = []
        monkeypatch.setattr(main_yolo, "error", lambda m: errors.append(m))
        return main_yolo, built, errors

    def test_conflict_blocks_startup_before_sdk_init(self, monkeypatch):
        main_yolo, built, errors = self._setup(
            monkeypatch, reg_with_two_gige_filters())
        rc = main_yolo.run_production_mode()
        assert rc == 1, f"应该拒绝启动，实际返回 {rc}"
        assert built == [], "拦截失效：已经走到 gx.DeviceManager() 了"
        txt = "\n".join(errors)
        assert "拒绝启动" in txt
        assert "MVGIgEFilter" in txt and "GXGigEFilter" in txt
        assert "NicFilterCheck --yes" in txt      # 必须给出可执行的修复命令

    def test_unreadable_registry_does_not_block(self, monkeypatch):
        """读不到注册表 ≠ 装了两个驱动。这种情况必须放过，只留一条日志。"""
        main_yolo, built, errors = self._setup(monkeypatch, reg=None)
        rc = main_yolo.run_production_mode()
        assert built, "读不到注册表却把启动拦下来了 —— 开发机会永远起不来"
        assert not any("拒绝启动" in e for e in errors)
        assert rc == 1          # 后面因为没相机才失败，与拦截无关

    def test_single_filter_does_not_block(self, monkeypatch):
        reg = FakeReg(
            subkeys={NIC_KEY: ["0001"]},
            values={NIC_KEY + r"\0001": {
                        "DriverDesc": "Intel(R) I219-LM",
                        "UpperBindings": ["GXGigEFilter"]},
                    SVC + r"\GXGigEFilter": {
                        "DisplayName": "Galaxy GigE Filter Driver"}})
        main_yolo, built, errors = self._setup(monkeypatch, reg=reg)
        main_yolo.run_production_mode()
        assert built, "只有一个大恒过滤驱动也被拦了"
        assert not any("拒绝启动" in e for e in errors)

    def test_two_filters_on_two_nics_does_not_block(self, monkeypatch):
        reg = FakeReg(
            subkeys={NIC_KEY: ["0001", "0002"]},
            values={
                NIC_KEY + r"\0001": {"DriverDesc": "NIC A",
                                     "UpperBindings": ["MVGIgEFilter"]},
                NIC_KEY + r"\0002": {"DriverDesc": "NIC B",
                                     "UpperBindings": ["GXGigEFilter"]},
                SVC + r"\MVGIgEFilter": {"DisplayName": "Hikvision MVS GigE Filter"},
                SVC + r"\GXGigEFilter": {"DisplayName": "Galaxy GigE Filter"}})
        main_yolo, built, errors = self._setup(monkeypatch, reg=reg)
        main_yolo.run_production_mode()
        assert built, "分挂两张网卡不是冲突，不该拦"
        assert not any("拒绝启动" in e for e in errors)

    def test_check_disabled_skips_gate_entirely(self, monkeypatch):
        main_yolo, built, errors = self._setup(
            monkeypatch, reg_with_two_gige_filters())
        monkeypatch.setitem(main_yolo.LOGGING_CONFIG, "nic_filter_check", False)
        main_yolo.run_production_mode()
        assert built, "nic_filter_check: false 时不该做任何检查"
        assert not any("拒绝启动" in e for e in errors)


# ------------------------------------------- §9 数据来源（真机日志逼出来的）
def _ps_rows():
    """Get-NetAdapterBinding 的真实形状：一张 Realtek 网卡上有 WFP + 两个 GigE"""
    return [
        {"Name": "以太网", "InterfaceDescription": "Realtek PCIe GbE Family Controller",
         "ComponentID": "ms_wfplwf_upper", "Enabled": True, "Class": "Filter"},
        {"Name": "以太网", "InterfaceDescription": "Realtek PCIe GbE Family Controller",
         "ComponentID": "MVGIgEFilter", "Enabled": True, "Class": "Filter"},
        {"Name": "以太网", "InterfaceDescription": "Realtek PCIe GbE Family Controller",
         "ComponentID": "GXGigEFilter", "Enabled": True, "Class": "Filter"},
        # 协议/服务不是过滤驱动，必须被排除
        {"Name": "以太网", "InterfaceDescription": "Realtek PCIe GbE Family Controller",
         "ComponentID": "ms_tcpip", "Enabled": True, "Class": "Protocol"},
        {"Name": "以太网", "InterfaceDescription": "Realtek PCIe GbE Family Controller",
         "ComponentID": "ms_server", "Enabled": True, "Class": "Service"},
    ]


def _ps_runner(rows=None, rc=0, out=None):
    import json as _json
    payload = out if out is not None else _json.dumps(
        rows if rows is not None else _ps_rows())
    def run(cmd, timeout=20):
        run.calls.append(cmd)
        return rc, payload, ""
    run.calls = []
    return run


class TestDataSources:
    def test_non_instance_subkeys_are_not_treated_as_nics(self):
        """真机日志里冒出过「网卡 Configuration (名称未知)」
        「网卡 Properties (名称未知)」——类键下这些不是网卡实例。"""
        reg = FakeReg(
            subkeys={NIC_KEY: ["0000", "0001", "Configuration", "Properties"]},
            values={
                NIC_KEY + r"\0000": {"DriverDesc": "Realtek PCIe GbE"},
                NIC_KEY + r"\0001": {"DriverDesc": "VMware VMnet1"},
            })
        nics = nfc.enumerate_nic_filters(reg=reg)
        keys = [n["nic_key"] for n in nics]
        assert "Configuration" not in keys, keys
        assert "Properties" not in keys, keys
        assert keys == ["0000", "0001"], keys

    def test_powershell_source_finds_what_registry_misses(self):
        """这就是真机那个 bug：注册表的 Upper/LowerBindings 覆盖不到 NDIS 6
        过滤驱动，只有 Get-NetAdapterBinding 能拿到。"""
        reg = FakeReg(                       # 注册表这边**什么都没有**
            subkeys={NIC_KEY: ["0000"]},
            # ⚠️ DriverDesc 必须和 PowerShell 的 InterfaceDescription **同串**：
            #    真机上这两个值来自同一个 INF，是一样的。合并键就靠这个对齐。
            values={NIC_KEY + r"\0000": {
                        "DriverDesc": "Realtek PCIe GbE Family Controller"},
                    SVC + r"\MVGIgEFilter": {"DisplayName": "Hikvision MVS GigE"},
                    SVC + r"\GXGigEFilter": {"DisplayName": "Galaxy GigE"},
                    SVC + r"\ms_wfplwf_upper": {"DisplayName": "WFP Lightweight Filter"}},
        )
        nics = nfc.enumerate_nic_filters(reg=reg, use_powershell=True,
                                         ps_runner=_ps_runner())
        assert len(nics) == 1
        svcs = [f["service"] for f in nics[0]["filters"]]
        assert "MVGIgEFilter" in svcs and "GXGigEFilter" in svcs, svcs
        assert "netadapterbinding" in nics[0]["sources"]

    def test_all_bindings_listed_and_ndis_filter_flag_from_registry(self):
        """契约变更（真机实证逼出来的）：
        `Get-NetAdapterBinding` **没有 -Class 参数**，输出对象有没有 Class
        属性也无法确认。原先按 Class 过滤，一旦属性不存在就会把**所有**记录
        滤掉 —— 静默假阴性。现在改成：全部列出，"是不是 NDIS 过滤驱动"由
        注册表 Services 扫描（FilterClass/FilterRunOrder）判定。"""
        reg = FakeReg(
            subkeys={NIC_KEY: ["0000"],
                     SVC: ["ms_wfplwf_upper", "MVGIgEFilter", "GXGigEFilter"]},
            values={NIC_KEY + r"\0000": {"DriverDesc":
                                          "Realtek PCIe GbE Family Controller"},
                    SVC + r"\ms_wfplwf_upper": {"FilterClass": "Custom"},
                    SVC + r"\MVGIgEFilter": {"FilterClass": "Custom"},
                    SVC + r"\GXGigEFilter": {"FilterClass": "Custom"}})
        nics = nfc.enumerate_nic_filters(reg=reg, use_powershell=True,
                                         ps_runner=_ps_runner())
        by = {f["service"]: f for f in nics[0]["filters"]}
        # 协议/服务绑定也要列出来（完整清单，操作员看得到全貌）
        assert "ms_tcpip" in by and "ms_server" in by
        # 但注册表能认出哪些是真过滤驱动
        assert by["ms_wfplwf_upper"]["is_ndis_filter"] is True
        assert by["ms_tcpip"]["is_ndis_filter"] is False
        assert by["ms_server"]["is_ndis_filter"] is False

    def test_ps_query_does_not_use_the_nonexistent_class_parameter(self):
        """防回归：真机上 `-Class` 会直接报"找不到与参数名称 Class 匹配的参数"。"""
        assert "-Class" not in nfc._PS_QUERY, nfc._PS_QUERY
        assert "Class" not in nfc._PS_QUERY.split("Select-Object")[1]

    def test_powershell_conflict_still_blocks(self):
        """权威来源查到双驱动 → 必须判定为冲突（这是硬闸门的输入）"""
        reg = FakeReg(subkeys={NIC_KEY: ["0000"]},
                      values={NIC_KEY + r"\0000": {"DriverDesc": "Realtek PCIe GbE"},
                              SVC + r"\MVGIgEFilter": {"DisplayName": "Hikvision MVS GigE"},
                              SVC + r"\GXGigEFilter": {"DisplayName": "Galaxy GigE"}})
        nics = nfc.enumerate_nic_filters(reg=reg, use_powershell=True,
                                         ps_runner=_ps_runner())
        rep = nfc.analyse_nic_filters(nics)
        assert len(rep["conflicts"]) == 1
        assert sorted(f["service"] for f in rep["conflicts"][0]["gige_filters"]) == \
            ["GXGigEFilter", "MVGIgEFilter"]

    def test_convert_to_json_single_object_is_handled(self):
        """ConvertTo-Json 只有一条记录时输出对象而不是数组"""
        import json as _json
        one = {"Name": "以太网", "InterfaceDescription": "NIC A",
               "ComponentID": "ms_wfplwf_upper", "Enabled": True, "Class": "Filter"}
        reg = FakeReg(subkeys={NIC_KEY: ["0000"]},
                      values={NIC_KEY + r"\0000": {"DriverDesc": "NIC A"}})
        nics = nfc.enumerate_nic_filters(
            reg=reg, use_powershell=True,
            ps_runner=_ps_runner(out=_json.dumps(one)))
        assert len(nics) == 1
        assert [f["service"] for f in nics[0]["filters"]] == ["ms_wfplwf_upper"]

    def test_powershell_failure_falls_back_to_registry(self):
        def bad_runner(cmd, timeout=20):
            raise OSError("powershell 被策略禁用")
        reg = FakeReg(subkeys={NIC_KEY: ["0000"]},
                      values={NIC_KEY + r"\0000": {"DriverDesc": "NIC A"}})
        nics = nfc.enumerate_nic_filters(reg=reg, use_powershell=True,
                                         ps_runner=bad_runner)
        assert nics is not None, "PowerShell 挂了就整个查不到了？"
        assert nics[0]["sources"] == ["registry"]

    def test_empty_or_garbage_output_means_source_unavailable(self):
        for payload in ("", "   ", "not json at all", "[]", "null"):
            got = nfc.enumerate_via_powershell(
                runner=_ps_runner(out=payload))
            assert got is None, payload


class TestInstalledFilterScan:
    """已安装但未绑定的过滤驱动也要报——"海康 MVS 装过、只是没勾上"
    现在不惹事，但换网卡/重装驱动时可能自己绑回去。"""

    def _reg(self):
        return FakeReg(
            subkeys={NIC_KEY: ["0000"],
                     SVC: ["MVGIgEFilter", "GXGigEFilter", "ms_wfplwf_upper",
                           "Tcpip", "W32Time"]},
            values={
                NIC_KEY + r"\0000": {"DriverDesc": "Realtek PCIe GbE"},
                # 两个 GigE 过滤驱动都装了，但**都没绑定**
                SVC + r"\MVGIgEFilter": {"DisplayName": "Hikvision MVS GigE Filter",
                                         "FilterClass": "Custom",
                                         "FilterRunOrder": ["Begin"]},
                SVC + r"\GXGigEFilter": {"DisplayName": "Galaxy GigE Filter",
                                         "FilterClass": "Custom"},
                SVC + r"\ms_wfplwf_upper": {"DisplayName": "WFP Lightweight Filter",
                                            "FilterClass": "Custom"},
                # 这两个没有 FilterClass / FilterRunOrder → 不是过滤驱动
                SVC + r"\Tcpip": {"DisplayName": "TCP/IP Protocol Driver"},
                SVC + r"\W32Time": {"DisplayName": "Windows Time"},
            })

    def test_only_ndis_filters_are_returned(self):
        got = nfc.enumerate_installed_filters(reg=self._reg())
        names = sorted(f["service"] for f in got)
        assert names == ["GXGigEFilter", "MVGIgEFilter", "ms_wfplwf_upper"], names

    def test_gige_ones_are_flagged(self):
        got = nfc.enumerate_installed_filters(reg=self._reg())
        flagged = sorted(f["service"] for f in got if f["matched_gige"])
        assert flagged == ["GXGigEFilter", "MVGIgEFilter"], flagged

    def test_non_windows_returns_empty_not_error(self, monkeypatch):
        monkeypatch.setattr(nfc.sys, "platform", "linux")
        assert nfc.enumerate_installed_filters() == []


class TestHonestDegradation:
    """只有遗留来源时，日志**不能**给出一个假的 ✓。

    这是真机日志暴露的问题：一张 Realtek GbE 报"无"，而 Windows 8+ 上这块卡
    至少绑着 WFP Lightweight Filter。全空 = 数据源不够，不是真的没有。"""

    def test_registry_only_must_not_claim_success(self):
        reg = FakeReg(
            subkeys={NIC_KEY: ["0000"]},
            values={NIC_KEY + r"\0000": {"DriverDesc": "Realtek PCIe GbE"}})
        out = []
        # 模拟"只有注册表来源"：把 PowerShell 来源打断
        orig = nfc.enumerate_via_powershell
        nfc.enumerate_via_powershell = lambda runner=None, timeout=20: None
        try:
            nfc.report_nic_filters(reg=reg, log_fn=out.append)
        finally:
            nfc.enumerate_via_powershell = orig
        txt = "\n".join(out)
        assert "✓" not in txt, "只有遗留来源却给了 ✓ —— 这是骗人"
        assert "不能" in txt and "NDIS 6" in txt, txt
        assert "Get-NetAdapterBinding" in txt      # 必须给出手工核对命令


class TestReportWithPowerShell:
    """report_nic_filters 走权威来源时的完整输出（之前这条路径在非 Windows 上
    被平台守卫挡掉，测不到——已透传 use_powershell）。"""

    NIC_DESC = "Realtek PCIe GbE Family Controller"

    # 本机**已安装**的过滤驱动（Services 键下）。刻意和"已绑定"分开：
    # 装了不等于绑了，这正是这个测试要覆盖的区别。
    INSTALLED = ("ms_wfplwf_upper", "GXGigEFilter", "MVGIgEFilter")

    def _reg(self, bindings=("ms_wfplwf_upper", "GXGigEFilter")):
        rows = [{"Name": "以太网", "InterfaceDescription": self.NIC_DESC,
                 "ComponentID": c, "Enabled": True, "Class": "Filter"}
                for c in bindings]
        reg = FakeReg(
            subkeys={NIC_KEY: ["0000"],
                     SVC: list(self.INSTALLED) + ["Tcpip"]},
            values={
                NIC_KEY + r"\0000": {"DriverDesc": self.NIC_DESC},
                SVC + r"\ms_wfplwf_upper": {"DisplayName": "WFP Lightweight Filter",
                                            "FilterClass": "Custom"},
                SVC + r"\GXGigEFilter": {"DisplayName": "Galaxy GigE Filter",
                                         "FilterClass": "Custom"},
                SVC + r"\MVGIgEFilter": {"DisplayName": "Hikvision MVS GigE",
                                         "FilterClass": "Custom"},
                SVC + r"\Tcpip": {"DisplayName": "TCP/IP Protocol Driver"},
            })
        return reg, rows

    def test_single_gige_reports_success_and_shows_wfp(self):
        reg, rows = self._reg()
        out = []
        rep = nfc.report_nic_filters(reg=reg, use_powershell=True,
                                     ps_runner=_ps_runner(rows), log_fn=out.append)
        txt = "\n".join(out)
        assert "✓" in txt
        assert "WFP Lightweight Filter" in txt   # 权威来源确实生效了
        assert rep["sources"] == ["netadapterbinding", "registry"]
        assert len(rep["all_filters"]) == 1, "同一块卡被拆成两条了"

    def test_two_gige_via_powershell_warns_and_counts_conflict(self):
        reg, rows = self._reg(("MVGIgEFilter", "GXGigEFilter"))
        out = []
        rep = nfc.report_nic_filters(reg=reg, use_powershell=True,
                                     ps_runner=_ps_runner(rows), log_fn=out.append)
        assert len(rep["conflicts"]) == 1
        assert "⚠" in "\n".join(out)

    def test_installed_but_unbound_is_reported(self):
        """装了但没绑定的 GigE 过滤驱动也要报出来。"""
        reg, rows = self._reg(("ms_wfplwf_upper",))   # 只绑了 WFP
        out = []
        rep = nfc.report_nic_filters(reg=reg, use_powershell=True,
                                     ps_runner=_ps_runner(rows), log_fn=out.append)
        txt = "\n".join(out)
        bound = {f["service"] for e in rep["all_filters"] for f in e["filters"]}
        assert "GXGigEFilter" not in bound           # 确实没绑定
        assert "未绑定到任何网卡" in txt, txt
        assert "已安装的疑似 GigE 过滤驱动" in txt


# --------------------------------- §10 真机实证的大恒 ComponentID（回归钉子）
# 用户在生产机上跑 PowerShell 得到的真实身份：
#   ComponentID = ms_GxGVLWFilter
#   DisplayName = "GigE Vision LightWeight Filter"
#   ImagePath   = ...\GxGVLWFilter.sys
# 我原先 KEEP_PATTERNS 里只有 daheng/galaxy/gxgige/gxiapi —— 一个都不匹配，
# _is_kept() 返回 False，"绝不禁用大恒自家驱动"那道闸在真机上是空的。
REAL_DAHENG = {
    "service": "ms_GxGVLWFilter",
    "display_name": "GigE Vision LightWeight Filter",
    "description": "",
    "image_path": r"\SystemRoot\system32\DRIVERS\GxGVLWFilter.sys",
}


class TestRealDahengComponentId:
    def test_is_kept_recognises_the_real_daheng_driver(self):
        assert nfc._is_kept(REAL_DAHENG) is True, (
            "大恒真机驱动名 ms_GxGVLWFilter 没被认成自己人 —— "
            "禁用工具会去动项目自己要用的驱动")

    def test_attributed_to_daheng_not_generic_gige(self):
        hay = nfc._haystack(REAL_DAHENG["service"], REAL_DAHENG["display_name"],
                            "", REAL_DAHENG["image_path"])
        assert nfc._matches(hay, nfc.DEFAULT_GIGE_PATTERNS) == "gxgv"

    def test_is_kept_by_display_name_alone(self):
        """只有 DisplayName 时也要认得出（注册表读不到 ImagePath 的情况）"""
        assert nfc._is_kept({"service": "ms_GxGVLWFilter", "display_name": "",
                             "description": "", "image_path": ""}) is True

    def test_hikvision_is_still_not_kept(self):
        assert nfc._is_kept({"service": "MVGIgEFilter",
                             "display_name": "MVS GigE Filter Driver",
                             "description": "Hikvision MVS GigE Vision filter",
                             "image_path": r"...\MVGIgEFilter.sys"}) is False

    def test_plan_disable_protects_real_daheng_and_targets_hikvision(self):
        """端到端：真机身份的大恒 + 海康，禁用工具必须只动海康。"""
        reg = FakeReg(
            subkeys={NIC_KEY: ["0000"], SVC: ["ms_GxGVLWFilter", "MVGIgEFilter"]},
            values={
                NIC_KEY + r"\0000": {"DriverDesc": "Realtek PCIe GbE Family Controller",
                                     "UpperBindings": ["ms_GxGVLWFilter",
                                                       "MVGIgEFilter"]},
                SVC + r"\ms_GxGVLWFilter": {
                    "DisplayName": "GigE Vision LightWeight Filter",
                    "FilterClass": "Custom",
                    "ImagePath": r"\SystemRoot\system32\DRIVERS\GxGVLWFilter.sys"},
                SVC + r"\MVGIgEFilter": {"DisplayName": "MVS GigE Filter Driver",
                                         "FilterClass": "Custom"},
            })
        rep = nfc.analyse_nic_filters(nfc.enumerate_nic_filters(reg=reg))
        assert len(rep["conflicts"]) == 1
        plan = nfc.plan_disable(report=rep)
        assert [a["service"] for a in plan["actions"]] == ["MVGIgEFilter"]
        assert [x["service"] for x in plan["skipped"]] == ["ms_GxGVLWFilter"]
        assert "大恒" in plan["skipped"][0]["reason"]


# --------------------------- §11 真机第二轮日志暴露的两个缺陷（回归钉子）
class TestDisplayNameMustFeedClassification:
    """真机实证：以太网那块卡上有**三个** GigE Vision 过滤驱动，
    但只有大恒那个被认出来 —— 因为 _classify() 用的是注册表的 DisplayName，
    而 PowerShell 给的那个补得太晚。漏掉的 HKR_neuGEVFilter 正是海康机器人的。"""

    ETH = "Realtek PCIe GbE Family Controller"
    ROWS = [
        ("ms_GxGVLWFilter", "GigE Vision LightWeight Filter"),
        ("HKR_neuGEVFilter", "Neu GigEVision Image Filter Driver"),
        ("MVTec_Hgevstrm", "MVTec GigE Vision Streaming Filter"),
        ("ms_tcpip", "Internet 协议版本 4 (TCP/IPv4)"),
    ]

    def _nics(self):
        import json as _json
        rows = [{"Name": "以太网", "InterfaceDescription": self.ETH,
                 "ComponentID": c, "DisplayName": d, "Enabled": True}
                for c, d in self.ROWS]
        # 注册表**故意不给** DisplayName —— 真机就是读不到，才暴露了这个 bug
        reg = FakeReg(
            subkeys={NIC_KEY: ["0000"],
                     SVC: ["ms_GxGVLWFilter", "HKR_neuGEVFilter", "MVTec_Hgevstrm"]},
            values={NIC_KEY + r"\0000": {"DriverDesc": self.ETH},
                    SVC + r"\ms_GxGVLWFilter": {"FilterClass": "Custom"},
                    SVC + r"\HKR_neuGEVFilter": {"FilterClass": "Custom"},
                    SVC + r"\MVTec_Hgevstrm": {"FilterClass": "Custom"}})
        return nfc.enumerate_nic_filters(
            reg=reg, use_powershell=True,
            ps_runner=lambda c, t=20: (0, _json.dumps(rows), ""))

    def test_all_three_gige_filters_are_detected(self):
        by = {f["service"]: f["matched_gige"] for f in self._nics()[0]["filters"]}
        assert by["ms_GxGVLWFilter"], by
        assert by["HKR_neuGEVFilter"], "海康机器人的驱动漏检了: %r" % by
        assert by["MVTec_Hgevstrm"], "MVTec 的驱动漏检了: %r" % by

    def test_hikrobot_driver_is_attributed_to_hikrobot(self):
        by = {f["service"]: f["matched_gige"] for f in self._nics()[0]["filters"]}
        assert by["HKR_neuGEVFilter"] == "hkr"

    def test_three_filters_on_one_nic_is_a_conflict(self):
        rep = nfc.analyse_nic_filters(self._nics())
        assert len(rep["conflicts"]) == 1
        assert len(rep["conflicts"][0]["gige_filters"]) == 3

    def test_plan_disable_protects_daheng_and_targets_the_rest(self):
        rep = nfc.analyse_nic_filters(self._nics())
        plan = nfc.plan_disable(report=rep)
        acts = sorted(a["service"] for a in plan["actions"])
        assert acts == ["HKR_neuGEVFilter", "MVTec_Hgevstrm"], acts
        assert [x["service"] for x in plan["skipped"]] == ["ms_GxGVLWFilter"]

    def test_protocol_binding_is_not_counted_as_gige(self):
        by = {f["service"]: f["matched_gige"] for f in self._nics()[0]["filters"]}
        assert by["ms_tcpip"] is None


class TestBase64Transport:
    """乱码根因：Windows PowerShell 5.1 重定向输出用控制台/OEM 代码页
    （中文机器 cp936），不是 UTF-8。改走 base64 后与控制台代码页无关。"""

    def test_query_uses_base64_not_raw_text(self):
        assert "ToBase64String" in nfc._PS_QUERY, nfc._PS_QUERY

    def test_chinese_survives_the_round_trip(self):
        import json as _json, base64 as _b64
        rows = [{"Name": "以太网", "InterfaceDescription": "Realtek PCIe GbE",
                 "ComponentID": "ms_tcpip", "DisplayName": "Internet 协议版本 4",
                 "Enabled": True}]
        payload = _b64.b64encode(_json.dumps(rows).encode("utf-8")).decode("ascii")
        assert payload.isascii()                     # 载荷本身必须纯 ASCII
        got = nfc.enumerate_via_powershell(
            runner=lambda c, t=20: (0, payload, ""))
        assert got[0]["Name"] == "以太网", got
        assert got[0]["DisplayName"] == "Internet 协议版本 4", got

    def test_non_base64_output_still_parses_as_json(self):
        """向后兼容：不是 base64 就当普通 JSON 试一次。"""
        import json as _json
        rows = [{"Name": "NIC A", "InterfaceDescription": "NIC A",
                 "ComponentID": "ms_tcpip", "Enabled": True}]
        got = nfc.enumerate_via_powershell(
            runner=lambda c, t=20: (0, _json.dumps(rows), ""))
        assert got[0]["ComponentID"] == "ms_tcpip"

    def test_error_text_is_not_mistaken_for_data(self):
        got = nfc.enumerate_via_powershell(
            runner=lambda c, t=20: (
                1, "", "Get-NetAdapterBinding : 找不到与参数名称 Class 匹配的参数。"))
        assert got is None

    def test_classification_uses_the_powershell_display_name(self):
        """⚠️ 这条是**专门钉顺序**的，必须和特征表覆盖度解耦。

        上面几条测试其实盖不住顺序 bug：因为 DEFAULT_GIGE_PATTERNS 里补的
        'hkr' / 'gevstrm' 光靠**服务名**就能匹配上，于是把 DisplayName 合并
        挪回 _classify() 之后，测试照样全绿（实测 80 passed，注入无效）。

        所以这里造一个**服务名里没有任何 GigE 线索、只有 DisplayName 有**的
        驱动。它能不能被认出来，只取决于顺序。"""
        import json as _json
        ETH = "Realtek PCIe GbE Family Controller"
        rows = [{"Name": "以太网", "InterfaceDescription": ETH,
                 "ComponentID": "zzz_opaque_svc",          # 服务名毫无线索
                 "DisplayName": "Some GigE Vision Streaming Filter",
                 "Enabled": True}]
        reg = FakeReg(
            subkeys={NIC_KEY: ["0000"], SVC: ["zzz_opaque_svc"]},
            values={NIC_KEY + r"\0000": {"DriverDesc": ETH},
                    SVC + r"\zzz_opaque_svc": {"FilterClass": "Custom"}})
        nics = nfc.enumerate_nic_filters(
            reg=reg, use_powershell=True,
            ps_runner=lambda c, t=20: (0, _json.dumps(rows), ""))
        f = nics[0]["filters"][0]
        assert f["display_name"] == "Some GigE Vision Streaming Filter"
        assert f["matched_gige"], (
            "DisplayName 没有参与分类 —— _classify() 又跑在合并之前了")


# ----------------------- §12 已禁用的过滤驱动不能算冲突（真机第三轮日志）
class TestDisabledFiltersAreNotConflicts:
    """用户按正确步骤操作了：ncpa.cpl 里取消勾选 + 重启。程序却仍然拒绝启动。

    根因：analyse_nic_filters 数的是 `matched_gige` 的全部条目，**从来没看
    enabled**。而在 ncpa.cpl 里取消勾选只是把 Enabled 置为 False —— 绑定项
    **依然存在**，Get-NetAdapterBinding **依然会列出来**，只是不在收包路径上。
    于是"已禁用"被当成了"还挂着"。"""

    ETH = "Realtek PCIe GbE Family Controller"

    def _reg_and_rows(self, enabled_map):
        import json as _json
        rows = []
        for cid, disp in [("HKR_neuGEVFilter", "Neu GigEVision Image Filter Driver"),
                          ("ms_GxGVLWFilter", "GigE Vision LightWeight Filter"),
                          ("MVTec_Hgevstrm", "MVTec GigE Vision Streaming Filter")]:
            rows.append({"Name": "以太网", "InterfaceDescription": self.ETH,
                         "ComponentID": cid, "DisplayName": disp,
                         "Enabled": enabled_map[cid]})
        reg = FakeReg(
            subkeys={NIC_KEY: ["0000"], SVC: list(enabled_map)},
            values={NIC_KEY + r"\0000": {"DriverDesc": self.ETH},
                    SVC + r"\ms_GxGVLWFilter": {"FilterClass": "Custom"},
                    SVC + r"\HKR_neuGEVFilter": {"FilterClass": "Custom"},
                    SVC + r"\MVTec_Hgevstrm": {"FilterClass": "Custom"}})
        return reg, _json.dumps(rows)

    def _rep(self, enabled_map):
        reg, payload = self._reg_and_rows(enabled_map)
        return nfc.report_nic_filters(
            reg=reg, use_powershell=True,
            ps_runner=lambda c, t=20: (0, payload, ""), log_fn=lambda m: None)

    def test_the_users_exact_scenario_now_passes(self):
        """用户的实际操作：海康和 MVTec 取消勾选，大恒保留 → 必须放行。"""
        rep = self._rep({"HKR_neuGEVFilter": False,
                         "ms_GxGVLWFilter": True,
                         "MVTec_Hgevstrm": False})
        assert rep["conflicts"] == [], "已禁用的还在算冲突"

    def test_all_enabled_is_still_a_conflict(self):
        """反向守住：三个都启用时必须照样拦。"""
        rep = self._rep({"HKR_neuGEVFilter": True,
                         "ms_GxGVLWFilter": True,
                         "MVTec_Hgevstrm": True})
        assert len(rep["conflicts"]) == 1
        assert len(rep["conflicts"][0]["gige_active"]) == 3

    def test_two_enabled_is_still_a_conflict(self):
        rep = self._rep({"HKR_neuGEVFilter": True,
                         "ms_GxGVLWFilter": True,
                         "MVTec_Hgevstrm": False})
        assert len(rep["conflicts"]) == 1
        assert len(rep["conflicts"][0]["gige_active"]) == 2

    def test_disabled_ones_are_still_listed_and_marked(self):
        """已禁用的仍然要打印出来 —— 操作员得看得见它们还在，只是被关掉了。"""
        rep = self._rep({"HKR_neuGEVFilter": False,
                         "ms_GxGVLWFilter": True,
                         "MVTec_Hgevstrm": False})
        out = []
        reg, payload = self._reg_and_rows({"HKR_neuGEVFilter": False,
                                           "ms_GxGVLWFilter": True,
                                           "MVTec_Hgevstrm": False})
        nfc.report_nic_filters(reg=reg, use_powershell=True,
                               ps_runner=lambda c, t=20: (0, payload, ""),
                               log_fn=out.append)
        txt = "\n".join(out)
        assert "HKR_neuGEVFilter" in txt and "MVTec_Hgevstrm" in txt
        assert "已禁用" in txt
        assert len(rep["all_filters"][0]["gige_disabled"]) == 2

    def test_singles_reports_the_active_one_not_a_disabled_one(self):
        """日志里 ✓ 那行必须报**启用中**的那个。
        原来取 gige_filters[0]，会把一个已经不在收包路径上的驱动
        说成"这张卡上唯一在用的 GigE 驱动"。"""
        reg, payload = self._reg_and_rows({"HKR_neuGEVFilter": False,
                                           "ms_GxGVLWFilter": True,
                                           "MVTec_Hgevstrm": False})
        out = []
        nfc.report_nic_filters(reg=reg, use_powershell=True,
                               ps_runner=lambda c, t=20: (0, payload, ""),
                               log_fn=out.append)
        singles_line = [l for l in out if l.strip().startswith("网卡 以太网")]
        assert singles_line
        assert "ms_GxGVLWFilter" in singles_line[0], singles_line[0]
        assert "HKR_neuGEVFilter" not in singles_line[0]

    def test_unknown_enabled_state_is_treated_as_active(self):
        """只有注册表来源时拿不到 Enabled —— 一律**当启用**，宁可多拦不漏拦。"""
        reg = FakeReg(
            subkeys={NIC_KEY: ["0001"]},
            values={NIC_KEY + r"\0001": {
                        "DriverDesc": "NIC",
                        "UpperBindings": ["MVGIgEFilter", "GXGigEFilter"]},
                    SVC + r"\MVGIgEFilter": {"DisplayName": "Hikvision MVS GigE"},
                    SVC + r"\GXGigEFilter": {"DisplayName": "Galaxy GigE"}})
        nics = nfc.enumerate_nic_filters(reg=reg)
        assert all(f["enabled"] is None for f in nics[0]["filters"])
        rep = nfc.analyse_nic_filters(nics)
        assert len(rep["conflicts"]) == 1, "状态未知时必须按启用处理"

    def test_plan_disable_skips_already_disabled(self):
        reg, payload = self._reg_and_rows({"HKR_neuGEVFilter": False,
                                           "ms_GxGVLWFilter": True,
                                           "MVTec_Hgevstrm": True})
        rep = nfc.report_nic_filters(
            reg=reg, use_powershell=True,
            ps_runner=lambda c, t=20: (0, payload, ""), log_fn=lambda m: None)
        plan = nfc.plan_disable(report=rep)
        assert [a["service"] for a in plan["actions"]] == ["MVTec_Hgevstrm"]

    def test_registry_path_does_not_claim_enabled(self):
        """注册表来源给不出启用状态，不能谎报 True。"""
        reg = FakeReg(
            subkeys={NIC_KEY: ["0001"]},
            values={NIC_KEY + r"\0001": {
                        "DriverDesc": "NIC", "UpperBindings": ["GXGigEFilter"]},
                    SVC + r"\GXGigEFilter": {"DisplayName": "Galaxy GigE"}})
        nics = nfc.enumerate_nic_filters(reg=reg)
        assert nics[0]["filters"][0]["enabled"] is None
