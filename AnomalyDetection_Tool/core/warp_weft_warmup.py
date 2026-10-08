#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: warp_weft_warmup.py
路径: AnomalyDetection_Tool/core/warp_weft_warmup.py
职责: WeftDetector预热单例管理。

预热目的:
  - 消除首次推理时GPU/模型初始化的额外延迟（通常100~500ms）
  - 预热在后台线程中异步执行，不阻塞主链路启动

优化点（v2.2）:
  - 预热也使用低优先级Stream，避免与YOLO推理的初始化竞争
  - 预热失败不影响主功能（捕获所有异常）
"""

import threading
from typing import Optional

import numpy as np

from General_Tool.EnhancedLogger import info, error
from AnomalyDetection_Tool.config.settings import WEFT_OUTPUT_DIR
from AnomalyDetection_Tool.analysis.weft_skew_analyzer import WeftDetector
from AnomalyDetection_Tool.core.cuda_stream_pool import get_analysis_stream_pool

_warmup_weft_detector: Optional[WeftDetector] = None
_warmup_lock = threading.Lock()


def get_warmup_weft_detector() -> WeftDetector:
    """
    懒加载预热专用单例（线程安全）。
    仅用于模型预热，不参与正式推理。
    """
    global _warmup_weft_detector
    if _warmup_weft_detector is None:
        with _warmup_lock:
            if _warmup_weft_detector is None:
                _warmup_weft_detector = WeftDetector(
                    output_dir=WEFT_OUTPUT_DIR,
                    save_annotated=False,
                    angle_filter_thresh=1.0,
                    edge_margin_ratio=0.02,
                    max_spacing_threshold=260,
                    leak_detect_ratio=0.6,
                    edge_leak_threshold=1.5,
                    use_gpu=True,
                )
                info("[WeftDetector] 预热单例已创建")
    return _warmup_weft_detector


def run_warmup(worker_id: int = 0) -> None:
    """
    在后台线程中执行预热，不阻塞主链路。
    ★ 预热时使用低优先级Stream，避免与YOLO模型加载竞争GPU资源。
    """
    try:
        dummy = np.random.randint(0, 255, (1080, 1920, 3), dtype=np.uint8)
        detector = get_warmup_weft_detector()

        # 预热也使用低优先级Stream，与YOLO的高优先级Stream隔离
        stream_pool = get_analysis_stream_pool()
        with stream_pool.acquire(timeout=5.0) as stream_ctx:
            with stream_ctx:
                detector.detect(dummy, image_name="__warmup__")

        info(f"[WeftDetector] 预热完成 (worker_id={worker_id})")
    except Exception as e:
        # 预热失败不影响主功能，仅记录日志
        error(f"[WeftDetector] 预热失败（不影响正式推理）: {e}")