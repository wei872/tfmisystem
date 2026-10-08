#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CH340 继电器控制（缺陷停机时短接触发织机停车）

端口选择策略（按优先级）：
  1. config.yaml 的 relay.port（如 "COM6"）
  2. 未配置或配置的端口当前不存在时，按 CH340 的 VID/PID 自动识别

端口在**首次成功解析后固定下来**，运行期间不再变动；改 config.yaml
需重启生效。这样现场排查时"当前用的是哪个口"是确定的，不会中途漂移。
（解析失败不会被缓存，因此开机时还没插好的继电器，插上后仍能被识别到。）
"""

import threading
import time
from typing import Optional

import serial
import serial.tools.list_ports as lp

from General_Tool.EnhancedLogger import error, info

VID_CH340 = 0x1A86  # QinHeng
PID_CH340 = 0x7523

# 已解析并固定下来的端口（只缓存成功结果）
_resolved_port: Optional[str] = None
_resolve_lock = threading.Lock()


def _relay_config() -> dict:
    """读取继电器配置（热更新段，改完约 2 秒生效）"""
    try:
        from AnomalyDetection_Tool.config.settings import RELAY_CONFIG
        return RELAY_CONFIG
    except Exception:
        return {}


def _list_ch340_ports() -> list:
    """列出所有 CH340 设备的端口名"""
    try:
        return [p.device for p in lp.comports()
                if (p.vid, p.pid) == (VID_CH340, PID_CH340)]
    except Exception as e:
        error(f"[继电器] 枚举串口失败: {e}")
        return []


def _all_ports() -> list:
    try:
        return [p.device for p in lp.comports()]
    except Exception:
        return []


def _do_resolve(preferred: str) -> Optional[str]:
    """实际的端口解析逻辑（不含缓存）"""
    available = _all_ports()
    ch340 = _list_ch340_ports()

    # ① 优先用配置指定的端口
    if preferred:
        for dev in available:
            if dev.upper() == preferred.upper():
                info(f"[继电器] 使用配置端口 {dev}")
                return dev

        # 配置了但当前不存在：不直接失败，退回自动识别，但要明确告警
        error(f"[继电器] 配置的端口 {preferred} 不存在"
              f"（当前串口: {available or '无'}），尝试自动识别 CH340")

    # ② 自动识别。只有唯一一个 CH340 时才敢用，
    #    多个设备时不猜 —— 工控现场误触发别的串口设备后果不可控。
    if len(ch340) == 1:
        info(f"[继电器] 自动识别到 CH340 @ {ch340[0]}")
        return ch340[0]

    if len(ch340) > 1:
        error(f"[继电器] 检测到多个 CH340 设备 {ch340}，"
              f"请在 config.yaml 的 relay.port 中明确指定")
        return None

    error(f"[继电器] 未找到 CH340 继电器模块（当前串口: {available or '无'}）")
    return None


def resolve_port(preferred: str = None, force: bool = False) -> Optional[str]:
    """
    解析要使用的串口，成功后固定下来。

    原实现把端口写死为 COM6，并且要求 VID/PID 与端口名**同时**匹配 ——
    设备一旦被 Windows 枚举成 COM7，就会静默地找不到继电器，
    缺陷停机时继电器不动作却只在日志里留一句"未找到"。

    Args:
        preferred: 指定端口；None 表示取 config.yaml 的 relay.port
        force:     True 则忽略缓存重新解析（换设备后免重启用）
    """
    global _resolved_port

    if not force and _resolved_port is not None:
        return _resolved_port

    if preferred is None:
        preferred = str(_relay_config().get("port", "") or "").strip()

    with _resolve_lock:
        if not force and _resolved_port is not None:
            return _resolved_port
        dev = _do_resolve(preferred)
        # 只缓存成功结果：开机时继电器还没插好的话，插上后仍能被识别
        if dev is not None:
            _resolved_port = dev
        return dev


def init_relay() -> Optional[str]:
    """
    启动阶段解析一次串口，把结果固定下来并打进日志。

    放在启动流程里调用，好处是"继电器到底用哪个口"在开机日志里
    一眼可见，而不是等到第一次缺陷停机才暴露问题。
    """
    dev = resolve_port(force=True)
    if dev is None:
        error("[继电器] 初始化失败：未解析到可用串口，"
              "缺陷停机将无法触发继电器（请检查 config.yaml 的 relay.port）")
    else:
        cfg = _relay_config()
        info(f"[继电器] 初始化完成 | 端口={dev} "
             f"波特率={cfg.get('baudrate', 9600)} "
             f"通道={cfg.get('channel', 1)} "
             f"脉宽={cfg.get('pulse_ms', 50)}ms")
    return dev


def _relay_cmd(port: str, addr: int, cmd: int, baudrate: int = 9600,
               timeout: float = 0.3) -> bool:
    """发 4 字节指令并校验反馈（若有）"""
    checksum = (0xA0 + addr + cmd) & 0xFF
    pkt = bytes([0xA0, addr, cmd, checksum])
    try:
        with serial.Serial(port, baudrate, timeout=timeout) as ser:
            ser.write(pkt)
            if cmd in (0x02, 0x03, 0x05):  # 需要反馈
                rsp = ser.read(4)
                return len(rsp) == 4 and rsp[3] == (sum(rsp[:3]) & 0xFF)
            return True  # 无反馈指令
    except Exception as e:
        error(f"[继电器] 串口通信失败 {port}: {e}")
        return False


def relay_pulse(port: str = None, channel: int = 1, ms: int = 50) -> bool:
    """
    短接继电器一次。

    Args:
        port:    串口号；None 表示按 config.yaml -> 自动识别 的顺序解析
        channel: 通道 1 / 2
        ms:      吸合时长（毫秒）

    Returns:
        True 表示吸合与断开均执行成功
    """
    if channel not in (1, 2):
        raise ValueError('channel 只能是 1 或 2')

    cfg = _relay_config()
    baudrate = int(cfg.get("baudrate", 9600) or 9600)

    port = port or resolve_port()
    if port is None:
        return False

    info(f'[继电器] {port} 通道 {channel} 短接 {ms} ms')

    if not _relay_cmd(port, channel, 0x03, baudrate):   # 吸合
        error(f'[继电器] {port} 通道 {channel} 吸合失败')
        return False

    time.sleep(ms / 1000)

    if not _relay_cmd(port, channel, 0x02, baudrate):   # 断开
        error(f'[继电器] {port} 通道 {channel} 断开失败')
        return False

    info(f'[继电器] {port} 通道 {channel} 触发完成')
    return True


# ---------------- 用法示例 ----------------
if __name__ == '__main__':
    print("可用串口:", _all_ports())
    print("CH340 设备:", _list_ch340_ports())
    print("解析到的端口:", resolve_port())
    print("触发结果:", relay_pulse())
