# -*- coding: utf-8 -*-
"""触发错峰（TriggerDelay）+ 触发突发探针的回归测试。

背景：8 台相机接同一根 Line0 触发线，一次触发 8 台同时推流。实测数据：
一次突发上线 38.1 MB、BGR 114.4 MB 在 8 条 SDK 回调线程上同时分配。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from General_Tool import TriggerBurstProbe as tbp          # noqa: E402


# ------------------------------------------------------------------ §1 相机序号
class TestCameraIndex:
    """错峰靠相机序号算 TriggerDelay，序号解析错 = 延时算错。"""

    @staticmethod
    def _fn():
        # CameraOperation 顶层会 import gxipy，非 Windows 上导入不了；
        # 这里只取纯函数，用源码切片执行，避免把整个模块拉起来。
        import re as _re
        src = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "Camera_Tool", "CameraOperation.py"), encoding="utf-8").read()
        c0 = src.index("_MAX_TRIGGER_DELAY_US = ")
        c1 = src.index("\n\n\n", c0)
        f0 = src.index("def _camera_index_from_name")
        f1 = src.index("def _ip_to_int")
        ns = {"re": _re}
        exec(src[c0:c1], ns)
        exec(src[f0:f1], ns)
        return ns["_camera_index_from_name"], ns["_MAX_TRIGGER_DELAY_US"]

    def test_camera_names_map_to_zero_based_index(self):
        fn, _ = self._fn()
        assert [fn(f"camera{i}") for i in range(1, 9)] == list(range(8))

    def test_variants_accepted(self):
        fn, _ = self._fn()
        assert fn("camera_3") == 2
        assert fn("Camera7") == 6
        assert fn(" camera2 ") == 1

    def test_serial_number_must_not_be_parsed_as_index(self):
        """⚠️ 关键回归：camera_name 缺省会退化成序列号。
        用 re.search 抓第一串数字时，"KBA26070032" 会解析成 26070032，
        TriggerDelay 变成天文数字 → 相机触发后一直等、永远不出图。"""
        fn, _ = self._fn()
        for sn in ("KBA26070032", "KBA26070064", "FA123456789"):
            assert fn(sn) == 0, f"序列号 {sn} 被当成了相机序号"

    def test_unparseable_returns_zero(self):
        fn, _ = self._fn()
        for bad in ("", None, "weird", "camera"):
            assert fn(bad) == 0


# ------------------------------------------------------------------ §2 聚类
class TestClusterBursts:
    def test_empty(self):
        assert tbp.TriggerBurstProbe.cluster_bursts([]) == []

    def test_eight_simultaneous_form_one_burst(self):
        t0 = 1000.0
        items = [(t0 + i * 0.0004, f"camera{i+1}") for i in range(8)]
        b = tbp.TriggerBurstProbe.cluster_bursts(items)
        assert len(b) == 1 and len(b[0]) == 8

    def test_two_triggers_481ms_apart_are_separate(self):
        t0 = 1000.0
        items = [(t0 + i * 0.0004, f"camera{i+1}") for i in range(8)]
        items += [(t0 + 0.481 + i * 0.0004, f"camera{i+1}") for i in range(8)]
        b = tbp.TriggerBurstProbe.cluster_bursts(items)
        assert len(b) == 2

    def test_unsorted_input_is_handled(self):
        """同一次触发的到达记录不保证有序（8 条 SDK 线程并发追加）。
        ⚠️ 间隔要用**亚毫秒**：聚类阈值 _BURST_GAP_SEC 是 0.1s，
        用几百毫秒的间隔本来就该被拆成多次触发。"""
        items = [(1000.0005, "camera3"), (1000.0, "camera1"),
                 (1000.0002, "camera2")]
        b = tbp.TriggerBurstProbe.cluster_bursts(items)
        assert len(b) == 1
        assert [c for _, c in b[0]] == ["camera1", "camera2", "camera3"]


# ------------------------------------------------------------------ §3 探针
class TestProbe:
    def _probe(self, **kw):
        out = []
        p = tbp.TriggerBurstProbe(logger=out.append, **kw)
        p._pool_info_fn = lambda: None       # 不碰真实的系统调用
        return p, out

    def test_detects_simultaneous_arrival_and_reports_spread(self):
        p, out = self._probe()
        t0 = 1000.0
        for i in range(8):
            p.record_arrival(f"camera{i+1}", t0 + i * 0.0004)   # 2.8 ms 内
        res = p.analyse_and_report()
        assert res["multi"] == 1
        assert res["max_size"] == 8
        assert 2.0 < res["max_spread_ms"] < 4.0, res
        txt = "\n".join(out)
        assert "8 台" in txt and "几乎同时" in txt

    def test_single_camera_arrival_is_not_reported(self):
        p, out = self._probe()
        for k in range(5):
            p.record_arrival("camera1", 1000.0 + k)
        res = p.analyse_and_report()
        assert res["multi"] == 0
        assert not any("几乎同时" in l for l in out)

    def test_well_spread_arrivals_do_not_warn(self):
        """错峰之后到达间隔应该拉开，不该再报"几乎同时"。"""
        p, out = self._probe()
        t0 = 1000.0
        for i in range(8):
            p.record_arrival(f"camera{i+1}", t0 + i * 0.002)     # 每台隔 2ms
        res = p.analyse_and_report()
        assert res["multi"] == 1
        assert res["max_spread_ms"] > 5.0
        assert not any("几乎同时" in l for l in out)

    def test_disabled_probe_costs_nothing_and_records_nothing(self):
        p, out = self._probe(enabled=False)
        for i in range(100):
            p.record_arrival(f"camera{i%8+1}")
        assert p.total_arrivals == 0
        assert p.analyse_and_report() == {
            "bursts": 0, "multi": 0, "max_size": 0,
            "max_spread_ms": 0.0, "arrivals": 0}

    def test_record_arrival_never_raises(self):
        p, _ = self._probe()
        p.record_arrival(None)                       # 名字是 None
        p.record_arrival("camera1", "not-a-float")   # 时间戳是垃圾
        p.record_arrival("camera1", float("nan"))
        assert True                                  # 没抛就算过

    def test_overflow_is_reported_not_lost_silently(self):
        p, out = self._probe()
        n = tbp._MAX_RECORDS + 50
        for i in range(n):
            p.record_arrival(f"camera{i%8+1}", 1000.0 + i * 1e-6)
        p.analyse_and_report(force=True)
        assert any("溢出" in l for l in out), out

    def test_start_is_idempotent_and_stop_is_safe(self):
        p, _ = self._probe(report_interval=60)
        p.start()
        p.start()                       # 幂等
        assert p._thread is not None
        p.stop()
        p.stop()                        # 重复 stop 不炸


# ------------------------------------------------------------------ §4 热路径
class TestHotPath:
    def test_record_arrival_is_cheap(self):
        """跑在 SDK 的 C 回调线程上，8 台 × 2fps。必须便宜。"""
        p = tbp.TriggerBurstProbe(enabled=True)
        p._pool_info_fn = lambda: None
        n = 20000
        t = time.perf_counter()
        for i in range(n):
            p.record_arrival("camera1", 1000.0 + i * 1e-6)
        per = (time.perf_counter() - t) / n * 1e6
        assert per < 20.0, f"单次 record_arrival {per:.2f} us，太贵了"

    def test_module_level_entry_is_noop_before_init(self):
        tbp.shutdown()
        assert tbp.get_probe() is None
        tbp.record_arrival("camera1")          # 不该抛
        assert tbp.get_probe() is None
