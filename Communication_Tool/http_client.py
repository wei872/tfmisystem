#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
comms/http_client.py -- 统一 HTTP 请求客户端
所有对外 HTTP 接口调用集中在此模块，便于管理 URL、超时、重试策略。

依赖: aiohttp (异步 HTTP)
配置: 从 detection.config.settings.get_upload_config() 获取 api_host/api_port
"""

import json
import asyncio
from typing import Dict, Any, Optional, List

import aiohttp
import cv2
import numpy as np

from General_Tool.EnhancedLogger import info, error


# ================================================================
# URL 构建
# ================================================================

def get_base_url() -> str:
    """从 config.yaml 获取 API 基础 URL（唯一来源: settings.get_api_base_url）"""
    from AnomalyDetection_Tool.config.settings import get_api_base_url
    return get_api_base_url()


# ================================================================
# 1. 上传检测信息 JSON（生产主路径）
# ================================================================

async def upload_detection_info(
        detection_info: Dict[str, Any],
        api_url: str = "/image/upload",
        headers: Optional[Dict[str, str]] = None,
        timeout: int = 30,
) -> Optional[Dict[str, Any]]:
    """
    上传疵点检测信息 JSON（不含图片字节）

    Args:
        detection_info: 检测信息字典
        api_url: 接口路径（拼接到 base_url 之后）
        headers: 额外请求头
        timeout: 超时秒数

    Returns:
        服务端响应 JSON 或 None
    """
    default_headers = {"User-Agent": "TFMISystem-DetectionInfo"}
    if headers:
        default_headers.update(headers)

    full_url = get_base_url() + api_url

    try:
        data = aiohttp.FormData()
        qo_json_str = json.dumps(detection_info, ensure_ascii=False, indent=2)
        data.add_field("qoJson", qo_json_str, content_type="application/json")

        info(f"[Upload] POST {full_url}")

        async with aiohttp.ClientSession(headers=default_headers) as session:
            async with session.post(
                full_url, data=data,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    info(f"[Upload] HTTP {response.status}: {text}")
                    return None

                try:
                    result = await response.json()
                except Exception:
                    text = await response.text()
                    result = {"raw": text}

                info(f"[Upload] OK: {result}")
                return result

    except asyncio.TimeoutError:
        info("[Upload] timeout")
        return None
    except aiohttp.ClientError as e:
        info(f"[Upload] network error: {e}")
        return None
    except Exception as e:
        info(f"[Upload] error: {e}")
        return None


# ================================================================
# 2. 上传 OpenCV 图片 + 元数据（图片二进制 + JSON）
# ================================================================

async def upload_cv2_image(
        image: np.ndarray,
        api_url: str = "/image/upload",
        file_param_name: str = "file",
        extra_data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: int = 30,
) -> Optional[Dict[str, Any]]:
    """
    将 OpenCV 图像 + 元数据上传到后端 API（multipart form）

    Args:
        image: BGR 格式 numpy 图片
        api_url: 接口路径
        file_param_name: 文件字段名
        extra_data: 额外 JSON 元数据
        headers: 额外请求头
        timeout: 超时秒数

    Returns:
        服务端响应 JSON 或 None
    """
    if extra_data is None:
        extra_data = {}

    extension = "." + extra_data.get("imageType", "jpg").lower()
    success, buffer = cv2.imencode(extension, image)
    if not success:
        error("[Upload] image encode failed")
        return None

    image_bytes = buffer.tobytes()

    default_headers = {"User-Agent": "TFMISystem-ImageUpload"}
    if headers:
        default_headers.update(headers)

    # 构造文件名
    try:
        cam_id = extra_data.get("cameraId", "unknown")
        d_size_list = extra_data.get("defectSize", [0, 0])
        d_size = f"{d_size_list[0]}x{d_size_list[1]}"
        d_center_list = extra_data.get("defectCenter", [0, 0])
        d_center = f"[{d_center_list[0]}_{d_center_list[1]}]"
        ts = extra_data.get("timestamp", 0)
        d_type = str(extra_data.get("defectType", "none")).replace(" ", "_")
        img_ext = extra_data.get("imageType", "jpg")
        filename = f"{cam_id}_{d_size}_{d_center}_{ts}_{d_type}.{img_ext}"
    except (KeyError, IndexError, TypeError) as e:
        info(f"[Upload] filename build error: {e}")
        filename = f"upload_{extra_data.get('timestamp', 'now')}.jpg"

    data = aiohttp.FormData()
    data.add_field(
        file_param_name, image_bytes,
        filename=filename,
        content_type=f"image/{extension.lstrip('.')}")
    ex_json = json.dumps(extra_data, ensure_ascii=False, indent=2)
    data.add_field("qoJson", ex_json, content_type="application/json")

    full_url = get_base_url() + api_url

    try:
        async with aiohttp.ClientSession(headers=default_headers) as session:
            async with session.post(
                full_url, data=data,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                if response.status != 200:
                    text = await response.text()
                    info(f"[Upload] HTTP {response.status}: {text}")
                    return None

                result = await response.json()
                info(f"[Upload] image OK: {result}")
                return result

    except asyncio.TimeoutError:
        info("[Upload] image timeout")
        return None
    except aiohttp.ClientError as e:
        info(f"[Upload] image network error: {e}")
        return None
    except Exception as e:
        info(f"[Upload] image error: {e}")
        return None


# ================================================================
# 3. 批量并发上传图片
# ================================================================

async def batch_upload_cv2_images(
        images: List[np.ndarray],
        api_url: str = "/image/upload",
        file_param_name: str = "file",
        extra_data: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """批量并发上传多张 OpenCV 图像"""
    tasks = [
        upload_cv2_image(img, api_url, file_param_name, extra_data=extra_data)
        for img in images
    ]
    results = await asyncio.gather(*tasks)
    return [r for r in results if r is not None]


# ================================================================
# 4. 布料尺寸信息上报
# ================================================================

async def update_fabric_size(
        width: float,
        length: float,
) -> Optional[dict]:
    """
    上报布料宽度和长度到后端

    Args:
        width: 布幅宽度 (cm)
        length: 累计长度 (mm)

    Returns:
        服务端响应 JSON 或 None
    """
    url = get_base_url() + "/v1/defect/log/size"
    headers = {"Content-Type": "application/json"}
    payload = {"width": width, "length": length}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as response:
                if response.status != 200:
                    text = await response.text()
                    info(f"[FabricSize] HTTP {response.status}: {text}")
                    return None
                return await response.json()
    except asyncio.TimeoutError:
        info("[FabricSize] timeout")
        return None
    except aiohttp.ClientError as e:
        info(f"[FabricSize] network error: {e}")
        return None
    except Exception as e:
        info(f"[FabricSize] error: {e}")
        return None
