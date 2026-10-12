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

import json
import re
import sys
from typing import Any, Dict, List, Optional, Sequence

from General_Tool.EnhancedLogger import info, warning, error

# 网卡类 GUID（NDIS 的 "Net" 类），所有网卡实例都挂在这个键下面
_NIC_CLASS_KEY = (
    r"SYSTEM\CurrentControlSet\Control\Class"
    r"\{4d36e972-e325-11ce-bfc1-08002be10318}"
)
_SERVICES_KEY = r"SYSTEM\CurrentControlSet\Services"

# ⚠️ 网卡类键下面的子键**不全是网卡实例**。除了 0000/0001/... 之外还有
#    Properties、Configuration 等非实例键。真机日志实证过：不加这个过滤，
#    日志里会冒出「网卡 Configuration (名称未知)」「网卡 Properties (名称未知)」。
_NIC_INSTANCE_RE = re.compile(r"^\d{4}$")

# 扫描上限。正常机器上网卡实例是几个到十几个；这个上限纯粹是防御性的，
# 避免注册表异常时把启动流程拖住。
_MAX_NIC_KEYS = 64
_MAX_FILTERS_PER_NIC = 64
_MAX_SERVICE_KEYS = 8192

# ------------------------------------------------------------------
# 数据来源
# ------------------------------------------------------------------
# ⚠️ 这是本模块**最关键**的一点，也是真机日志逼出来的教训：
#
#   `UpperBindings` / `LowerBindings` 是 **NDIS 5.x 时代**中间驱动/协议驱动
#   的绑定机制。**NDIS 6 轻量过滤驱动（GigE Vision 过滤驱动就是这一类）
#   不写这两个值。** 所以在现代 Windows 上只读它们，结果是每张网卡都"无"。
#   真机日志实证：一块 Realtek PCIe GbE 报"无"，而任何 Windows 8+ 机器上
#   这块卡至少绑着 "WFP Lightweight Filter"。全空 = 读错了地方。
#
# 因此改用三个来源合并：
#   1. `Get-NetAdapterBinding`（**权威**，就是设备管理器那个勾选框的数据源，
#      只读、不需要管理员权限）
#   2. 扫描 Services 找**已安装**的 NDIS 过滤驱动（判据：服务键下有
#      FilterClass / FilterRunOrder）—— 抓"装了但没绑定"的情况，这对判断
#      "海康 MVS 到底卸干净没有"更重要
#   3. UpperBindings / LowerBindings（保留，兼容老的 NDIS 5 驱动）
# ⚠️ **不要**用 `Get-NetAdapterBinding -Class Filter`：真机实证该 cmdlet
#    **没有 -Class 参数**（"找不到与参数名称 Class 匹配的参数"）。
#    也不要在 Select-Object 里取 Class —— 无法确认输出对象有这个属性，
#    而一旦没有，Python 侧 `row["Class"] == "filter"` 会把**所有**记录都
#    过滤掉，变成静默的假阴性。
#    所以这里取全部绑定项，"是不是 NDIS 过滤驱动"改由注册表 Services 扫描
#    （FilterClass / FilterRunOrder）来判定 —— 那个判据是可靠的。
# ⚠️ 输出走 **base64**，这是为了根除中文乱码。
#
#   Windows PowerShell 5.1 在 stdout 被重定向时用的是**控制台/OEM 代码页**
#   （中文机器 cp936），不是 UTF-8。真机实证：直接取文本会把"以太网"变成
#   "?̫?"、"Microsoft 网络客户端"变成"Microsoft ????ͻ???"。
#   靠猜代码页不可靠（ANSI 页和 OEM 页在非中文系统上还不是同一个）。
#
#   base64 只含 ASCII，**与控制台代码页完全无关**，解码侧不需要猜任何东西：
#   base64 → UTF-8 字节 → JSON。这是唯一能保证不出乱码的路子。
_PS_QUERY = (
    "$j = Get-NetAdapterBinding | "
    "Select-Object Name, InterfaceDescription, ComponentID, DisplayName, "
    "Enabled | ConvertTo-Json -Compress -Depth 3; "
    "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($j))"
)
_PS_TIMEOUT_SEC = 20

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
    # 大恒 Galaxy。⚠️ gxgv 必须在最前：真机上的 ComponentID 是
    # ms_GxGVLWFilter，不含 gxgige/gxiapi/galaxy 任何一个。
    "gxgv",
    "gvlwfilter",
    "daheng",
    "gxgige",
    "gxiapi",
    "galaxy",
    # ⚠️ 真机实证补的（用户生产机 PowerShell 输出里实际存在，而原表漏了）：
    #   HKR_neuGEVFilter  "Neu GigEVision Image Filter Driver"  <- 海康机器人
    #   MVTec_Hgevstrm    "MVTec GigE Vision Streaming Filter"  <- HALCON 厂商
    "hkr",           # HikRobot 前缀
    "hikrobot",
    "neu gev",
    "gevision",      # "GigEVision" 常写成连写
    "gevfilter",
    "gevstrm",
    "hgev",
    "mvtec",
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

def _service_meta(reg: _Registry, svc: str) -> Dict:
    """从 Services 键取一个服务的基本信息，用于认厂商。"""
    sbase = _SERVICES_KEY + "\\" + svc
    return {
        "service": svc,
        "display_name": reg.value(sbase, "DisplayName") or "",
        "description": reg.value(sbase, "Description") or "",
        "image_path": reg.value(sbase, "ImagePath") or "",
    }


def _classify(meta: Dict, gp: Sequence[str], cp: Sequence[str]) -> Dict:
    hay = _haystack(meta.get("service", ""), meta.get("display_name", ""),
                    meta.get("description", ""), meta.get("image_path", ""))
    out = dict(meta)
    out["matched_gige"] = _matches(hay, gp)
    out["matched_capture"] = _matches(hay, cp)
    return out


def enumerate_via_powershell(runner=None, timeout: int = _PS_TIMEOUT_SEC):
    """用 ``Get-NetAdapterBinding`` 枚举每张网卡上的绑定项。

    这是**权威**来源：设备管理器里"此连接使用下列项目"那个勾选框，读的就是
    同一份数据。只读，不需要管理员权限。

    :param runner: ``(cmd, timeout) -> (rc, stdout, stderr)``，测试可注入
    :return: list of dict（每项含 Name/InterfaceDescription/ComponentID/
             Enabled）；**来源不可用时返回 None**（PowerShell 不存在、
             被策略禁用、超时、输出不是 JSON 都算不可用）
    """
    run = runner or _run_powershell
    try:
        rc, out, err = run(_PS_QUERY, timeout)
    except Exception:
        return None
    if not out or not out.strip():
        return None
    payload = out.strip()
    # 正常路径：base64 → UTF-8 字节 → JSON。与控制台代码页无关，不会乱码。
    try:
        import base64
        payload = base64.b64decode(payload).decode("utf-8")
    except Exception:
        pass          # 不是 base64（比如 cmdlet 报错时输出的是错误文本），
                      # 就按原样交给 json 解析，解析不出来返回 None
    try:
        data = json.loads(payload)
    except Exception:
        return None
    # ConvertTo-Json 在只有一条记录时会输出对象而不是数组
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list) or not data:
        return None
    return data


def enumerate_installed_filters(reg: Optional[_Registry] = None,
                                gige_patterns=None) -> List[Dict]:
    """扫描 Services，找出本机**已安装**的 NDIS 过滤驱动。

    判据是服务键下存在 ``FilterClass`` 或 ``FilterRunOrder`` —— 这两个值只有
    NDIS 6 轻量过滤驱动才会注册。

    为什么单独做这一步：``Get-NetAdapterBinding`` 只能看到**已绑定**的过滤
    驱动。而"海康 MVS 装过、只是没勾上"同样是个隐患（下次有人装驱动、或
    换了网卡，它可能自己绑上去）。这一步跟网卡无关，纯粹看装没装。

    :return: list of dict，每项含 ``service`` / ``display_name`` /
             ``description`` / ``image_path`` / ``filter_class`` /
             ``matched_gige``。读不到返回空列表。
    """
    if reg is None:
        if sys.platform != "win32":
            return []
        reg = _WinRegistry()
        if not reg.available():
            return []

    gp = tuple(gige_patterns) if gige_patterns else DEFAULT_GIGE_PATTERNS
    try:
        names = reg.subkeys(_SERVICES_KEY)
    except Exception:
        return []

    out: List[Dict] = []
    for name in names[:_MAX_SERVICE_KEYS]:
        try:
            sbase = _SERVICES_KEY + "\\" + name
            fclass = reg.value(sbase, "FilterClass")
            forder = reg.value(sbase, "FilterRunOrder")
            if fclass is None and forder is None:
                continue          # 不是 NDIS 过滤驱动
            meta = _service_meta(reg, name)
            meta["filter_class"] = str(fclass or "")
            hay = _haystack(meta["service"], meta["display_name"],
                            meta["description"], meta["image_path"],
                            meta["filter_class"])
            meta["matched_gige"] = _matches(hay, gp)
            out.append(meta)
        except Exception:
            continue
    return out


def enumerate_nic_filters(reg: Optional[_Registry] = None,
                          gige_patterns: Optional[Sequence[str]] = None,
                          capture_patterns: Optional[Sequence[str]] = None,
                          ps_runner=None,
                          use_powershell: Optional[bool] = None
                          ) -> Optional[List[Dict]]:
    """枚举每张网卡上绑定的 NDIS 过滤驱动（三个来源合并）。

    :param reg:              注册表实现，默认用真实 winreg；测试注入假实现
    :param gige_patterns:    覆盖默认的 GigE 特征表（现场发现漏了某个厂商时，
                             从 config.yaml 的 ``logging.nic_filter_patterns``
                             传进来）。传 None 用 :data:`DEFAULT_GIGE_PATTERNS`。
    :param capture_patterns: 同上，抓包类驱动特征表
    :param ps_runner:        ``Get-NetAdapterBinding`` 的执行器，测试可注入
    :param use_powershell:   None = 自动（Windows 且没显式传 reg 时才用）
    :return: list of dict，每项一张网卡：
             ``{"nic_key", "driver_desc", "conn_name", "sources",
             "filters": [{"service", "display_name", "description",
             "image_path", "matched_gige", "matched_capture", "enabled"}]}``；
             **所有来源都拿不到数据时返回 None**（不是空列表——空列表的意思是
             "查到了，一张网卡都没有"，两者不能混）。
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

    # --- 来源 1：Get-NetAdapterBinding（权威）----------------------------
    ps = None
    if use_powershell is True or (
            use_powershell is None and sys.platform == "win32"):
        ps = enumerate_via_powershell(runner=ps_runner)

    nics: Dict[str, Dict] = {}      # key: 网卡标识 -> 记录

    if ps:
        # 哪些 ComponentID 是**真正的 NDIS 过滤驱动**：由 Services 扫描判定
        # （服务键下有 FilterClass / FilterRunOrder）。这比依赖
        # Get-NetAdapterBinding 的 Class 属性可靠 —— 而那个属性存不存在
        # 根本无法确认，猜错了就是静默假阴性。
        try:
            _known_filters = {f["service"] for f in
                              enumerate_installed_filters(reg,
                                                          gige_patterns=gp)}
        except Exception:
            _known_filters = set()

        for row in ps:
            try:
                cid = str(row.get("ComponentID") or "").strip()
                if not cid:
                    continue
                name = str(row.get("Name") or "").strip()
                idesc = str(row.get("InterfaceDescription") or "").strip()
                # ⚠️ 合并键必须和注册表那边一致，否则**同一块物理网卡会被拆成
                #    两条记录**：PowerShell 给的是连接名（"以太网 2"），注册表
                #    给的是 DriverDesc（"Realtek PCIe GbE Family Controller"）。
                #    两边能对上的是 InterfaceDescription == DriverDesc，所以
                #    一律用它做键。拆开的后果不只是日志重复 —— 两个来源各看到
                #    一部分过滤驱动时，"≥2"的阈值可能谁都不满足，硬闸门会漏判。
                key = idesc or name
                if not key:
                    continue
                rec = nics.setdefault(key, {
                    "nic_key": name, "driver_desc": idesc,
                    "conn_name": name, "sources": set(), "filters": []})
                rec["driver_desc"] = rec["driver_desc"] or idesc
                rec["sources"].add("netadapterbinding")
                # ⚠️ **顺序是要害**：PowerShell 给的 DisplayName 必须在
                #    _classify() **之前**合进 meta。真机实证：注册表里读不到
                #    HKR_neuGEVFilter / MVTec_Hgevstrm 的 DisplayName，于是
                #    haystack 只剩服务名（"hkr_neugevfilter" 里没有 "gige"），
                #    matched_gige 变成 None —— 一张网卡上三个 GigE 过滤驱动
                #    只认出一个，硬闸门因此没触发。而漏掉的 HKR_neuGEVFilter
                #    正是海康机器人（HikRobot）的驱动。
                meta = _service_meta(reg, cid)
                ps_disp = str(row.get("DisplayName") or "").strip()
                if ps_disp:
                    meta["display_name"] = ps_disp
                f = _classify(meta, gp, cp)
                f["enabled"] = bool(row.get("Enabled", True))
                # 注册表确认它是 NDIS 过滤驱动；否则只是普通协议/服务绑定。
                # 拿不准时（Services 扫描失败）标 None，不冒充。
                f["is_ndis_filter"] = (cid in _known_filters
                                       if _known_filters else None)
                rec["filters"].append(f)
            except Exception:
                continue

    # --- 来源 2/3：注册表网卡类键（含遗留的 Upper/LowerBindings）-----------
    try:
        keys = reg.subkeys(_NIC_CLASS_KEY)
    except Exception:
        keys = []
    for sub in keys:
        # ⚠️ 必须过滤：类键下还有 Properties / Configuration 等非实例子键
        if not _NIC_INSTANCE_RE.match(str(sub)):
            continue
        try:
            base = _NIC_CLASS_KEY + "\\" + sub
            desc = reg.value(base, "DriverDesc")
            bound = list(_as_str_list(reg.value(base, "UpperBindings")))
            bound += [s for s in _as_str_list(reg.value(base, "LowerBindings"))
                      if s not in bound]
            if not bound and not nics:
                # 这张卡没有任何遗留绑定，而 PowerShell 那边也没有它的记录：
                # 仍然要列出来，让操作员看见"这张卡查过了、是空的"
                pass
            key = str(desc or "").strip() or sub
            rec = nics.setdefault(key, {
                "nic_key": sub, "driver_desc": desc or "",
                "conn_name": "", "sources": set(), "filters": []})
            rec["sources"].add("registry")
            seen = {f["service"] for f in rec["filters"]}
            for svc in bound[:_MAX_FILTERS_PER_NIC]:
                if svc in seen:
                    continue
                f = _classify(_service_meta(reg, svc), gp, cp)
                # 注册表的 Upper/LowerBindings **给不出启用状态**，
                # 不要谎报 True —— 标 None 让上层知道这是"未知"。
                f["enabled"] = None
                rec["filters"].append(f)
                seen.add(svc)
        except Exception:
            continue      # 单张网卡读失败不影响其他网卡

    if not nics:
        return None
    out = []
    for rec in nics.values():
        rec["sources"] = sorted(rec["sources"])
        out.append(rec)
    return out


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
        # ⚠️ **判定只看启用中的**。真机实证：在 ncpa.cpl 里取消勾选后，
        #    绑定**依然存在**、Get-NetAdapterBinding 依然会列出来，只是
        #    Enabled 变成 False —— 那时它已经不在收包路径上，不构成卡死风险。
        #    原来的代码把已禁用的一起算，于是用户明明按正确步骤禁用并重启了，
        #    程序还是拒绝启动。
        #    enabled 为 None 表示"来源给不出这个信息"（只有注册表时），
        #    一律**当启用**处理 —— 宁可多拦，不能漏拦。
        gige_active = [f for f in gige if f.get("enabled") is not False]
        entry = {
            "nic": nic.get("nic_key", ""),
            "driver_desc": nic.get("driver_desc", ""),
            "filters": filters,
            "gige_filters": gige,
            "gige_active": gige_active,
            "gige_disabled": [f for f in gige if f.get("enabled") is False],
            # 这条记录的数据从哪来（netadapterbinding / registry）。
            # 日志要靠它区分"权威来源查到是空的"和"只有遗留来源、结果不可信"。
            "sources": nic.get("sources", []),
        }
        out["all_filters"].append(entry)
        if len(gige_active) >= 2:
            out["conflicts"].append(entry)
        elif len(gige_active) == 1:
            out["singles"].append(entry)
    return out


def _fmt_filter(f: Dict) -> str:
    tag = ""
    if f.get("matched_gige"):
        tag = f"  [疑似GigE: 命中 '{f['matched_gige']}']"
    elif f.get("matched_capture"):
        tag = f"  [抓包驱动: 命中 '{f['matched_capture']}']"
    # 启用状态**必须打出来**：这是判断"禁用到底生效没有"的唯一依据。
    # 之前不打印，导致用户明明禁用了、程序还在拦，双方都看不出问题在哪。
    en = f.get("enabled")
    if en is False:
        tag += "  [已禁用·不在收包路径上]"
    elif en is None:
        tag += "  [启用状态未知]"
    name = f.get("display_name") or f.get("service", "?")
    return f"{f.get('service', '?')}  ({name}){tag}"


def report_nic_filters(report: Optional[Dict] = None,
                       reg: Optional[_Registry] = None,
                       log_fn=None,
                       gige_patterns: Optional[Sequence[str]] = None,
                       ps_runner=None,
                       use_powershell: Optional[bool] = None) -> Dict:
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
    :param ps_runner:     ``Get-NetAdapterBinding`` 的执行器，测试注入用
    :param use_powershell: None = 自动（仅 Windows）。**显式传 True 时不再判
                          平台** —— 否则平台守卫会把注入的执行器挡掉，
                          PowerShell 那条路径就永远测不到。
    :return: analyse_nic_filters() 的结果；异常时返回 ``{"checked": False}``
    """
    log = log_fn or info
    warn = log_fn or warning

    try:
        nics = enumerate_nic_filters(reg, gige_patterns=gige_patterns,
                                     ps_runner=ps_runner,
                                     use_powershell=use_powershell)
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

    # 本机**已安装**的 NDIS 过滤驱动（跟有没有绑定无关）。
    # 抓的是"海康 MVS 装过、只是没勾上"这种情况——它现在不惹事，
    # 但换网卡 / 重装驱动时可能自己绑回去。
    try:
        installed = enumerate_installed_filters(reg, gige_patterns=gige_patterns)
    except Exception:
        installed = []
    result["installed_filters"] = installed
    result["installed_gige"] = [f for f in installed if f.get("matched_gige")]

    # 用了哪些数据来源
    srcs = sorted({s for e in result["all_filters"] for s in e.get("sources", [])})
    ps_ok = "netadapterbinding" in srcs
    result["sources"] = srcs

    try:
        if conflicts:
            warn("[网卡过滤驱动] ⚠ 发现 {} 张网卡上同时挂了多个疑似 "
                 "GigE Vision 过滤驱动 —— 这是整机网络栈卡死/电脑死机的"
                 "高频原因，务必只保留大恒 Galaxy 的那一个：".format(
                     len(conflicts)))
            for c in conflicts:
                warn("  网卡 {} ({}):".format(
                    c["nic"], c["driver_desc"] or "名称未知"))
                for f in c.get("gige_active") or c["gige_filters"]:
                    warn("      " + _fmt_filter(f))
                dis = c.get("gige_disabled") or []
                if dis:
                    warn("      （另有 {} 个同类驱动**已禁用**，不算冲突）："
                         .format(len(dis)))
                    for f in dis:
                        warn("      " + _fmt_filter(f))
            warn("  处理办法：**取消勾选即可，不必卸载**。")
            warn("    Win+R → ncpa.cpl → 对该网卡右键 → 属性 → 只留 "
                 "'GigE Vision LightWeight Filter'（大恒），"
                 "取消勾选其余所有 GigE Vision 过滤驱动 → 确定。")
            warn("    取消勾选会把 Enabled 置为 False，驱动就不再在收包路径上；"
                 "绑定项仍会出现在清单里，但会被标成 [已禁用]，不再算冲突。")
            warn("  详见 docs/SDK迁移_海康转大恒.md 的现场部署步骤。")
        elif result["singles"]:
            log("[网卡过滤驱动] ✓ 未发现重复的 GigE 过滤驱动（每张网卡最多 1 个）")
            for s in result["singles"]:
                # ⚠️ 取 gige_active[0] 而不是 gige_filters[0]：
                #    后者可能是**已禁用**的那个，于是日志会把一个已经不在
                #    收包路径上的驱动说成"这张卡上唯一在用的 GigE 驱动"。
                act = s.get("gige_active") or s["gige_filters"]
                log("  网卡 {} ({}): {}".format(
                    s["nic"], s["driver_desc"] or "名称未知",
                    _fmt_filter(act[0])))
        elif not ps_ok:
            # ⚠️ 这种情况**不能**说"没有 GigE 过滤驱动"。
            # 只拿到注册表时，Upper/LowerBindings 覆盖不到 NDIS 6 轻量过滤
            # 驱动，"全空"极可能是数据源不够而不是真的没有。必须如实说明，
            # 并给出可手工核对的命令 —— 否则就是拿一个假的 ✓ 骗人。
            warn("[网卡过滤驱动] ⚠ 未能通过 Get-NetAdapterBinding 取到网卡绑定"
                 "数据（PowerShell 不可用 / 被策略禁用 / 超时），"
                 "只查到了注册表里的遗留绑定项。")
            warn("  ⚠ 因此下面清单里的\"无\"**不能**当作\"这张卡没有过滤驱动\"："
                 "注册表的 UpperBindings/LowerBindings 覆盖不到 NDIS 6 轻量"
                 "过滤驱动，而 GigE Vision 过滤驱动正是这一类。")
            warn("  请手工核对（不需要管理员权限）：")
            warn("    powershell -NoProfile -Command \""
                 "Get-NetAdapterBinding -Class Filter | "
                 "Format-Table Name, InterfaceDescription, ComponentID, Enabled\"")
        else:
            log("[网卡过滤驱动] ✓ 各网卡上均未发现疑似 GigE 过滤驱动"
                "（数据来源：Get-NetAdapterBinding，权威）。")
            log("  如果现场确实在用 GigE 相机却看到这条，请把上面清单里的驱动名"
                "补进 config.yaml 的 logging.nic_filter_patterns。")

        # 已安装但未绑定的 GigE 过滤驱动：不是冲突，但值得知道
        if result["installed_gige"]:
            bound_svcs = {f.get("service")
                          for e in result["all_filters"] for f in e["filters"]}
            # 注册表里读不到 DisplayName 时（真机上 HKR_neuGEVFilter /
            # MVTec_Hgevstrm 就是这样），用 Get-NetAdapterBinding 给的名字回填，
            # 否则清单里会是一排空括号，看不出是哪个驱动。
            names = {f.get("service"): f.get("display_name")
                     for e in result["all_filters"] for f in e["filters"]
                     if f.get("display_name")}
            for f in result["installed_gige"]:
                if not f.get("display_name"):
                    f["display_name"] = names.get(f.get("service"), "")
            unbound = [f for f in result["installed_gige"]
                       if f.get("service") not in bound_svcs]
            log("[网卡过滤驱动] 本机已安装的疑似 GigE 过滤驱动 {} 个：".format(
                len(result["installed_gige"])))
            for f in result["installed_gige"]:
                state = ("已绑定" if f.get("service") in bound_svcs
                         else "未绑定到任何网卡")
                log("      {}  ({})  [{}]".format(
                    f.get("service"), f.get("display_name") or "", state))
            if unbound:
                log("  ↑ 未绑定的这些现在不影响运行，但换网卡或重装驱动时可能"
                    "自己绑上去；确认不再使用海康设备的话建议卸载。")

        # 完整清单一律打印：名字表不可能覆盖所有厂商
        log("[网卡过滤驱动] 各网卡已绑定的过滤驱动完整清单：")
        for e in result["all_filters"]:
            tag = "/".join(e.get("sources") or []) or "无来源"
            if not e["filters"]:
                log("  网卡 {} ({}): 无   [来源: {}]".format(
                    e["nic"], e["driver_desc"] or "名称未知", tag))
                continue
            log("  网卡 {} ({}):   [来源: {}]".format(
                e["nic"], e["driver_desc"] or "名称未知", tag))
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
    # ⚠️ 真机实证（用户 PowerShell 输出）：大恒的过滤驱动实际叫
    #    ComponentID  = ms_GxGVLWFilter
    #    DisplayName  = "GigE Vision LightWeight Filter"
    #    ImagePath    = ...\GxGVLWFilter.sys
    # 原来这张表只有 daheng/galaxy/gxgige/gxiapi —— **一个都匹配不上**，
    # 于是 _is_kept() 返回 False，"绝不禁用大恒自家驱动"那道闸在真机上是
    # 空的。下面 gxgv / gvlwfilter / gvlw 就是照真机名字补的。
    "gxgv",            # ms_GxGVLWFilter / GxGVLWFilter.sys
    "gvlwfilter",
    "gvlw",
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
        # 已禁用（Enabled=False）的既不算冲突、也没必要再动一次，
        # 所以整个计划里都不出现。enabled 为 None（来源给不出）时按启用处理。
        gige_idx = [i for i, f in enumerate(filters)
                    if f.get("matched_gige") and f.get("enabled") is not False]

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


def _decode_console(raw: bytes) -> str:
    """把子进程输出按**正确的**代码页解码。

    ⚠️ 真机实证的坑（本模块自己踩的回归）：
      Windows PowerShell 5.1 在 stdout 被重定向时，用的是**控制台/OEM 代码页**，
      中文机器是 cp936(GBK)，**不是 UTF-8**。之前这里写死 encoding="utf-8"，
      于是"以太网"变成"?̫?"、"Microsoft 网络客户端"变成"Microsoft ????ͻ???"。
      讽刺的是：不写 encoding、让 subprocess 用系统默认反而是对的。

    策略：拿**字节**，按 UTF-8 严格 → 本机 ANSI/OEM → 兜底 replace 逐级尝试。
    UTF-8 严格能过就说明是 UTF-8（PowerShell 7，或下面显式设过 OutputEncoding）；
    过不了就是本机代码页。
    """
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        import locale
        enc = locale.getpreferredencoding(False)
        if enc:
            return raw.decode(enc)
    except Exception:
        pass
    return raw.decode("utf-8", errors="replace")


def _run_powershell(cmd: str, timeout: int = 60):
    """执行一条 PowerShell 命令，返回 (returncode, stdout, stderr)。

    超时和任何异常都由**调用方**负责吞掉——本函数只负责跑。
    **不用 text=True**：编码由 :func:`_decode_console` 自己判，
    交给 subprocess 猜会在中文机器上出乱码。
    """
    import subprocess
    p = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
        capture_output=True, timeout=timeout)
    return p.returncode, _decode_console(p.stdout), _decode_console(p.stderr)


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
