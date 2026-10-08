#!/usr/bin/env python3
"""
========================================
S7-200 SMART 编码器触发控制系统
========================================
功能：
1. 实时读取PLC的HC0计数值
2. 计算速度、距离、方向
3. 判断触发条件并发送触发命令
4. 支持正反转判断和速度过滤
5. 实时显示运行状态

作者：[您的名字]
日期：2026-03-09
版本：1.2 (修复缺陷停机误伤硬触发问题)

新增功能：
- 织机停机时自动切换相机为软触发预览模式（低频出图，仅供人工查看）
- 织机恢复运行时立刻切回硬触发，恢复正常疵点检测
- 两种模式产生的图像通过 metadata 区分，预览帧不进入疵点检测/布长统计

 本版本修复：
- 之前 should_trigger 的判定错误地使用了"缺陷停机"逻辑标志(TC.getIsStop())，
  导致一旦检测到疵点、触发继电器后，即便织机物理上仍在惯性转动（is_idle 尚未确认为 True），
  硬触发命令也会被立刻掐断，造成"检测到一次疵点后就再也不出图/不检测"的假象。
- 现在 should_trigger 只受编码器测得的真实物理停机状态 self.is_idle 控制，
  is_stopped(缺陷停机标志) 只用于自动恢复状态机(_check_auto_resume)判断，
  不再影响是否继续发送硬触发/继续检测。

========================================
"""
import threading
from General_Tool.RunningSystemRegistry import set_running_system, get_running_system
from Camera_Tool import CameraRegistry  #  新增：相机触发源统一调度
import aiohttp
import asyncio
from pymodbus.client import ModbusTcpClient
from dataclasses import dataclass
from typing import Optional, Tuple
import time
import sys

import Communication_Tool.ThriftControl as TC
from General_Tool.EnhancedLogger import info, error, warning
from AnomalyDetection_Tool.output.fabric_width_service import get_fabric_width
from AnomalyDetection_Tool.output.encoder_state_service import get_encoder_state

# ========================================
# 配置类
# ========================================
@dataclass
class SystemConfig:
    """系统配置参数"""
    # PLC连接参数
    plc_host: str = '192.168.123.70'
    plc_port: int = 502
    device_id: int = 1

    # 机械参数
    wheel_diameter: float = 60.0  # 胶轮直径(mm)
    encoder_ppr: int = 2000  # 编码器分辨率(P/R)
    trigger_distance: float = 178.0  # 触发距离(mm)
    scaling_factor: float = .78651
    # 控制参数
    read_interval: float = 0.001  # 读取间隔 s
    speed_threshold_ratio: float = 0.0  # 速度阈值比例(60%)
    avg_speed_window: int = 100  # 平均速度计算窗口(样本数)

    # Modbus地址映射
    addr_hc0: int = 0  # HC0计数值地址(VD0 -> 40001-40002)
    addr_trigger: int = 4  # 触发命令地址(VW4 -> 40003)
    addr_reset: int = 6  # 复位命令地址(VW6 -> 40004)
    #  新增：累计米长持久化地址（VD8 双字，需在 PLC 内对应到断电保持的 V 区/HR 区，
    #        以保证停电重启后仍能读回上次的累计距离）。
    #  存储单位：mm（int32 足以覆盖数十千米），由程序自行换算米/毫米。
    addr_total_distance: int = 8
    #  累计距离写回 PLC 的节流间隔（秒）+ 最小变化量（mm），
    #  避免每帧都占用 Modbus 通道；只要距离增量超过阈值或时间到点就刷一次。
    persist_interval: float = 1.0
    persist_min_delta_mm: float = 5.0

    #  布料尺寸 HTTP 上报(/v1/defect/log/size)的节流间隔（秒）。
    #  原实现主循环每轮(read_interval=100ms)都无条件起线程+新建连接上报一次，
    #  10 请求/秒 7x24 空转；periodic_report_service 每 5s 的密度上报已含 length/width。
    fabric_upload_interval: float = 5.0

    #  新增：停机预览相关配置
    idle_speed_threshold: float = 0.5      # m/min，低于此速度视为"可能已停机"
    idle_confirm_duration: float = 3.0     # 秒，持续低于阈值这么久才真正判定为"已停机"
                                            # （防止减速/抖动瞬间误判）
    preview_interval: float = 1.0          # 秒，停机预览期间的软触发间隔（1fps足够看状态）

    #  编码器合理性上限（m/min）。
    #  PLC 的 HC0 计数器被复位、断电回零、或 32 位回绕时，
    #  pulse_delta = hc0 - last_hc0 会瞬间变成一个巨大的正/负数，
    #  算出来的 current_speed 是天文数字。后果有两个，都很难查：
    #    1. current_speed 永远高于 idle_speed_threshold → is_idle 永远为 False
    #       → 停机预览模式再也进不去，软触发预览彻底失效；
    #    2. accumulated_distance 被灌进一个巨大的值，触发节拍彻底错乱。
    #  超过这个上限的增量一律判为计数器突变，丢弃本拍并重新对齐基准。
    #  默认 300 m/min：织机实际线速度远低于此，留足余量不会误杀。
    max_plausible_speed_mpm: float = 300.0

    def __post_init__(self):
        """计算衍生参数"""
        # 胶轮周长(mm)
        self.wheel_circumference = 3.141 * self.wheel_diameter

        # 编码器总分辨率(4倍频)
        self.encoder_resolution = self.encoder_ppr * 4

        # 脉冲当量(mm/脉冲)
        self.pulse_equivalent = self.wheel_circumference / self.encoder_resolution

        # 触发脉冲数
        self.trigger_pulses = int(self.trigger_distance / self.pulse_equivalent)
        # 实际的画幅高度下 理论触发脉冲数 = 画幅高度 / (60*3.14)/(2000*4)
        info(f"系统配置:"
             f"  - 触发距离：: {self.trigger_distance / self.scaling_factor:.2f} mm"
             f"  - 胶轮周长: {self.wheel_circumference:.2f} mm"
             f"  - 编码器分辨率: {self.encoder_resolution} 脉冲/转"
             f"  - 脉冲当量: {self.pulse_equivalent:.6f} mm/脉冲"
             f"  - 触发脉冲数: {self.trigger_pulses}"
             f"  - 停机判定阈值: {self.idle_speed_threshold} m/min，"
             f"确认时长: {self.idle_confirm_duration}s"
             f"  - 预览帧间隔: {self.preview_interval}s")


# ========================================
# Modbus通信类
# ========================================
class ModbusClient:
    """Modbus TCP通信管理"""

    def __init__(self, config: SystemConfig):
        self.config = config
        self.client = None
        self._connected = False

    def connect(self) -> bool:
        """连接到PLC"""
        try:
            self.client = ModbusTcpClient(
                host=self.config.plc_host,
                port=self.config.plc_port,
            )
            self._connected = self.client.connect()
            if self._connected:
                info(f"已连接到PLC: {self.config.plc_host}:{self.config.plc_port}")
            return self._connected
        except Exception as e:
            error(f"✗ 连接PLC失败: {e}")
            self._connected = False
            return False

    def disconnect(self):
        """断开连接"""
        if self.client:
            self.client.close()
            self._connected = False
            info("已断开PLC连接")

    def is_connected(self) -> bool:
        """检查连接状态"""
        return self._connected and self.client and self.client.connected

    def read_dword(self, address: int) -> Optional[int]:
        """
        读取32位有符号整数(DWORD)

        Args:
            address: 寄存器地址(字节偏移/2)

        Returns:
            32位有符号整数，失败返回None
        """
        if not self.is_connected():
            return None

        try:
            reg_addr = address // 2
            result = self.client.read_holding_registers(
                address=reg_addr,
                count=2,
                device_id=self.config.device_id
            )

            if result.isError():
                return None

            # 大端模式：高字在前，低字在后
            high_word = result.registers[0]
            low_word = result.registers[1]
            value = (high_word << 16) | low_word

            # 转换为有符号整数
            if value >= 0x80000000:
                value -= 0x100000000

            return value

        except Exception as e:
            error(f"读取DWORD错误: {e}")
            return None

    def write_word(self, address: int, value: int) -> bool:
        """
        写入16位整数(WORD)

        Args:
            address: 寄存器地址(字节偏移/2)
            value: 要写入的值

        Returns:
            成功返回True，失败返回False
        """
        if not self.is_connected():
            return False

        try:
            reg_addr = address // 2

            # 转换为无符号16位整数
            if value < 0:
                value += 0x10000

            result = self.client.write_register(
                reg_addr,
                value,
                device_id=self.config.device_id
            )

            return not result.isError()

        except Exception as e:
            error(f"✗ 写入WORD错误: {e}")
            return False

    def write_dword(self, address: int, value: int) -> bool:
        """
        写入32位有符号整数(DWORD)

        Args:
            address: 寄存器地址(字节偏移/2)，占 2 个连续寄存器
            value: 要写入的值（int32，可为负）

        Returns:
            成功返回True，失败返回False
        """
        if not self.is_connected():
            return False

        try:
            reg_addr = address // 2

            # 转换为无符号32位整数
            value = int(value) & 0xFFFFFFFF
            high_word = (value >> 16) & 0xFFFF
            low_word = value & 0xFFFF

            result = self.client.write_registers(
                reg_addr,
                [high_word, low_word],
                device_id=self.config.device_id
            )

            return not result.isError()

        except Exception as e:
            error(f"✗ 写入DWORD错误: {e}")
            return False

    def read_hc0(self) -> Optional[int]:
        """读取HC0计数值"""
        return self.read_dword(self.config.addr_hc0)

    def read_total_distance(self) -> Optional[int]:
        """读取 PLC 持久化的累计距离(mm)，断电重启后从这里恢复"""
        return self.read_dword(self.config.addr_total_distance)

    def write_total_distance(self, value_mm: int) -> bool:
        """把累计距离(mm)写回 PLC 保持寄存器，确保停电重启可恢复"""
        return self.write_dword(self.config.addr_total_distance, int(value_mm))

    def send_trigger(self) -> bool:
        """发送触发命令"""
        return self.write_word(self.config.addr_trigger, 1)

    def send_reset(self) -> bool:
        """发送复位命令"""
        return self.write_word(self.config.addr_reset, 1)


async def update_fabric_size(width: float, length: float):
    """
    上报布料尺寸。

    实现已统一到 Communication_Tool.http_client.update_fabric_size
    （此前这里有一份重复实现 + 硬编码的 localhost:8890 地址）。
    此函数保留为兼容入口，仅做转发。
    """
    from Communication_Tool.http_client import (
        update_fabric_size as _update_fabric_size,
    )
    return await _update_fabric_size(width, length)


# ========================================
# 运动控制类
# ========================================
class MotionController:
    """运动控制和触发判断"""

    def __init__(self, config: SystemConfig, last_hc0: float):
        self.config = config

        # 状态变量
        self.last_hc0 = 0  # 上次HC0值
        self.last_time = time.perf_counter() * 1000  # 上次读取时间
        self.accumulated_distance = 0.0  # 发出距离计数(mm)
        self.cumulative_distance = 0.0  # 总累计距离
        #  新增：累计距离断电保持基准。
        #   程序启动时从 PLC 保持寄存器读回上次累计距离(mm)，作为本会话的累计基准，
        #   本会话累计 = base_cumulative_distance + 本会话新增距离。
        #   这样即使停电/restart，累计米长也能从前一次的最后位置继续往上加，
        #   而不是从 0 开始 —— 除非用户在前端点了「开始」，触发 PCL_reset() 把 base 与 PLC 寄存器同时清零。
        self.base_cumulative_distance: float = 0.0
        self.trigger_count = 0  # 触发次数

        # 速度相关
        self.current_speed = 0.0  # 当前速度(m/min)
        self.speed_history = []  # 速度历史记录
        self.average_speed = 0.0  # 平均速度(m/min)

        # 方向和状态
        self.is_forward = True  # 是否正转
        self.is_running = False  # 是否达到运行速度

        #  新增：停机(idle)判定状态 —— 与 TC.getIsStop() 语义不同！
        # TC.getIsStop() 是"手动暂停/缺陷停机"，属于人为/检测触发的停机
        # 这里的 is_idle 是"织机物理上真的没在转"，只看编码器速度
        #  修复关键点：should_trigger（是否继续硬触发拍照检测）只应该由 is_idle 决定，
        #   不应该被 TC.getIsStop() 这种"逻辑上的缺陷停机标志"影响，
        #   否则一检测到疵点、触发继电器后，硬触发会被立刻掐断，
        #   即便织机因惯性还在转动也拍不到新图，造成"检测到一次疵点后就哑火"的假象。
        self._idle_since: Optional[float] = None
        self.is_idle = False

    def update(self, hc0: int, is_stopped: bool = False) -> Tuple[bool, dict]:
        """
        更新运动状态并判断是否触发

        Args:
            hc0: 当前HC0计数值
            is_stopped: 【仅保留兼容旧调用/状态展示用】当前是否处于"缺陷停机"逻辑状态。
                         修复说明：本参数不再参与 should_trigger 的判定！
                        是否继续发送硬触发、是否继续检测，只取决于织机的真实物理转速(is_idle)，
                        与是否检测到过疵点(is_stopped)无关。is_stopped 仅在 status 字典中
                        原样透出，供上层(如自动恢复状态机/显示面板)按需使用。
        """
        current_time = time.perf_counter() * 1000

        # 计算时间间隔
        dt = current_time - self.last_time
        if dt <= 0:
            dt = 0.001  # 防止除零

        # 计算脉冲增量
        pulse_delta = hc0 - self.last_hc0

        # ── 计数器突变防护 ──────────────────────────────────────────
        # HC0 被复位/回零/32位回绕时，pulse_delta 会瞬间变成天文数字。
        # 按本拍时长折算成速度，超过 max_plausible_speed_mpm 就判为突变：
        # 丢弃本拍、重新对齐基准，避免污染速度和累计距离。
        max_pulses = abs(
            self.config.max_plausible_speed_mpm / 60.0 * 1000.0
            * (dt / 1000.0) / self.config.pulse_equivalent
        )
        if abs(pulse_delta) > max_pulses:
            self._glitch_count = getattr(self, "_glitch_count", 0) + 1
            bogus_speed = abs(pulse_delta * self.config.pulse_equivalent / dt * 60)
            warning(
                f"[编码器] HC0 计数突变，已丢弃本拍："
                f"last={self.last_hc0} -> now={hc0} (Δ={pulse_delta} 脉冲, "
                f"折算速度 {bogus_speed:,.0f} m/min 超过上限 "
                f"{self.config.max_plausible_speed_mpm:.0f} m/min)。"
                f"通常是 PLC 计数器被复位或 32 位回绕。"
                f"累计 {self._glitch_count} 次。")
            self.last_hc0 = hc0
            self.last_time = current_time
            # 保持突变前的状态值，只把本拍的增量置零，
            # 不让这个假速度渗进 is_idle / 累计距离 / 展示面板。
            return False, {
                'hc0': hc0,
                'pulse_delta': 0,
                'distance_delta': 0.0,
                'accumulated_distance': self.accumulated_distance,
                'cumulative_distance': self.cumulative_distance,
                'current_speed': self.current_speed,
                'average_speed': self.average_speed,
                'is_forward': self.is_forward,
                'is_running': self.is_running,
                'trigger_count': self.trigger_count,
                'is_idle': self.is_idle,
                'is_defect_stopped': is_stopped,
            }

        # 判断方向
        if pulse_delta > 0:
            self.is_forward = True
        elif pulse_delta < 0:
            self.is_forward = False

        # 计算距离增量(mm)
        distance_delta = pulse_delta * self.config.pulse_equivalent

        # 更新累计距离
        if self.is_forward:
            self.accumulated_distance += distance_delta
        else:
            # 反转时减去距离，但不允许为负
            self.accumulated_distance = max(0, self.accumulated_distance + distance_delta)

        #

        #  m/min                        mm        ms
        self.current_speed = abs(distance_delta / dt * 60)
        # 更新速度历史
        if self.current_speed != 0:
            self.speed_history.append(self.current_speed)
        if len(self.speed_history) > self.config.avg_speed_window:
            self.speed_history.pop(0)

        # # 计算平均速度
        # if len(self.speed_history) > 0:
        #     self.average_speed = sum(self.speed_history) / len(self.speed_history)
        #
        # # 判断是否达到运行速度(平均速度的60%)
        #
        # speed_threshold = self.average_speed * self.config.speed_threshold_ratio
        # self.is_running = self.current_speed >= speed_threshold and self.average_speed > 0.1
        self.is_running = True

        #  新增：织机物理停机判定（基于真实速度，与手动/缺陷停机无关）
        # 这是判定 is_idle 的唯一依据：编码器测得的真实速度是否持续低于阈值。
        now_sec = time.time()
        if self.current_speed < self.config.idle_speed_threshold:
            if self._idle_since is None:
                self._idle_since = now_sec
            self.is_idle = (now_sec - self._idle_since) >= self.config.idle_confirm_duration
        else:
            # 只要速度回升，立刻结束idle状态（不等确认期），
            # 保证织机一启动就尽快切回硬触发，避免漏拍第一帧
            self._idle_since = None
            self.is_idle = False

        #  修复：should_trigger 只由 self.is_idle（物理停机状态）控制，
        #   不再使用 is_stopped（缺陷停机逻辑标志）。
        #   原因：is_stopped 会在检测到一次疵点、触发继电器的瞬间就变为 True，
        #   而此时织机可能因惯性仍在转动（is_idle 还没到 idle_confirm_duration 的确认期），
        #   如果用 is_stopped 拦截，会导致硬触发被提前掐断，造成检测"假死"的现象。
        #   现在只有编码器真实测到"速度持续低于阈值达到确认时长"才会停止硬触发，
        #   这与 _handle_idle_preview() 切换软/硬触发所使用的信号源完全一致，逻辑自洽。
        should_trigger = False
        if not self.is_idle and self.is_forward and self.is_running:
            if self.accumulated_distance >= self.config.trigger_distance:
                should_trigger = True
                self.accumulated_distance -= self.config.trigger_distance
                self.trigger_count += 1

        # 更新状态
        self.last_hc0 = hc0
        self.last_time = current_time
        self.cumulative_distance = (
            self.base_cumulative_distance
            + self.trigger_count * self.config.trigger_distance
            + self.accumulated_distance
        )
        # 构建状态信息
        status = {
            'hc0': hc0,
            'pulse_delta': pulse_delta,
            'distance_delta': distance_delta,
            'accumulated_distance': self.accumulated_distance,
            'cumulative_distance': self.cumulative_distance,
            'current_speed': self.current_speed,
            'average_speed': self.average_speed,
            # 'speed_threshold': speed_threshold,
            'is_forward': self.is_forward,
            'is_running': self.is_running,
            'trigger_count': self.trigger_count,
            'is_idle': self.is_idle,  #  物理停机状态（决定触发/预览切换）
            'is_defect_stopped': is_stopped,  #  【新增】原样透出缺陷停机逻辑标志，仅供展示/上层参考，不参与触发判定
        }

        return should_trigger, status

    def reset_all(self):
        """完整重置所有运动状态（点击开始/停止时调用）

        说明：将本会话累计基准 base_cumulative_distance 也归零，
              并且上层调用方(PCL_reset)会同步把 PLC 保持寄存器写 0，
              以保证停电重启后累计米长也是从 0 起。
        """
        self.trigger_count = 0
        self.accumulated_distance = 0.0
        self.cumulative_distance = 0.0
        self.base_cumulative_distance = 0.0
        info("[MotionController] 完整状态已重置（距离、触发次数、累计基准）")

    def restore_base(self, value_mm: float) -> None:
        """从 PLC 保持寄存器恢复本会话累计基准（程序启动时调用）

        Args:
            value_mm: PLC 中持久化的上次累计距离(mm)，可为 0
        """
        self.base_cumulative_distance = max(0.0, float(value_mm or 0.0))
        self.cumulative_distance = (
            self.base_cumulative_distance
            + self.trigger_count * self.config.trigger_distance
            + self.accumulated_distance
        )
        info(f"[MotionController] 累计基准恢复为 {self.base_cumulative_distance:.2f}mm "
             f"(本会话累计起算: {self.cumulative_distance:.2f}mm)")

    def resync_hc0(self, hc0: int):
        """重新同步HC0基准值，不重置距离状态（PLC复位后调用）

        PLC复位可能将HC0清零，若不同步会导致下一周期pulse_delta巨大，
        被误判为反转并产生异常距离偏移。
        """
        self.last_hc0 = hc0
        self.last_time = time.perf_counter() * 1000
        info(f"[MotionController] HC0基准同步为 {hc0}，距离状态不变 "
             f"(累计: {self.cumulative_distance:.2f}mm, 触发: {self.trigger_count}次)")

    def reset_trigger_count(self):
        """重置触发计数及距离（兼容旧调用，内部改用 reset_all）"""
        self.reset_all()


# ========================================
# 显示管理类
# ========================================
class DisplayManager:
    """实时显示管理"""

    def __init__(self):
        self.last_update_time = time.time()
        self.update_interval = 0.001  # 1ms更新一次显示

    def should_update(self) -> bool:
        """判断是否应该更新显示"""
        current_time = time.time()
        if current_time - self.last_update_time >= self.update_interval:
            self.last_update_time = current_time
            return True
        return False

    def clear_line(self):
        """清除当前行"""
        sys.stdout.write('\r' + ' ' * 120 + '\r')
        sys.stdout.flush()

    def display_status(self, status: dict, trigger_mode: str = "Line0"):
        """
        同行更新显示状态信息

        Args:
            status: 状态信息字典
        """
        if not self.should_update():
            return

        # 方向指示
        direction = "→ 正转" if status['is_forward'] else "← 反转"

        # 运行状态
        running_status = "✓ 运行中" if status['is_running'] else "✗ 未达速"
        idle_flag = " [织机停机-预览模式]" if status.get('is_idle') else ""
        defect_flag = " [缺陷逻辑停机]" if status.get('is_defect_stopped') else ""

        # 构建显示字符串
        display_str = (
            f"HC0:{status['hc0']:8f} | "
            f"{direction} | "
            f"速度:{status['current_speed']:6.4f}m/min | "
            f"平均:{status['average_speed']:6.4f}m/min | "
            # f"阈值:{status['speed_threshold']:6.2f}m/min | "
            f"距离:{status['accumulated_distance']:6.2f}mm | "
            f"触发:{status['trigger_count']:4f}次 | "
            f"总行进距离：{status['cumulative_distance']:6.2f}mm | "
            f"{running_status} | 触发源:{trigger_mode}{idle_flag}{defect_flag}"
        )
        info(display_str)

    def display_trigger(self, count: int):
        """显示触发信息"""
        info(f"[触发] 第 {count} 次触发！")

    def display_error(self, message: str):
        """显示错误信息"""
        error(f"[错误] {message}")


# ========================================
# 主控制类
# ========================================
class TriggerControlSystem:
    """触发控制系统主类"""

    def __init__(self, config: SystemConfig):
        self.config = config
        self.modbus = ModbusClient(config)
        self.motion = MotionController(config, 0)  # 初始化时HC0为0
        self.display = DisplayManager()
        self.running = False
        # 自动恢复状态机标记
        self._has_stopped_after_defect = False

        #  新增：相机触发模式状态机
        self._current_trigger_mode = "Line0"   # "Line0"(硬触发) / "Software"(软触发预览)
        self._preview_last_trigger_time = 0.0

        #  新增：累计距离写回 PLC 的节流状态
        self._last_persist_time: float = 0.0
        self._last_persisted_distance: float = 0.0

        #  布料尺寸上报节流状态（配合 config.fabric_upload_interval）
        self._last_fabric_upload_time: float = 0.0

    def start(self):
        """启动系统"""
        # 显示配置
        info(f"S7-200 SMART 编码器触发控制系统配置参数:"
             f"  PLC地址: {self.config.plc_host}:{self.config.plc_port}"
             f"  读取间隔: {self.config.read_interval * 1000:.1f}ms"
             f"  触发距离: {self.config.trigger_distance}mm"
             f"  速度阈值: {self.config.speed_threshold_ratio * 100:.0f}%")

        # 连接到PLC
        if not self.modbus.connect():
            error("无法连接到PLC")
            return False
        info("PLC连接成功")

        #  新增：从 PLC 保持寄存器读回上次关闭/停电时的累计距离(mm)，
        #        作为本会话累计基准；与本会话新增距离累加。
        #        这样即便停电重启也能继续累计，除非前端点过「开始」(会同时把寄存器清零)。
        try:
            last_total = self.modbus.read_total_distance()
            if last_total is not None:
                self.motion.restore_base(last_total)
            else:
                warning("[启动] 读取 PLC 累计距离失败，本次从 0 起累计")
        except Exception as e:
            error(f"[启动] 读取 PLC 累计距离异常: {e}，本次从 0 起累计")

        self.running = True
        return True

    def stop(self):
        """停止系统"""
        self.running = False
        self.modbus.disconnect()

    def reset_total_distance(self) -> None:
        """完全重置累计距离（供前端点「开始」时由 PCL_reset 调用）。

        同步完成三件事：
          1) motion.reset_all()  —— 本会话内存累计 + base 全部归零
          2) write_total_distance(0) —— PLC 保持寄存器清零（保证停电重启仍是 0）
          3) 复位写回节流状态 —— 防止 reset 后第一帧因 delta 过大立刻误写
        """
        # 1) 内存重置
        self.motion.reset_all()
        # 2) PLC 持久化重置
        try:
            if not self.modbus.write_total_distance(0):
                error("[reset_total_distance] PLC 累计距离寄存器清零失败")
        except Exception as e:
            error(f"[reset_total_distance] PLC 累计距离寄存器清零异常: {e}")
        # 3) 节流状态同步归零
        self._last_persisted_distance = 0.0
        self._last_persist_time = time.time()
        info("[reset_total_distance] 累计距离已完全归零（内存 + PLC 持久化 + 节流状态）")

    def get_trigger_mode(self) -> str:
        """对外暴露当前相机触发模式，供其他模块（如相机回调、Thrift接口）查询"""
        return self._current_trigger_mode

    def is_preview_mode(self) -> bool:
        return self._current_trigger_mode == "Software"

    def _check_auto_resume(self, status: dict):
        """
        停机自动恢复状态机：
        阶段1（冷却期）: 停机后至少等待 MIN_COOL_DOWN 秒，忽略一切速度
        阶段2（确认停稳）: 观察到速度低于 STOP_SPEED 才认为"已停稳"
        阶段3（确认恢复）: 停稳后速度重新超过 RESUME_SPEED 才触发恢复

        说明：本状态机只负责"缺陷停机"逻辑标志(TC.isDefectStopped())的自动复位，
        与是否继续硬触发拍照检测(should_trigger/is_idle)完全独立，互不影响。
        """

        # 只对缺陷停机执行自动恢复，手动暂停不处理
        if not TC.isDefectStopped():
            return

        # --- 关键常量 ---
        MIN_COOL_DOWN = 2.0  # 停机后强制冷却时间(秒)，此期间无论速度多快都不恢复
        STOP_SPEED = 1.0  # 低于此速度视为"已停稳"(m/min)
        RESUME_SPEED = 5.0  # 停稳后高于此速度视为"速度恢复"(m/min)

        current_speed = status.get('current_speed', 0.0)
        stop_time = TC.getDefectStopTime()
        elapsed = time.time() - stop_time

        # 阶段1：冷却期，强制等待，防止读到停机前的惯性速度
        if elapsed < MIN_COOL_DOWN:
            info(f"[自动恢复] 冷却期 {elapsed:.1f}/{MIN_COOL_DOWN}s，"
                 f"当前速度 {current_speed:.2f} m/min，等待中...")
            # 重置"已停稳"标记，确保冷却期结束后重新从零判断
            self._has_stopped_after_defect = False
            return

        # 阶段2：等停稳
        if not self._has_stopped_after_defect:
            if current_speed <= STOP_SPEED:
                self._has_stopped_after_defect = True
                info(f"[自动恢复] 已确认停稳，速度 {current_speed:.2f} m/min，"
                     f"等待速度恢复到 {RESUME_SPEED} m/min 以上...")
            else:
                info(f"[自动恢复] 等待停稳，当前速度 {current_speed:.2f} m/min > {STOP_SPEED}...")
            return

        # 阶段3：已停稳，判断速度是否重新恢复
        if current_speed >= RESUME_SPEED:
            info(f"[自动恢复] 速度 {current_speed:.2f} m/min >= {RESUME_SPEED}，"
                 f"缺陷停机自动恢复运行")
            self._has_stopped_after_defect = False  # 重置状态，为下次停机做准备
            TC.setIsStop(False, "Auto恢复", "Auto")

    # ==================================================================
    #  新增：停机预览状态机
    # ==================================================================
    def _handle_idle_preview(self, status: dict):
        """
        根据织机真实运行/停止状态（is_idle），动态切换所有相机的触发源：
        - 织机停止：切换为 Software 软触发，低频出图仅供人工预览，
                    不进入疵点检测/布长统计（由相机回调侧负责过滤，见下方示例）
        - 织机恢复运行：立即切回 Line0 硬触发，恢复正常疵点检测

        注意：这里只做触发源切换和预览帧的软触发调度，
        不影响 should_trigger 相关的疵点检测触发逻辑（那部分完全独立，
        且现在两者使用的是同一个物理判据 is_idle，逻辑自洽）
        """
        is_idle = status.get('is_idle', False)

        if is_idle and self._current_trigger_mode != "Software":
            CameraRegistry.set_all_trigger_source("Software")
            self._current_trigger_mode = "Software"
            self._preview_last_trigger_time = 0.0  # 立刻触发一帧，不等待间隔
            info("[预览模式] 检测到织机停止，相机已切换为软触发预览模式")

        elif not is_idle and self._current_trigger_mode != "Line0":
            CameraRegistry.set_all_trigger_source("Line0")
            self._current_trigger_mode = "Line0"
            info("[预览模式] 检测到织机恢复运行，相机已切回硬触发模式")

        # 停机期间，按固定间隔发送一次软触发，刷新预览画面
        if self._current_trigger_mode == "Software":
            now = time.time()
            if now - self._preview_last_trigger_time >= self.config.preview_interval:
                CameraRegistry.trigger_all_software()
                self._preview_last_trigger_time = now

    def run(self):
        if not self.start():
            return

        set_running_system(self)
        encoder_state = get_encoder_state()
        consecutive_failures = 0
        MAX_FAILURES = 5  # 连续失败超过此数则重连

        try:
            while self.running:
                hc0 = self.modbus.read_hc0()

                if hc0 is None:
                    consecutive_failures += 1
                    error(f"读取HC0失败 ({consecutive_failures}/{MAX_FAILURES})")

                    # 连续失败则尝试重连
                    if consecutive_failures >= MAX_FAILURES:
                        warning("连续失败，尝试重连PLC...")
                        self.modbus.disconnect()
                        time.sleep(2.0)
                        if self.modbus.connect():
                            info("重连成功，继续运行")
                            consecutive_failures = 0
                        else:
                            error("重连失败，等待后重试...")
                            time.sleep(5.0)
                    else:
                        time.sleep(0.5)
                    continue

                # 读取成功，重置失败计数
                consecutive_failures = 0

                # 当前"缺陷停机"逻辑标志（仅用于自动恢复状态机判断/状态展示，
                #  不再传给 motion.update() 用于拦截 should_trigger）
                is_stopped = TC.getIsStop()

                #  修复：is_stopped 仅原样透传用于状态展示，
                #   should_trigger 的实际判定已改为只依赖 motion.is_idle（物理停机）
                should_trigger, status = self.motion.update(hc0, is_stopped=is_stopped)
                encoder_state.update(status)
                self._async_update_fabric(status)
                #  新增：节流把累计距离写回 PLC 保持寄存器，保证停电可恢复
                self._persist_total_distance(status['cumulative_distance'])
                self.display.display_status(status, trigger_mode=self._current_trigger_mode)

                #  停机预览状态机（与手动/缺陷停机的自动恢复相互独立，
                #  且与 should_trigger 使用同一个物理判据 is_idle，逻辑自洽）
                self._handle_idle_preview(status)

                self._check_auto_resume(status)

                #  修复：疵点检测触发只需要判断"当前是否硬触发模式" + "距离是否达到阈值"，
                #   不再额外用 TC.getIsStop() 二次拦截。
                #   原因：TC.getIsStop() 是缺陷停机的逻辑标志，可能因为一次疵点检测而长期为 True
                #   （要等 _check_auto_resume 三段式状态机走完才会复位），
                #   如果继续用它拦截，会导致织机仍在惯性转动、is_idle 还未确认为 True 的这段时间里，
                #   硬触发被提前掐断，造成"检测到一次疵点后就再也不出图/不检测"的假象。
                #   现在只要 self.motion.is_idle 还是 False（织机物理上没有真正停稳），
                #   should_trigger 就会按正常节拍继续为 True，硬触发持续进行，检测不中断。
                if self._current_trigger_mode == "Line0" and should_trigger:
                    if self.modbus.send_trigger():
                        self.display.display_trigger(status['trigger_count'])
                        if not self.modbus.send_reset():
                            self.display.display_error("发送重置命令失败")
                    else:
                        self.display.display_error("发送触发命令失败")

                time.sleep(self.config.read_interval)

        except KeyboardInterrupt:
            warning("\n检测到用户中断")
        except Exception as e:
            error(f"运行错误: {e}")
            import traceback
            traceback.print_exc()
        finally:
            set_running_system(None)
            self.stop()
            info("编码器触发控制系统已停止")

    def _async_update_fabric(self, status: dict):
        """异步上传布料尺寸，不阻塞主循环。

        节流：主循环每 100ms 一轮，原实现每轮都起线程+新建 TCP 连接
        POST 一次（10 请求/秒且停机期间不停），现按 fabric_upload_interval
        节流，到点才上报。密度数据(length/width)另由 periodic_report_service
        每 5s 上报，本接口仅作为布料尺寸的独立通道保留。
        """
        now = time.time()
        if now - self._last_fabric_upload_time < self.config.fabric_upload_interval:
            return
        self._last_fabric_upload_time = now

        def _upload():
            try:
                fabric_width = get_fabric_width()
                loop = asyncio.new_event_loop()
                loop.run_until_complete(
                    update_fabric_size(
                        (fabric_width or 0) * 10,
                        status['cumulative_distance']
                    )
                )
                loop.close()
            except Exception as e:
                error(f"上传布料信息失败: {e}")

        threading.Thread(target=_upload, daemon=True).start()

    def _persist_total_distance(self, current_distance_mm: float) -> None:
        """节流把累计距离(mm)写回 PLC 保持寄存器。

        触发条件（满足其一即写）：
          1) 距上次写入的时间间隔 >= persist_interval(秒)
          2) 距上次写入的距离增量 >= persist_min_delta_mm(mm)

        说明：写入失败只记日志，不影响主循环，下一周期会再尝试。
        """
        now_sec = time.time()
        delta = abs(current_distance_mm - self._last_persisted_distance)
        time_due = (now_sec - self._last_persist_time) >= self.config.persist_interval
        delta_due = delta >= self.config.persist_min_delta_mm

        if not (time_due or delta_due):
            return

        # 取整后写回（PLC 寄存器存的是 int32 mm 值）
        value_mm = int(current_distance_mm)
        if self.modbus.write_total_distance(value_mm):
            self._last_persisted_distance = current_distance_mm
            self._last_persist_time = now_sec
        # 写入失败时不更新 last_*，下一周期立即重试


# ========================================
# 程序入口
# ========================================
def ModbusHelperSlave():
    """程序入口"""
    # host = "192.168.123.70"
    # port = 502
    # diameter = 60.0
    # ppr = 2000
    # distance = 178.0 - 38  # mm

    interval = 100  # ms
    threshold = 0.0  # %
    scaling_factor = 0.78651  # 触发长度修正系数
    # 创建配置
    config = SystemConfig(
        plc_host="192.168.123.70",  # PLC TCP/IP 地址,
        plc_port=502,  # PLC Modbus(TCP) 端口地址
        wheel_diameter=60.0,  # 胶轮直径
        encoder_ppr=2000,  # 编码器脉冲数
        # 使用约0.786516的修正系数
        # 实际填写的触发距离(trigger_distance) ≈ 理论计算的画幅高 * 0.78651
        trigger_distance=102 * scaling_factor,
        scaling_factor=0.78651,
        # 实际的画幅高度下 理论触发脉冲数 = 画幅高度/ (60*3.14)/(2000*4)
        read_interval=interval / 1000.0,  # 转换为s
        speed_threshold_ratio=threshold / 100.0,  # 转换为小数
        #  新增配置，可按需调整
        idle_speed_threshold=0.5,
        idle_confirm_duration=3.0,
        preview_interval=1.0,
    )

    # 创建并运行系统
    system = TriggerControlSystem(config)
    system.run()
    # system.modbus.connect()
    # system.modbus.send_trigger()
    # system.modbus.send_reset()


if __name__ == "__main__":
    ModbusHelperSlave()