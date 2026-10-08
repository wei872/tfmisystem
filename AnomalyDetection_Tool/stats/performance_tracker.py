#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
性能统计追踪器
从 YOLODetectionManager 中抽离的性能计时和报告逻辑
"""

import time
import threading
from typing import Optional, Dict, Callable

from General_Tool.EnhancedLogger import info

DEFAULT_REPORT_INTERVAL = 20


class PerformanceTracker:
    """性能统计追踪器"""

    def __init__(self, report_interval: int = DEFAULT_REPORT_INTERVAL,
                 num_workers: int = 1,
                 analysis_mode: str = "none",
                 extra_stats_fn: Optional[Callable] = None):
        self._report_interval = report_interval
        self._num_workers = num_workers
        self._analysis_mode = analysis_mode
        self._extra_stats_fn = extra_stats_fn

        self._lock = threading.Lock()
        self._timing_count = 0
        self._timing_parse_fail = 0

        # 批次统计
        self._batch_start_wall: Optional[float] = None
        self._batch_end_wall: float = 0.0
        self._batch_count: int = 0
        self._batch_yolo: float = 0.0
        self._batch_analysis: float = 0.0
        self._batch_postprocess: float = 0.0
        self._batch_capture_delay: float = 0.0
        self._batch_queue_wait: float = 0.0

        # 全局统计
        self._global_wall_start: Optional[float] = None
        self._global_wall_end: float = 0.0
        self._global_yolo: float = 0.0
        self._global_analysis: float = 0.0
        self._global_postprocess: float = 0.0
        self._global_capture_delay: float = 0.0
        self._global_queue_wait: float = 0.0

    def record(self, capture_delay: float,
               queue_wait: float,
               yolo_time: float,
               analysis_time: float,
               postprocess_time: float):
        """记录一帧的时间"""
        now = time.time()
        with self._lock:
            self._timing_count += 1

            # 全局统计（纯处理时间）
            if self._global_wall_start is None:
                self._global_wall_start = (
                    now - yolo_time - analysis_time - postprocess_time)
            self._global_wall_end = now
            self._global_yolo += yolo_time
            self._global_analysis += analysis_time
            self._global_postprocess += postprocess_time
            self._global_capture_delay += capture_delay
            self._global_queue_wait += queue_wait

            # 批次统计（纯处理时间）
            if self._batch_start_wall is None:
                self._batch_start_wall = (
                    now - yolo_time - analysis_time - postprocess_time)
            self._batch_end_wall = now
            self._batch_count += 1
            self._batch_yolo += yolo_time
            self._batch_analysis += analysis_time
            self._batch_postprocess += postprocess_time
            self._batch_capture_delay += capture_delay
            self._batch_queue_wait += queue_wait

            if self._batch_count >= self._report_interval:
                self._print_report()

    def record_parse_fail(self):
        with self._lock:
            self._timing_parse_fail += 1

    def _print_report(self):
        """打印批次性能报告"""
        n = self._batch_count
        wall_elapsed = self._batch_end_wall - self._batch_start_wall
        throughput = n / wall_elapsed if wall_elapsed > 0 else 0

        work_total = (self._batch_yolo
                      + self._batch_analysis
                      + self._batch_postprocess)
        max_cap = wall_elapsed * self._num_workers
        util = (work_total / max_cap * 100) if max_cap > 0 else 0

        g_count = self._timing_count
        g_wall = (self._global_wall_end - self._global_wall_start
                  if self._global_wall_start else 0)
        g_throughput = g_count / g_wall if g_wall > 0 else 0

        # 收集额外统计
        extra = ""
        if self._extra_stats_fn:
            try:
                extra = self._extra_stats_fn()
            except Exception:
                pass

        report = (
            "\n" + "=" * 65
            + f"\n[性能报告] 第 {g_count - n + 1}~{g_count} 帧 "
              f"（本批 {n} 帧）"
            + f"\n  ┌─ 墙钟总用时:       {wall_elapsed:.3f}s"
            + f"\n  ├─ 吞吐量:           {throughput:.2f} 帧/秒"
            + "\n  ├──────────────────────────"
            + f"\n  ├─ YOLO推理:         {self._batch_yolo:.3f}s"
              f"  (均 {self._batch_yolo / n:.3f}s/帧)"
            + f"\n  ├─ 分析推理({self._analysis_mode}):"
              f"     {self._batch_analysis:.3f}s"
              f"  (均 {self._batch_analysis / n:.3f}s/帧)"
            + f"\n  ├─ 后处理:           {self._batch_postprocess:.3f}s"
              f"  (均 {self._batch_postprocess / n:.3f}s/帧)"
            + f"\n  ├─ 处理工时合计:     {work_total:.3f}s"
            + "\n  ├──────────────────────────"
            + f"\n  ├─ 排队等待:         {self._batch_queue_wait:.3f}s"
              f"  (均 {self._batch_queue_wait / n:.3f}s/帧)"
            + f"\n  ├─ 采集延迟:         {self._batch_capture_delay:.3f}s"
              f"  (均 {self._batch_capture_delay / n:.3f}s/帧)"
            + extra
        )

        # 仅在多worker时显示利用率
        if self._num_workers > 1:
            report += (
                f"\n  ├─ Worker数量:       {self._num_workers}"
                f"\n  ├─ Worker利用率:     {util:.1f}%"
                f"  ({work_total:.1f}s / {max_cap:.1f}s)"
            )

        report += (
            f"\n  └─ 全局: {g_count}帧  墙钟={g_wall:.1f}s"
            f"  吞吐={g_throughput:.2f}帧/秒"
            f"  解析失败={self._timing_parse_fail}"
            + "\n" + "=" * 65
        )
        info(report)

        # 重置批次
        self._batch_start_wall = None
        self._batch_end_wall = 0.0
        self._batch_count = 0
        self._batch_yolo = 0.0
        self._batch_analysis = 0.0
        self._batch_postprocess = 0.0
        self._batch_capture_delay = 0.0
        self._batch_queue_wait = 0.0

    def get_stats(self) -> Dict:
        with self._lock:
            n = self._timing_count
            g_wall = (self._global_wall_end - self._global_wall_start
                      if self._global_wall_start else 0)
            return {
                "count": n,
                "parse_fail": self._timing_parse_fail,
                "wall_elapsed": g_wall,
                "throughput": n / g_wall if g_wall > 0 else 0,
                "yolo_total": self._global_yolo,
                "analysis_total": self._global_analysis,
                "postprocess_total": self._global_postprocess,
                "capture_delay_total": self._global_capture_delay,
                "queue_wait_total": self._global_queue_wait,
                "avg_yolo": self._global_yolo / n if n else 0,
                "avg_analysis": self._global_analysis / n if n else 0,
                "avg_postprocess": (
                    self._global_postprocess / n if n else 0),
                "avg_capture_delay": (
                    self._global_capture_delay / n if n else 0),
                "avg_queue_wait": (
                    self._global_queue_wait / n if n else 0),
            }