#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
文件名: analysis/coarse_defect_analyzer.py
模块概述: 粗粒度缺陷分析器（基于传统图像处理，不需要GPU）
===============================================================================
"""

import cv2
import numpy as np
from typing import Dict

from General_Tool.EnhancedLogger import info
from AnomalyDetection_Tool.config.settings import (
    PIXEL_AREA_TO_MM2,
    COARSE_DETECT_CONFIG,
    CLASS_DETECT_CONFIG,
    DEFAULT_DETECT_CONFIG,
)

class CoarseDefectAnalyzer:
    """
    粗粒度缺陷分析器（传统图像处理版）

    与 DinomalyDefectAnalyzer 接口兼容，可互换使用。
    """

    def __init__(self, slice_size: int = 640):
        self.slice_size = slice_size
        info("[CoarseAnalyzer] 粗粒度缺陷分析器已初始化")

    @staticmethod
    def detect_abnormal_region(gray, detect_dark=False, percentile=85):
        """检测异常区域"""
        config = COARSE_DETECT_CONFIG

        blur_size = config.get("blur_kernel_size", 9)
        blur_sigma = config.get("blur_sigma", 2.5)
        gray_blur = cv2.GaussianBlur(gray, (blur_size, blur_size), blur_sigma)

        if detect_dark:
            threshold = np.percentile(gray_blur, percentile)
            abnormal_mask = gray_blur < threshold
        else:
            threshold = np.percentile(gray_blur, percentile)
            abnormal_mask = gray_blur > threshold

        abnormal_mask = abnormal_mask.astype(np.uint8) * 255

        morph_size = config.get("morph_kernel_size", 11)
        kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (morph_size, morph_size))

        close_iter = config.get("close_iterations", 2)
        abnormal_mask = cv2.morphologyEx(
            abnormal_mask, cv2.MORPH_CLOSE, kernel, iterations=close_iter)

        open_iter = config.get("open_iterations", 1)
        kernel_small = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (morph_size // 2, morph_size // 2))
        abnormal_mask = cv2.morphologyEx(
            abnormal_mask, cv2.MORPH_OPEN, kernel_small, iterations=open_iter)

        abnormal_mask_blur = cv2.GaussianBlur(abnormal_mask, (7, 7), 2)
        abnormal_mask = (abnormal_mask_blur > 127).astype(np.uint8) * 255

        return abnormal_mask, threshold

    @staticmethod
    def filter_small_regions(mask, min_area=20):
        """过滤小区域"""
        num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(
            mask, connectivity=8)

        filtered_mask = np.zeros_like(mask)
        total_pixels = 0
        num_valid_regions = 0

        for i in range(1, num_labels):
            area = stats[i, cv2.CC_STAT_AREA]
            if area >= min_area:
                filtered_mask[labels == i] = 255
                total_pixels += area
                num_valid_regions += 1

        return filtered_mask, total_pixels, num_valid_regions

    def extract_defect_features(self, image: np.ndarray, box,
                                class_name: str) -> Dict:
        """
        提取缺陷特征（与 DinomalyDefectAnalyzer 接口兼容）

        参数:
            image: 原图（完整大图）
            box: 检测框（含 x1,y1,x2,y2 属性）
            class_name: str

        返回: 与 DinomalyDefectAnalyzer.extract_defect_features 相同结构的字典
        """
        empty = {
            'pixel_area': 0, 'heatmap': None, 'mask': None,
            'num_regions': 0, 'fill_ratio': 0, 'anomaly_score': 0,
            'debug_info': {},
        }

        if image is None or image.size == 0:
            return empty

        # 提取ROI
        h, w = image.shape[:2]
        x1 = max(0, int(box.x1))
        y1 = max(0, int(box.y1))
        x2 = min(w, int(box.x2))
        y2 = min(h, int(box.y2))

        roi = image[y1:y2, x1:x2]
        if roi.size == 0 or roi.shape[0] < 5 or roi.shape[1] < 5:
            return empty

        # 获取类别配置
        config = CLASS_DETECT_CONFIG.get(class_name, DEFAULT_DETECT_CONFIG)
        detect_dark = config.get('detect_dark', False)
        percentile = config.get('percentile', 85 if not detect_dark else 15)
        min_area = config.get(
            'min_area', COARSE_DETECT_CONFIG.get('min_area_pixels', 20))

        # 转换为灰度
        if len(roi.shape) == 3:
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        else:
            gray = roi.copy()

        # 1. 检测异常区域
        raw_mask, threshold = self.detect_abnormal_region(
            gray, detect_dark, percentile)

        # 2. 反转掩膜
        abnormal_mask = 255 - raw_mask

        # 3. 过滤小区域
        filtered_mask, abnormal_pixels, num_valid_regions = \
            self.filter_small_regions(abnormal_mask, min_area)

        # 4. 统计
        total_roi_pixels = gray.shape[0] * gray.shape[1]
        normal_pixels = total_roi_pixels - abnormal_pixels
        fill_ratio = normal_pixels / total_roi_pixels if total_roi_pixels > 0 else 0
        abnormal_ratio = abnormal_pixels / total_roi_pixels if total_roi_pixels > 0 else 0

        # 5. 生成热力图
        median_val = np.median(gray)
        if detect_dark:
            intensity = (median_val - gray.astype(np.float32)) / (median_val + 1e-6)
        else:
            intensity = (gray.astype(np.float32) - median_val) / (255 - median_val + 1e-6)
        intensity = np.clip(intensity, 0, 1)
        heatmap = intensity.copy()
        heatmap[filtered_mask == 0] = 0

        debug_info = {
            'threshold': threshold,
            'detect_dark': detect_dark,
            'percentile': percentile,
            'roi_total_pixels': total_roi_pixels,
            'abnormal_pixels': abnormal_pixels,
            'normal_pixels': normal_pixels,
            'abnormal_ratio': abnormal_ratio,
            'fill_ratio': fill_ratio,
            'num_regions': num_valid_regions,
            'defect_ratio': abnormal_ratio,
            'pixel_area': abnormal_pixels,
            'mask_threshold': 'N/A (coarse mode)',
            'slice_size': self.slice_size,
        }

        return {
            'pixel_area': abnormal_pixels,
            'heatmap': heatmap,
            'mask': filtered_mask,
            'num_regions': num_valid_regions,
            'fill_ratio': fill_ratio,
            'anomaly_score': abnormal_ratio,  # 粗粒度模式用异常占比作为score
            'debug_info': debug_info,
        }

    @staticmethod
    def calculate_real_area_mm2(pixel_area: int) -> float:
        """转换像素面积为实际面积（mm²）"""
        return pixel_area * PIXEL_AREA_TO_MM2

    def release(self):
        """释放资源（粗粒度模式无需释放）"""
        info("[CoarseAnalyzer] 已释放")