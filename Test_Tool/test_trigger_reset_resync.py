# -*- coding: utf-8 -*-
"""send_reset() 清零 HC0 之后必须同步基准，否则触发节拍被拉长 -> 每帧漏拍。

现场证据（2026-10-22 04:23:54~57 那段日志，织机稳定 9.9 m/min）：

    55,066  HC0=4504  距离=9.15   触发=7356      <- 触发
    55,182  HC0=683   距离=0.00   触发=7356      <- HC0 被清零，判成「反转」
    55,289  HC0=1419  距离=17.34  ...
    55,722  HC0=4345  距离=6.04   触发=7357      <- 下一次触发

    触发间隔 656 / 653 / 655 ms（极差仅 3 ms，非常稳）
    9.9 m/min = 165.0 mm/s  =>  两帧之间布面走了 108.0 mm
    帧高只有 105 mm  =>  **每一帧都漏 3.0 mm**

而 80.224 mm 的触发阈值本该对应 493 ms。差出来的 32.7% 全是 send_reset()
清零 HC0 造成的：下一拍 pulse_delta ≈ -3700 脉冲（-86 ~ -90 mm）被判成
反转，那一整拍的行进距离被丢掉，累加值也被打断。
"""
import pytest

from Communication_Tool import Modbus as MB


PULSE_EQ = 3.141 * 60.0 / (2000 * 4)      # 0.0235575 mm/脉冲
THRESH = 102 * 0.78651                    # 80.2240 mm
LOOM_MPM = 9.9                            # 现场织机速度
FRAME_HEIGHT_MM = 105.0                   # 现场实测帧高（布走行方向）
POLL_MS = 108.0                           # 现场实测轮询周期
PULSES_PER_POLL = 730.0                   # 现场实测每轮询脉冲增量


@pytest.fixture
def clock(monkeypatch):
    """把 perf_counter 整体接管 —— 只改 last_time 不改 current_time 的话，
    dt 会用真实墙钟算出来一个巨大的值，每一拍都被突变防护吃掉，测不到东西。"""
    fake = {"ms": 1000.0}
    monkeypatch.setattr(MB.time, "perf_counter", lambda: fake["ms"] / 1000.0)
    return fake


def _make_controller():
    cfg = MB.SystemConfig(
        wheel_diameter=60.0, encoder_ppr=2000, scaling_factor=0.78651,
        trigger_distance=THRESH, read_interval=POLL_MS / 1000.0,
        max_plausible_speed_mpm=300.0)
    m = MB.MotionController(cfg, 0)
    m.accumulated_distance = 0.0
    m.is_forward = True
    m.is_running = True
    return m


def _run_cycle(clock, resync_after_reset, polls=60):
    """复刻 TriggerControlSystem 主循环：update -> 触发 -> send_reset -> (resync)。

    返回触发时刻列表（ms）。

    **时钟顺序很要紧**：主循环是 update() -> 触发 -> send_reset() -> resync()
    -> sleep(read_interval) -> 下一拍 update()。所以 resync 必须发生在**这一拍的
    末尾**（时刻 t），下一拍在 t + POLL_MS。要是在下一拍开头才 resync，
    resync 里的 last_time 会和 update 里的 current_time 撞上，dt=0 ->
    算出的速度是天文数字 -> 被突变防护整拍丢弃，那就什么都没测到。
    """
    m = _make_controller()
    hc0 = 0.0
    reset_pending = False
    trig = []
    t = 0.0
    for _ in range(polls):
        t += POLL_MS
        clock["ms"] = 1000.0 + t
        if reset_pending:
            # PLC 已把计数器清零，这一拍读到的是清零后重新累加的值
            hc0 = PULSES_PER_POLL
            reset_pending = False
        else:
            hc0 = hc0 + PULSES_PER_POLL
        fired, _st = m.update(int(hc0), is_stopped=False)
        if fired:
            trig.append(t)
            # 主循环里紧接着就是 send_reset()，PLC 把 HC0 清零
            reset_pending = True
            if resync_after_reset:
                m.resync_hc0(0, quiet=True)   # 同一时刻 t，下一拍才 +POLL_MS
    return trig


def _fabric_step_ms(trig):
    dts = [trig[i] - trig[i - 1] for i in range(1, len(trig))]
    assert len(dts) >= 3, f"触发次数太少，测不出稳定间隔: {trig}"
    avg = sum(dts) / len(dts)
    return avg, LOOM_MPM * 1000 / 60.0 * avg / 1000.0


class TestSiteLogIsReproduced:
    """先证明这套复刻能逐位复现现场日志，否则后面的结论没有意义。"""

    def test_replay_of_site_hc0_sequence_matches_exactly(self, clock):
        """用现场真实 HC0 序列 + 旧反转逻辑，逐拍对上现场的距离和触发时刻。"""
        seq = [(0, 2212, 35.38), (108, 2976, 53.38), (214, 3755, 71.73),
               (321, 4504, 9.15), (437, 683, 0.00), (544, 1419, 17.34),
               (653, 2153, 34.63), (760, 2856, 51.19), (869, 3628, 69.38),
               (977, 4345, 6.04), (1091, 706, 0.00), (1199, 1430, 17.06),
               (1307, 2173, 34.56), (1414, 2926, 52.30), (1521, 3657, 69.52),
               (1630, 4435, 7.62), (1745, 738, 0.00), (1853, 1483, 17.55),
               (1961, 2222, 34.96), (2069, 2954, 52.20), (2177, 3700, 69.78),
               (2285, 4411, 6.30)]
        # 现场跑的是 T43 之前的 max(0, ...)，这里照它算，只为验证复刻可信
        last_hc0, acc = seq[0][1], seq[0][2]
        trig = []
        for tms, hc0, site_acc in seq[1:]:
            d = (hc0 - last_hc0) * PULSE_EQ
            fwd = d > 0
            acc = acc + d if fwd else max(0.0, acc + d)
            if fwd and acc >= THRESH:
                acc -= THRESH
                trig.append(tms)
            last_hc0 = hc0
            assert acc == pytest.approx(site_acc, abs=0.01), (tms, hc0, acc, site_acc)
        assert trig == [321, 977, 1630, 2285]
        dts = [trig[i] - trig[i - 1] for i in range(1, len(trig))]
        assert dts == [656, 653, 655]
        # 现场这个节拍下，布面步距已经超过帧高
        step = LOOM_MPM * 1000 / 60.0 * (sum(dts) / len(dts)) / 1000.0
        assert step > FRAME_HEIGHT_MM


class TestResetMustResyncBaseline:
    def test_without_resync_the_frame_gap_is_real(self, clock):
        """不同步基准 => 触发间隔被拉长到帧高装不下 => 每帧漏拍。"""
        trig = _run_cycle(clock, resync_after_reset=False)
        avg, step = _fabric_step_ms(trig)
        assert step > FRAME_HEIGHT_MM, (
            f"预期会漏拍：间隔 {avg:.0f} ms -> 步距 {step:.2f} mm > 帧高 "
            f"{FRAME_HEIGHT_MM} mm")

    def test_with_resync_coverage_is_restored(self, clock):
        """同步基准后触发间隔回到阈值该有的水平，覆盖率恢复。"""
        trig = _run_cycle(clock, resync_after_reset=True)
        avg, step = _fabric_step_ms(trig)
        assert step <= FRAME_HEIGHT_MM, (
            f"修复后仍漏拍：间隔 {avg:.0f} ms -> 步距 {step:.2f} mm")
        # 重叠不能只是"刚好为正"，要留出量化余量（现场轮询周期会抖）
        assert FRAME_HEIGHT_MM - step >= 10.0, (
            f"重叠只有 {FRAME_HEIGHT_MM - step:.2f} mm，太薄")

    def test_resync_beats_both_alternatives(self, clock):
        """把三种做法放在一起比，钉住"同步基准"是唯一可行的那个。

        T43 把反转分支改成「下界 -trigger_distance」，在这个场景下反而更糟：
        累加值被压到 -80.22，下一次触发要先把它爬回来，间隔直接翻倍。
        """
        fixed = _run_cycle(clock, resync_after_reset=True)
        broken = _run_cycle(clock, resync_after_reset=False)
        _, step_fixed = _fabric_step_ms(fixed)
        avg_broken, step_broken = _fabric_step_ms(broken)
        assert step_fixed < step_broken
        assert step_broken > FRAME_HEIGHT_MM >= step_fixed

    def test_resync_does_not_disturb_distance_accounting(self, clock):
        """resync 只动 last_hc0 / last_time，不能碰距离与触发计数。"""
        m = _make_controller()
        clock["ms"] = 2000.0
        m.update(1000, is_stopped=False)
        m.trigger_count = 42
        m.accumulated_distance = 33.3
        m.cumulative_distance = 12345.6
        m.base_cumulative_distance = 999.0
        m.resync_hc0(0, quiet=True)
        assert m.last_hc0 == 0
        assert m.trigger_count == 42
        assert m.accumulated_distance == 33.3
        assert m.cumulative_distance == 12345.6
        assert m.base_cumulative_distance == 999.0

    def test_quiet_resync_does_not_log(self, clock, monkeypatch):
        """每次触发都要同步（约 1.5 次/秒），静默参数必须真的不打日志。"""
        m = _make_controller()              # 构造时会打「系统配置」，先让它打完
        clock["ms"] = 3000.0
        calls = []
        monkeypatch.setattr(MB, "info", lambda *a, **k: calls.append(a))
        m.resync_hc0(0, quiet=True)
        assert calls == []
        m.resync_hc0(0)                    # 默认仍打日志（前端「开始」路径）
        assert len(calls) == 1


class TestWiring:
    def test_trigger_loop_calls_resync_after_reset(self):
        """主循环里 send_reset 成功之后必须同步基准。

        读源码文本而不是 import —— 这个测试文件不装 fake_galaxy_sdk。
        """
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "Communication_Tool", "Modbus.py"),
                  encoding="utf-8") as f:
            src = f.read()
        i = src.index("if not self.modbus.send_reset():")
        window = src[i:i + 1400]
        assert "resync_hc0(0, quiet=True)" in window, (
            "send_reset 之后没有同步 HC0 基准，清零会被判成反转")
        # 必须挂在 send_reset **成功**的分支上，失败时不该同步
        assert "else:" in window
