#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
文件名: General_Tool/NicFilterCheck.py
模块概述: 启动时检查网卡上是否同时挂了多个 GigE Vision 过滤驱动

背景（为什么值得单独写一个模块）：
    本项目从海康 MVS SDK 换到大恒 Galaxy SDK。代码里的海康调用已经清零，
    但**驱动是装在操作系统上的，不随代码走**。如果网卡上同时挂着海康的
    和大恒的两个 NDIS 过滤驱动，两个驱动会在同一条收包路径上互相抢包 ——
    这是"整机网络栈卡死 / 电脑死机"的高频原因，而且**从 Python 侧完全看不出
    异常**：进程还活着，只是整台机器不动了。

    所以这件事只能在启动时主动查一次，把结果打进日志。

判定依据（都是注册表，不需要装任何第三方工具）：
    1. HKLM\\SYSTEM\\CurrentControlSet\\Control\\Class\\
       {4d36e972-e325-11ce-bfc1-08002be10318}\\<NNNN>
       每个子键是一张网卡实例，``UpperBindings`` / ``LowerBindings``
       （REG_MULTI_SZ）列出绑在它上面的 NDIS 过滤驱动服务名。
    2. HKLM\\SYSTEM\\CurrentControlSet\\Services\\<服务名>
       取 ``DisplayName`` / ``Description`` / ``ImagePath`` 用来认厂商。

安全性（和 CameraOperation 的包长路径同一条纪律）：
    **任何一步失败都只返回 None / 空结果，绝不抛异常，绝不阻塞启动。**
    查不到驱动的失败模式必须是"日志里说查不到"，不能是"相机起不来"。

可测试性：
    注册表访问全部收在 ``_Registry`` 协议里，测试注入假实现即可，
    不需要 Windows 也能把判定逻辑跑满。
===============================================================================
"""

import sys
from typing import Any, Dict, List, Optional, Sequence

from General_Tool.EnhancedLogger import info, warning, error

# 网卡类 GUID（NDIS 的 "Net" 类），所有网卡实例都挂在这个键下面
_NIC_CLASS_KEY = (
    r"SYSTEM\CurrentControlSet\Control\Class"
    r"\{4d36e972-e325-11ce-bfc1-08002be10318}"
)
_SERVICES_KEY = r"SYSTEM\CurrentControlSet\Services"

# 扫描上限。正常机器上网卡实例是几个到十几个；这个上限纯粹是防御性的，
# 避免注册表异常时把启动流程拖住。
_MAX_NIC_KEYS = 64
_MAX_FILTERS_PER_NIC = 64

# ------------------------------------------------------------------
# GigE Vision 过滤驱动的名字特征（大小写不敏感子串匹配）
# ------------------------------------------------------------------
# ⚠️ 这份表**不保证完整**，尤其是海康的确切服务名随 MVS 版本变过。
#    所以本模块的设计是"名字匹配只做高亮，完整列表一律打印"——
#    即使这张表漏了某个驱动，操作员也能从日志里看出来。
#    现场发现漏了就往 config.yaml 的 logging.nic_filter_patterns 里加。
#
# ⚠️ **顺序有意义**：_matches 返回第一个命中的特征串，而它会被打进日志
#    （"命中 'xxx'"）。厂商特征必须排在通用特征前面，否则日志只会说
#    "命中 'gige'" 而不说是哪家的 —— 而"是哪家"正是操作员决定卸谁所需要的信息。
DEFAULT_GIGE_PATTERNS: Sequence[str] = (
    # ---- 厂商特征优先（日志里要能看出是哪家的驱动）----
    # 海康 MVS
    "hikvision",
    "hikrobot",
    "mvs gige",
    "mvgige",
    "mv_gige",
    "mvs",
    # 大恒 Galaxy
    "daheng",
    "gxgige",
    "gxiapi",
    "galaxy",
    # 其他常见 GigE Vision 厂商（现场可能装过试用版驱动）
    "basler",
    "pleora",
    "matrox",
    "stemmer",
    "nte ",
    # ---- 通用特征兜底 ----
    "gige filter",
    "gigefilter",
    "gige_filter",
    "gigabit ethernet filter",
    "gvsp",
    "gige",
)

# 明确的"抓包类"过滤驱动。它们本身不是 GigE，但会挂在收包路径上，
# 数量多了一样值得看一眼。
CAPTURE_PATTERNS: Sequence[str] = (
    "npcap", "winpcap", "packet driver", "ndiscap",
)


# ==================================================================
#  注册表访问层（唯一的平台依赖，测试可整体替换）
# ==================================================================

class _Registry:
    """HKLM 注册表的最小读取封装。

    单独抽一层的原因：判定逻辑（哪些算 GigE、几个算重复）才是需要测的
    部分，不该和 winreg 绑死。测试注入假的 _Registry 就能在非 Windows 上
    把所有分支跑满。
    """

    def subkeys(self, path: str) -> List[str]:
        raise NotImplementedError

    def value(self, path: str, name: str) -> Any:
        raise NotImplementedError


class _WinRegistry(_Registry):
    """真实的 winreg 实现。**任何异常都吞掉，返回空值。**"""

    def __init__(self):
        try:
            import winreg
            self._winreg = winreg
        except Exception:
            self._winreg = None

    def available(self) -> bool:
        return self._winreg is not None

    def subkeys(self, path: str) -> List[str]:
        if self._winreg is None:
            return []
        out = []
        try:
            with self._winreg.OpenKey(self._winreg.HKEY_LOCAL_MACHINE,
                                      path) as key:
                i = 0
                while len(out) < _MAX_NIC_KEYS:
                    try:
                        out.append(self._winreg.EnumKey(key, i))
                    except OSError:
                        break
                    i += 1
        except Exception:
            return out
        return out

    def value(self, path: str, name: str) -> Any:
        if self._winreg is None:
            return None
        try:
            with self._winreg.OpenKey(self._winreg.HKEY_LOCAL_MACHINE,
                                      path) as key:
                v, _ = self._winreg.QueryValueEx(key, name)
                return v
        except Exception:
            return None


def _as_str_list(v) -> List[str]:
    """REG_MULTI_SZ 可能是 list[str]，也可能被写成单个 str，都接住"""
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if x and str(x).strip()]
    return []


def _haystack(*parts) -> str:
    return " ".join(str(p) for p in parts if p).lower()


def _matches(text: str, patterns: Sequence[str]) -> Optional[str]:
    """返回第一个命中的特征串；没命中返回 None。

    大小写不敏感。调用方（_haystack）已经转过小写，这里再转一次是**防御性**的：
    特征匹配漏掉大小写会直接导致漏报冲突，而这个函数的名字并没有说明
    "入参必须已经是小写"这种前提。

    注意顺序：patterns 里排在前面的优先返回，所以厂商特征要放在通用特征前面。
    """
    if not text:
        return None
    low = str(text).lower()
    for p in patterns:
        if p and str(p).lower() in low:
            return p
    return None


# ==================================================================
#  枚举 + 判定
# ==================================================================

def enumerate_nic_filters(reg: Optional[_Registry] = None,
                          gige_patterns: Optional[Sequence[str]] = None,
                          capture_patterns: Optional[Sequence[str]] = None
                          ) -> Optional[List[Dict]]:
    """枚举每张网卡上绑定的 NDIS 过滤驱动。

    :param reg:              注册表实现，默认用真实 winreg；测试注入假实现
    :param gige_patterns:    覆盖默认的 GigE 特征表（现场发现漏了某个厂商时，
                             从 config.yaml 的 ``logging.nic_filter_patterns``
                             传进来）。传 None 用 :data:`DEFAULT_GIGE_PATTERNS`。
    :param capture_patterns: 同上，抓包类驱动特征表
    :return: list of dict，每项一张网卡：
             ``{"nic_key", "driver_desc", "filters": [{"service",
             "display_name", "description", "image_path",
             "matched_gige", "matched_capture"}]}``；
             **非 Windows / 读不到注册表时返回 None**（不是空列表——
             空列表的意思是"查到了，一张网卡都没有"，两者不能混）。
    """
    gp = tuple(gige_patterns) if gige_patterns else DEFAULT_GIGE_PATTERNS
    cp = tuple(capture_patterns) if capture_patterns else CAPTURE_PATTERNS

    if reg is None:
        # 只有走默认路径才判平台：调用方**显式**传了 reg，说明它明确要用
        # 这个实现（测试注入假注册表就是靠这个），不该被平台检查挡掉。
        if sys.platform != "win32":
            return None
        reg = _WinRegistry()
        if not reg.available():
            return None

    try:
        keys = reg.subkeys(_NIC_CLASS_KEY)
    except Exception:
        return None
    if not keys:
        # 键存在但一个子键都没有：要么真没网卡，要么权限不够。
        # 一律当"没查到"，不猜。
        return None

    nics = []
    for sub in keys:
        try:
            base = _NIC_CLASS_KEY + "\\" + sub
            desc = reg.value(base, "DriverDesc")
            bound = list(_as_str_list(reg.value(base, "UpperBindings")))
            bound += [s for s in _as_str_list(reg.value(base, "LowerBindings"))
                      if s not in bound]

            filters = []
            for svc in bound[:_MAX_FILTERS_PER_NIC]:
                sbase = _SERVICES_KEY + "\\" + svc
                disp = reg.value(sbase, "DisplayName")
                descr = reg.value(sbase, "Description")
                img = reg.value(sbase, "ImagePath")
                hay = _haystack(svc, disp, descr, img)
                filters.append({
                    "service": svc,
                    "display_name": disp or "",
                    "description": descr or "",
                    "image_path": img or "",
                    "matched_gige": _matches(hay, gp),
                    "matched_capture": _matches(hay, cp),
                })

            nics.append({
                "nic_key": sub,
                "driver_desc": desc or "",
                "filters": filters,
            })
        except Exception:
            # 单张网卡读失败不影响其他网卡
            continue

    return nics if nics else None


def analyse_nic_filters(nics: Optional[List[Dict]]) -> Dict:
    """判定有没有"多个 GigE 过滤驱动挂在同一张网卡上"。

    这个函数不碰注册表，纯逻辑，所以能在任何平台上测。

    :return: dict
        ``checked``     bool，是否真的查到了数据
        ``conflicts``   list，每项 ``{"nic", "driver_desc", "filters"}``，
                        表示这张网卡上挂了 ≥2 个疑似 GigE 过滤驱动
        ``singles``     list，只有 1 个疑似 GigE 过滤驱动的网卡（正常情况）
        ``all_filters`` 每张网卡的完整过滤驱动清单（**不论是否匹配都列**）
    """
    out = {"checked": bool(nics), "conflicts": [], "singles": [],
           "all_filters": []}
    if not nics:
        return out

    for nic in nics:
        filters = nic.get("filters") or []
        gige = [f for f in filters if f.get("matched_gige")]
        entry = {
            "nic": nic.get("nic_key", ""),
            "driver_desc": nic.get("driver_desc", ""),
            "filters": filters,
            "gige_filters": gige,
        }
        out["all_filters"].append(entry)
        if len(gige) >= 2:
            out["conflicts"].append(entry)
        elif len(gige) == 1:
            out["singles"].append(entry)
    return out


def _fmt_filter(f: Dict) -> str:
    tag = ""
    if f.get("matched_gige"):
        tag = f"  [疑似GigE: 命中 '{f['matched_gige']}']"
    elif f.get("matched_capture"):
        tag = f"  [抓包驱动: 命中 '{f['matched_capture']}']"
    name = f.get("display_name") or f.get("service", "?")
    return f"{f.get('service', '?')}  ({name}){tag}"


def report_nic_filters(report: Optional[Dict] = None,
                       reg: Optional[_Registry] = None,
                       log_fn=None,
                       gige_patterns: Optional[Sequence[str]] = None) -> Dict:
    """查 + 打日志。**这是给 main_yolo 启动流程调的入口，永不抛异常。**

    日志策略（刻意的设计）：
      * 查到冲突 → WARNING，逐个点名，并写明卸载海康那个
      * 只查到 1 个 → INFO，把它的名字打出来，让人知道检查确实跑了
      * **查不到（非 Windows / 无权限 / 无网卡）→ INFO 说明原因，不报错**
        失败模式必须是"没查成"，不能是"启动失败"
      * 无论哪种情况都把**完整过滤驱动清单**打出来：名字表不可能全，
        漏掉某个厂商时操作员还能靠眼睛看出来

    :param gige_patterns: 覆盖默认 GigE 特征表（来自
                          ``config.yaml: logging.nic_filter_patterns``）
    :return: analyse_nic_filters() 的结果；异常时返回 ``{"checked": False}``
    """
    log = log_fn or info
    warn = log_fn or warning

    try:
        nics = enumerate_nic_filters(reg, gige_patterns=gige_patterns)
    except Exception as e:                       # 防御性兜底，正常到不了
        try:
            log(f"[网卡过滤驱动] 检查失败（不影响启动）: {e!r}")
        except Exception:
            pass
        return {"checked": False}

    if nics is None:
        try:
            if sys.platform != "win32":
                log("[网卡过滤驱动] 非 Windows 平台，跳过检查")
            else:
                log("[网卡过滤驱动] 未能读取网卡绑定信息（可能是权限不足），"
                    "跳过检查。可手工用 'netcfg -q' 或设备管理器 → 网卡属性 "
                    "→ 查看已安装的项目 确认")
        except Exception:
            pass
        return {"checked": False}

    result = analyse_nic_filters(nics)
    conflicts = result["conflicts"]

    try:
        if conflicts:
            warn("[网卡过滤驱动] ⚠ 发现 {} 张网卡上同时挂了多个疑似 "
                 "GigE Vision 过滤驱动 —— 这是整机网络栈卡死/电脑死机的"
                 "高频原因，务必只保留大恒 Galaxy 的那一个：".format(
                     len(conflicts)))
            for c in conflicts:
                warn("  网卡 {} ({}):".format(
                    c["nic"], c["driver_desc"] or "名称未知"))
                for f in c["gige_filters"]:
                    warn("      " + _fmt_filter(f))
            warn("  处理办法：控制面板 → 网络适配器 → 属性，取消勾选海康 "
                 "MVS 的 GigE Filter Driver，或直接卸载海康 MVS；"
                 "只留大恒 Galaxy 的过滤驱动。")
            warn("  详见 docs/SDK迁移_海康转大恒.md 的现场部署步骤。")
        elif result["singles"]:
            log("[网卡过滤驱动] ✓ 未发现重复的 GigE 过滤驱动（每张网卡最多 1 个）")
            for s in result["singles"]:
                log("  网卡 {} ({}): {}".format(
                    s["nic"], s["driver_desc"] or "名称未知",
                    _fmt_filter(s["gige_filters"][0])))
        else:
            log("[网卡过滤驱动] 未识别到任何疑似 GigE 过滤驱动。"
                "如果现场确实在用 GigE 相机，请把下面清单里的驱动名补进 "
                "config.yaml 的 logging.nic_filter_patterns。")

        # 完整清单一律打印：名字表不可能覆盖所有厂商
        log("[网卡过滤驱动] 各网卡已绑定的过滤驱动完整清单：")
        for e in result["all_filters"]:
            if not e["filters"]:
                log("  网卡 {} ({}): 无".format(
                    e["nic"], e["driver_desc"] or "名称未知"))
                continue
            log("  网卡 {} ({}):".format(
                e["nic"], e["driver_desc"] or "名称未知"))
            for f in e["filters"]:
                log("      " + _fmt_filter(f))
    except Exception:
        # 打印本身失败也不能影响启动
        pass

    return result


def check_nic_filter_drivers(gige_patterns=None) -> Dict:
    """启动流程用的最简入口：**保证不抛异常**。

    :param gige_patterns: 现场补充的 GigE 特征串（来自 config.yaml 的
                          ``logging.nic_filter_patterns``）。**追加**到默认表上，
                          不是替换——默认表已经覆盖了海康/大恒/Basler 等常见厂商。
    """
    try:
        pats = None
        if gige_patterns:
            extra = [str(x).strip().lower() for x in gige_patterns
                     if x and str(x).strip()]
            if extra:
                pats = tuple(DEFAULT_GIGE_PATTERNS) + tuple(extra)
        return report_nic_filters(gige_patterns=pats)
    except Exception as e:
        try:
            error(f"[网卡过滤驱动] 检查异常（已忽略，不影响启动）: {e!r}")
        except Exception:
            pass
        return {"checked": False}


# ============================================================================
#  显式禁用（人工调用，**绝不在启动路径上**）
# ============================================================================
#
# 为什么这部分不能挂到 main_yolo 里：
#
#   1. 绑定状态在 HKLM\SYSTEM\CurrentControlSet\... 下，**写入需要管理员权限**。
#      本项目全仓库零提权代码、零 subprocess 调用（已 grep 确认），程序是普通
#      用户身份跑的。普通用户写 HKLM = PermissionError，而本模块的纪律是"永不
#      抛异常"，于是会变成**静默失效**——日志说已禁用、其实什么都没做。这比
#      不做更糟。
#   2. 禁用过滤驱动会让**网卡短暂 reset**，8 路 GigE 相机全部断线重连。在生产
#      运行中执行等于主动制造一次停机。
#   3. 万一这台机器上还有别处在用海康相机，静默禁用会造成一个"某台相机突然
#      找不到"的故障，排查方向完全错。
#
# 所以这里的入口是命令行：由现场人员在**停产时**、**以管理员身份**、**显式加
# --yes** 才会执行。main_yolo 永远不调用本文件里任何 disable_* 函数。

# 绝对不能动的驱动：大恒自己的过滤驱动。
# 这是硬约束，不是可配置项——把它关掉等于让本项目自己收不到图。
KEEP_PATTERNS: Sequence[str] = (
    "daheng",
    "galaxy",
    "gxgige",
    "gxiapi",
    "gx_igev",
)


def is_admin() -> bool:
    """当前进程是否有管理员权限。任何异常一律当 False。"""
    try:
        if sys.platform != "win32":
            return False
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _is_kept(f: Dict) -> bool:
    """这个驱动是否属于"绝对不能动"的（大恒自己的）。"""
    return _matches(_haystack(f.get("service", ""),
                             f.get("display_name", ""),
                             f.get("description", ""),
                             f.get("image_path", "")), KEEP_PATTERNS) is not None


def plan_disable(report: Optional[Dict] = None,
                 reg: Optional[_Registry] = None,
                 only_conflicting_nics: bool = True) -> Dict:
    """**纯函数**：算出要禁用哪些驱动、以及确切的 PowerShell 命令。不执行任何东西。

    三条安全不变量（都有测试盯着）：

    1. **绝不禁用大恒自己的驱动**（匹配 KEEP_PATTERNS 的一律跳过）
    2. **绝不把某张网卡上最后一个 GigE 过滤驱动也禁掉**——那会让相机彻底收不到图
    3. 默认只处理**有冲突的网卡**；两个过滤驱动分挂两张网卡不算冲突、也不该动

    :return: ``{"actions": [...], "skipped": [...], "blocked": [...]}``
             每个 action 是 ``{"nic", "nic_desc", "service", "display_name",
             "probe_cmd", "disable_cmd"}``
    """
    if report is None:
        report = analyse_nic_filters(enumerate_nic_filters(reg=reg))

    actions: List[Dict] = []
    skipped: List[Dict] = []
    blocked: List[Dict] = []

    nics = report.get("all_filters") or []
    conflicting = {c.get("nic") for c in (report.get("conflicts") or [])}

    for nic in nics:
        nic_name = nic.get("nic")
        if only_conflicting_nics and nic_name not in conflicting:
            continue
        filters = nic.get("filters") or []
        # ⚠️ 用**下标**而不是字典相等来标记身份。同一张网卡上完全可能出现两条
        #    字段一模一样的驱动记录，用 `f in keepable` 会互相误判。
        gige_idx = [i for i, f in enumerate(filters) if f.get("matched_gige")]

        # 不变量 1：大恒自家驱动永远不动
        protected = {i for i in gige_idx if _is_kept(filters[i])}
        candidates = [i for i in gige_idx if i not in protected]

        # 不变量 2：这张网卡上必须至少留下一个 GigE 过滤驱动。
        #   已经有受保护的（大恒）了 → 候选全都可以禁；
        #   一个受保护的都没有     → 保留第一个候选，其余禁掉（留哪个由现场定，
        #                              工具不擅自替人做这个决定）。
        if not protected and candidates:
            protected.add(candidates[0])
            candidates = candidates[1:]
        actionable = set(candidates)

        for i in gige_idx:
            f = filters[i]
            svc = str(f.get("service") or "").strip()
            desc = str(nic.get("driver_desc") or "").strip()
            rec = {"nic": nic_name, "nic_desc": desc,
                   "service": svc, "display_name": f.get("display_name") or ""}
            if not svc:
                blocked.append(dict(rec, reason="服务名为空，无法定位组件"))
                continue
            if _is_kept(f):
                skipped.append(dict(rec, reason="属于大恒 Galaxy 自家驱动，禁止禁用"))
                continue
            if i not in actionable:
                skipped.append(dict(
                    rec, reason="保留项：禁掉它这张网卡上就没有 GigE 过滤驱动了，"
                                "相机会收不到图"))
                continue
            # ⚠️ 描述为空就**拒绝**，不要生成命令。
            #    生成的命令会是 `-eq ''`，在 PowerShell 里匹配不到任何网卡，
            #    于是"执行成功"但什么都没改 —— 正是要避免的静默失效。
            #    宁可不做，也不要让操作员以为做了。
            if not desc:
                blocked.append(dict(
                    rec, reason="注册表里 DriverDesc 为空，无法定位这张网卡；"
                                "请手工在设备管理器里处理"))
                continue
            # 按 InterfaceDescription 定位，而不是靠猜连接名——DriverDesc 是注册表里
            # 能拿到的，而 Get-NetAdapterBinding 需要的连接名（"以太网 2"之类）拿不到。
            desc = desc.replace("'", "''")
            rec["probe_cmd"] = (
                "Get-NetAdapterBinding -ComponentID '{}' | "
                "Where-Object {{ $_.InterfaceDescription -eq '{}' }} | "
                "Format-List Name, InterfaceDescription, ComponentID, Enabled"
            ).format(svc, desc)
            rec["disable_cmd"] = (
                "Get-NetAdapterBinding -ComponentID '{}' | "
                "Where-Object {{ $_.InterfaceDescription -eq '{}' }} | "
                "Disable-NetAdapterBinding"
            ).format(svc, desc)
            actions.append(rec)

    return {"actions": actions, "skipped": skipped, "blocked": blocked}


def disable_filter_bindings(confirm: bool = False,
                            only_conflicting_nics: bool = True,
                            runner=None) -> Dict:
    """真正执行禁用。**默认只 dry-run**。

    :param confirm: 必须显式为 True 才会执行（CLI 上的 ``--yes``）。
                    为 False 时只打印计划，不动任何东西。
    :param runner:  执行命令的可调用对象，签名 ``runner(cmd) -> (rc, out, err)``。
                    默认用 PowerShell。抽出来是为了能在非 Windows 上测试编排逻辑。
    :return: ``{"admin", "dry_run", "plan", "results"}``
    """
    plan = plan_disable(only_conflicting_nics=only_conflicting_nics)
    out: Dict = {"admin": is_admin(), "dry_run": not confirm,
                 "plan": plan, "results": []}

    acts = plan["actions"]
    print(f"\n[网卡过滤驱动] 计划禁用 {len(acts)} 个过滤驱动：")
    for a in acts:
        print(f"  网卡 {a['nic']} ({a['nic_desc']}) → {a['service']}"
              f"  ({a['display_name']})")
    for s in plan["skipped"]:
        print(f"  跳过 {s['service']}: {s['reason']}")
    for b in plan["blocked"]:
        print(f"  拒绝 {b['service']}: {b['reason']}")

    if not confirm:
        print("\n[网卡过滤驱动] 这是预演（dry-run），没有改动任何东西。"
              "确认无误后加 --yes 才会真的执行。")
        return out

    if not out["admin"]:
        print("\n[网卡过滤驱动] ✗ 当前不是管理员权限，无法修改网卡绑定。"
              "请用管理员身份重新打开命令行。", file=sys.stderr)
        out["error"] = "not_admin"
        return out

    if not acts:
        print("\n[网卡过滤驱动] 没有需要禁用的驱动。")
        return out

    print("\n[网卡过滤驱动] ⚠ 执行后网卡会短暂 reset，所有 GigE 相机会断线重连。"
          "请确认产线已停。")
    if runner is None:
        runner = _run_powershell
    for a in acts:
        print(f"\n$ {a['probe_cmd']}")
        rc, o, e = runner(a["probe_cmd"])
        if o:
            print(o.rstrip())
        if "Enabled : True" not in (o or ""):
            print("  → 该组件在这张网卡上未处于启用状态，跳过禁用。")
            out["results"].append({**a, "rc": rc, "skipped": True})
            continue
        print(f"$ {a['disable_cmd']}")
        rc, o, e = runner(a["disable_cmd"])
        out["results"].append({**a, "rc": rc, "skipped": False})
        print("  → 完成" if rc == 0 else f"  → 失败 (rc={rc}) {e}")

    print("\n[网卡过滤驱动] 全部执行完毕。建议**重启本机**让 NDIS 绑定彻底干净，"
          "然后重新启动本程序确认日志里出现 ✓。")
    return out


def _run_powershell(cmd: str):
    """执行一条 PowerShell 命令，返回 (returncode, stdout, stderr)。"""
    import subprocess
    p = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
        capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout or "", p.stderr or ""


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口：``python -m General_Tool.NicFilterCheck [--yes] [--all-nics]``"""
    argv = list(sys.argv[1:] if argv is None else argv)
    yes = "--yes" in argv
    all_nics = "--all-nics" in argv

    print("=" * 72)
    print(" 网卡 GigE 过滤驱动检查 / 禁用工具")
    print("=" * 72)
    print(f" 管理员权限: {'是' if is_admin() else '否（禁用操作需要管理员权限）'}")
    print()
    rep = analyse_nic_filters(enumerate_nic_filters())
    if not rep.get("checked"):
        print("\n读不到注册表里的网卡绑定信息，无法继续。")
        return 2
    for c in rep.get("conflicts") or []:
        print(f"\n⚠ 冲突网卡 {c.get('nic')} ({c.get('driver_desc')}):")
        for f in c.get("filters") or []:
            print(f"    {_fmt_filter(f)}")
    if not rep.get("conflicts"):
        print("✓ 未发现同一张网卡上有多个 GigE 过滤驱动，无需处理。")

    res = disable_filter_bindings(confirm=yes,
                                  only_conflicting_nics=not all_nics)
    return 0 if res.get("error") is None else 3


if __name__ == "__main__":
    sys.exit(main())
