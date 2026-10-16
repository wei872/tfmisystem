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
from typing import Any, Dict, Optional, Tuple

import aiohttp

from General_Tool.EnhancedLogger import info, warning, error

# 接口路径。**这里确实是两层 /api，不要"顺手修正"成一层。**
#
# 这个接口由另一个服务提供，它的 context-path 本身就带 /api，对外暴露的
# 完整地址是 http://<host>:8001/api/api/v1/defect/material/current-config。
# 基址（见下面 _get_base_url）已含 api_base_path="/api"，所以这里再写一层
# "/api/v1/..."，拼出来正好是两层。
#
# 曾经出现过一次现场 404：
#   {"status":404,"path":"/api/api/v1/defect/material/current-config"}
# 当时误判成"路径多写了一层 /api"，把它改成 "/v1/..." —— 那是错的。
# 真正的原因是**端口**：请求打到了全局的 api_port(8890)，而这个接口在 8001。
# 路径从头到尾都是对的，404 的 path 字段回显的就是正确的两层 /api。
API_PATH = "/api/v1/defect/material/current-config"

# 这个接口所在服务的默认端口。与全局 api_port（8890，主后端）**不是同一个
# 服务**，所以单独配，绝不能去改全局那三项——其他所有上报都靠它们。
DEFAULT_API_PORT = 8001

# 视为成功的 code。后端各接口的约定不完全一致，这里从宽。
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
    }
    model = data.get("model")
    if isinstance(model, dict):
        meta["modelPath"] = model.get("modelPath")
        meta["modelName"] = model.get("modelName")
        meta["modelVersion"] = model.get("modelVersion")
        meta["modelStatus"] = model.get("status")

    if skipped_threshold:
        warning(f"[材质配置] 跳过 {len(skipped_threshold)} 条阈值："
                + "；".join(skipped_threshold[:12])
                + ("…" if len(skipped_threshold) > 12 else ""))
    if skipped_stop:
        warning(f"[材质配置] 跳过 {len(skipped_stop)} 条停机规则："
                + "；".join(skipped_stop[:12])
                + ("…" if len(skipped_stop) > 12 else ""))
    return thresholds, stop_rules, meta


def _check_label(cam_id: str, name: str, label: Any) -> None:
    """用 DEFECT_LABEL_MAP 交叉校验 defectLabel，抓后端名称对不上的情况。"""
    if not label:
        return
    try:
        from AnomalyDetection_Tool.output.detection_info_formatter import (
            DEFECT_LABEL_MAP)
        expect = DEFECT_LABEL_MAP.get(name)
        if expect is not None and str(label) != expect:
            warning(f"[材质配置] {cam_id}/{name} 的 defectLabel={label!r} "
                    f"与本地映射 {expect!r} 不一致，已按 defectName 应用")
    except Exception:
        pass


# ======================================================================
#  拉取 + 应用
# ======================================================================
def _get_base_url() -> str:
    """本功能专用的基址。

    **不复用** Communication_Tool.http_client.get_base_url()：那个读的是
    全局 api_host/api_port/api_base_path（现场 8890，主后端），而这个接口
    在另一个服务的 8001 上。改全局会把所有上报一起带跑，所以这里单独取。

    material_config 段里没写的项，回落到全局同名项——这样只换端口时
    不必把 host / base_path 也抄一遍。
    """
    from AnomalyDetection_Tool.config.settings import (
        MATERIAL_CONFIG, get_upload_config)
    g = get_upload_config()
    host = MATERIAL_CONFIG.get("api_host") or g.get("api_host") or "localhost"
    port = MATERIAL_CONFIG.get("api_port") or g.get("api_port") or DEFAULT_API_PORT
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
                    info(f"[材质配置] HTTP {resp.status}: {text[:200]}")
                    return None
                try:
                    import json as _json
                    result = _json.loads(text)
                except Exception:
                    info(f"[材质配置] 响应不是 JSON: {text[:200]}")
                    return None
                if not _is_ok(result):
                    info(f"[材质配置] 业务错误 code="
                         f"{result.get('code')!r} msg={result.get('message')!r}"
                         f"（没有运行/暂停中的白布等情况属正常）")
                    return None
                data = result.get("data")
                if not isinstance(data, dict):
                    info(f"[材质配置] data 不是对象: {type(data).__name__}")
                    return None
                return data
    except asyncio.TimeoutError:
        info("[材质配置] 拉取超时")
        return None
    except aiohttp.ClientError as e:
        info(f"[材质配置] 网络错误: {e}")
        return None
    except Exception as e:
        info(f"[材质配置] 拉取异常: {e}")
        return None


def apply_config(data: dict) -> bool:
    """解析并写入覆盖层。成功返回 True。

    解析失败时**不动**已有配置。
    """
    from AnomalyDetection_Tool.config.remote_config_store import (
        get_remote_config_store)
    try:
        thresholds, stop_rules, meta = parse_config(data)
    except Exception as e:
        error(f"[材质配置] 解析失败，保留上一份配置: {e}")
        return False
    if not thresholds and not stop_rules:
        warning("[材质配置] 解析后没有任何可用配置，保留上一份"
                "（不会用空配置覆盖）")
        return False
    get_remote_config_store().apply(thresholds, stop_rules, meta)
    info(f"[材质配置] 已应用：白布={meta.get('clothNumber')} "
         f"卷号={meta.get('roller')} 材质={meta.get('material')} "
         f"规格={meta.get('specification')} "
         f"相机={meta.get('camera_count')} 台，"
         f"阈值 {len(thresholds)} 条 / 停机规则 {len(stop_rules)} 条")
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
        warning(f"[材质配置] 后端指定模型 {remote!r} 与本地 "
                f"yolo.model_path={local!r} 不一致。"
                f"**不会在运行中热切模型**（需重建各 worker 的 TensorRT "
                f"engine），请人工确认后改配置重启。")


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
        info(f"[材质配置] 拉取超过 {timeout + 3:.0f}s 未返回，放弃本次")
        return False
    if box.get("e") is not None:
        info(f"[材质配置] 拉取异常: {box['e']}")
        return False
    data = box.get("d")
    if data is None:
        return False
    return apply_config(data)
