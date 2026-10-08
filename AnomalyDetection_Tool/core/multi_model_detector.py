#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
多模型 YOLO 检测器
职责:
  1. 根据布料类型（含边缘/不含边缘）动态选择模型。
  2. 统一管理多个 YOLO 模型实例的生命周期。
"""

import os
from typing import Dict, Optional, List
import numpy as np

from General_Tool.EnhancedLogger import info, error
from AnomalyDetection_Tool.core.yolo_detector import YOLODetector
from AnomalyDetection_Tool.models.data_types import DetectionBox


class MultiModelYOLODetector:
    """支持多模型切换的 YOLO 检测器"""

    def __init__(self, 
                 default_model_path: str,
                 with_edge_model_path: Optional[str] = None,
                 without_edge_model_path: Optional[str] = None,
                 confidence: float = 0.1,
                 device: str = None,
                 image_size: int = 640,
                 iou_threshold: float = 0.5,
                 worker_id: int = 0,
                 use_cuda_stream: bool = True):

        self.detectors: Dict[str, YOLODetector] = {}
        self.worker_id = worker_id

        self.confidence = confidence
        self.device = device
        self.image_size = image_size
        self.iou_threshold = iou_threshold

        self._load_detector("default", default_model_path, worker_id, use_cuda_stream)

        if with_edge_model_path and os.path.exists(with_edge_model_path):
            self._load_detector("with_edge", with_edge_model_path, worker_id, use_cuda_stream)
        if without_edge_model_path and os.path.exists(without_edge_model_path):
            self._load_detector("without_edge", without_edge_model_path, worker_id, use_cuda_stream)

        info(f"[MultiModelDetector] Worker-{worker_id} 初始化完成 | "
             f"已加载模型: {list(self.detectors.keys())}")

    def _load_detector(self, key: str, path: str,
                       worker_id: int = 0,           # ← 新增
                       use_cuda_stream: bool = True): # ← 新增
        try:
            self.detectors[key] = YOLODetector(
                model_path=path,
                confidence=self.confidence,
                device=self.device,
                image_size=self.image_size,
                iou_threshold=self.iou_threshold,
                worker_id=worker_id,          # ← 修改：传入真实 worker_id
                use_cuda_stream=use_cuda_stream  # ← 修改：启用独立 CUDA Stream
            )
        except Exception as e:
            error(f"[MultiModelDetector] Worker-{worker_id} 加载模型 {key} 失败: {e}")

    def speed_str(self) -> str:
        """透传底层检测器的分段耗时（预处理/GPU推理/NMS）"""
        det = self.detectors.get("default")
        if det is None and self.detectors:
            det = next(iter(self.detectors.values()))
        return det.speed_str() if det is not None else ""

    def detect(self, image: np.ndarray, fabric_type: str = "default") -> List[DetectionBox]:
        """
        执行检测
        :param image: 输入图像
        :param fabric_type: 布料类型 ('default', 'with_edge', 'without_edge')
        """
        detector = self.detectors.get(fabric_type) or self.detectors.get("default")
        if not detector:
            raise RuntimeError("没有可用的检测器实例")
        return detector.detect(image)

    def detect_batch(self, images: List[np.ndarray], fabric_types: List[str] = None) -> List[List[DetectionBox]]:
        """
        批量检测
        :param images: 图像列表
        :param fabric_types: 对应的布料类型列表，若为 None 则全部使用默认模型
        """
        if not images:
            return []
        
        if fabric_types is None:
            fabric_types = ["default"] * len(images)
        
        # 简单实现：按模型分组进行批量推理以优化性能
        # 这里为了逻辑清晰，先按帧调用，后续可优化为真正的 Grouped Batching
        results = []
        for img, ftype in zip(images, fabric_types):
            results.append(self.detect(img, ftype))
        return results

    def release(self):
        for det in self.detectors.values():
            det.release()
        self.detectors.clear()
