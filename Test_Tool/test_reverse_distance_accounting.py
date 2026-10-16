"""反向增量的距离账目测试。

背景
----
``MotionController.update()`` 里原来是这么处理反向的::

    self.accumulated_distance = max(0, self.accumulated_distance + distance_delta)

一旦反向增量超过当前累加值，超出的部分就被**永久丢掉**。而
``accumulated_distance`` 同时是「距上次触发走了多远」的账本，
``cumulative_distance``（总行进距离）又完全由它推导::

    base + trigger_count * trigger_distance + accumulated_distance

所以这里丢多少，总行进距离和触发节拍就一起错多少。

顺带说明：**这不是 HC0 回绕**。32 位回绕的增量是 -2,147,483,648 脉冲，
16 位是 -65,536 脉冲，而现场观测到的量级只有个位数脉冲，差了近 8 个数量
级；而且真发生回绕的话 300 m/min 的突变防护会先把整拍丢掉并重新对齐基准，
根本走不到这个钳位。把这两件事分开，否则查的人会盯着错误的方向。

这些测试全部直接驱动真实的 ``MotionController``，不 mock 任何计算。
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Communication_Tool.Modbus import MotionController, SystemConfig  # noqa: E402


# 现场实际取值（main_yolo.py:662 从 config.yaml 的 plc 段构造，与此一致）
TRIGGER_DISTANCE = 102 * 0.78651          # 80.22402 mm
READ_INTERVAL_MS = 100.0


@pytest.fixture
def mc():
    cfg = SystemConfig(trigger_distance=TRIGGER_DISTANCE,
                       read_interval=READ_INTERVAL_MS / 1000.0)
    return MotionController(cfg, 0)


def step(mc, hc0):
    """推一拍，并把时钟回拨成真实的 100 ms 节拍。

    必须手工设 last_time：``update()`` 用的是 ``time.perf_counter()``，
    连续调用之间只有几微秒，突变防护会把每一拍都当成计数器突变丢掉
    （第一版探针就是这么"通过"的——它测的是一条被短路掉的路径）。
    """
    mc.last_time = time.perf_counter() * 1000 - READ_INTERVAL_MS
    return mc.update(hc0, False)


PE = TRIGGER_DISTANCE  # 占位，真正的脉冲当量在测试里从 config 取


class TestDeficitIsCarried:
    """核心修复：反向的亏空要记下来，不能钳成 0。"""

    def test_deficit_survives(self, mc):
        pe = mc.config.pulse_equivalent
        fwd, back = 3000, -5000
        step(mc, fwd)
        _, st = step(mc, fwd + back)

        expected = (fwd + back) * pe
        assert st["accumulated_distance"] == pytest.approx(expected)
        # 修复前这里是 0.0，亏掉 47.115 mm
        assert st["accumulated_distance"] < 0

    def test_no_clamp_loss_when_within_one_trigger(self, mc):
        step(mc, 3000)
        _, st = step(mc, 3000 - 5000)
        # -47.1 mm 的亏空在一个触发距离(80.2 mm)以内，不该被截断
        assert st["reverse_clamp_count"] == 0
        assert st["reverse_clamp_loss_mm"] == 0.0
        # 前提：亏空(47.1 mm)在一个触发距离(80.2 mm)以内。
        # 注意反向**增量**本身是 117.8 mm，比触发距离大——要看的是亏空。
        assert abs(st["accumulated_distance"]) < mc.config.trigger_distance

    def test_deficit_reported_in_status(self, mc):
        step(mc, 3000)
        _, st = step(mc, 3000 - 5000)
        assert st["reverse_deficit_mm"] == pytest.approx(
            st["accumulated_distance"])
        assert st["reverse_deficit_mm"] < 0

    def test_deficit_zero_when_forward(self, mc):
        _, st = step(mc, 3000)
        assert st["reverse_deficit_mm"] == 0.0


class TestCumulativeDistance:
    """总行进距离必须跟上真实净前进，而不是被钳位抬高。"""

    def test_tracks_true_net_travel_after_reverse(self, mc):
        pe = mc.config.pulse_equivalent
        hc0 = 0
        step(mc, 3000)
        hc0 = 3000 - 5000
        step(mc, hc0)
        # 再往前走 3 个触发距离
        for _ in range(15):
            hc0 += int(0.2 * TRIGGER_DISTANCE / pe)
            _, st = step(mc, hc0)

        true_net = hc0 * pe
        # 修复前这里会差 47.115 mm（钳位把亏空抹掉，触发提前）
        assert st["cumulative_distance"] == pytest.approx(true_net, abs=0.05)

    def test_trigger_spacing_is_one_trigger_distance(self, mc):
        pe = mc.config.pulse_equivalent
        step(mc, 3000)
        hc0 = 3000 - 5000
        step(mc, hc0)

        positions = []
        step_pulses = max(1, int(0.05 * TRIGGER_DISTANCE / pe))
        for _ in range(400):
            hc0 += step_pulses
            trig, _ = step(mc, hc0)
            if trig:
                positions.append(hc0 * pe)
            if len(positions) >= 3:
                break

        assert len(positions) == 3
        gaps = [b - a for a, b in zip(positions, positions[1:])]
        for g in gaps:
            # 步长是整数脉冲，量化误差最大就是一个步长
            assert g == pytest.approx(TRIGGER_DISTANCE,
                                      abs=step_pulses * pe + 1e-6)

    def test_first_trigger_waits_out_the_deficit(self, mc):
        """亏空 47.1 mm 时，第一次触发必须等到真实位置越过 80.2 mm。"""
        pe = mc.config.pulse_equivalent
        step(mc, 3000)
        hc0 = 3000 - 5000
        step(mc, hc0)
        deficit = mc.accumulated_distance          # -47.115

        start = hc0 * pe            # 反转结束时的真实位置
        step_pulses = max(1, int(0.02 * TRIGGER_DISTANCE / pe))
        travelled = None
        for _ in range(2000):
            hc0 += step_pulses
            trig, _ = step(mc, hc0)
            if trig:
                travelled = hc0 * pe - start
                break

        assert travelled is not None
        # 从反转后的位置算起，要走满 td + |亏空| 才轮到下一次触发
        assert travelled >= TRIGGER_DISTANCE + abs(deficit) - step_pulses * pe
        # 而绝对位置回到 td 附近（因为亏空正好把之前的超前抵消掉）
        assert hc0 * pe == pytest.approx(TRIGGER_DISTANCE,
                                         abs=step_pulses * pe + 1e-6)


class TestClampFloor:
    """下界 = 一个触发距离：亏空不能无限期压住触发。"""

    def test_floor_is_one_trigger_distance(self, mc):
        pe = mc.config.pulse_equivalent
        # 反向幅度要远超一个触发距离，但**必须小于突变上限**
        # （dt=100ms 时上限是 21225 脉冲 / 500 mm），否则整拍会被突变防护
        # 丢掉，根本走不到钳位这条分支。
        back = 15000                       # 353.4 mm，约 4.4 个触发距离
        assert back < 300 / 60 * 1000 * 0.1 / pe
        step(mc, 1000)
        _, st = step(mc, 1000 - back)

        assert st["accumulated_distance"] == pytest.approx(
            -TRIGGER_DISTANCE, abs=1e-6)

    def test_excess_beyond_floor_is_counted(self, mc):
        pe = mc.config.pulse_equivalent
        back = 15000
        step(mc, 1000)
        _, st = step(mc, 1000 - back)

        assert st["glitch_discard_count"] == 0     # 确实走到了钳位分支
        assert st["reverse_clamp_count"] == 1
        # 丢掉的 = 真实亏空 - 下界
        true_deficit = 1000 * pe + (-back) * pe
        expected_loss = abs(true_deficit) - TRIGGER_DISTANCE
        assert st["reverse_clamp_loss_mm"] == pytest.approx(
            expected_loss, abs=0.05)

    def test_floor_bounds_the_blind_spot(self, mc):
        """触发最多被压住一个触发距离，不会无限期哑火。"""
        pe = mc.config.pulse_equivalent
        back = 15000
        step(mc, 1000)
        hc0 = 1000 - back
        step(mc, hc0)

        d = max(1, int(0.02 * TRIGGER_DISTANCE / pe))
        fired_at = None
        travelled = 0.0
        for _ in range(2000):
            hc0 += d
            travelled += d * pe
            trig, _ = step(mc, hc0)
            if trig:
                fired_at = travelled
                break

        assert fired_at is not None
        # 最多欠一个触发距离 = 2 个触发距离的行程，再加一个步长的量化误差
        assert fired_at <= 2 * TRIGGER_DISTANCE + d * pe
        # 而且确实被压住了（不是立刻触发）：至少走了一个触发距离以上
        assert fired_at > TRIGGER_DISTANCE


class TestGlitchAccounting:
    """突变防护整拍丢弃时，丢掉的距离要单独记账。"""

    def test_glitch_discard_is_counted(self, mc):
        pe = mc.config.pulse_equivalent
        # 一拍跳 40 万脉冲 -> 折算速度远超 300 m/min，判为突变
        _, st = step(mc, 400000)
        assert st["glitch_discard_count"] == 1
        assert st["glitch_discard_mm"] == pytest.approx(400000 * pe, abs=0.01)

    def test_glitch_does_not_touch_accumulated(self, mc):
        step(mc, 3000)
        before = mc.accumulated_distance
        _, st = step(mc, 400000)
        assert st["accumulated_distance"] == pytest.approx(before)
        assert st["distance_delta"] == 0

    def test_glitch_baseline_realigned(self, mc):
        """丢掉之后基准要对齐，下一拍不能再算成突变。"""
        step(mc, 400000)
        _, st = step(mc, 400000 + 1000)
        assert st["glitch_discard_count"] == 1      # 没有再增加
        assert st["pulse_delta"] == 1000

    def test_glitch_accumulates_across_events(self, mc):
        pe = mc.config.pulse_equivalent
        step(mc, 400000)
        step(mc, 400000 + 1000)
        _, st = step(mc, 400000 + 1000 + 500000)
        assert st["glitch_discard_count"] == 2
        assert st["glitch_discard_mm"] == pytest.approx(
            (400000 + 500000) * pe, abs=0.01)


class TestStatusShape:
    """新字段在两条返回路径上都必须在——上层读状态字典时不该分叉。"""

    FIELDS = ("reverse_deficit_mm", "reverse_clamp_count",
              "reverse_clamp_loss_mm", "glitch_discard_count",
              "glitch_discard_mm")

    def test_normal_branch_has_all_fields(self, mc):
        _, st = step(mc, 3000)
        for f in self.FIELDS:
            assert f in st, f

    def test_glitch_branch_has_all_fields(self, mc):
        _, st = step(mc, 400000)
        for f in self.FIELDS:
            assert f in st, f


class TestReset:
    """账目计数器随会话归零，不能把上一卷布的丢弃量算到这一卷头上。"""

    def test_reset_all_zeroes_counters(self, mc):
        step(mc, 400000)                      # 触发一次突变记账
        step(mc, 400000 + 3000)
        step(mc, 400000 + 3000 - 15000)       # 触发一次钳位记账（未超突变上限）
        assert mc.glitch_discard_count == 1
        assert mc.reverse_clamp_count == 1
        assert mc.reverse_clamp_loss_mm > 0

        mc.reset_all()
        assert mc.reverse_clamp_count == 0
        assert mc.reverse_clamp_loss_mm == 0.0
        assert mc.glitch_discard_count == 0
        assert mc.glitch_discard_mm == 0.0
        assert mc.accumulated_distance == 0.0

    def test_reset_keeps_negative_accumulator_out(self, mc):
        """reset 之后累加值必须回到 0，不能留着上一卷的亏空。"""
        step(mc, 3000)
        step(mc, 3000 - 5000)
        assert mc.accumulated_distance < 0
        mc.reset_all()
        assert mc.accumulated_distance == 0.0


class TestNoRegression:
    """纯正向的既有行为必须一模一样——这是最常见的工况。"""

    def test_forward_only_unchanged(self, mc):
        pe = mc.config.pulse_equivalent
        hc0 = 0
        for _ in range(20):
            hc0 += 1000
            _, st = step(mc, hc0)
        # 纯正向时，累加值 = 总距离扣掉已触发的整数个触发距离
        assert st["accumulated_distance"] == pytest.approx(
            hc0 * pe - st["trigger_count"] * TRIGGER_DISTANCE, abs=1e-6)
        assert st["cumulative_distance"] == pytest.approx(hc0 * pe, abs=0.05)
        assert st["reverse_clamp_count"] == 0
        assert st["glitch_discard_count"] == 0
        assert st["reverse_deficit_mm"] == 0.0

    def test_trigger_count_matches_distance(self, mc):
        pe = mc.config.pulse_equivalent
        hc0 = 0
        for _ in range(200):
            hc0 += 500
            _, st = step(mc, hc0)
        expected = int(hc0 * pe // TRIGGER_DISTANCE)
        assert st["trigger_count"] == expected

    def test_small_jitter_does_not_clamp(self, mc):
        """编码器 ±几脉冲的抖动不该触发钳位，也不该丢距离。"""
        pe = mc.config.pulse_equivalent
        hc0 = 0
        for i in range(200):
            hc0 += 1000 + (-3 if i % 7 == 0 else 0)
            _, st = step(mc, hc0)
        assert st["reverse_clamp_count"] == 0
        assert st["reverse_clamp_loss_mm"] == 0.0
        assert st["cumulative_distance"] == pytest.approx(hc0 * pe, abs=0.05)


class TestNotAWrap:
    """钉住"这不是回绕"这个结论，免得下一个人又往那个方向查。"""

    def test_real_32bit_wrap_is_caught_by_glitch_guard(self, mc):
        """真回绕的增量是 -2^31 量级，会被突变防护整拍丢弃并记账。"""
        # 注意：从 last_hc0=0 跳到 2e9 本身就是一次突变，所以回绕这一拍
        # 是第二次。这里要的就是"两拍都被判为突变、都不进反向分支"。
        step(mc, 2_000_000_000)
        _, st = step(mc, -2_000_000_000)
        assert st["glitch_discard_count"] == 2
        assert st["pulse_delta"] == 0
        # 它不会走到反向钳位那条路上
        assert st["reverse_clamp_count"] == 0

    def test_observed_magnitude_is_not_a_wrap(self, mc):
        """现场观测到的 -2.7 脉冲量级，与任何位宽的回绕都差了几个数量级。"""
        wrap32 = 2 ** 31
        wrap16 = 2 ** 16
        observed = 2.7
        assert wrap32 / observed > 1e8
        assert wrap16 / observed > 2e4

    def test_small_reverse_reaches_the_clamp_path(self, mc):
        """小反向增量能安然穿过突变防护——所以钳位那条路真的会被走到。"""
        step(mc, 3000)
        _, st = step(mc, 3000 - 5000)
        assert st["glitch_discard_count"] == 0     # 没被判成突变
        assert st["pulse_delta"] == -5000          # 真的进了反向分支


class TestDisplayVisibility:
    """账目必须出现在现场看得见的状态行上（「漏了多少」不能是黑箱）。

    同时钉住另一半要求：**正常工况不能加噪音**，否则这行日志很快就没人看了。
    """

    def _render(self, mc, hc0, caplog):
        from Communication_Tool.Modbus import DisplayManager
        _, st = step(mc, hc0)
        dm = DisplayManager.__new__(DisplayManager)
        dm.last_update_time = 0
        dm.update_interval = 0
        import Communication_Tool.Modbus as M
        seen = []
        orig = M.info
        M.info = lambda msg: seen.append(msg)
        try:
            DisplayManager.display_status(dm, st, "硬触发")
        finally:
            M.info = orig
        assert seen, "display_status 没有输出任何内容"
        return seen[-1]

    def test_clean_run_adds_nothing(self, mc):
        out = self._render(mc, 3000, None)
        assert "距离账目" not in out

    def test_deficit_is_shown(self, mc):
        step(mc, 3000)
        out = self._render(mc, 3000 - 5000, None)
        assert "距离账目" in out
        assert "当前亏空" in out

    def test_clamp_loss_is_shown(self, mc):
        step(mc, 1000)
        out = self._render(mc, 1000 - 15000, None)
        assert "反向截断" in out
        assert "次" in out

    def test_glitch_discard_is_shown(self, mc):
        out = self._render(mc, 400000, None)
        assert "突变丢弃" in out
