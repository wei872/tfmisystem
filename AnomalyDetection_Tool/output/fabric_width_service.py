#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
文件名: AnomalyDetection_Tool/output/fabric_width_service.py
模块概述: 布幅检测器的全局单例管理
         - main_yolo.py 负责初始化和写入（process_frame）
         - Modbus.py 等外部模块负责读取（get_fabric_width）
         - 两边都只依赖本模块，不互相依赖
===============================================================================
"""

from typing import Optional

_fabric_detector = None  # type: Optional["FabricWidthDetector"]


def init_fabric_detector(config: dict):
    """
    初始化布幅检测器（由 main_yolo.py 调用一次）

    Args:
        config: FABRIC_WIDTH_CONFIG 配置字典
    """
    global _fabric_detector
    from AnomalyDetection_Tool.analysis.fabric_width_detector import (
        FabricWidthDetector,
    )
    _fabric_detector = FabricWidthDetector(config)
    return _fabric_detector


def get_fabric_detector():
    """获取布幅检测器实例（可能为 None）"""
    return _fabric_detector


def get_fabric_width() -> Optional[float]:
    """
    获取最新布幅值（cm）

    供 Modbus.py 等外部模块调用，线程安全。
    返回 None 表示数据不足或未启用。
    """
    if _fabric_detector is None:
        return None
    return _fabric_detector.get_fabric_width()


def get_fabric_stats() -> dict:
    """获取布幅统计信息"""
    if _fabric_detector is None:
        return {"enabled": False}
    return _fabric_detector.get_stats()