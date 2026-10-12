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
    类别阈值配置（支持按相机覆盖）。

    thresholds_by_name 通常直接持有 settings.CLASS_CONFIDENCE_THRESHOLDS
    这个热更新字典，因此按类别名查到的阈值天然跟随 config.yaml 变化。

    默认阈值是个标量，无法就地更新，所以额外支持传入
    default_threshold_provider（一个返回当前默认值的函数）来实现热更新。

    按相机覆盖
    ----------
    8 台相机的安装位置/光照不同，同一套阈值不一定都合适。传入
    ``camera_override_provider``（camera_name -> 覆盖块 dict 或 None）后，
    ``get_threshold()`` 多接受一个 camera_name 参数。

    **生效优先级（从具体到宽泛）**：

      1. 该相机 + 类别名
      2. 该相机 + 类别 id
      3. 该相机的 default_class_confidence
      4. 全局 类别名
      5. 全局 类别 id
      6. 全局 default_threshold_provider
      7. 全局 default_threshold

    ⚠️ 注意第 3 条：相机级 default 会**盖掉全局的按类阈值**。也就是说给
    camera3 配了 ``default_class_confidence: 0.15``，那么全局里精心调过的
    ``油点线: 0.85`` 在 camera3 上也会变成 0.15。这是"覆盖块遮蔽全局块"的
    标准语义，但容易配错，所以 settings.describe_camera_thresholds() 会在
    启动时把每台相机的实际生效值打印出来，便于核对。

    想只改某一台的某一个类、其余沿用全局，就只写
    ``class_confidence_thresholds``，**不要写** ``default_class_confidence``。
    """
    thresholds_by_name: Dict[str, float] = field(default_factory=dict)
    thresholds_by_id: Dict[int, float] = field(default_factory=dict)
    default_threshold: float = 0.25
    default_threshold_provider: Optional[Callable[[], float]] = None
    camera_override_provider: Optional[Callable[[str], Optional[Dict]]] = None

    def _camera_block(self, camera_name: Optional[str]) -> Optional[Dict]:
        """取某台相机的覆盖块。任何异常都退化成"没有覆盖"，不能影响检测。"""
        if not camera_name or self.camera_override_provider is None:
            return None
        try:
            block = self.camera_override_provider(camera_name)
        except Exception:
            return None
        return block if isinstance(block, dict) else None

    def get_threshold(self, class_name: str, class_id: int,
                      camera_name: Optional[str] = None) -> float:
        # ---- 1~3：该相机的覆盖 ----
        block = self._camera_block(camera_name)
        if block:
            per_class = block.get("class_confidence_thresholds")
            if isinstance(per_class, dict) and class_name in per_class:
                try:
                    return float(per_class[class_name])
                except (TypeError, ValueError):
                    pass            # 配错就当没配，继续往下走
            per_id = block.get("class_confidence_thresholds_by_id")
            if isinstance(per_id, dict) and class_id in per_id:
                try:
                    return float(per_id[class_id])
                except (TypeError, ValueError):
                    pass
            cam_default = block.get("default_class_confidence")
            if cam_default is not None:
                try:
                    return float(cam_default)
                except (TypeError, ValueError):
                    pass

        # ---- 4~7：全局（原有逻辑，未配置覆盖时行为完全不变）----
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
                         confidence: float,
                         camera_name: Optional[str] = None) -> bool:
        return confidence >= self.get_threshold(class_name, class_id, camera_name)