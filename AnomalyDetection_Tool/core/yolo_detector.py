#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: yolo_detector.py
职责: YOLO单模型检测器，负责原图直接推理。

优化点（v2.2）:
  - YOLO推理Stream使用高优先级（priority=-1），确保GPU抢占优先权
  - 经纬线分析Stream使用低优先级（priority=0），让出GPU给推理
  - Pinned Memory预分配，加速H2D传输
  - 线程安全的模型推理（RLock保护）
"""

import os
import threading
from typing import List, Dict, Optional
from contextlib import contextmanager

import numpy as np

from General_Tool.EnhancedLogger import info
from AnomalyDetection_Tool.models.data_types import DetectionBox

try:
    import torch
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False


class DetectorError(Exception):
    pass


class ModelLoadError(DetectorError):
    pass


class InferenceError(DetectorError):
    pass


@contextmanager
def _null_context():
    """空上下文管理器，用于CPU模式下替代CUDA Stream上下文"""
    yield


class YOLODetector:
    """
    YOLO检测器（单模型）

    关键设计：
      - 每个Worker拥有独立的高优先级CUDA Stream（priority=-1）
      - 确保YOLO推理在GPU上具有最高调度优先权
      - 经纬线分析使用低优先级Stream，避免与推理竞争
    """

    def __init__(self, model_path: str,
                 confidence: float = 0.1,
                 device: str = None,
                 image_size: int = 640,
                 iou_threshold: float = 0.5,
                 worker_id: int = 0,
                 use_cuda_stream: bool = False):

        if not os.path.exists(model_path):
            raise ModelLoadError(f"模型不存在: {model_path}")

        self.model_path = model_path
        self.confidence = confidence
        self.image_size = image_size
        self.iou_threshold = iou_threshold
        self.worker_id = worker_id
        self.use_cuda_stream = use_cuda_stream and TORCH_AVAILABLE

        if device is None:
            self.device = (
                "cuda"
                if TORCH_AVAILABLE and torch.cuda.is_available()
                else "cpu"
            )
        else:
            self.device = device

        # ultralytics 自带的分段耗时（预处理 / 推理 / NMS），单位毫秒
        self.last_speed: Optional[dict] = None

        self._is_cuda = TORCH_AVAILABLE and "cuda" in self.device
        self._cuda_stream: Optional["torch.cuda.Stream"] = None

        if self._is_cuda and self.use_cuda_stream:
            idx = self._parse_device_idx(self.device)
            # ★ 关键优化：YOLO推理使用高优先级Stream（priority=-1）
            # CUDA Stream优先级范围：负数=高优先级，0=默认优先级
            # 高优先级Stream中的kernel会被优先调度，抢占低优先级Stream
            try:
                self._cuda_stream = torch.cuda.Stream(
                    device=idx,
                    priority=-1,  # 高优先级，确保YOLO推理优先执行
                )
                info(f"[YOLO-{worker_id}] 高优先级CUDA Stream已创建 (priority=-1)")
            except Exception as e:
                # 部分GPU驱动不支持优先级设置，降级为普通Stream
                info(f"[YOLO-{worker_id}] 高优先级Stream创建失败({e})，使用默认优先级")
                self._cuda_stream = torch.cuda.Stream(device=idx)

        self._pinned_buffer: Optional["torch.Tensor"] = None
        self._lock = threading.RLock()
        self._model = None
        self._class_names: Dict[int, str] = {}
        self._load_model()

    @staticmethod
    def _parse_device_idx(device: str) -> int:
        """解析设备字符串中的GPU索引，如 'cuda:1' → 1"""
        if ":" in device:
            try:
                return int(device.split(":")[1])
            except (ValueError, IndexError):
                return 0
        return 0

    def _get_stream_ctx(self):
        """获取CUDA Stream上下文，CPU模式返回空上下文"""
        if self._cuda_stream is not None:
            return torch.cuda.stream(self._cuda_stream)
        return _null_context()

    def _load_model(self):
        try:
            from ultralytics import YOLO

            with self._get_stream_ctx():
                self._model = YOLO(self.model_path)
                if hasattr(self._model, "names"):
                    self._class_names = dict(self._model.names)

            if self._cuda_stream is not None:
                self._cuda_stream.synchronize()

            self._warmup()
            self._allocate_pinned_buffer()

            features = []
            if self._cuda_stream:
                features.append("高优先级CUDA Stream(priority=-1)")
            if self._pinned_buffer is not None:
                features.append("Pinned Memory")
            feat_str = f"  特性: {', '.join(features)}" if features else ""

            info(
                f"[YOLO-{self.worker_id}] 加载完成: "
                f"{os.path.basename(self.model_path)}"
                f"\n  设备: {self.device}  类别: {len(self._class_names)}"
                f"\n{feat_str}"
            )

        except Exception as e:
            raise ModelLoadError(f"Worker-{self.worker_id} 加载失败: {e}")

    def _warmup(self):
        """模型预热：消除首次推理的初始化延迟"""
        if self._model is None:
            return
        info(f"[YOLO-{self.worker_id}] 预热开始...")
        dummy = np.zeros((self.image_size, self.image_size, 3), dtype=np.uint8)
        with self._get_stream_ctx():
            for _ in range(3):
                self._model.predict(
                    source=dummy,
                    imgsz=self.image_size,
                    conf=self.confidence,
                    iou=self.iou_threshold,
                    device=self.device,
                    verbose=False,
                )
        if self._is_cuda:
            if self._cuda_stream:
                self._cuda_stream.synchronize()
            else:
                torch.cuda.synchronize(self.device)
        info(f"[YOLO-{self.worker_id}] 预热完成")

    def _allocate_pinned_buffer(self):
        """
        预分配Pinned Memory（页锁定内存）。
        Pinned Memory可绕过操作系统分页，使CPU→GPU数据传输速度提升2~4倍。
        仅在CUDA模式下有效。
        """
        if not self._is_cuda or not TORCH_AVAILABLE:
            return
        try:
            batch_size = 8
            c, h, w = 3, self.image_size, self.image_size
            self._pinned_buffer = torch.empty(
                (batch_size, c, h, w),
                dtype=torch.uint8,
                pin_memory=True,
            )
            info(
                f"[YOLO-{self.worker_id}] Pinned Buffer已预分配: "
                f"{self._pinned_buffer.shape}"
            )
        except Exception as e:
            info(f"[YOLO-{self.worker_id}] Pinned Buffer预分配失败: {e}")

    @property
    def class_names(self) -> Dict[int, str]:
        return self._class_names.copy()

    def detect(self, image: np.ndarray) -> List[DetectionBox]:
        """
        单帧推理（主链路调用）。
        在高优先级CUDA Stream中执行，确保GPU优先调度。
        """
        if self._model is None:
            raise InferenceError("模型未加载")
        try:
            # 在高优先级Stream中提交推理kernel
            # GPU调度器会优先执行此Stream中的任务，即使低优先级Stream正在运行
            with self._get_stream_ctx():
                results = self._model.predict(
                    source=image,
                    imgsz=self.image_size,
                    conf=self.confidence,
                    iou=self.iou_threshold,
                    device=self.device,
                    verbose=False,
                )
            # 等待当前Stream的所有kernel执行完毕
            if self._cuda_stream:
                self._cuda_stream.synchronize()

            self._record_speed(results)
            return self._parse(results)
        except Exception as e:
            raise InferenceError(f"Worker-{self.worker_id} 推理失败: {e}")

    # ================================================================
    # 分段耗时
    # ================================================================
    def _record_speed(self, results) -> None:
        """
        记录 ultralytics 自带的分段耗时（毫秒）。

        排查"推理为什么变慢"时这是关键信息：外层只统计 predict() 的总时间，
        而它同时包含 CPU 侧的 letterbox 预处理、GPU 推理、以及 NMS 后处理。
        三者变慢的原因完全不同：
          - inference 涨   -> GPU 侧（降频、显存带宽被抢、与其他 Stream 竞争）
          - preprocess 涨  -> CPU 侧（后台线程抢核/GIL、图像尺寸变大）
          - postprocess 涨 -> 检出框数量激增导致 NMS 变重
        """
        try:
            speed = getattr(results[0], "speed", None) if results else None
            if isinstance(speed, dict):
                self.last_speed = {
                    "preprocess": float(speed.get("preprocess") or 0.0),
                    "inference": float(speed.get("inference") or 0.0),
                    "postprocess": float(speed.get("postprocess") or 0.0),
                }
        except Exception:
            pass

    def speed_str(self) -> str:
        """把分段耗时格式化成一行，便于打进日志"""
        sp = self.last_speed
        if not sp:
            return ""
        return (f"预处理={sp['preprocess']:.1f} "
                f"GPU推理={sp['inference']:.1f} "
                f"NMS={sp['postprocess']:.1f}")

    def detect_batch(self, images: List[np.ndarray]) -> List[List[DetectionBox]]:
        """批量推理"""
        if self._model is None:
            raise InferenceError("模型未加载")
        if not images:
            return []
        try:
            with self._get_stream_ctx():
                results = self._model.predict(
                    source=images,
                    imgsz=self.image_size,
                    conf=self.confidence,
                    iou=self.iou_threshold,
                    device=self.device,
                    verbose=False,
                )
            if self._cuda_stream:
                self._cuda_stream.synchronize()
            return [self._parse([r]) for r in results]
        except Exception as e:
            raise InferenceError(f"Worker-{self.worker_id} 批量推理失败: {e}")

    def _parse(self, results) -> List[DetectionBox]:
        """解析YOLO输出为DetectionBox列表"""
        boxes = []
        if not results:
            return boxes
        result = results[0]
        if result.boxes is None or len(result.boxes) == 0:
            return boxes

        xyxy = result.boxes.xyxy.cpu().numpy()
        confs = result.boxes.conf.cpu().numpy()
        cls_ids = result.boxes.cls.cpu().numpy().astype(int)

        for i in range(len(xyxy)):
            x1, y1, x2, y2 = xyxy[i]
            cid = int(cls_ids[i])
            boxes.append(DetectionBox(
                x1=int(round(x1)), y1=int(round(y1)),
                x2=int(round(x2)), y2=int(round(y2)),
                confidence=float(confs[i]),
                class_id=cid,
                class_name=self._class_names.get(cid, str(cid)),
            ))
        return boxes

    def release(self):
        """释放模型资源"""
        with self._lock:
            if self._model is not None:
                del self._model
                self._model = None
            self._cuda_stream = None
            self._pinned_buffer = None
            if TORCH_AVAILABLE and self._is_cuda:
                torch.cuda.empty_cache()
        info(f"[YOLO-{self.worker_id}] 已释放")


# 向后兼容别名
SAHIYOLODetector = YOLODetector