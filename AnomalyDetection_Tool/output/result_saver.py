#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检测结果保存器
目录结构: base_path / launch_tag / camera_name / Normal /
"""

import os
import threading
from datetime import datetime
from typing import Dict, Any

import numpy as np

from General_Tool.EnhancedLogger import info
from AnomalyDetection_Tool.config.settings import (
    SaveConfig, SAVE_IMAGE_EXT, SAVE_JPEG_QUALITY,
)
from AnomalyDetection_Tool.models.data_types import DetectionResult
from AnomalyDetection_Tool.utils.image_io import ImageIO
from AnomalyDetection_Tool.utils.path_utils import safe_filename
from AnomalyDetection_Tool.utils.time_utils import ts_to_ms_str


class ResultSaver:
    """检测结果保存器"""

    def __init__(self, base_path: str, save_config: SaveConfig,
                 patch_size: int = 640, launch_tag: str = ""):
        self.base_path = base_path
        self.save_config = save_config
        self.patch_size = patch_size
        self.launch_tag = (
            launch_tag
            or datetime.now().strftime("%Y%m%d%H%M%S"))
        self._lock = threading.Lock()
        self._camera_counts: Dict[str, int] = {}
        self._class_counts: Dict[str, Dict[str, int]] = {}
        os.makedirs(base_path, exist_ok=True)
        info(f"[ResultSaver] base={base_path} "
             f"launch_tag={self.launch_tag}")

    def _get_normal_dir(self, camera_name: str) -> str:
        normal_dir = os.path.join(
            self.base_path, self.launch_tag,
            camera_name, "Normal")
        os.makedirs(normal_dir, exist_ok=True)
        return normal_dir

    def ensure_anomaly_image(self, image: np.ndarray,
                             camera_name: str,
                             filename: str) -> str:
        """确保原图已保存"""
        from pathlib import Path
        normal_dir = self._get_normal_dir(camera_name)
        base = Path(filename).stem
        p = os.path.join(normal_dir, f"{base}{SAVE_IMAGE_EXT}")
        if not os.path.exists(p):
            ImageIO.save(p, image, SAVE_JPEG_QUALITY)
        return p

    def save(self, result: DetectionResult,
             original_image: np.ndarray,
             camera_name: str = "default",
             camera_sn: str = "",
             capture_timestamp: str = "") -> Dict[str, Any]:
        """保存检测结果"""
        saved = {
            "saved": False,
            "anomaly_image": None,
            "saved_files": [],
        }
        if not result.has_detection:
            return saved

        normal_dir = self._get_normal_dir(camera_name)
        safe_sn = safe_filename(camera_sn) if camera_sn else "NOSN"
        ts = capture_timestamp or result.capture_timestamp
        ts_str = ts_to_ms_str(ts)

        # ── 先算好要写的文件名（纯计算，不做 I/O）──────────────────
        targets = []
        for box_idx, box in enumerate(result.boxes):
            safe_cls = safe_filename(box.class_name)
            fn = f"{safe_sn}_{ts_str}_{safe_cls}_{box_idx}{SAVE_IMAGE_EXT}"
            targets.append((box, os.path.join(normal_dir, fn)))

        # ── 编码一次，写多份 ────────────────────────────────────────
        # 原实现对每个检测框都重新编码同一张整帧原图：
        # 一帧 5 个框 = 编码 5 次（PNG 约 179ms/次，合计近 0.9 秒）。
        # 图像内容完全相同，编码一次复用即可，落盘文件名保持不变，
        # 对下游（每个疵点一条记录、各自带 imageUrl）没有任何影响。
        encoded = None
        if self.save_config.save_original and targets:
            need_write = [t for t in targets if not os.path.exists(t[1])]
            if need_write:
                encoded = ImageIO.encode(
                    original_image, SAVE_IMAGE_EXT, SAVE_JPEG_QUALITY)

        # ── 写盘：在锁外做 ──────────────────────────────────────────
        # 原实现把写盘放在 self._lock 内，而 ResultSaver 是所有相机共享的
        # 单例 —— 等于所有异步保存线程在这里排队，4 个 worker 退化成 1 个。
        if self.save_config.save_original:
            for _box, save_path in targets:
                if encoded is not None and not os.path.exists(save_path):
                    ImageIO.write_bytes(save_path, encoded)
                saved["saved_files"].append(save_path)
                if saved["anomaly_image"] is None:
                    saved["anomaly_image"] = save_path

        # ── 锁内只更新计数器 ────────────────────────────────────────
        with self._lock:
            saved["saved"] = True
            self._class_counts.setdefault(camera_name, {})
            cc = self._class_counts[camera_name]
            for box, _p in targets:
                cc[box.class_name] = cc.get(box.class_name, 0) + 1
            self._camera_counts[camera_name] = (
                self._camera_counts.get(camera_name, 0) + 1)

        return saved

    def get_camera_stats(self) -> Dict[str, int]:
        with self._lock:
            return self._camera_counts.copy()

    def get_class_stats(self) -> Dict[str, Dict[str, int]]:
        with self._lock:
            return {k: v.copy()
                    for k, v in self._class_counts.items()}