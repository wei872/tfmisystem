#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
布幅兜底值测试。

背景：布幅算不出来时 ``get_fabric_width()`` 返回 None，而两个消费方都是
``or 0``（``periodic_report_service`` 与 ``Modbus``），于是上游收到
``width=0``，会被当成断布/异常。现场要求这种情况下上报一个在
1580~1600mm 之间波动的值。

本文件重点守三件事：

1. **兜底值绝不能污染原始测量字段** ``last_fabric_width``。合成值混进
   测量字段，以后就再也分不清哪个是真测的了。
2. **一旦算出过真值就永久退出兜底**，即使之后帧全空白。
3. **一个刷新周期内所有读取方拿到同一个值**，否则 Modbus 上报和定时
   上报会对不上。

跑法::

    python -m pytest Test_Tool/test_fabric_width_fallback.py -v
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402
import yaml  # noqa: E402

from AnomalyDetection_Tool.analysis.fabric_width_detector import (  # noqa: E402
    FabricWidthDetector, _as_float,
)


# ------------------------------------------------------------------
# 夹具
# ------------------------------------------------------------------

# 贴近现场的配置：主 camera8(左)/camera1(右)，备 camera7(左)/camera2(右)
BASE_CFG = {
    "enabled": True,
    "primary_cameras": {"camera1": "right", "camera8": "left"},
    "fallback_cameras": {"camera2": "right", "camera7": "left"},
    "camera8_right_edge": 31.0,
    "camera8_left_edge": 0.6,
    "camera7_right_edge": 53.4,
    "camera7_left_edge": 19.0,
    "camera2_left_edge": 139.0,
    "camera2_right_edge": 168,
    "camera1_left_edge": 157.0,
    "camera1_right_edge": 178.6,
    "window_size": 20,
    "report_interval": 0,          # 关掉周期打印，免得测试输出被刷屏
    "fallback_width_min_mm": 1580,
    "fallback_width_max_mm": 1600,
    "fallback_refresh_sec": 5.0,
}


def make(**over):
    cfg = dict(BASE_CFG, **over)
    return FabricWidthDetector(cfg)


def feed_real_width(det, left_ratio=0.35, right_ratio=0.35):
    """走**真实**计算路径产出一个实测布幅。

    直接调 _append_ratio + _finalize，即 _calculate_width 的真身，
    而不是给 _last_fabric_width 赋值——那样测的是替身不是被测代码。
    """
    with det._lock:
        det._append_ratio("left", left_ratio)
        det._append_ratio("right", right_ratio)
        return det._finalize()


# ==================================================================
# §1 兜底值本身
# ==================================================================

class TestFallbackValue:

    def test_returns_value_when_never_computed(self):
        det = make()
        assert det.get_fabric_width() is not None

    def test_value_within_range(self):
        """必须在 1580~1600mm，即内部 158.0~160.0cm。

        内部一律用 cm，单位搞错就是 10 倍偏差，所以这里两头都钉。
        """
        det = make(fallback_refresh_sec=0.0)     # 每次读都重新取
        for _ in range(500):
            cm = det.get_fabric_width()
            assert 158.0 <= cm <= 160.0, f"{cm} cm 超出 158.0~160.0"
            assert 1580.0 <= cm * 10.0 <= 1600.0

    def test_actually_varies(self):
        """刷新周期为 0 时，多次读取必须真的波动，不能是个常数"""
        det = make(fallback_refresh_sec=0.0)
        vals = {round(det.get_fabric_width(), 6) for _ in range(50)}
        assert len(vals) > 1, f"50 次读取全是同一个值: {vals}"

    def test_stable_within_refresh_cycle(self):
        """刷新周期内必须恒定——Modbus 和定时上报跑在不同线程不同时刻"""
        det = make(fallback_refresh_sec=3600.0)
        vals = {det.get_fabric_width() for _ in range(20)}
        assert len(vals) == 1, f"同一周期内取到了不同值: {vals}"

    def test_refreshes_after_cycle(self, monkeypatch):
        """过了刷新周期要重新取值"""
        det = make(fallback_refresh_sec=5.0)
        first = det.get_fabric_width()
        clock = [1000.0]
        monkeypatch.setattr(
            "AnomalyDetection_Tool.analysis.fabric_width_detector"
            ".time.monotonic", lambda: clock[0])
        det._fb_generated_at = clock[0]
        det._fb_value_cm = first

        clock[0] += 4.9
        assert det.get_fabric_width() == first      # 还没到
        clock[0] += 0.2
        after = det.get_fabric_width()
        assert after is not None and 158.0 <= after <= 160.0
        assert det._fb_generated_at == clock[0]     # 时间戳已更新

    def test_boundary_values_inclusive(self):
        """min == max 时就是那个固定值"""
        det = make(fallback_width_min_mm=1590, fallback_width_max_mm=1590,
                   fallback_refresh_sec=0.0)
        for _ in range(20):
            assert det.get_fabric_width() == pytest.approx(159.0)


# ==================================================================
# §2 兜底值绝不能污染原始测量字段
# ==================================================================

class TestNoContamination:

    def test_last_fabric_width_stays_none(self):
        """原始测量字段必须仍是 None。

        合成值混进 last_fabric_width 之后，就再也分不清哪个是真测的了——
        这是这个功能最大的风险，所以单独守。
        """
        det = make()
        for _ in range(10):
            det.get_fabric_width()
        assert det._last_fabric_width is None
        assert det.get_stats()["last_fabric_width"] is None

    def test_stats_separates_measured_from_effective(self):
        det = make()
        det.get_fabric_width()
        s = det.get_stats()
        assert s["last_fabric_width"] is None            # 实测：没有
        assert s["fabric_width_effective"] is not None   # 生效：兜底
        assert s["fabric_width_is_fallback"] is True
        assert s["fallback_width_range_mm"] == [1580.0, 1600.0]

    def test_is_fallback_width_true(self):
        assert make().is_fallback_width() is True


# ==================================================================
# §3 一旦算出真值就永久退出兜底
# ==================================================================

class TestRealValueWins:

    def test_real_value_disables_fallback(self):
        det = make()
        assert det.get_fabric_width() != det._last_fabric_width  # 现在是兜底
        fw = feed_real_width(det)
        assert fw is not None
        assert det.get_fabric_width() == fw
        assert det.is_fallback_width() is False

    def test_real_value_is_physically_sane(self):
        """实测值应落在 140~170cm 这个量级，兜底范围 158~160 就在其中。

        这条同时验证了"1580~1600mm"这个兜底范围选得合理：
        满幅 178.6−0.6=178.0cm，减去两侧切除比后大约 147~162cm。
        """
        det = make()
        for lr in (0.2, 0.35, 0.5, 0.65):
            d2 = make()
            fw = feed_real_width(d2, lr, lr)
            assert 140.0 < fw < 175.0, f"切除比 {lr} 算出 {fw}cm，量级不对"
        assert det._last_fabric_width is None

    def test_fallback_never_returns_after_real_value(self):
        """之后即使帧全空白，也沿用真值，不退回兜底。

        这是"只在从未算出来时兜底"这条选择的直接后果。
        """
        det = make()
        fw = feed_real_width(det, 0.4, 0.4)
        for _ in range(5):
            assert det.get_fabric_width() == fw
        assert det.is_fallback_width() is False
        assert det.get_stats()["fabric_width_is_fallback"] is False


# ==================================================================
# §4 配置容错：配错不能把检测器搞崩
# ==================================================================

class TestConfigSafety:

    def test_disabled_when_both_zero(self):
        det = make(fallback_width_min_mm=0, fallback_width_max_mm=0)
        assert det._fb_enabled is False
        assert det.get_fabric_width() is None
        assert det.is_fallback_width() is False
        assert det.get_stats()["fallback_width_range_mm"] is None

    def test_disabled_when_keys_absent(self):
        """没配这两项 = 关闭兜底（保持改动前的行为）"""
        cfg = {k: v for k, v in BASE_CFG.items() if not k.startswith("fallback_width")}
        det = FabricWidthDetector(cfg)
        assert det._fb_enabled is False
        assert det.get_fabric_width() is None

    def test_disabled_when_min_above_max(self):
        det = make(fallback_width_min_mm=1600, fallback_width_max_mm=1580)
        assert det._fb_enabled is False
        assert det.get_fabric_width() is None

    def test_disabled_when_max_zero_only(self):
        det = make(fallback_width_min_mm=1580, fallback_width_max_mm=0)
        assert det._fb_enabled is False

    def test_non_numeric_config_does_not_crash(self):
        """配置写错不能让检测器构造失败——布幅起不来会连带影响经线密度"""
        det = make(fallback_width_min_mm="很宽", fallback_width_max_mm=1600)
        assert det._fb_min_mm == 0.0            # 回落默认
        assert det._fb_enabled is False
        assert det.get_fabric_width() is None

    def test_as_float_helper(self):
        assert _as_float("1580", 0.0) == 1580.0
        assert _as_float(1580, 0.0) == 1580.0
        assert _as_float(None, 7.0) == 7.0
        assert _as_float("abc", 7.0) == 7.0
        assert _as_float("", 7.0) == 7.0

    def test_disabled_detector_returns_none(self):
        det = FabricWidthDetector(dict(BASE_CFG, enabled=False))
        assert det.get_fabric_width() is None
        assert det.is_fallback_width() is False
        assert det.get_stats() == {"enabled": False}


# ==================================================================
# §5 消费方口径：上游拿到的是 mm
# ==================================================================

class TestConsumerUnits:
    """两个消费方都是 `cm * 10` 转 mm 上报，兜底值必须落在 1580~1600。

    这条守的是单位约定本身：内部 cm、上报 mm。搞反就是 10 倍错误。
    """

    def test_periodic_report_formula(self):
        """复刻 periodic_report_service 的算法：round(cm*10, 1)"""
        det = make(fallback_refresh_sec=0.0)
        for _ in range(200):
            cm = det.get_fabric_width() or 0.0
            width_mm = round(cm * 10.0, 1)
            assert 1580.0 <= width_mm <= 1600.0, width_mm

    def test_modbus_formula(self):
        """复刻 Modbus 的算法：(cm or 0) * 10"""
        det = make(fallback_refresh_sec=0.0)
        for _ in range(200):
            fw = det.get_fabric_width()
            assert 1580.0 <= (fw or 0) * 10 <= 1600.0

    def test_no_more_zero_reported(self):
        """改动的目的本身：算不出来时不再上报 0"""
        det = make()
        fw = det.get_fabric_width()
        assert (fw or 0) * 10 != 0


# ==================================================================
# §6 report_interval 守卫（顺带修掉的既有隐患）
# ==================================================================

class TestReportIntervalGuard:

    def test_zero_report_interval_does_not_raise(self):
        """report_interval=0 是"关掉周期打印"的自然写法，不能崩。

        改动前 _finalize 里是 ``total_frames % report_interval``，没有 > 0
        保护，配成 0 会在**每一帧、锁内**抛 ZeroDivisionError。
        """
        det = make(report_interval=0)
        assert feed_real_width(det) is not None
        for _ in range(5):
            feed_real_width(det)

    def test_negative_report_interval_does_not_raise(self):
        det = make(report_interval=-1)
        assert feed_real_width(det) is not None

    def test_report_still_fires_with_positive_interval(self):
        """别为了防崩把周期打印整个弄没了"""
        det = make(report_interval=2)
        calls = []
        det._print_report = lambda: calls.append(det._total_frames)
        feed_real_width(det)                       # 第 1 帧
        assert calls == []
        feed_real_width(det)                       # 第 2 帧
        assert calls == [2]


# ==================================================================
# §7 出厂 config.yaml
# ==================================================================

@pytest.fixture(scope="module")
def shipped():
    with open(os.path.join(_ROOT, "config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)["fabric_width"]


class TestShippedConfig:

    def test_keys_present_and_numeric(self, shipped):
        assert shipped["fallback_width_min_mm"] == 1580
        assert shipped["fallback_width_max_mm"] == 1600
        assert isinstance(shipped["fallback_refresh_sec"], (int, float))

    def test_range_is_valid(self, shipped):
        lo, hi = shipped["fallback_width_min_mm"], shipped["fallback_width_max_mm"]
        assert lo > 0 and hi >= lo, f"兜底范围无效: {lo}~{hi}"

    def test_detector_built_from_shipped_config(self, shipped):
        """用真实 config 构造检测器，兜底必须生效且落在范围内"""
        det = FabricWidthDetector(dict(shipped, report_interval=0))
        assert det._fb_enabled is True
        cm = det.get_fabric_width()
        lo = shipped["fallback_width_min_mm"] / 10.0
        hi = shipped["fallback_width_max_mm"] / 10.0
        assert lo <= cm <= hi
        assert det.get_stats()["fabric_width_is_fallback"] is True


# ==================================================================
# §8 真实上报路径 GlobalReportScheduler._build_density_payload
# ==================================================================
# periodic_report_service 可以脱离相机导入，所以这里测的是**现场真正
# 组装上报 payload 的那个方法**，确认兜底值确实以 mm 落到 width 字段上。

try:
    from AnomalyDetection_Tool.services.periodic_report_service import (  # noqa: E402
        GlobalReportScheduler, CameraReportContext,
        FABRIC_FALLBACK_WARN_INTERVAL,
    )
    from AnomalyDetection_Tool.output import fabric_width_service as _fws  # noqa: E402
    _SCHED_OK = True
except Exception as _e:                                  # pragma: no cover
    _SCHED_OK = False
    _SCHED_ERR = _e


class _StubCache:
    """warp/weft 缓存桩：返回"从未更新过"，让上报只关心布幅这一项"""

    def read(self):
        return (None, 0.0)


@pytest.mark.skipif(not _SCHED_OK, reason="periodic_report_service 无法导入")
class TestRealReportPath:

    @pytest.fixture
    def sched(self, monkeypatch):
        det = make()                      # 兜底开、从未算出真值
        monkeypatch.setattr(_fws, "_fabric_detector", det)
        monkeypatch.setattr(
            "AnomalyDetection_Tool.services.periodic_report_service"
            ".get_fabric_width", det.get_fabric_width)
        monkeypatch.setattr(
            "AnomalyDetection_Tool.services.periodic_report_service"
            ".get_fabric_stats", det.get_stats)
        return GlobalReportScheduler()

    def _ctx(self, name="camera1"):
        return CameraReportContext(camera_sn="SN001", camera_name=name,
                                   warp_cache=_StubCache(),
                                   weft_cache=_StubCache())

    def test_payload_width_is_fallback_in_mm(self, sched):
        """payload 的 width 字段必须落在 1580~1600mm，不再是 0"""
        p = sched._build_density_payload(self._ctx())
        assert p["width"] != 0.0, "改动的目的：不再上报 0"
        assert 1580.0 <= p["width"] <= 1600.0, p["width"]

    def test_payload_keys_unchanged(self, sched):
        """兜底不能改动上报字段集合——上游按固定 key 解析"""
        p = sched._build_density_payload(self._ctx())
        assert set(p) == {"cameraSn", "cameraName", "latDensity", "lngDensity",
                          "latSlope", "length", "width", "evennessValue"}

    def test_fallback_warning_is_throttled(self, sched, monkeypatch):
        """5s 一轮 × 8 台 ≈ 96 次/分钟，必须节流，否则刷满日志"""
        warns = []
        monkeypatch.setattr(
            "AnomalyDetection_Tool.services.periodic_report_service"
            ".warning", lambda *a, **k: warns.append(a[0] if a else ""))
        clock = [1_000_000.0]
        monkeypatch.setattr(
            "AnomalyDetection_Tool.services.periodic_report_service"
            ".time.time", lambda: clock[0])

        for _ in range(20):                       # 20 次上报，时间不动
            sched._build_density_payload(self._ctx())
        assert len(warns) == 1, f"20 次上报告警了 {len(warns)} 次，节流失效"
        assert "兜底合成值" in warns[0]

        clock[0] += FABRIC_FALLBACK_WARN_INTERVAL + 1
        sched._build_density_payload(self._ctx())
        assert len(warns) == 2, "过了节流间隔应当再告警一次"

    def test_no_fallback_warning_when_measured(self, sched, monkeypatch):
        """算出真值后不能再报"兜底合成值"，否则是假告警"""
        feed_real_width(sched and _fws._fabric_detector, 0.35, 0.35)
        warns = []
        monkeypatch.setattr(
            "AnomalyDetection_Tool.services.periodic_report_service"
            ".warning", lambda *a, **k: warns.append(a[0] if a else ""))
        p = sched._build_density_payload(self._ctx())
        assert not any("兜底合成值" in w for w in warns), warns
        assert 1400.0 < p["width"] < 1750.0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
