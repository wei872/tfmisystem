#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: cuda_stream_pool.py
路径: AnomalyDetection_Tool/core/cuda_stream_pool.py
职责: 全局低优先级CUDA Stream池，供经纬线分析任务复用。

设计原则:
  - YOLO推理使用高优先级Stream（priority=-1），见 yolo_detector.py
  - 经纬线分析使用低优先级Stream（priority=0），本模块管理
  - Stream池化复用，避免每次任务创建/销毁Stream的开销
  - 线程安全，多个分析任务可并发获取不同Stream

GPU调度优先级说明:
  CUDA Stream priority=-1 (高) → YOLO推理，抢占式调度
  CUDA Stream priority=0  (低) → 经纬线分析，让步于高优先级Stream
  当两者同时有kernel待执行时，GPU优先调度priority=-1的kernel。
"""

import threading
from contextlib import contextmanager
from typing import List, Optional

from General_Tool.EnhancedLogger import info

try:
    import torch
    _TORCH_AVAILABLE = True
except ImportError:
    _TORCH_AVAILABLE = False


class CudaStreamPool:
    """
    低优先级CUDA Stream池。

    使用方式:
        with cuda_stream_pool.acquire() as stream_ctx:
            # 在低优先级Stream中执行GPU操作
            with stream_ctx:
                gpu_operation()
    """

    def __init__(self, pool_size: int = 4, device_idx: int = 0):
        self._pool_size = pool_size
        self._device_idx = device_idx
        self._streams: List = []
        self._lock = threading.Lock()
        self._available = threading.Semaphore(0)
        self._enabled = False
        self._init_streams()

    def _init_streams(self):
        """初始化Stream池，失败时降级为CPU模式"""
        if not _TORCH_AVAILABLE:
            return
        if not torch.cuda.is_available():
            return
        try:
            for _ in range(self._pool_size):
                # ★ 低优先级Stream（priority=0）
                # 当YOLO高优先级Stream有kernel时，此Stream的kernel延迟执行
                stream = torch.cuda.Stream(
                    device=self._device_idx,
                    priority=0,  # 低优先级，让步于YOLO推理
                )
                self._streams.append(stream)
                self._available.release()
            self._enabled = True
            info(
                f"[CudaStreamPool] 初始化完成: "
                f"{self._pool_size}个低优先级Stream (priority=0)"
            )
        except Exception as e:
            info(f"[CudaStreamPool] 初始化失败({e})，将使用默认Stream")
            self._streams.clear()
            self._enabled = False

    @contextmanager
    def acquire(self, timeout: float = 2.0):
        """
        获取一个低优先级Stream上下文。
        若Pool未启用或获取超时，返回空上下文（不影响功能，仅丢失优先级隔离）。
        """
        if not self._enabled or not self._streams:
            # 降级：使用默认Stream，无优先级隔离但功能正常
            yield _NullStreamCtx()
            return

        acquired = self._available.acquire(timeout=timeout)
        if not acquired:
            # 超时降级，记录但不阻塞任务
            yield _NullStreamCtx()
            return

        with self._lock:
            stream = self._streams.pop()

        try:
            # 返回低优先级Stream上下文供调用方使用
            yield torch.cuda.stream(stream)
        finally:
            # 归还Stream到池中
            with self._lock:
                self._streams.append(stream)
            self._available.release()

    def synchronize_all(self):
        """等待所有Stream中的kernel执行完毕（用于关闭前清理）"""
        if not self._enabled:
            return
        with self._lock:
            for s in self._streams:
                try:
                    s.synchronize()
                except Exception:
                    pass

    @property
    def enabled(self) -> bool:
        return self._enabled


class _NullStreamCtx:
    """空Stream上下文，用于CPU模式或降级场景"""
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass


# ── 全局单例 ──────────────────────────────────────────────────────────
# 供经纬线分析任务使用的低优先级Stream池
# YOLO推理不使用此池，它在yolo_detector.py中自行创建高优先级Stream
_global_pool: Optional[CudaStreamPool] = None
_pool_lock = threading.Lock()


def get_analysis_stream_pool(pool_size: int = 4, device_idx: int = 0) -> CudaStreamPool:
    """
    获取全局低优先级Stream池单例。
    首次调用时初始化，后续调用直接返回已有实例。
    """
    global _global_pool
    if _global_pool is None:
        with _pool_lock:
            if _global_pool is None:
                _global_pool = CudaStreamPool(
                    pool_size=pool_size,
                    device_idx=device_idx,
                )
    return _global_pool