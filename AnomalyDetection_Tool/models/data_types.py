#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一数据结构定义
消除 DetectionBox / OriginalBoxInfo 的重复
"""

import time
from dataclasses import dataclass, field
from typing import Callable, List, Dict, Optional, Any, Tuple

import numpy as np


# ================================================================
# 检测框
# ================================================================

@dataclass
class DetectionBox:
    """统一检测框（原图坐标）"""
    x1: int
    y1: int
    x2: int
    y2: int
    confidence: float
    class_id: int
    class_name: str
    threshold_used: float = 0.0

    @property
    def width(self) -> int:
        return self.x2 - self.x1

    @property
    def height(self) -> int:
        return self.y2 - self.y1

    @property
    def center(self) -> Tuple[float, float]:
        return (self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0

    @property
    def area(self) -> int:
        return self.width * self.height

    def clamp(self, img_w: int, img_h: int) -> 'DetectionBox':
        """返回裁剪到图像边界内的新框"""
        return DetectionBox(
            x1=max(0, self.x1), y1=max(0, self.y1),
            x2=min(img_w, self.x2), y2=min(img_h, self.y2),
            confidence=self.confidence,
            class_id=self.class_id,
            class_name=self.class_name,
            threshold_used=self.threshold_used,
        )


# ================================================================
# 检测结果
# ================================================================

@dataclass
class DetectionResult:
    """检测结果"""
    filename: str
    original_width: int
    original_height: int
    has_detection: bool
    total_boxes: int
    max_confidence: float
    boxes: List[DetectionBox]
    capture_timestamp: str = ""
    detection_timestamp: str = ""
    process_time_ms: float = 0.0
    raw_boxes_count: int = 0
    filtered_count: int = 0


# ================================================================
# 检测任务
# ================================================================

@dataclass
class DetectionTask:
    """检测任务"""
    image: np.ndarray
    filename: str
    camera_name: str
    camera_sn: str
    capture_timestamp: str
    metadata: Optional[Dict] = None
    submit_time: float = field(default_factory=time.time)
    perf_submit: float = field(default_factory=time.perf_counter)  # 高精度计时起点
    infer_start: float = 0.0
    infer_end: float = 0.0
    perf_infer_start: float = 0.0  # 高精度推理开始
    perf_infer_end: float = 0.0  # 高精度推理结束


@dataclass
class PostProcessTask:
    """后处理任务"""
    result: DetectionResult
    task: DetectionTask
    worker_id: int
    yolo_finish_time: float = 0.0


# ================================================================
# 面积分析结果
# ================================================================

@dataclass
class AreaAnalysisResult:
    """面积分析结果"""
    triggered: bool = False
    analyzed: bool = False
    real_area_mm2: float = 0.0
    fill_ratio: float = 0.0
    debug_info: Dict[str, Any] = field(default_factory=dict)
    mask: Optional[np.ndarray] = None
    heatmap: Optional[np.ndarray] = None
    rule_used: Optional[Dict] = None
    anomaly_score: float = 0.0
    stop_image_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "triggered": self.triggered,
            "analyzed": self.analyzed,
            "real_area_mm2": self.real_area_mm2,
            "fill_ratio": self.fill_ratio,
            "debug_info": self.debug_info,
            "mask": self.mask,
            "heatmap": self.heatmap,
            "rule_used": self.rule_used,
            "anomaly_score": self.anomaly_score,
            "stop_image_path": self.stop_image_path,
        }


# ================================================================
# 框分析条目（后处理用）
# ================================================================

@dataclass
class BoxAnalysisEntry:
    """单个框的分析结果条目"""
    box: DetectionBox
    index: int
    is_stop: bool = False
    analyzed: bool = False
    area_result: AreaAnalysisResult = field(
        default_factory=AreaAnalysisResult)

    @staticmethod
    def empty(box: DetectionBox, idx: int) -> 'BoxAnalysisEntry':
        return BoxAnalysisEntry(
            box=box, index=idx,
            is_stop=False, analyzed=False,
            area_result=AreaAnalysisResult(),
        )


# ================================================================
# 阈值配置
# ================================================================

@dataclass
class ClassThresholdConfig:
    """
    类别阈值配置。

    thresholds_by_name 通常直接持有 settings.CLASS_CONFIDENCE_THRESHOLDS
    这个热更新字典，因此按类别名查到的阈值天然跟随 config.yaml 变化。

    默认阈值是个标量，无法就地更新，所以额外支持传入
    default_threshold_provider（一个返回当前默认值的函数）来实现热更新。
    """
    thresholds_by_name: Dict[str, float] = field(default_factory=dict)
    thresholds_by_id: Dict[int, float] = field(default_factory=dict)
    default_threshold: float = 0.25
    default_threshold_provider: Optional[Callable[[], float]] = None

    def get_threshold(self, class_name: str, class_id: int) -> float:
        if class_name in self.thresholds_by_name:
            return self.thresholds_by_name[class_name]
        if class_id in self.thresholds_by_id:
            return self.thresholds_by_id[class_id]
        if self.default_threshold_provider is not None:
            try:
                return float(self.default_threshold_provider())
            except Exception:
                pass
        return self.default_threshold

    def check_confidence(self, class_name: str, class_id: int,
                         confidence: float) -> bool:
        return confidence >= self.get_threshold(class_name, class_id)