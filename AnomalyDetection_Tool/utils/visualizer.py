#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检测结果可视化（支持中文显示）
"""

import os
from typing import List, Tuple, Dict, Any

import cv2
import numpy as np

from AnomalyDetection_Tool.utils.font_utils import FONT_SEARCH_PATHS

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


class Visualizer:
    """检测结果可视化"""

    _font_cache: Dict[int, Any] = {}

    @classmethod
    def _get_font(cls, size: int = 16):
        if not PIL_AVAILABLE:
            return None
        if size in cls._font_cache:
            return cls._font_cache[size]

        font = None
        for path in FONT_SEARCH_PATHS:
            if os.path.exists(path):
                try:
                    font = ImageFont.truetype(path, size)
                    break
                except Exception:
                    continue
        if font is None:
            try:
                font = ImageFont.load_default()
            except Exception:
                pass

        cls._font_cache[size] = font
        return font

    @classmethod
    def draw_box(cls, image: np.ndarray, box,
                 color: Tuple[int, int, int] = (0, 0, 255),
                 thickness: int = 2,
                 font_size: int = 14) -> np.ndarray:
        result = image.copy()
        x1, y1 = int(box.x1), int(box.y1)
        x2, y2 = int(box.x2), int(box.y2)
        label = f"{box.class_name}: {box.confidence:.2f}"

        cv2.rectangle(result, (x1, y1), (x2, y2), color, thickness)

        if PIL_AVAILABLE:
            font = cls._get_font(font_size)
            if font:
                pil_img = Image.fromarray(
                    cv2.cvtColor(result, cv2.COLOR_BGR2RGB))
                draw = ImageDraw.Draw(pil_img)
                try:
                    bbox = draw.textbbox((0, 0), label, font=font)
                    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
                except AttributeError:
                    tw, th = draw.textsize(label, font=font)

                ly = max(y1 - th - 6, 0)
                fill_color = (color[2], color[1], color[0])
                draw.rectangle(
                    [x1, ly, x1 + tw + 6, ly + th + 6],
                    fill=fill_color)
                draw.text(
                    (x1 + 3, ly + 3), label,
                    font=font, fill=(255, 255, 255))
                return cv2.cvtColor(
                    np.array(pil_img), cv2.COLOR_RGB2BGR)

        cv2.putText(
            result, label, (x1, max(y1 - 5, 15)),
            cv2.FONT_HERSHEY_SIMPLEX, 0.5,
            (255, 255, 255), 1)
        return result

    @classmethod
    def draw_boxes(cls, image: np.ndarray, boxes: List,
                   color: Tuple[int, int, int] = (0, 0, 255),
                   thickness: int = 2) -> np.ndarray:
        result = image.copy()
        for box in boxes:
            result = cls.draw_box(result, box, color, thickness)
        return result