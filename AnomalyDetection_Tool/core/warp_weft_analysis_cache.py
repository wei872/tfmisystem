# -*- coding: utf-8 -*-
"""
经纬线异步缓存容器
线程安全，主链路零等待读取

v2.1 改进：
  - 添加时间戳支持（用于数据时效性检查）
  - read() 方法返回 (result, timestamp)
  - 添加 write() 方法用于手动初始化
"""

import threading
import time
from typing import Optional, Tuple

from AnomalyDetection_Tool.analysis.warp_density_detector import WarpDensityResult


class WarpCache:
    """线程安全的经线结果缓存"""

    __slots__ = ("result", "timestamp", "lock", "in_flight", "total", "hit")

    def __init__(self):
        self.result: Optional[WarpDensityResult] = None
        self.timestamp: float = 0.0
        self.lock: threading.Lock = threading.Lock()
        self.in_flight: bool = False
        self.total: int = 0
        self.hit: int = 0

    def update(self, result: WarpDensityResult) -> None:
        """后台线程更新数据"""
        with self.lock:
            self.result = result
            self.timestamp = time.time()
            self.in_flight = False
            self.total += 1

    def read(self) -> Tuple[Optional[WarpDensityResult], float]:
        """主链路无锁读取，返回 (result, timestamp)"""
        self.hit += 1
        return self.result, self.timestamp

    def try_mark_inflight(self) -> bool:
        with self.lock:
            if self.in_flight:
                return False
            self.in_flight = True
            return True

    def reset_inflight(self) -> None:
        with self.lock:
            self.in_flight = False


class WeftCache:
    """线程安全的纬线结果缓存"""

    __slots__ = (
        "ok",
        "angle",
        "max_spacing",
        "timestamp",
        "lock",
        "in_flight",
        "total",
        "hit",
    )

    def __init__(self):
        self.ok: bool = False
        self.angle: float = 0.0
        self.max_spacing: int = 0
        self.timestamp: float = 0.0
        self.lock: threading.Lock = threading.Lock()
        self.in_flight: bool = False
        self.total: int = 0
        self.hit: int = 0

    def update(self, ok: bool, angle: float, max_spacing: int) -> None:
        """后台线程更新数据"""
        with self.lock:
            self.ok = ok
            self.angle = angle
            self.max_spacing = max_spacing
            self.timestamp = time.time()
            self.in_flight = False
            self.total += 1

    def write(self, ok: bool, angle: float, max_spacing: int) -> None:
        """
        手动写入数据（用于初始化）。
        与 update() 的区别：
          - write() 不重置 in_flight 标记（避免干扰正在执行的任务）
          - write() 不增加 total 计数（非正常检测结果）
        """
        with self.lock:
            self.ok = ok
            self.angle = angle
            self.max_spacing = max_spacing
            self.timestamp = time.time()

    def read(self) -> Tuple[bool, float, int, float]:
        """主链路无锁读取，返回 (ok, angle, max_spacing, timestamp)"""
        self.hit += 1
        return self.ok, self.angle, self.max_spacing, self.timestamp

    def try_mark_inflight(self) -> bool:
        with self.lock:
            if self.in_flight:
                return False
            self.in_flight = True
            return True

    def reset_inflight(self) -> None:
        with self.lock:
            self.in_flight = False