# General_Tool/RunningSystemRegistry.py
"""
全局运行实例注册中心
- 不导入任何业务模块，专门用于打断循环依赖
- 使用 TYPE_CHECKING 做类型提示，不产生运行时导入
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Optional

from General_Tool.EnhancedLogger import info, error

if TYPE_CHECKING:
    # 仅用于 IDE 类型提示，运行时不执行，不产生循环导入
    from Communication_Tool.Modbus import TriggerControlSystem

_lock = threading.Lock()
_running_system: Optional["TriggerControlSystem"] = None


def get_running_system() -> Optional["TriggerControlSystem"]:
    """获取当前正在运行的 TriggerControlSystem 实例"""
    with _lock:
        return _running_system


def set_running_system(system: Optional["TriggerControlSystem"]) -> None:
    """注册 / 注销正在运行的 TriggerControlSystem 实例"""
    global _running_system
    with _lock:
        _running_system = system
        if system is not None:
            info("[Registry] 已注册运行中的 TriggerControlSystem 实例")
        else:
            info("[Registry] 已注销 TriggerControlSystem 实例")