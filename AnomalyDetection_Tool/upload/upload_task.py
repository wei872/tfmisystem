#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
上传任务数据结构
"""

import time
from typing import Dict, Optional


class UploadTask:
    """上传任务"""
    __slots__ = [
        'image_path', 'camera_sn', 'camera_name', 'frame_number',
        'class_name', 'class_id', 'confidence',
        'x1', 'y1', 'x2', 'y2',
        'is_stop', 'real_area_mm2', 'fill_ratio',
        'detection_info', 'created_at', 'retry_count',
    ]

    def __init__(self, image_path: str, camera_sn: str,
                 camera_name: str, frame_number: str,
                 class_name: str, class_id: int,
                 confidence: float,
                 x1: int, y1: int, x2: int, y2: int,
                 is_stop: bool = False,
                 real_area_mm2: float = 0,
                 fill_ratio: float = 0,
                 detection_info: Optional[Dict] = None):
        self.image_path = image_path
        self.camera_sn = camera_sn
        self.camera_name = camera_name
        self.frame_number = frame_number
        self.class_name = class_name
        self.class_id = class_id
        self.confidence = confidence
        self.x1 = x1
        self.y1 = y1
        self.x2 = x2
        self.y2 = y2
        self.is_stop = is_stop
        self.real_area_mm2 = real_area_mm2
        self.fill_ratio = fill_ratio
        self.detection_info = detection_info or {}
        self.created_at = time.time()
        self.retry_count = 0