#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
相机注册中心
统一管理所有已连接相机的 CameraOperation 实例
供 TriggerControlSystem（Communication_Tool.Modbus）统一调度触发源切换
（硬触发 Line0 <-> 软触发 Software），用于织机停机时的预览兜底方案。

设计原则：
- CameraOperation 本身不感知业务层的相机命名规则(camera1/camera2...)，
  由主程序在打开设备成功后，用 CAMERA_SN_MAP 里的名字完成注册
- TriggerControlSystem 只关心"切成什么模式"，不关心具体有几台相机
"""
import threading
from typing import Dict

from General_Tool.EnhancedLogger import info, error

_lock = threading.Lock()
_cameras: Dict[str, object] = {}  # {camera_name: CameraOperation instance}


def register_camera(camera_name: str, camera_op) -> None:
    with _lock:
        _cameras[camera_name] = camera_op
    info(f"[CameraRegistry] 相机已注册: {camera_name}")


def unregister_camera(camera_name: str) -> None:
    with _lock:
        _cameras.pop(camera_name, None)
    info(f"[CameraRegistry] 相机已移除: {camera_name}")


def clear_all() -> None:
    with _lock:
        _cameras.clear()


def get_camera(camera_name: str):
    with _lock:
        return _cameras.get(camera_name)


def get_all_cameras() -> Dict[str, object]:
    with _lock:
        return dict(_cameras)


def set_all_trigger_source(source: str) -> None:
    """
    统一切换所有已注册相机的触发源
    source: "Line0"(硬触发，织机运行时用于疵点检测)
            或 "Software"(软触发，织机停机时用于低频预览)
    """
    cameras = get_all_cameras()
    if not cameras:
        return
    for name, cam_op in cameras.items():
        try:
            cam_op.Set_trigger_source(source)
        except Exception as e:
            error(f"[CameraRegistry] 相机 {name} 切换触发源为 {source} 失败: {e}")


def trigger_all_software() -> None:
    """对所有已注册相机发送一次软触发（停机预览用）"""
    cameras = get_all_cameras()
    for name, cam_op in cameras.items():
        try:
            cam_op.Trigger_once()
        except Exception as e:
            error(f"[CameraRegistry] 相机 {name} 软触发失败: {e}")


def get_camera_count() -> int:
    with _lock:
        return len(_cameras)