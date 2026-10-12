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
