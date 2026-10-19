#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从后端拉取"当前运行白布的检测配置"并应用到运行时。

    GET {api_base_url}/api/v1/defect/material/current-config

后端按当前白布的**材质 + 规格**下发：每台相机的检测阈值（``thresholds``）
和停机规则（``stopSetting``）。这是业务上的权威来源，优先级高于本地
config.yaml。

拉取失败时的行为（重要）
----------------------
**保留上一次成功的结果，绝不清空。** 拉取失败就把配置清掉，等于让整条
产线突然换回另一套阈值 —— 这比继续用一份稍旧的配置危险得多。
第一次就失败时存储本来就是空的，两个解析入口会自动退回本地 yaml。

``status`` / ``enabled`` 的处理
------------------------------
接口约定"启用、禁用项都会返回，调用方需根据 status、enabled 判断是否使用"。

  * ``thresholds[].status == 0`` → **不下发这条阈值**，该类退回本地配置。
    没有把它当成"这个类别不检测"来做：静默关掉一个类别就是静默漏检，
    这种决定不该由一个后端字段悄悄触发。被跳过的条目会打进日志。
  * ``stopSetting[].enabled == false`` → 照原样应用为 ``enabled: false``，
    即"这台相机上这个类不停机"。这与本地 yaml 的语义完全一致，
    也和"不检测"是两回事（仍然检测、仍然记录，只是不触发停机）。

模型路径（``model.modelPath``）**只报告不热切**
--------------------------------------------
换模型要重建每个 worker 的 TensorRT engine，是重操作且不可回滚。
这里只把它与本地 ``yolo.model_path`` 比对，不一致时告警，
由人决定是否改配置重启 —— 不在运行中偷偷换模型。
"""

import asyncio
import threading
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

from General_Tool.EnhancedLogger import info, warning, error

# 接口路径。**基址之后**的那一段，不要再写 /api。
#
# 现场确认的完整地址是：
#     http://localhost:8890/api/v1/defect/material/current-config
# 基址 get_api_base_url() 已经含 api_base_path("/api")，所以这里只写
# "/v1/..."，拼出来正好一层。
#
# 这段路径被改错过两次，原因记下来免得再犯：
#   1) 曾写成 "/api/v1/..."，拼出 /api/api/v1/...，404。当时误以为"接口
#      文档给的地址带 /api/v1 所以要照抄"——文档给的是**完整 URL**，
#      这里要的是**基址之后的相对段**，两者不能混。
#   2) 404 响应体回显的 path 字段是**服务端收到的路径**，它对不代表请求
#      打对了地方；端口错了同样会 404。别拿它当"路径拼错了"的证据。
# 仓库里其他调用点也是同一惯例："/image/upload"、"/v1/defect/log/size"。
API_PATH = "/v1/defect/material/current-config"


# 视为成功的 code。后端各接口的约定不完全一致，这里从宽。
# 拉取用的是 GET（见 fetch_current_config 里的 session.get）。
# 别写成 POST —— 那是**另一个**接口 /api/image/upload（图片上报）的方法。
HTTP_METHOD = "GET"


def _endpoint() -> str:
    """本次配置来自哪个接口：请求方法 + 完整地址。

    变更日志里带上它，是为了对账时一眼看出"这份阈值是从哪拉的"。
    地址由 _get_base_url() + API_PATH 现算，跟着 config.yaml 走、不写死 ——
    万一后端挪了端口，日志不会继续报旧地址。
    """
    try:
        return f"{HTTP_METHOD} {_get_base_url()}{API_PATH}"
    except Exception as e:
        return f"{HTTP_METHOD} <地址解析失败: {e}>"


_OK_CODES = {0, 200, "0", "200", "SUCCESS", "success", "OK", "ok"}


# ======================================================================
#  解析
# ======================================================================
def _to_float(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_ok(result: Any) -> bool:
    """判断 Result 包装是否表示成功。没有 code 字段时从宽当成功。"""
    if not isinstance(result, dict):
        return False
    if "code" not in result:
        return True
    return result.get("code") in _OK_CODES


def parse_config(data: Any) -> Tuple[Dict[Tuple[str, str], float],
                                     Dict[Tuple[str, str], Dict],
                                     Dict]:
    """把接口 data 解析成 (阈值表, 停机规则表, 元信息)。

    单条脏数据只跳过它自己并记日志，不让整份配置失败 ——
    后端某一行配错不该让整条产线退回本地配置。
    """
    thresholds: Dict[Tuple[str, str], float] = {}
    stop_rules: Dict[Tuple[str, str], Dict] = {}
    skipped_threshold = []
    skipped_stop = []

    if not isinstance(data, dict):
        raise ValueError(f"data 不是对象: {type(data).__name__}")

    cam_ids: List[str] = []
    cameras = data.get("cameras")
    if cameras is None:
        cameras = []
    if not isinstance(cameras, list):
        raise ValueError(f"cameras 不是数组: {type(cameras).__name__}")

    for cam in cameras:
        if not isinstance(cam, dict):
            continue
        cam_id = cam.get("cameraId")
        if not cam_id or not isinstance(cam_id, str):
            skipped_stop.append(f"<无 cameraId 的条目 {cam!r}>")
            continue
        cam_id = cam_id.strip()
        cam_ids.append(cam_id)

        # ---- 检测阈值 ----
        ths = cam.get("thresholds") or []
        if not isinstance(ths, list):
            skipped_threshold.append(f"{cam_id}: thresholds 不是数组")
            ths = []
        for t in ths:
            if not isinstance(t, dict):
                continue
            name = t.get("defectName")
            if not name or not isinstance(name, str):
                skipped_threshold.append(f"{cam_id}: 阈值条目缺 defectName")
                continue
            name = name.strip()
            # status=0 是"这条不启用"，跳过它，该类退回本地配置。
            # 不用它去关掉整个类别 —— 那是静默漏检。
            status = t.get("status", 1)
            try:
                status = int(status)
            except (TypeError, ValueError):
                status = 1
            if status != 1:
                skipped_threshold.append(
                    f"{cam_id}/{name}: status={t.get('status')!r} 未启用，"
                    f"退回本地阈值")
                continue
            val = _to_float(t.get("thresholdValue"))
            if val is None:
                skipped_threshold.append(
                    f"{cam_id}/{name}: thresholdValue="
                    f"{t.get('thresholdValue')!r} 不是数字")
                continue
            if not (0.0 <= val <= 1.0):
                skipped_threshold.append(
                    f"{cam_id}/{name}: thresholdValue={val} 不在 [0,1]")
                continue
            label = t.get("defectLabel")
            thresholds[(cam_id, name)] = val
            _check_label(cam_id, name, label)

        # ---- 停机规则 ----
        stops = cam.get("stopSetting") or []
        if not isinstance(stops, list):
            skipped_stop.append(f"{cam_id}: stopSetting 不是数组")
            stops = []
        for s in stops:
            if not isinstance(s, dict):
                continue
            name = s.get("name")
            if not name or not isinstance(name, str):
                skipped_stop.append(f"{cam_id}: 停机条目缺 name")
                continue
            name = name.strip()
            rule: Dict[str, Any] = {}
            # enabled 缺省按 true：后端漏传不该等于"不停机"
            rule["enabled"] = bool(s.get("enabled", True))
            mc = _to_float(s.get("minConfidence"))
            if mc is not None:
                rule["min_confidence"] = mc
            ma = _to_float(s.get("minAreaMm2"))
            if ma is not None:
                rule["min_area_mm2"] = ma
            mode = s.get("triggerMode")
            if mode:                                  # null/空 = 面积规则
                rule["trigger_mode"] = str(mode)
            mw = _to_float(s.get("minWidthMm"))
            if mw is not None:
                rule["min_width_mm"] = mw
            mh = _to_float(s.get("minHeightMm"))
            if mh is not None:
                rule["min_height_mm"] = mh
            stop_rules[(cam_id, name)] = rule

    meta = {
        "checkLogId": data.get("checkLogId"),
        "clothNumber": data.get("clothNumber"),
        "roller": data.get("roller"),
        "materialId": data.get("materialId"),
        "material": data.get("material"),
        "specificationId": data.get("specificationId"),
        "specification": data.get("specification"),
        "camera_count": len(cameras),
        # 相机名单（含配了但一条有效项都没有的），用于"未变化相机"统计
        "camera_ids": list(cam_ids),
    }
    model = data.get("model")
    if isinstance(model, dict):
        meta["modelPath"] = model.get("modelPath")
        meta["modelName"] = model.get("modelName")
        meta["modelVersion"] = model.get("modelVersion")
        meta["modelStatus"] = model.get("status")

    # status=0 / 字段缺失这类跳过是后端的稳定状态，不是每轮的新事件。
    # 每 2 秒重打一遍毫无信息量，只在跳过内容**变化**时打。
    if skipped_threshold:
        _log_stable("cfg:skip_thr", "warning",
                    f"[材质配置] 跳过 {len(skipped_threshold)} 条阈值："
                    + "；".join(skipped_threshold[:12])
                    + ("…" if len(skipped_threshold) > 12 else "")
                    + "（内容变化前不再重复）")
    if skipped_stop:
        _log_stable("cfg:skip_stop", "warning",
                    f"[材质配置] 跳过 {len(skipped_stop)} 条停机规则："
                    + "；".join(skipped_stop[:12])
                    + ("…" if len(skipped_stop) > 12 else "")
                    + "（内容变化前不再重复）")
    return thresholds, stop_rules, meta


_LOG_SEEN: Dict[str, str] = {}


def _log_stable(key: str, level: str, msg: str, sig: Optional[str] = None) -> None:
    """「稳定状态」类日志：内容没变就不再重复打。

    本模块是被定时轮询驱动的（现场 refresh_interval_sec=2，即每 2 秒一轮）。
    后端的配置状态往往几十分钟都不变 —— 哪几台的哪个类 status=0、拉取失败的
    原因、当前生效的是哪一份配置。这些每轮重打一遍，会把真正有用的
    「变更明细」彻底淹掉。所以只在**内容发生变化**时打。

    :param sig: 去重用的签名。默认取 msg 本身；消息里含每次都变的字段
                （如「Ns 前应用」）时必须单独传签名，否则去重永远不生效。
    """
    sig = msg if sig is None else sig
    if _LOG_SEEN.get(key) == sig:
        return
    _LOG_SEEN[key] = sig
    fn = error if level == "error" else (warning if level == "warning" else info)
    fn(msg)


def _log_reset(prefix: str) -> None:
    """清掉某一类去重记录，让同样的问题下次重新打出来。

    拉取恢复正常之后，去重表里那条失败记录必须清掉 —— 否则之后再次以同样
    原因失败时会被当成"已经打过"而静默，那就漏报故障了。
    """
    for k in [k for k in list(_LOG_SEEN) if k.startswith(prefix)]:
        _LOG_SEEN.pop(k, None)


def _quiet_when_unchanged() -> bool:
    """没有变更时是否完全不打日志。默认 True（现场要求：只在出错或变更时打）。"""
    try:
        from AnomalyDetection_Tool.config.settings import MATERIAL_CONFIG
        v = MATERIAL_CONFIG.get("quiet_when_unchanged", True)
        return True if v is None else bool(v)
    except Exception:
        return True


# 已经告警过的 (相机, 类, label)。同一个不一致每次刷新都会命中，
# refresh_interval_sec 调小后会刷屏（8 台相机 × 每 2s 一次），所以只报第一次。
_LABEL_WARNED: set = set()


def _check_label(cam_id: str, name: str, label: Any) -> None:
    """用 DEFECT_LABEL_MAP 交叉校验 defectLabel，抓后端名称对不上的情况。

    同一个 (相机, 类, label) 只告警一次 —— 这是配置层面的固定不一致，
    重复刷屏只会把真正有用的变更日志淹掉。label 变了会重新告警。
    """
    if not label:
        return
    try:
        from AnomalyDetection_Tool.output.detection_info_formatter import (
            DEFECT_LABEL_MAP)
        expect = DEFECT_LABEL_MAP.get(name)
        if expect is None or str(label) == expect:
            return
        key = (cam_id, name, str(label))
        if key in _LABEL_WARNED:
            return
        _LABEL_WARNED.add(key)
        warning(f"[材质配置] {cam_id}/{name} 的 defectLabel={label!r} "
                f"与本地映射 {expect!r} 不一致，已按 defectName 应用"
                f"（同一项不再重复告警）")
    except Exception:
        pass


# ======================================================================
#  拉取 + 应用
# ======================================================================
def _get_base_url() -> str:
    """本功能专用的基址。

    目前这个接口就在主后端上，所以默认**全部回落到全局**
    api_host/api_port/api_base_path，结果与 http_client.get_base_url()
    一致（现场 http://localhost:8890/api）。

    单独留这一层，是为了万一后端把它挪到别的服务/端口：改 config.yaml 的
    material_config.api_port 即可，不用动代码，也不会把其他上报一起带跑
    —— 全局那三项被检测信息上报、布料尺寸上报、周期上报共用。
    """
    from AnomalyDetection_Tool.config.settings import (
        MATERIAL_CONFIG, get_upload_config)
    g = get_upload_config()
    host = MATERIAL_CONFIG.get("api_host") or g.get("api_host") or "localhost"
    port = MATERIAL_CONFIG.get("api_port") or g.get("api_port")
    base = MATERIAL_CONFIG.get("api_base_path")
    if base is None:
        base = g.get("api_base_path") or ""
    if base and not base.startswith("/"):
        base = "/" + base
    return f"http://{host}:{int(port)}{base.rstrip('/')}"


async def fetch_current_config(timeout: float = 5.0) -> Optional[dict]:
    """拉一次。返回 Result 的 data；失败/业务错误返回 None。"""
    url = _get_base_url() + API_PATH
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                text = await resp.text()
                if resp.status != 200:
                    _log_stable("fail:http", "info",
                                f"[材质配置] HTTP {resp.status}: {text[:200]}")
                    return None
                try:
                    import json as _json
                    result = _json.loads(text)
                except Exception:
                    _log_stable("fail:json", "info",
                                f"[材质配置] 响应不是 JSON: {text[:200]}")
                    return None
                if not _is_ok(result):
                    _log_stable("fail:biz", "info",
                            f"[材质配置] 业务错误 code="
                            f"{result.get('code')!r} msg={result.get('message')!r}"
                            f"（没有运行/暂停中的白布等情况属正常）")
                    return None
                data = result.get("data")
                if not isinstance(data, dict):
                    _log_stable("fail:data", "info",
                            f"[材质配置] data 不是对象: {type(data).__name__}")
                    return None
                return data
    except asyncio.TimeoutError:
        _log_stable("fail:timeout", "info", "[材质配置] 拉取超时")
        return None
    except aiohttp.ClientError as e:
        _log_stable("fail:net", "info", f"[材质配置] 网络错误: {e}")
        return None
    except Exception as e:
        _log_stable("fail:exc", "info", f"[材质配置] 拉取异常: {e}")
        return None


def _diff_max_lines() -> int:
    """单次变更明细最多打多少行；0 = 全部打印（默认）。

    现场一次「全部相机」改值最多 144 条阈值 + 120 条停机规则，默认全打 ——
    截断会让 camera2 之后的相机看起来「没变」。真要限制，在 config.yaml 里
    设 material_config.max_diff_lines，改完热更新即生效。
    """
    try:
        from AnomalyDetection_Tool.config.settings import MATERIAL_CONFIG
        v = MATERIAL_CONFIG.get("max_diff_lines", 0)
        return int(v) if v is not None else 0
    except Exception:
        return 0


def _stop_rule_brief(rule: Optional[dict]) -> str:
    """停机规则的紧凑表示，用于「保持原值」那一行。"""
    r = rule or {}
    parts = ["enabled=" + ("yes" if r.get("enabled") else "no")]
    if "min_confidence" in r:
        parts.append("conf>=" + _f(r["min_confidence"]))
    if "min_area_mm2" in r:
        parts.append("area>=" + _f(r["min_area_mm2"]) + "mm2")
    if r.get("trigger_mode"):
        parts.append("mode=" + str(r["trigger_mode"]))
    if "min_width_mm" in r:
        parts.append("w>=" + _f(r["min_width_mm"]) + "mm")
    if "min_height_mm" in r:
        parts.append("h>=" + _f(r["min_height_mm"]) + "mm")
    return ",".join(parts)


def _holdout_lines(thresholds, thr_changed, stop_rules, stop_changed):
    """同一类别里「部分相机变了、部分没变」时，把没变的连保留值一起列出来。

    前端「全部相机」改阈值时，个别相机（特殊值）会被保留原值。只打「变了什么」
    看不出这一点 —— 必须同时说明哪些相机没跟着变、保留的是什么值，否则现场
    没法确认特殊值真的被保住了。
    """
    out: List[str] = []

    by_cls: Dict[str, Dict[str, float]] = {}
    for (cam, cls), nv in thresholds.items():
        by_cls.setdefault(cls, {})[cam] = nv
    for cls in sorted(by_cls):
        cams = by_cls[cls]
        ch = {c for (c, k) in thr_changed if k == cls}
        if ch and len(ch) < len(cams):
            hold = sorted(c for c in cams if c not in ch)
            out.append("[检测阈值] %s: %d 台已更新, %d 台保持原值 -> %s" % (
                cls, len(ch), len(hold),
                ", ".join("%s=%s" % (c, _f(cams[c])) for c in hold)))

    by_cls_s: Dict[str, Dict[str, dict]] = {}
    for (cam, cls), r in stop_rules.items():
        by_cls_s.setdefault(cls, {})[cam] = r
    for cls in sorted(by_cls_s):
        cams = by_cls_s[cls]
        ch = {c for (c, k) in stop_changed if k == cls}
        if ch and len(ch) < len(cams):
            hold = sorted(c for c in cams if c not in ch)
            out.append("[停机规则] %s: %d 台已更新, %d 台保持原值 -> %s" % (
                cls, len(ch), len(hold),
                ", ".join("%s[%s]" % (c, _stop_rule_brief(cams[c]))
                          for c in hold)))
    return out



def _f(v) -> str:
    """把值格式化成短字符串，None 显示为「无」。"""
    if v is None:
        return "无"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _same(a, b) -> bool:
    """值相等判断，浮点带容差。

    后端回传的 JSON 浮点可能带尾巴（0.30000000000000004），直接 != 会
    把"没变"判成"变了"，反过来也会把真变更淹在噪声里。
    """
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    try:
        return abs(float(a) - float(b)) <= 1e-9
    except (TypeError, ValueError):
        return a == b


def _stop_rule_diff(old: Optional[dict], new: dict) -> List[str]:
    """比较两份停机规则，返回 ['字段 旧 -> 新', ...]。

    按**并集**比，不是只看新规则里有的字段。覆盖层是整份替换：前端把某个
    字段清空后，这个字段就从生效规则里消失了 —— 只比新规则里有的键，
    这种"字段被删掉"的变更会被完全漏掉（实测过，确实漏）。
    """
    out: List[str] = []
    old = old or {}
    new = new or {}
    for k in ("enabled", "min_confidence", "min_area_mm2", "trigger_mode",
              "min_width_mm", "min_height_mm"):
        ov, nv = old.get(k), new.get(k)
        if ov is None and nv is None:
            continue
        if not _same(ov, nv):
            out.append(f"{k} {_f(ov)}->{_f(nv)}")
    return out


def _collect_previous(thresholds, stop_rules):
    """取每个 (相机, 类) 在**本次应用之前**实际生效的值和它的来源。

    来源判定：覆盖层里有 → 上一次后端下发的；覆盖层里没有 → 当前生效的是
    config.yaml（全局 / 按相机覆盖）里的值。
    """
    from AnomalyDetection_Tool.config.remote_config_store import (
        get_remote_config_store)
    store = get_remote_config_store()
    was_empty = store.is_empty()
    prev_thr, prev_stop, src = {}, {}, {}
    for (cam, cls) in thresholds:
        v = store.get_threshold(cam, cls)
        if v is None:
            try:
                from AnomalyDetection_Tool.config.settings import (
                    _camera_det_threshold)
                v = _camera_det_threshold(cam, cls)
                src[(cam, cls, "thr")] = "config.yaml"
            except Exception:
                v = None
        else:
            src[(cam, cls, "thr")] = "后端"
        prev_thr[(cam, cls)] = v
    for (cam, cls), rule in stop_rules.items():
        r = store.get_stop_rule(cam, cls)
        if r is None:
            try:
                from AnomalyDetection_Tool.config.settings import (
                    effective_stop_rule)
                r = effective_stop_rule(cls, cam)
                src[(cam, cls, "stop")] = "config.yaml"
            except Exception:
                r = None
        else:
            src[(cam, cls, "stop")] = "后端"
        prev_stop[(cam, cls)] = r
    return prev_thr, prev_stop, src, was_empty


def apply_config(data: dict) -> bool:
    """解析并写入覆盖层。成功返回 True。

    解析失败时**不动**已有配置，并明确打出"变更失败"以及当前仍在生效的
    来源，避免出现"日志说拉到了、实际没生效"的黑箱。
    """
    from AnomalyDetection_Tool.config.remote_config_store import (
        get_remote_config_store)
    store = get_remote_config_store()

    # 当前生效来源，用于失败时说明"仍在用什么"
    cur_src = "config.yaml（后端从未成功下发）" if store.is_empty() else "后端数据库（上次成功的下发）"

    try:
        thresholds, stop_rules, meta = parse_config(data)
    except Exception as e:
        _log_stable("fail:parse", "error",
                    f"[材质配置] 变更失败：解析异常 {e}；当前仍沿用 {cur_src}"
                    f"（来源接口 {_endpoint()}）")
        return False

    if not thresholds and not stop_rules:
        _log_stable("fail:empty", "warning",
                    f"[材质配置] 变更失败：后端返回为空/无法解析，"
                    f"**不会**用空配置覆盖；当前仍沿用 {cur_src}"
                    f"（来源接口 {_endpoint()}）")
        return False

    prev_thr, prev_stop, src, was_empty = _collect_previous(
        thresholds, stop_rules)

    # 应用**前**的完整快照。覆盖层是整份替换（remote_config_store.apply
    # 明确不做增量合并），所以后端这次没下发的条目会真的从生效值里消失、
    # 悄悄退回 config.yaml。只比对"新下发里有什么"，就会漏掉"原来有、
    # 现在没了"的那一半 —— 而那正是生效值发生变化的时候。
    try:
        _before = store.snapshot()
    except Exception:
        _before = {}
    prev_meta = _before.get("meta") or {}
    prev_thr_keys = {tuple(str(k).split("/", 1))
                     for k in (_before.get("thresholds") or {})}
    prev_stop_keys = {tuple(str(k).split("/", 1))
                      for k in (_before.get("stop_rules") or {})}
    # 拍平键 "相机/类别" -> 值，用于报"这条被删掉前是多少"。
    # 不能用 prev_thr：_collect_previous 只收集**本次新下发**的键，
    # 被删掉的那条根本不在里面，直接取会得到 None 显示成"无"。
    prev_thr_flat = _before.get("thresholds") or {}

    store.apply(thresholds, stop_rules, meta)

    # ---- 算变更明细：按相机分组，并单独标出"没跟着变"的相机 ----
    by_cam: Dict[str, List[str]] = {}
    thr_changed, stop_changed = set(), set()
    for (cam, cls), nv in sorted(thresholds.items()):
        ov = prev_thr.get((cam, cls))
        if ov is None or abs(float(ov) - float(nv)) > 1e-9:
            by_cam.setdefault(cam, []).append(
                f"{cls} 阈值 {_f(ov)}->{_f(nv)} "
                f"[原:{src.get((cam, cls, 'thr'), '?')}]")
            thr_changed.add((cam, cls))
    for (cam, cls), rule in sorted(stop_rules.items()):
        d = _stop_rule_diff(prev_stop.get((cam, cls)), rule)
        if d:
            by_cam.setdefault(cam, []).append(
                f"{cls} 停机 " + ", ".join(d)
                + f" [原:{src.get((cam, cls, 'stop'), '?')}]")
            stop_changed.add((cam, cls))

    # ---- 后端这次**没下发**的条目：生效值已退回本地，必须打出来 ----
    # 前端把某个类停用（status=0）或整条删掉，都会走到这里。不打的话，
    # 现场会看到"我明明改了/停了，日志一点动静都没有"。
    for (cam, cls) in sorted(prev_thr_keys - set(thresholds)):
        local = None
        try:
            from AnomalyDetection_Tool.config.settings import (
                _camera_det_threshold)
            local = _camera_det_threshold(cam, cls)
        except Exception:
            local = None
        by_cam.setdefault(cam, []).append(
            f"{cls} 阈值 本次未下发（已停用/删除），"
            f"{_f(prev_thr_flat.get(f'{cam}/{cls}'))}->退回本地 {_f(local)}")
        thr_changed.add((cam, cls))
    for (cam, cls) in sorted(prev_stop_keys - set(stop_rules)):
        by_cam.setdefault(cam, []).append(
            f"{cls} 停机规则 本次未下发（已停用/删除），已退回本地规则")
        stop_changed.add((cam, cls))

    # ---- 元信息变化：换布卷/换材质本身就是要看得见的事 ----
    meta_changes: List[str] = []
    if not was_empty:
        for key, label in (("clothNumber", "白布"), ("roller", "卷号"),
                           ("material", "材质"), ("specification", "规格"),
                           ("modelPath", "模型")):
            ov, nv = prev_meta.get(key), meta.get(key)
            if _f(ov) != _f(nv):
                meta_changes.append(f"{label} {_f(ov)}->{_f(nv)}")

    # 名单以 payload 为准：某台相机配了但一条有效项都没有时，它既不在
    # thresholds 也不在 stop_rules 里，但现场需要知道"这台什么都没变"。
    all_cams = sorted(set(meta.get("camera_ids") or [])
                      | {c for c, _ in thresholds}
                      | {c for c, _ in stop_rules})
    changed_cams = sorted(by_cam)
    unchanged_cams = [c for c in all_cams if c not in by_cam]
    n_changes = len(thr_changed) + len(stop_changed)

    head = (f"[材质配置] 变更成功（来源：后端数据库）"
            f"白布={meta.get('clothNumber')} 卷号={meta.get('roller')} "
            f"材质={meta.get('material')} 规格={meta.get('specification')} "
            f"相机={meta.get('camera_count')} 台 | "
            f"阈值 {len(thresholds)} 条 / 停机规则 {len(stop_rules)} 条 | "
            f"生效 {len(thresholds) + len(stop_rules)} 项，"
            f"变化 {n_changes} 项，涉及 {len(changed_cams)} 台相机")
    if was_empty:
        head += "（首次下发，此前生效的是 config.yaml）"

    # 成功拿到并应用了配置 -> 清掉失败/来源类的去重记录，
    # 之后再次失败时才能重新打出来（否则漏报故障）。
    _log_reset("fail:")
    _log_reset("src:")

    if not by_cam and not meta_changes:
        # 现场要求：只在**出错**或**发生变更**时打日志。2s 一轮的轮询下，
        # "无需改动"每分钟会重复 30 次，没有任何信息量。
        # 注意这里必须连 meta_changes 一起判：只改卷号/材质而阈值没动，
        # 也是一次真实的变更，不能静默。
        if not _quiet_when_unchanged():
            info(head + " —— 与当前生效值一致，无需改动")
    else:
        out = [head, "  来源接口：" + _endpoint()]
        if meta_changes:
            out.append("  布卷/材质变更：" + ", ".join(meta_changes))
        # ① 哪台相机变了 —— 这一行永远打全，不受行数上限影响。
        #    前端「全部相机」一改就是 8 台一起动，明细被截断时至少这里看得见。
        #    只改了布卷/材质时 changed_cams 是空的，这时整段都不打，
        #    否则留下"变化相机（0 台）："这种空标题。
        if changed_cams:
            out.append("  变化相机（%d 台）：%s" % (
                len(changed_cams),
                "  ".join("%s(%d项)" % (c, len(by_cam[c]))
                          for c in changed_cams)))
            if unchanged_cams:
                out.append("  未变化相机（%d 台）：%s" % (
                    len(unchanged_cams), ", ".join(unchanged_cams)))
        # ② 同类别里谁没跟着变 —— 用来确认「特殊值」真的被保住了
        hold = _holdout_lines(thresholds, thr_changed,
                              stop_rules, stop_changed)
        if hold:
            out.append("  同类别未跟随变更（保持原值的相机）：")
            out.extend("    " + h for h in hold)
        # ③ 完整明细，按相机分组
        body: List[str] = []
        for c in changed_cams:
            body.append("【%s】%d 项变化" % (c, len(by_cam[c])))
            body.extend("    - " + x for x in by_cam[c])
        cap = _diff_max_lines()
        if cap > 0 and len(body) > cap:
            body = body[:cap] + [
                "... 另有 %d 行未列出（config.yaml "
                "material_config.max_diff_lines 可调，0=全部打印）"
                % (len(body) - cap)]
        if body:
            out.append("  变更明细（按相机）：")
            out.extend("    " + b for b in body)
        info("\n".join(out))

    _warn_model_mismatch(meta)
    return True


def _warn_model_mismatch(meta: dict) -> None:
    """模型路径只报告不热切 —— 换 engine 是重操作且不可回滚。"""
    remote = meta.get("modelPath")
    if not remote:
        return
    try:
        from AnomalyDetection_Tool.config.settings import get_yolo_config
        local = (get_yolo_config() or {}).get("model_path")
    except Exception:
        return
    if local and str(remote) != str(local):
        _log_stable("cfg:model", "warning",
                    f"[材质配置] 后端指定模型 {remote!r} 与本地 "
                    f"yolo.model_path={local!r} 不一致。"
                    f"**不会在运行中热切模型**（需重建各 worker 的 TensorRT "
                    f"engine），请人工确认后改配置重启。"
                    f"（内容变化前不再重复）")


def _log_source(reason: str) -> None:
    """拉取失败时，明确打出"当前生效的是哪一份配置"。

    优先级本来就是「后端数据库 > config.yaml」，但日志里如果只写"拉取失败"，
    现场看不出此刻检测到底按哪套阈值在跑。这里把来源说清楚。
    """
    from AnomalyDetection_Tool.config.remote_config_store import (
        get_remote_config_store)
    try:
        st = get_remote_config_store()
        if st.is_empty():
            _log_stable("src:empty", "info",
                        f"[材质配置] 变更未生效（{reason}）——后端数据库为空，"
                        f"**当前使用 config.yaml 本地配置**"
                        f"（来源接口 {_endpoint()}；原因变化前不再重复）")
        else:
            snap = st.snapshot()
            # 签名**不含 age_sec** —— 它每秒都在变，带上就永远去重不掉。
            _log_stable("src:stale", "info",
                        f"[材质配置] 变更未生效（{reason}）——继续使用"
                        f"**后端数据库上一次成功下发的配置**"
                        f"（阈值 {snap['threshold_count']} 条 / "
                        f"停机规则 {snap['stop_rule_count']} 条，"
                        f"{snap['age_sec']}s 前应用）"
                        f"（来源接口 {_endpoint()}；原因变化前不再重复）",
                        sig=f"{reason}|{snap['threshold_count']}|"
                            f"{snap['stop_rule_count']}")
    except Exception:
        _log_stable("src:err", "info",
                    f"[材质配置] 变更未生效（{reason}）")


def active_source() -> str:
    """当前**实际生效**的检测/停机配置来自哪一份，给人看的一句话。

    开局必须能一眼看出检测按哪套阈值在跑 —— 只写"拉取成功"是不够的，
    拉到了空数据、或某几类被跳过退回本地，都属于"部分不是后端的"。
    """
    from AnomalyDetection_Tool.config.remote_config_store import (
        get_remote_config_store)
    try:
        st = get_remote_config_store()
        if st.is_empty():
            return "config.yaml 本地配置（后端数据库从未成功下发）"
        snap = st.snapshot()
        return ("后端数据库（阈值 %d 条 / 停机规则 %d 条，%ss 前应用）"
                % (snap["threshold_count"], snap["stop_rule_count"],
                   snap["age_sec"]))
    except Exception as e:
        return "无法确定（读取覆盖层异常: %s）" % e


def refresh_once(timeout: float = 5.0) -> bool:
    """同步入口：拉一次并应用。供启动、「开始」回调、定时线程调用。

    **总是在一个独立线程里跑自己的事件循环。** 第一版直接 asyncio.run()，
    在已有事件循环的上下文里调用会抛 RuntimeError；那个"退回新线程"的
    兜底写得是坏的（协程没被 await，请求根本没发出去）。与其分两条路，
    不如统一走一条：无论调用方在什么上下文，行为都一样。

    另外 join 带硬超时 —— 请求挂死时不能把调用方也一起挂住。
    """
    box: Dict[str, Any] = {}

    def _run():
        try:
            box["d"] = asyncio.run(fetch_current_config(timeout=timeout))
        except Exception as e:
            box["e"] = e

    t = threading.Thread(target=_run, name="material-config-fetch",
                         daemon=True)
    t.start()
    t.join(timeout + 3)
    if t.is_alive():
        _log_source(f"拉取超过 {timeout + 3:.0f}s 未返回")
        return False
    if box.get("e") is not None:
        _log_source(f"拉取异常 {box['e']}")
        return False
    data = box.get("d")
    if data is None:
        _log_source("后端未返回可用配置")
        return False
    return apply_config(data)
