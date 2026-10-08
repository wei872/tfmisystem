#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
继电器触发服务（含冷却时间管理）
"""

import time
import threading

from General_Tool.EnhancedLogger import info, error
from AnomalyDetection_Tool.config.settings import RELAY_CONFIG
from AnomalyDetection_Tool.services.comm_service import comm_service


class RelayService:
    """继电器服务"""

    def __init__(self):
        self._last_trigger_time: float = 0.0
        self._lock = threading.Lock()

    def trigger(self, camera_name: str, class_name: str):
        """异步触发继电器（带冷却）"""
        with self._lock:
            now = time.time()
            # RELAY_CONFIG 是热更新段，这里每次触发都取当前值
            cd = RELAY_CONFIG.get("cooldown_seconds", 0)
            if now - self._last_trigger_time < cd:
                info("[继电器] 冷却中")
                return
            self._last_trigger_time = now

        def _do():
            try:
                # ========== 时间追踪：继电器触发开始 ==========
                from datetime import datetime
                info(f'[时间追踪3] 继电器开始触发 | '
                     f'{camera_name} | {class_name} | '
                     f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]}')
                # =========================================
                info("[继电器] 触发...")
                ok = comm_service.trigger_relay(
                    channel=int(RELAY_CONFIG.get("channel", 1)),
                    ms=int(RELAY_CONFIG.get("pulse_ms", 50)),
                    port=str(RELAY_CONFIG.get("port", "") or "") or None,
                )
                info(f"[继电器] {'成功' if ok else '失败'}")
            except Exception as e:
                error(f"[继电器] {e}")

        threading.Thread(target=_do, daemon=True).start()


# 全局单例
relay_service = RelayService()