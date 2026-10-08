#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
停机文件夹保存管理器
目录结构: base_path / launch_tag / camera_name / StopMachine /
"""

import os
import threading
from datetime import datetime

import cv2
import numpy as np

from General_Tool.EnhancedLogger import info
from AnomalyDetection_Tool.config.settings import (
    HEATMAP_CONFIG, SAVE_IMAGE_EXT, SAVE_JPEG_QUALITY,
)
from AnomalyDetection_Tool.utils.path_utils import safe_filename
from AnomalyDetection_Tool.utils.image_io import ImageIO

# ================================================================
# 热力图配色表
# ================================================================
_COLORMAP_TABLE = {
    "jet": cv2.COLORMAP_JET,
    "jet_r": cv2.COLORMAP_JET,
    "hot": cv2.COLORMAP_HOT,
    "inferno": cv2.COLORMAP_INFERNO,
    "turbo": cv2.COLORMAP_TURBO,
    "viridis": cv2.COLORMAP_VIRIDIS,
}

# ================================================================
# 标注样式
# ================================================================
_ANNO = {
    "box_color": (0, 0, 255),
    "box_thickness": 2,
    "font": cv2.FONT_HERSHEY_SIMPLEX,
    "font_scale": 0.7,
    "font_thickness": 2,
    "text_color": (255, 255, 255),
    "bg_color": (0, 0, 180),
    "bg_alpha": 0.7,
    "line_spacing": 8,
    "padding": 4,
}


class MachineStopSaver:
    """停机保存器"""

    def __init__(self, base_path: str, launch_tag: str):
        if not launch_tag:
            raise ValueError("launch_tag cannot be empty")
        self.base_path = base_path
        self.launch_tag = launch_tag
        self._lock = threading.Lock()
        self._save_count = 0
        self._class_counts: dict = {}
        os.makedirs(base_path, exist_ok=True)

    def save(self, image: np.ndarray, camera_name: str,
             filename: str, box_info: dict,
             analysis_result: dict, rule_used: dict,
             camera_sn: str = "", capture_timestamp: str = "",
             box_index: int = 0) -> str:
        """保存停机图（原图 + 热力图 + 掩膜叠加图）"""
        with self._lock:
            stop_dir = os.path.join(
                self.base_path, self.launch_tag,
                camera_name, "StopMachine")
            os.makedirs(stop_dir, exist_ok=True)

            cls_name = box_info.get("class_name", "unknown")
            confidence = box_info.get("confidence", 0)
            x1 = box_info.get("x1", 0)
            y1 = box_info.get("y1", 0)
            x2 = box_info.get("x2", 0)
            y2 = box_info.get("y2", 0)
            area_mm2 = analysis_result.get("real_area_mm2", 0)
            debug_info = analysis_result.get("debug_info", {})
            abnormal_ratio = debug_info.get("abnormal_ratio", 0)
            mask = analysis_result.get("mask")
            heatmap = analysis_result.get("heatmap")

            safe_sn = (safe_filename(camera_sn)
                       if camera_sn else "NOSN")
            safe_cls = safe_filename(cls_name)

            ts = (capture_timestamp
                  or datetime.now().strftime(
                      "%Y-%m-%d %H:%M:%S.%f")[:-3])
            dt = datetime.fromisoformat(ts)
            ts_str = str(int(dt.timestamp() * 1000))

            base_fn = f"{safe_sn}_{ts_str}_{safe_cls}_{box_index}"
            image_bgr = ImageIO.ensure_bgr(image)

            # 原图
            save_path = os.path.join(stop_dir, f"{base_fn}{SAVE_IMAGE_EXT}")
            ImageIO.save(save_path, image_bgr, SAVE_JPEG_QUALITY)

            # 热力图
            if (heatmap is not None
                    and HEATMAP_CONFIG.get("save_overlay")):
                self._save_heatmap_overlay(
                    image_bgr, heatmap,
                    x1, y1, x2, y2,
                    confidence, area_mm2, abnormal_ratio,
                    os.path.join(stop_dir, f"{base_fn}_heatmap{SAVE_IMAGE_EXT}"))

            # 掩膜
            if (mask is not None
                    and HEATMAP_CONFIG.get("save_mask_overlay")):
                self._save_mask_overlay(
                    image_bgr, mask,
                    x1, y1, x2, y2,
                    confidence, area_mm2, abnormal_ratio,
                    os.path.join(stop_dir, f"{base_fn}_mask{SAVE_IMAGE_EXT}"))

            self._save_count += 1
            self._class_counts[cls_name] = (
                self._class_counts.get(cls_name, 0) + 1)

            info(f"  [停机保存] {save_path} "
                 f"area={area_mm2:.2f}mm²")
            return save_path

    def get_save_count(self) -> int:
        with self._lock:
            return self._save_count

    def get_class_counts(self) -> dict:
        with self._lock:
            return self._class_counts.copy()

    # ================================================================
    # 标注绘制
    # ================================================================
    @staticmethod
    def _draw_info(image, x1, y1, x2, y2,
                   confidence, area_mm2, abnormal_ratio):
        """在图像上绘制检测框和信息标注"""
        canvas = image.copy()
        h, w = canvas.shape[:2]
        c = _ANNO

        cv2.rectangle(
            canvas, (x1, y1), (x2, y2),
            c["box_color"], c["box_thickness"])

        lines = [
            f"conf: {confidence:.2f}",
            f"area: {area_mm2:.2f} mm2",
        ]
        if abnormal_ratio > 0:
            lines.append(f"abnormal: {abnormal_ratio * 100:.1f}%")

        text_sizes = []
        for line in lines:
            (tw, th), _ = cv2.getTextSize(
                line, c["font"], c["font_scale"], c["font_thickness"])
            text_sizes.append((tw, th))

        max_tw = max(ts[0] for ts in text_sizes)
        total_th = (sum(ts[1] for ts in text_sizes)
                    + c["line_spacing"] * (len(lines) - 1))
        bg_w = max_tw + c["padding"] * 2
        bg_h = total_th + c["padding"] * 2

        bg_x1 = max(0, x1)
        bg_y1 = (y1 - bg_h - 4) if (y1 - bg_h - 4) >= 0 else (y1 + 2)
        if bg_x1 + bg_w > w:
            bg_x1 = max(0, w - bg_w)
        bg_x2 = min(w, bg_x1 + bg_w)
        bg_y2 = min(h, bg_y1 + bg_h)

        overlay = canvas.copy()
        cv2.rectangle(
            overlay, (bg_x1, bg_y1), (bg_x2, bg_y2),
            c["bg_color"], -1)
        cv2.addWeighted(
            overlay, c["bg_alpha"],
            canvas, 1 - c["bg_alpha"], 0, canvas)

        cur_y = bg_y1 + c["padding"]
        for i, line in enumerate(lines):
            cur_y += text_sizes[i][1]
            cv2.putText(
                canvas, line, (bg_x1 + c["padding"], cur_y),
                c["font"], c["font_scale"],
                c["text_color"], c["font_thickness"],
                cv2.LINE_AA)
            cur_y += c["line_spacing"]

        return canvas

    # ================================================================
    # 热力图叠加
    # ================================================================
    @classmethod
    def _save_heatmap_overlay(cls, image, heatmap,
                              x1, y1, x2, y2,
                              confidence, area_mm2,
                              abnormal_ratio, save_path):
        full = image.copy()
        roi_h, roi_w = y2 - y1, x2 - x1
        if roi_h <= 0 or roi_w <= 0:
            return

        hm = cv2.resize(
            heatmap.astype(np.float32), (roi_w, roi_h),
            interpolation=cv2.INTER_LINEAR)

        lo, hi = hm.min(), hm.max()
        if hi - lo > 1e-6:
            hm_u8 = ((hm - lo) / (hi - lo) * 255).astype(np.uint8)
        else:
            hm_u8 = np.zeros((roi_h, roi_w), dtype=np.uint8)

        cm_name = HEATMAP_CONFIG.get("colormap", "jet_r")
        cv_cm = _COLORMAP_TABLE.get(cm_name, cv2.COLORMAP_JET)
        colored = cv2.applyColorMap(hm_u8, cv_cm)
        if cm_name.endswith("_r"):
            colored = colored[:, :, ::-1]

        alpha = HEATMAP_CONFIG.get("overlay_alpha", 0.5)
        roi = full[y1:y2, x1:x2].astype(np.float32)
        blended = roi * (1 - alpha) + colored.astype(np.float32) * alpha
        full[y1:y2, x1:x2] = np.clip(blended, 0, 255).astype(np.uint8)

        full = cls._draw_info(
            full, x1, y1, x2, y2,
            confidence, area_mm2, abnormal_ratio)
        ImageIO.save(save_path, full, SAVE_JPEG_QUALITY)

    # ================================================================
    # 掩膜叠加
    # ================================================================
    @classmethod
    def _save_mask_overlay(cls, image, mask,
                           x1, y1, x2, y2,
                           confidence, area_mm2,
                           abnormal_ratio, save_path):
        full = image.copy()
        roi_h, roi_w = y2 - y1, x2 - x1
        if roi_h <= 0 or roi_w <= 0:
            return

        if mask.shape[0] != roi_h or mask.shape[1] != roi_w:
            mask_r = cv2.resize(
                mask.astype(np.uint8), (roi_w, roi_h)) > 0
        else:
            mask_r = mask > 0

        alpha = HEATMAP_CONFIG.get("overlay_alpha", 0.5)
        mc = np.array(
            HEATMAP_CONFIG.get("mask_color", [0, 0, 255]),
            dtype=np.float32)

        roi = full[y1:y2, x1:x2].astype(np.float32)
        m3 = np.stack([mask_r] * 3, axis=2).astype(np.float32)
        cl = np.full_like(roi, mc)
        blended = roi * (1 - m3 * alpha) + cl * m3 * alpha
        full[y1:y2, x1:x2] = np.clip(blended, 0, 255).astype(np.uint8)

        full = cls._draw_info(
            full, x1, y1, x2, y2,
            confidence, area_mm2, abnormal_ratio)
        ImageIO.save(save_path, full, SAVE_JPEG_QUALITY)