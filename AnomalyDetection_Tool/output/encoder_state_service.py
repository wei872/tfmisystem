#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
encoder_state.py — 编码器状态共享单例
线程安全，供编码器线程写入、检测线程读取
"""

import threading


class EncoderState:
    """编码器运动状态（全局单例）"""

    def __init__(self):
        self._lock = threading.RLock()
        self._cumulative_distance: float = 0.0
        self._current_speed: float = 0.0
        self._trigger_count: int = 0
        self._is_forward: bool = True

    def update(self, status: dict):
        """
        由编码器线程调用，更新当前状态

        Args:
            status: MotionController.update() 返回的状态字典
        """
        with self._lock:
            self._cumulative_distance = status.get(
                "cumulative_distance", 0.0)
            self._current_speed = status.get(
                "current_speed", 0.0)
            self._trigger_count = status.get(
                "trigger_count", 0)
            self._is_forward = status.get(
                "is_forward", True)

    @property
    def cumulative_distance(self) -> float:
        """当前累计行进距离（mm）"""
        with self._lock:
            return self._cumulative_distance

    @property
    def current_speed(self) -> float:
        """当前速度（m/min）"""
        with self._lock:
            return self._current_speed

    @property
    def trigger_count(self) -> int:
        """触发次数"""
        with self._lock:
            return self._trigger_count

    @property
    def is_forward(self) -> bool:
        """是否正转"""
        with self._lock:
            return self._is_forward

    def snapshot(self) -> dict:
        """
        获取当前状态快照（原子读取所有字段）
        检测线程调用此方法，避免多次加锁
        """
        with self._lock:
            return {
                "cumulative_distance": self._cumulative_distance,
                "current_speed":       self._current_speed,
                "trigger_count":       self._trigger_count,
                "is_forward":          self._is_forward,
            }


# 全局单例
_encoder_state = EncoderState()


def get_encoder_state() -> EncoderState:
    """获取全局编码器状态单例"""
    return _encoder_state