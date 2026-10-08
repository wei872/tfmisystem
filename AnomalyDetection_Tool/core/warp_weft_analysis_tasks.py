#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: warp_weft_analysis_tasks.py
路径: AnomalyDetection_Tool/core/warp_weft_analysis_tasks.py
职责: 后台分析任务集合——经线密度检测 + 纬线检测（含纬斜、断纬）。

优化点（v2.2）:
  - 经纬线分析的GPU操作通过低优先级CUDA Stream执行
  - 与YOLO推理的高优先级Stream形成优先级隔离
  - GPU繁忙时，经纬线分析自动让步，不影响YOLO推理延迟
  - 所有停机事件聚合后一次性处理，减少重复上传

GPU优先级说明:
  YOLO推理    priority=-1（高）→ GPU优先调度
  经纬线分析  priority=0 （低）→ YOLO空闲时执行，不与推理争抢
"""

import pathlib
import threading
import traceback
import time
from typing import Optional, List, Dict, TYPE_CHECKING

import numpy as np

from General_Tool.EnhancedLogger import info, error
from AnomalyDetection_Tool.config.settings import (
    WEFT_OUTPUT_DIR, SAVE_WEFT,
    # 🔥 经纬停机阈值改用访问器读取，支持 config.yaml 热更新（无需重启）
    get_warp_trigger, get_warp_stop_trigger,
    get_weft_trigger, get_weft_stop_trigger,
    get_lat_slope_threshold,
)
from AnomalyDetection_Tool.core.cuda_stream_pool import get_analysis_stream_pool

# 避免循环导入：仅用于类型提示
if TYPE_CHECKING:
    from AnomalyDetection_Tool.core.warp_weft_analysis_cache import WarpCache, WeftCache
    from AnomalyDetection_Tool.services.warp_weft_stop_handler import StopHandler
    from AnomalyDetection_Tool.analysis.warp_density_detector import WarpDensityDetector
    from AnomalyDetection_Tool.analysis.weft_skew_analyzer import WeftDetector

# 需要过滤飘絮的机台号（纬线检测不参与）
MACHINE_FILTER_PIAOXU = {"DA9670313", "DA9670350"}


class AnalysisTasks:
    """
    后台分析任务集合（由DetectionPipeline实例持有）。

    职责分离:
      - 经线密度分析 → bg_detect_warp()
      - 纬线检测     → bg_detect_weft()（含纬斜、断纬判断）
    所有停机动作委托给StopHandler。

    GPU隔离策略:
      - 经纬线分析的GPU调用包裹在低优先级Stream上下文中
      - 低优先级Stream在YOLO高优先级Stream执行时会被GPU调度器延迟
      - 实现"YOLO优先，分析让步"的GPU时间片分配
    """

    def __init__(
        self,
        warp_cache: "WarpCache",
        weft_cache: "WeftCache",
        stop_handler: "StopHandler",
        warp_detector: Optional["WarpDensityDetector"],
        weft_detector: "WeftDetector",
        stats: dict,
        stats_lock: threading.Lock,
        calculate_cut_ratio_fn,
    ):
        self._warp_cache = warp_cache
        self._weft_cache = weft_cache
        self._stop_handler = stop_handler
        self._warp_detector = warp_detector
        self._weft_detector = weft_detector
        self._stats = stats
        self._stats_lock = stats_lock
        self._calc_cut_ratio = calculate_cut_ratio_fn

        # 获取全局低优先级Stream池（与YOLO高优先级Stream形成优先级隔离）
        self._stream_pool = get_analysis_stream_pool()

    # ──────────────────────────────────────────────────────────────────
    #  经线密度后台任务
    # ──────────────────────────────────────────────────────────────────

    def bg_detect_warp(
        self,
        image: np.ndarray,
        camera_name: str,
        camera_sn: str = "",
    ) -> None:
        """
        在线程池中执行经线密度检测，结果写入WarpCache。

        GPU调用（如果WarpDensityDetector使用GPU）通过低优先级Stream执行，
        确保不与YOLO推理争抢GPU资源。
        """
        try:
            _, w = image.shape[:2]
            cut_ratio, cut_side = self._calc_cut_ratio(camera_name, w)

            # ★ 低优先级Stream包裹经线分析的GPU操作
            # 若WarpDensityDetector内部有GPU调用，会在低优先级Stream中执行
            # YOLO推理（高优先级）运行时，此处的GPU kernel会被延迟到YOLO空闲后执行
            with self._stream_pool.acquire(timeout=1.0) as stream_ctx:
                with stream_ctx:
                    result = self._warp_detector.detect(
                        image=image,
                        cut_ratio=cut_ratio,
                        cut_side=cut_side,
                    )

            self._warp_cache.update(result)

            # 更新滑动平均密度统计
            if result.confidence > 0.5:
                with self._stats_lock:
                    n = self._stats["warp_density_count"]
                    avg = self._stats["warp_density_avg"]
                    self._stats["warp_density_avg"] = (
                        (avg * n + result.density_per_10cm) / (n + 1)
                    )
                    self._stats["warp_density_count"] += 1

            info(
                f"[经线密度/后台] {camera_name} | "
                f"{result.density_per_10cm:.1f}根/10cm | "
                f"置信度{result.confidence:.2f} | "
                f"切除{result.cut_side}侧{result.cut_ratio:.1%}"
            )

            # 经线均匀度不足 → 触发停机（阈值支持热更新）
            warp_stop_trigger = get_warp_stop_trigger()
            if get_warp_trigger() and result.uniformity < warp_stop_trigger:
                info(
                    f"[经线密度] {camera_name} 经线不均匀触发停机 "
                    f"uniformity={result.uniformity:.3f} < {warp_stop_trigger}"
                )
                with self._stats_lock:
                    self._stats["stop_warp_count"] += 1

                self._stop_handler.trigger(
                    image=image,
                    camera_name=camera_name,
                    filename="warp_stop",
                    stop_type="warp",
                    stop_reason=f"uniformity_{result.uniformity:.3f}",
                    camera_sn=camera_sn,
                    weft_angle=None,
                    weft_max_spacing=None,
                    weft_broken_intensity=None,
                    warp_uniformity=round(result.uniformity, 4),
                )

        except Exception as e:
            # 异常时重置inflight标记，允许下一帧重新提交任务
            self._warp_cache.reset_inflight()
            error(f"[经线密度/后台] {camera_name} 失败: {e}")
            traceback.print_exc()

    # ──────────────────────────────────────────────────────────────────
    #  纬线检测后台任务
    # ──────────────────────────────────────────────────────────────────

    def bg_detect_weft(
        self,
        image: np.ndarray,
        filename: str,
        camera_name: str = "",
        camera_sn: str = "",
    ) -> None:
        """
        纬线检测后台任务（含纬斜、断纬判断）。

        GPU调用通过低优先级Stream执行，与YOLO推理形成优先级隔离。
        所有停机事件收集后聚合为一次上传，避免重复触发。
        """
        try:
            machine_id = filename.split("_")[0] if "_" in filename else filename
            # 过滤飘絮机台：这些机台的纬线检测结果不可信
            if machine_id in MACHINE_FILTER_PIAOXU:
                self._weft_cache.reset_inflight()
                return

            self._weft_detector.save_annotated = SAVE_WEFT
            self._weft_detector.output_dir = pathlib.Path(WEFT_OUTPUT_DIR)

            # ★ 低优先级Stream包裹纬线检测的GPU操作
            # WeftDetector内部的GPU操作（use_gpu=True）在低优先级Stream中执行
            # 相比YOLO高优先级Stream，GPU调度器会优先保障YOLO的kernel完成
            with self._stream_pool.acquire(timeout=1.0) as stream_ctx:
                with stream_ctx:
                    ok, angle, max_spacing = self._weft_detector.detect(
                        image, image_name=filename
                    )

            self._weft_cache.update(ok, angle, max_spacing)

            if ok:
                info(
                    f"[纬线检测/后台] [{filename}] "
                    f"angle={angle:.2f}° max_sp={max_spacing}px"
                )

            # 未检测到异常，或停机触发未开启，直接返回
            if not (ok and get_weft_trigger()):
                return

            # ── 收集本帧所有停机事件（聚合模式，一次上传）────────────
            stop_events: List[Dict] = []
            self._collect_spacing_event(angle, max_spacing, stop_events)
            self._collect_lat_slope_event(angle, max_spacing, stop_events)
            self._collect_broken_weft_event(angle, max_spacing, stop_events)

            if not stop_events:
                return

            # 更新各类停机计数
            with self._stats_lock:
                _key_map = {
                    "weft": "stop_weft_count",
                    "lat_slope": "stop_lat_slope_count",
                    "broken_weft": "stop_broken_weft_count",
                }
                for evt in stop_events:
                    key = _key_map.get(evt["stop_type"])
                    if key:
                        self._stats[key] += 1

            # 聚合后一次性触发停机（避免同一帧多次上传）
            self._stop_handler.trigger_aggregated(
                image=image,
                camera_name=camera_name,
                filename=filename,
                camera_sn=camera_sn,
                stop_events=stop_events,
            )

        except Exception as e:
            self._weft_cache.reset_inflight()
            error(f"[纬线检测/后台] [{filename}] 失败: {e}")

    # ──────────────────────────────────────────────────────────────────
    #  停机事件收集（不直接触发，交给聚合器）
    # ──────────────────────────────────────────────────────────────────

    def _collect_spacing_event(
        self, angle: float, max_spacing: int, stop_events: List[Dict]
    ) -> None:
        """纬线间距过大 → 缺纬停机事件"""
        if max_spacing <= get_weft_stop_trigger():
            return
        stop_events.append({
            "stop_type": "weft",
            "stop_reason": f"spacing_{max_spacing}",
            "weft_angle": angle,
            "weft_max_spacing": max_spacing,
            "weft_broken_intensity": 0.0,
            "warp_uniformity": self._get_latest_warp_uniformity(),
        })

    def _collect_lat_slope_event(
        self, angle: float, max_spacing: int, stop_events: List[Dict]
    ) -> None:
        """纬斜角度超阈值 → 纬斜停机事件"""
        if abs(angle) <= get_lat_slope_threshold():
            return
        stop_events.append({
            "stop_type": "lat_slope",
            "stop_reason": f"angle_{angle:.2f}deg",
            "weft_angle": angle,
            "weft_max_spacing": max_spacing,
            "weft_broken_intensity": 0.0,
            "warp_uniformity": self._get_latest_warp_uniformity(),
        })

    def _collect_broken_weft_event(
        self, angle: float, max_spacing: int, stop_events: List[Dict]
    ) -> None:
        """检测到有效断纬线 → 断纬停机事件"""
        broken_lines = self._weft_detector.broken_lines
        if not broken_lines:
            return
        # 过滤强度为0、0.5、1的噪声值，只保留有效强度
        valid_strengths = [
            bl["strength"] for bl in broken_lines
            if bl["strength"] not in (0, 1, 0.5)
        ]
        if not valid_strengths:
            return
        avg_str = float(np.mean(valid_strengths))
        stop_events.append({
            "stop_type": "broken_weft",
            "stop_reason": f"broken_{len(valid_strengths)}lines",
            "weft_angle": angle,
            "weft_max_spacing": max_spacing,
            "weft_broken_intensity": avg_str,
            "warp_uniformity": self._get_latest_warp_uniformity(),
        })

    # ──────────────────────────────────────────────────────────────────
    #  辅助方法
    # ──────────────────────────────────────────────────────────────────

    def _get_latest_warp_uniformity(self) -> Optional[float]:
        """
        从经线缓存读取最新均匀度（主链路无锁读取，读旧值可接受）。
        超过30秒的旧数据视为失效，返回None。
        """
        result, timestamp = self._warp_cache.read()
        if result is None:
            return None
        # 置信度过低，数据不可靠
        if result.confidence < 0.5:
            return None
        # 数据超过30秒，时效性不足
        if time.time() - timestamp > 30.0:
            return None
        return result.uniformity