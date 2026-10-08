#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
文件名: core/memory_guard.py
模块概述: 内存监控与熔断保护（基线感知版）

修复: 记录启动时的内存基线，基于增量判断是否应该丢帧，
      避免系统基线本身就高导致所有帧被丢弃。
===============================================================================
"""

import threading
import time
from General_Tool.EnhancedLogger import info, error

try:
    import psutil
    _PSUTIL_AVAILABLE = True
except ImportError:
    _PSUTIL_AVAILABLE = False

try:
    import torch
    _TORCH_AVAILABLE = True
except ImportError:
    _TORCH_AVAILABLE = False


class MemoryGuard:
    """
    内存安全守卫（基线感知版）

    策略:
    1. 启动时记录内存基线 (baseline)
    2. 绝对阈值: 系统内存超过 critical_watermark 才强制丢帧
    3. 增量阈值: 相比基线增长超过 max_growth_percent 才丢帧
    4. 两个条件都满足时才丢帧（避免误杀）

    示例:
      基线=81%, critical=95%, max_growth=10%
      - 内存82% → 增长1% < 10% → 不丢帧 ✓
      - 内存88% → 增长7% < 10% → 不丢帧 ✓
      - 内存92% → 增长11% > 10% 且 > 90%(high) → 丢帧
      - 内存96% → > 95%(critical) → 强制丢帧 + GC
    """

    def __init__(self,
                 high_watermark: float = 90.0,
                 critical_watermark: float = 95.0,
                 gpu_high_watermark: float = 90.0,
                 max_growth_percent: float = 10.0,
                 log_interval: float = 30.0):
        """
        Args:
            high_watermark:     系统内存警戒线 (%)
            critical_watermark: 系统内存临界线 (%), 超过强制丢帧+GC
            gpu_high_watermark: GPU 显存警戒线 (%)
            max_growth_percent: 相比基线的最大允许增长 (%)
            log_interval:       日志打印间隔 (秒)
        """
        self.high_watermark = high_watermark
        self.critical_watermark = critical_watermark
        self.gpu_high_watermark = gpu_high_watermark
        self.max_growth_percent = max_growth_percent
        self.log_interval = log_interval

        self._lock = threading.Lock()
        self._last_log_time: float = 0
        self._drop_count: int = 0
        self._gc_count: int = 0
        self._check_count: int = 0
        self._last_mem_percent: float = 0
        self._last_gpu_percent: float = 0

        #  启动时记录基线
        self._baseline_mem: float = self.get_system_memory_percent()
        self._baseline_gpu: float = self.get_gpu_memory_percent()
        self._baseline_time: float = time.time()

        if not _PSUTIL_AVAILABLE:
            info("[MemoryGuard] psutil 不可用，内存监控降级")

        info(f"[MemoryGuard] 初始化完成"
             f"\n  内存基线: {self._baseline_mem:.1f}%"
             f"\n  GPU基线:  {self._baseline_gpu:.1f}%"
             f"\n  警戒线:   {high_watermark}%  "
             f"临界线: {critical_watermark}%"
             f"\n  最大增长: {max_growth_percent}%  "
             f"(基线+{max_growth_percent}% = "
             f"{min(self._baseline_mem + max_growth_percent, 100):.1f}%)")

    def get_system_memory_percent(self) -> float:
        if not _PSUTIL_AVAILABLE:
            return 0.0
        try:
            return psutil.virtual_memory().percent
        except Exception:
            return 0.0

    def get_gpu_memory_percent(self, device_idx: int = 0) -> float:
        if not _TORCH_AVAILABLE or not torch.cuda.is_available():
            return 0.0
        try:
            allocated = torch.cuda.memory_allocated(device_idx)
            total = torch.cuda.get_device_properties(
                device_idx).total_memory
            if total == 0:
                return 0.0
            return (allocated / total) * 100.0
        except Exception:
            return 0.0

    def should_drop_frame(self) -> bool:
        """
        判断是否应该丢帧

        规则（按优先级）:
        1. 临界线: mem > critical → 强制丢帧 + GC
        2. 增量 + 警戒: mem > high 且 增长 > max_growth → 丢帧
        3. GPU 临界: gpu > gpu_high → 丢帧
        4. 其他: 不丢帧
        """
        mem_pct = self.get_system_memory_percent()
        gpu_pct = self.get_gpu_memory_percent()

        with self._lock:
            self._check_count += 1
            self._last_mem_percent = mem_pct
            self._last_gpu_percent = gpu_pct

        # psutil 不可用时不丢帧（靠信号量保护）
        if not _PSUTIL_AVAILABLE:
            return False

        #  规则1: 绝对临界线 — 无论什么情况都必须丢帧
        if mem_pct > self.critical_watermark:
            self._emergency_gc()
            with self._lock:
                self._drop_count += 1
            self._maybe_log(mem_pct, gpu_pct, "CRITICAL")
            return True

        #  规则2: 警戒线 + 增量双重判断
        growth = mem_pct - self._baseline_mem
        if (mem_pct > self.high_watermark
                and growth > self.max_growth_percent):
            with self._lock:
                self._drop_count += 1
            self._maybe_log(
                mem_pct, gpu_pct,
                f"HIGH (基线{self._baseline_mem:.1f}% "
                f"+ 增长{growth:.1f}%)")
            return True

        #  规则3: GPU 显存临界
        if gpu_pct > self.gpu_high_watermark:
            with self._lock:
                self._drop_count += 1
            self._maybe_log(mem_pct, gpu_pct, "GPU_HIGH")
            return True

        return False

    def is_safe(self) -> bool:
        return not self.should_drop_frame()

    def _emergency_gc(self):
        import gc
        gc.collect()

        if _TORCH_AVAILABLE and torch.cuda.is_available():
            try:
                torch.cuda.empty_cache()
            except Exception:
                pass

        with self._lock:
            self._gc_count += 1

    def _maybe_log(self, mem_pct: float, gpu_pct: float,
                   level: str):
        now = time.time()
        with self._lock:
            if now - self._last_log_time < self.log_interval:
                return
            self._last_log_time = now
            drop_cnt = self._drop_count
            gc_cnt = self._gc_count

        growth = mem_pct - self._baseline_mem
        gpu_info = (f"  GPU: {gpu_pct:.1f}%"
                    if gpu_pct > 0 else "")
        msg = (f"[MemoryGuard] [{level}]"
               f"  系统内存: {mem_pct:.1f}%"
               f"  (基线: {self._baseline_mem:.1f}%"
               f"  增长: {growth:+.1f}%)"
               f"{gpu_info}"
               f"  累计丢帧: {drop_cnt}  GC: {gc_cnt}")

        if "CRITICAL" in str(level):
            error(msg)
        else:
            info(msg)

    def update_baseline(self):
        """手动更新基线（例如模型加载完成后调用）"""
        old = self._baseline_mem
        self._baseline_mem = self.get_system_memory_percent()
        self._baseline_gpu = self.get_gpu_memory_percent()
        info(f"[MemoryGuard] 基线更新: "
             f"{old:.1f}% → {self._baseline_mem:.1f}%"
             f"  GPU: {self._baseline_gpu:.1f}%")

    def get_stats(self) -> dict:
        with self._lock:
            growth = (self._last_mem_percent
                      - self._baseline_mem)
            return {
                "system_memory_percent": self._last_mem_percent,
                "gpu_memory_percent": self._last_gpu_percent,
                "baseline_memory_percent": self._baseline_mem,
                "memory_growth": growth,
                "drop_count": self._drop_count,
                "gc_count": self._gc_count,
                "check_count": self._check_count,
                "high_watermark": self.high_watermark,
                "critical_watermark": self.critical_watermark,
                "max_growth_percent": self.max_growth_percent,
                "psutil_available": _PSUTIL_AVAILABLE,
            }