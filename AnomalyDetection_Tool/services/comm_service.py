#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通信模块统一封装
- ThriftControl: 停机标志
- DefectTrigger: 继电器
- 上传可用性: 检查 Communication_Tool.http_client
"""

import threading
from General_Tool.EnhancedLogger import info, error


class CommService:
    """通信服务封装（单例）"""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self.thrift_available = False
        self.upload_available = False
        self.relay_available = False

        self._thrift_module = None
        self._relay_func = None

        self._load_modules()

    def _load_modules(self):
        """加载所有通信模块"""
        # ThriftControl（停机标志）
        try:
            import Communication_Tool.ThriftControl as thrift
            self._thrift_module = thrift
            self.thrift_available = True
            info("[导入] ThriftControl ✓")
        except ImportError as e:
            error(f"[导入] ThriftControl ✗: {e}")

        # http_client（上传接口）
        # 原先经由 upload/upload_client.py 这个纯转发的一行文件中转，已删除。
        try:
            from Communication_Tool.http_client import (
                upload_detection_info,  # noqa: F401
            )
            self.upload_available = True
            info("[导入] http_client ✓")
        except ImportError as e:
            error(f"[导入] http_client ✗: {e}")

        # DefectTrigger（继电器）
        try:
            from Communication_Tool.DefectTrigger import relay_pulse
            self._relay_func = relay_pulse
            self.relay_available = True
            info("[导入] DefectTrigger ✓")
        except (ImportError, AttributeError) as e:
            error(f"[导入] DefectTrigger ✗: {e}")

    def set_defect_stop(self) -> bool:
        """
        触发"缺陷停机"（可自动恢复），等价于 ThriftControl.setDefectStop()。

        此前 StopMachineService / StopHandler 直接 import ThriftControl 调用底层，
        绕过了这里的可用性检查与异常兜底。统一走本方法。
        """
        if not self.thrift_available:
            error("[停机] ThriftControl 不可用，缺陷停机未生效")
            return False
        try:
            # setDefectStop() 在手动停止期间会返回 False（请求被忽略）。
            # 必须按返回值打日志，否则会出现"日志说已触发、实际未触发"的
            # 矛盾记录 —— 排查停机问题时这种日志最误导人。
            triggered = self._thrift_module.setDefectStop()
            if triggered:
                info("[停机] 缺陷停机已触发（setDefectStop）")
            else:
                info("[停机] 缺陷停机未生效（当前为手动停止状态，保持停止）")
            return bool(triggered)
        except Exception as e:
            error(f"[停机] setDefectStop 失败: {e}")
            return False

    def trigger_relay(self, channel: int, ms: int = 1,
                      port: str = None) -> bool:
        """
        触发继电器。

        port=None 时由 DefectTrigger 按 "config.yaml -> 自动识别 CH340"
        的顺序解析，因此串口号支持热更新。
        """
        if not self.relay_available:
            return False
        return self._relay_func(channel=channel, ms=ms, port=port)

    def status_summary(self) -> dict:
        return {
            "thrift": "✓" if self.thrift_available else "✗",
            "upload": "✓" if self.upload_available else "✗",
            "relay": "✓" if self.relay_available else "✗",
        }


# 全局单例
comm_service = CommService()