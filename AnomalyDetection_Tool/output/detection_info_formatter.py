#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检测结果 → 标准上传 JSON 格式
"""

import time
import cv2
import numpy as np
import base64
from AnomalyDetection_Tool.config.settings import PIXEL_TO_MM
from AnomalyDetection_Tool.utils.path_utils import (
    build_relative_image_path,
)
from AnomalyDetection_Tool.output.encoder_state_service import get_encoder_state
from AnomalyDetection_Tool.config.settings import REAL_WIDTH_CM, IMAGE_WIDTH_PX

# 缺陷类型中文 → 英文标识映射表
DEFECT_LABEL_MAP = {
    "包边不良": "edge_bad",
    "包边不重叠": "edge_separate",
    "单根包不进": "edge_not_in",
    "短废纱线": "waste_yarn",
    "多股线": "multi_line",
    "经线毛球": "meridian_ball",
    "棉团": "cotton_ball",
    "捻接器接头": "joint",
    "飘絮": "feather",
    "缺纬": "lack_lat",
    "纬线不直": "lat_not_straight",
    "纬线杂质": "lat_impurity",
    "纬斜": "lat_slope",
    "蚊虫小黑点": "black_cot",
    "稀缝": "thin_seam",
    "小尾巴": "small_tail",
    "油点线": "oil_dot_line",
}


def map_defect_type(class_name: str) -> str:
    """
    将缺陷类型中文名称映射为英文标识。
    若未找到对应映射，则返回原始名称，避免数据丢失。

    :param class_name: 模型输出的原始类别名称（中文）
    :return: 对应的英文标识字符串
    """
    return DEFECT_LABEL_MAP.get(class_name, class_name)


def format_detection_info(
    camera_sn: str,
    camera_name: str,
    image: np.ndarray,
    box,
    analysis_result: dict,
    should_stop: bool,
    frame_number: str = "",
    image_path: str = "",
    total_boxes: int = 1,
) -> dict:
    """将单个检测框信息整理为标准 dict"""
    img_h, img_w = image.shape[:2]
    x1, y1 = int(box.x1), int(box.y1)
    x2, y2 = int(box.x2), int(box.y2)
    center = [(x1 + x2) // 2, (y1 + y2) // 2]
    size = [x2 - x1, y2 - y1]

    # ---------- 以 box 中心裁剪 640x640，边缘不足则以边缘为边 ----------
    CROP_SIZE = 640
    half = CROP_SIZE // 2
    cx, cy = center

    crop_x1 = cx - half
    crop_y1 = cy - half
    crop_x2 = cx + half
    crop_y2 = cy + half

    # 水平方向：越界则整体平移，保持窗口 640 宽，不补黑边
    if crop_x1 < 0:
        crop_x1, crop_x2 = 0, CROP_SIZE
    elif crop_x2 > img_w:
        crop_x2, crop_x1 = img_w, img_w - CROP_SIZE

    # 垂直方向同理
    if crop_y1 < 0:
        crop_y1, crop_y2 = 0, CROP_SIZE
    elif crop_y2 > img_h:
        crop_y2, crop_y1 = img_h, img_h - CROP_SIZE

    # 极端兜底：图像本身小于 640 时取实际范围
    crop_x1 = max(0, crop_x1)
    crop_y1 = max(0, crop_y1)
    crop_x2 = min(img_w, crop_x2)
    crop_y2 = min(img_h, crop_y2)

    cropped = image[crop_y1:crop_y2, crop_x1:crop_x2]

    # 2. 编码参数设置：使用 JPEG 格式，并指定压缩质量
    encode_params = [int(cv2.IMWRITE_JPEG_QUALITY), 10]

    # 3. 编码为二进制流
    ok, buf = cv2.imencode(".jpg", cropped, encode_params)


    image_base64 = base64.b64encode(buf.tobytes()).decode("utf-8") if ok else ""

    real_area = (analysis_result.get("real_area_mm2", 0)
                 if analysis_result else 0)
    dbg = (analysis_result.get("debug_info", {})
           if analysis_result else {})

    fabric_x = round(center[0] * PIXEL_TO_MM, 2)
    fabric_y = round(center[1] * PIXEL_TO_MM, 2)

    relative_path = build_relative_image_path(image_path)

    # 严重程度
    ar = dbg.get("abnormal_ratio", 0)
    if ar > 0.5:
        severity = "严重"
    elif ar > 0.2:
        severity = "中等"
    else:
        severity = "轻微"

    region = "多区域" if total_boxes > 1 else "单区域"

    # 读取编码器累计距离（零锁开销的快照）
    enc = get_encoder_state()
    defect_length = round(enc.cumulative_distance, 2)

    # 缺陷类型：中文映射为英文标识
    defect_type = map_defect_type(box.class_name)

    return {
        "machineId": "094",
        "cameraId": camera_name,
        "cameraSn": camera_sn,
        "imageSize": [img_w, img_h],
        "imageType": "png",
        "colorSpace": "BGR",
        "imageUrl": relative_path,
        "imageBase64": image_base64,
        "defectType": defect_type,
        "confidence": round(box.confidence, 4),
        "timestamp": int(round(time.time() * 1000)),
        "isStop": 1 if should_stop else 0,
        "defectCenter": center,
        "defectSize": size,
        "defectArea": round(real_area, 2),
        "fabricCoords": [fabric_x, fabric_y],
        "additionalInfo": [severity, region],
        "defectLength": defect_length,  # 单位：mm，疵点发现时的累计行进距离
        "realLength": REAL_WIDTH_CM * 10,
        "pictureLength": IMAGE_WIDTH_PX,
    }