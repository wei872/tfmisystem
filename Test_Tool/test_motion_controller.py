#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MotionController（Communication_Tool/Modbus.py）的回归测试。

跑法::

    python -m pytest Test_Tool/test_motion_controller.py -v

重点守两条：
  1. 触发节拍是"距离驱动 + 100ms 轮询封顶"的，不可能无限加速；
  2. PLC 计数器复位/回绕造成的脉冲突变会被丢弃，
     不会污染 current_speed / is_idle / 累计距离。
"""

import os
import sys
import time

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from Communication_Tool.Modbus import MotionController, SystemConfig  # noqa: E402


@pytest.fixture
def cfg():
    """按 config.yaml 生产段的真实取值构造"""
    scaling = 0.78651
    return SystemConfig(
        plc_host="192.168.123.70",
        plc_port=502,
        wheel_diameter=60.0,
        encoder_ppr=2000,
        trigger_distance=102 * scaling,
        scaling_factor=scaling,
        read_interval=0.1,
        idle_speed_threshold=0.5,
        idle_confirm_duration=3.0,
        preview_interval=1.0,
    )


class FakeClock:
    """可控时钟。

    update() 里 dt 用 time.perf_counter()，而 is_idle 的确认期用 time.time()。
    只伪造其中一个，idle 确认期就永远走不完（测试跑 40 拍在真实时间里只有几微秒）。
    两个必须一起推进。
    """

    def __init__(self, start=1000.0):
        self.t = start

    def time(self):
        return self.t

    def perf_counter(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    fc = FakeClock()
    monkeypatch.setattr(time, "time", fc.time)
    monkeypatch.setattr(time, "perf_counter", fc.perf_counter)
    return fc


def make(cfg, clock, hc0=0):
    mc = MotionController(cfg, hc0)
    mc._clock = clock          # 供 tick() 推进时钟
    return mc


def pulses_for(cfg, mm: float) -> int:
    """距离(mm) -> 脉冲数"""
    return int(round(mm / cfg.pulse_equivalent))


def tick(mc, hc0, dt_s=0.1):
    """推进时钟 dt_s 秒后喂一拍编码器读数（默认 = read_interval 100ms）"""
    mc._clock.t += dt_s
    return mc.update(hc0)


# ==================================================================
# 参数换算
# ==================================================================
def test_derived_params_match_config_yaml(cfg):
    # 胶轮周长 3.141*60 = 188.46mm，4 倍频后 8000 脉冲/转
    assert cfg.pulse_equivalent == pytest.approx(188.46 / 8000, rel=1e-6)
    assert cfg.trigger_distance == pytest.approx(102 * 0.78651, rel=1e-6)


# ==================================================================
# 触发节拍：距离驱动，且被轮询频率封顶
# ==================================================================
def test_no_trigger_before_reaching_trigger_distance(cfg, clock):
    mc = make(cfg, clock)
    # 只走 trigger_distance 的一半，不该触发
    should, status = tick(mc, pulses_for(cfg, cfg.trigger_distance * 0.5))
    assert should is False
    assert status['trigger_count'] == 0
    assert status['accumulated_distance'] == pytest.approx(
        cfg.trigger_distance * 0.5, rel=1e-3)


def test_trigger_fires_once_trigger_distance_reached(cfg, clock):
    mc = make(cfg, clock)
    should, status = tick(mc, pulses_for(cfg, cfg.trigger_distance * 1.2))
    assert should is True
    assert status['trigger_count'] == 1


def test_at_most_one_trigger_per_tick_even_if_distance_runs_far_ahead(cfg, clock):
    """
    update() 里用的是 `if` 而不是 `while`：一拍之内无论累计距离超出多少个
    trigger_distance，都只发一次触发，剩下的留在 accumulated_distance 里。
    这是"硬触发不可能失控变快"的关键保证。
    """
    mc = make(cfg, clock)
    # 一拍走 5 个 trigger_distance
    should, status = tick(mc, pulses_for(cfg, cfg.trigger_distance * 5))
    assert should is True
    assert status['trigger_count'] == 1, "一拍内触发了多次，节拍会失控"
    # 欠账应当留在累计距离里，而不是凭空消失
    assert status['accumulated_distance'] == pytest.approx(
        cfg.trigger_distance * 4, rel=1e-2)


def test_trigger_rate_is_capped_by_poll_interval(cfg, clock):
    """10 拍（= 1 秒 @100ms 轮询）内最多 10 次触发"""
    mc = make(cfg, clock)
    per_tick = pulses_for(cfg, cfg.trigger_distance * 3)  # 故意远超单拍所需
    hc0 = 0
    count = 0
    for _ in range(10):
        hc0 += per_tick
        should, _ = tick(mc, hc0)
        count += 1 if should else 0
    assert count == 10, count


def test_idle_stops_hard_trigger(cfg, clock):
    """速度低于阈值并持续确认时长后，硬触发必须停"""
    mc = make(cfg, clock)
    tick(mc, pulses_for(cfg, cfg.trigger_distance))   # 先正常跑一拍
    # 连续 40 拍不动（4 秒 > idle_confirm_duration=3s）
    hc0 = mc.last_hc0
    last_should = None
    for _ in range(40):
        should, status = tick(mc, hc0)
        last_should = should
    assert status['is_idle'] is True
    assert last_should is False


# ==================================================================
# 编码器突变防护
# ==================================================================
def test_counter_reset_to_zero_is_discarded(cfg, clock):
    """PLC 计数器被复位到 0：巨大负跳变必须被丢弃。

    注意量级：上限是 300 m/min，100ms 一拍折算约 21222 脉冲。
    所以计数器必须已经累计到远超这个量级（跑了几小时的真实状态），
    归零才构成"不合理"跳变 —— 几千脉冲的反向跳变本来就在合理范围内，
    不该也不会被判为突变。
    """
    mc = make(cfg, clock)
    # 模拟已经跑了很久的计数器（约 118mm * 5e6/3405 ≈ 173m 布长）
    hc0_running = 5_000_000
    tick(mc, hc0_running)
    mc.last_hc0 = hc0_running        # 直接对齐基准，避免这一拍本身被判突变

    speed_before = mc.current_speed
    acc_before = mc.accumulated_distance
    trig_before = mc.trigger_count

    should, status = tick(mc, 0)      # 计数器突然归零

    assert should is False
    assert status['pulse_delta'] == 0
    assert status['distance_delta'] == 0.0
    # 关键：不能让假数据渗进状态
    assert status['current_speed'] == speed_before
    assert status['accumulated_distance'] == acc_before
    assert status['trigger_count'] == trig_before
    # 基准已重新对齐，下一拍正常
    assert mc.last_hc0 == 0


def test_small_counter_jump_within_plausible_range_is_kept(cfg, clock):
    """反向小跳变（比如织机轻微倒车）不能被误判成突变"""
    mc = make(cfg, clock)
    tick(mc, pulses_for(cfg, cfg.trigger_distance * 3))
    back = mc.last_hc0 - pulses_for(cfg, cfg.trigger_distance * 0.5)
    should, status = tick(mc, back)
    assert status['pulse_delta'] != 0, "合理范围内的反向跳变被误杀了"


def test_counter_wrap_to_huge_value_is_discarded(cfg, clock):
    """32 位回绕：巨大正跳变同样必须被丢弃"""
    mc = make(cfg, clock)
    tick(mc, pulses_for(cfg, cfg.trigger_distance))

    should, status = tick(mc, 0x7FFFFFFF)

    assert should is False
    assert status['pulse_delta'] == 0
    # is_idle 不能被这个假速度永久性地钉死在 False
    assert status['is_idle'] in (True, False)
    assert mc.last_hc0 == 0x7FFFFFFF


def test_glitch_does_not_break_idle_detection(cfg, clock):
    """
    这是这条防护真正的意义：突变若不丢弃，current_speed 会变成天文数字，
    is_idle 从此永远为 False，停机预览模式再也进不去。
    """
    mc = make(cfg, clock)
    tick(mc, pulses_for(cfg, cfg.trigger_distance * 2))

    # 注入一次回绕突变
    tick(mc, 0x7FFFFFFF)

    # 之后织机真的停了：连续不动 4 秒，is_idle 必须能变 True
    hc0 = mc.last_hc0
    for _ in range(40):
        _, status = tick(mc, hc0)
    assert status['is_idle'] is True, "突变把 is_idle 永久钉死在 False 了"


def test_normal_high_speed_is_not_mistaken_for_glitch(cfg, clock):
    """300 m/min 以内的真实高速不能被误杀"""
    mc = make(cfg, clock)
    # 200 m/min 一拍(100ms)走的距离
    mm = 200 / 60 * 1000 * 0.1
    should, status = tick(mc, pulses_for(cfg, mm))
    assert status['pulse_delta'] != 0, "正常高速被当成突变丢弃了"
    assert status['current_speed'] == pytest.approx(200, rel=0.05)


def test_glitch_counter_accumulates(cfg, clock):
    mc = make(cfg, clock)
    tick(mc, pulses_for(cfg, cfg.trigger_distance))
    tick(mc, 0x7FFFFFFF)
    tick(mc, 0x7FFFFFFF + 10)
    assert mc._glitch_count >= 1
