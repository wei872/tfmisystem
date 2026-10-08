#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
竖线误检区域过滤器：时间窗口内累计>=N个框在同一X坐标，永久标记为误检区域
所有多行日志合并为单次 info() 输出，防止多线程交叉
"""

import time
import threading
from typing import Dict, List, Set
from collections import defaultdict
from General_Tool.EnhancedLogger import info


class VerticalLineFilter:
    def __init__(self, target_cameras: Set[str],
                 target_classes: Set[str],
                 max_x_deviation: float = 50.0,
                 min_accumulate_count: int = 3,
                 time_window: float = 60.0):
        self.target_cameras = target_cameras
        self.target_classes = target_classes
        self.max_x_deviation = max_x_deviation
        self.min_count = min_accumulate_count
        self.time_window = time_window

        self._lock = threading.Lock()
        self._x_records: Dict[str, List[tuple]] = defaultdict(list)
        self._false_lines: Dict[str, List[float]] = defaultdict(list)
        self._stats = {
            "total_boxes_seen": 0,
            "filtered_boxes": 0,
            "false_lines_detected": 0,
        }

        info(f"[VerticalLineFilter] 初始化完成  "
             f"相机={target_cameras}  类别={target_classes}  "
             f"X阈值={max_x_deviation}px  "
             f"累计>={min_accumulate_count}  窗口={time_window}s")

    def _clean_expired_records(self, camera_name: str, now: float):
        cutoff = now - self.time_window
        self._x_records[camera_name] = [
            (x, t) for x, t in self._x_records[camera_name] if t > cutoff
        ]

    def check_and_filter(self, camera_name: str, boxes: list) -> List[int]:
        if camera_name not in self.target_cameras:
            return []

        target_boxes = []
        for idx, box in enumerate(boxes):
            if box.class_name in self.target_classes:
                cx, _ = box.center
                target_boxes.append((idx, box.class_name, cx))

        if not target_boxes:
            return []

        now = time.time()
        log_parts = []  # 收集日志片段

        with self._lock:
            self._clean_expired_records(camera_name, now)
            self._stats["total_boxes_seen"] += len(target_boxes)

            log_parts.append(
                f"[竖线] {camera_name} 框:{len(target_boxes)} "
                f"临时:{len(self._x_records[camera_name])} "
                f"永久:{len(self._false_lines[camera_name])}")

            filtered_indices = []

            # 检查永久误检区域
            for idx, cls, cx in target_boxes:
                if self._is_in_false_line(camera_name, cx):
                    filtered_indices.append(idx)
                    self._stats["filtered_boxes"] += 1
                    log_parts.append(
                        f"  框{idx} {cls} x={cx:.0f} → 永久误检，过滤")

            # 未过滤的加入临时记录
            for idx, cls, cx in target_boxes:
                if idx not in filtered_indices:
                    self._x_records[camera_name].append((cx, now))
                    log_parts.append(
                        f"  框{idx} {cls} x={cx:.0f} → 临时记录")

            # 检测新永久区域
            new_lines = self._detect_new_false_lines(camera_name)
            for nl in new_lines:
                log_parts.append(nl)

        # 合并为单条日志输出
        if log_parts:
            info("\n".join(log_parts))

        return filtered_indices

    def _is_in_false_line(self, camera_name: str, x: float) -> bool:
        for line_x in self._false_lines[camera_name]:
            if abs(x - line_x) <= self.max_x_deviation:
                return True
        return False

    def _detect_new_false_lines(self, camera_name: str) -> List[str]:
        """返回新增永久区域的日志行列表"""
        records = self._x_records[camera_name]
        if len(records) < self.min_count:
            return []

        log_lines = []
        x_values = [x for x, _ in records]
        used = set()

        for i, x in enumerate(x_values):
            if i in used:
                continue
            cluster = [i]
            for j, x2 in enumerate(x_values):
                if j in used or j == i:
                    continue
                if abs(x - x2) <= self.max_x_deviation:
                    cluster.append(j)

            if len(cluster) >= self.min_count:
                cluster_x = [x_values[k] for k in cluster]
                center_x = sum(cluster_x) / len(cluster_x)

                if not self._is_in_false_line(camera_name, center_x):
                    self._false_lines[camera_name].append(center_x)
                    self._stats["false_lines_detected"] += 1
                    log_lines.append(
                        f"  [竖线] 新增永久误检区域: {camera_name} "
                        f"X={center_x:.0f} ({len(cluster)}个框)")

                    for k in sorted(cluster, reverse=True):
                        if k < len(self._x_records[camera_name]):
                            self._x_records[camera_name].pop(k)

                for k in cluster:
                    used.add(k)

        return log_lines

    def get_false_lines(self, camera_name: str) -> List[float]:
        with self._lock:
            return self._false_lines[camera_name].copy()

    def get_stats(self) -> Dict:
        with self._lock:
            return {
                **self._stats,
                "false_lines_by_camera": {
                    cam: [f"{x:.0f}" for x in lines]
                    for cam, lines in self._false_lines.items()
                },
            }

    def reset(self):
        with self._lock:
            self._x_records.clear()
            self._false_lines.clear()
            self._stats = {
                "total_boxes_seen": 0,
                "filtered_boxes": 0,
                "false_lines_detected": 0,
            }
            info("[竖线] 已重置")