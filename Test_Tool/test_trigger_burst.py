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


# ------------------------------------------------- §5 两条触发路径都要下发错峰
class TestTriggerDelayWiring:
    """用假 SDK + 桩 _set_float 验证**接线**：Open_device 和 Set_trigger_source
    两处都必须下发 TriggerDelay。

    ⚠️ 假 SDK 的 C 层没有浮点特征（没有 gx_get_float_feature），所以这里把
    _set_float 打桩来记录调用。**相机真的收下了多少**这件事在本沙箱里验不了，
    只能靠现场日志确认。"""

    @staticmethod
    def _make(monkeypatch, camera_name="camera8", step=500):
        import fake_galaxy_sdk
        B = fake_galaxy_sdk.install()
        import gxipy as gx
        from Camera_Tool.CameraOperation import CameraOperation
        B.reset()
        B.add_camera("SNTEST", "MER2-500-14GM", "192.168.1.10", 32, 24,
                     fake_galaxy_sdk.PIXEL.BAYER_GB8)
        cam = CameraOperation(
            gx.DeviceManager(),
            {"index": 1, "sn": "SNTEST", "model_name": "MER2",
             "ip": "192.168.1.10"},
            camera_name=camera_name, trigger_stagger_us=step)
        calls = []
        monkeypatch.setattr(cam, "_set_float",
                            lambda n, v, readback=True: (calls.append((n, v)), 0)[1])
        return cam, calls

    def test_open_device_applies_the_delay(self, monkeypatch):
        cam, calls = self._make(monkeypatch)
        assert cam.Open_device() == 0
        assert ("TriggerDelay", 3500.0) in calls, calls      # camera8 → 7×500
        assert cam.trigger_delay_applied is True

    def test_set_trigger_source_reapplies_the_delay(self, monkeypatch):
        """⚠️ 关键：软触发是织机**停机预览**时才切过去的。如果切触发源时
        TriggerDelay 丢了，8 台会悄悄退回同时推流，而这恰好是最不容易被
        注意到的一条路径（停机时没人盯检测节拍）。"""
        cam, calls = self._make(monkeypatch)
        assert cam.Open_device() == 0
        n0 = len([c for c in calls if c[0] == "TriggerDelay"])
        assert n0 == 1, calls

        cam.Start_grabbing()
        assert cam.Set_trigger_source("Software") == 0
        n1 = len([c for c in calls if c[0] == "TriggerDelay"])
        assert n1 == 2, f"切到软触发后没有重下发 TriggerDelay: {calls}"

        assert cam.Set_trigger_source("Line0") == 0
        n2 = len([c for c in calls if c[0] == "TriggerDelay"])
        assert n2 == 3, f"切回硬触发后没有重下发 TriggerDelay: {calls}"

    def test_zero_stagger_never_touches_the_camera(self, monkeypatch):
        """默认 trigger_stagger_us=0 → 一次都不该碰相机（不改变现有行为）。"""
        cam, calls = self._make(monkeypatch, step=0)
        assert cam.Open_device() == 0
        assert [c for c in calls if c[0] == "TriggerDelay"] == []
        assert cam.trigger_delay_applied is False

    def test_camera1_gets_zero_delay(self, monkeypatch):
        """camera1 是基准，delay 必须为 0 —— 否则整组会被无谓地推迟。"""
        cam, calls = self._make(monkeypatch, camera_name="camera1", step=500)
        assert cam.trigger_delay_us == 0
        assert cam.Open_device() == 0
        assert [c for c in calls if c[0] == "TriggerDelay"] == []

    def test_unsupported_feature_does_not_block_startup(self, monkeypatch):
        """相机不支持该特征时：只告警，**绝不挡住启动**。
        不打桩 _set_float，直接用假 SDK（它没有浮点特征，等价于真机上
        遇到不支持的机型）。"""
        import fake_galaxy_sdk
        B = fake_galaxy_sdk.install()
        import gxipy as gx
        from Camera_Tool.CameraOperation import CameraOperation
        B.reset()
        B.add_camera("SNSUP", "MER2", "192.168.1.20", 32, 24,
                     fake_galaxy_sdk.PIXEL.BAYER_GB8)
        cam = CameraOperation(
            gx.DeviceManager(),
            {"index": 1, "sn": "SNSUP", "model_name": "MER2", "ip": "192.168.1.20"},
            camera_name="camera8", trigger_stagger_us=500)
        assert cam.Open_device() == 0, "不支持 TriggerDelay 就不该挡住启动"
        assert cam.trigger_delay_applied is False

    def test_log_does_not_claim_success_when_not_applied(self, monkeypatch):
        """回归：之前不看 _set_float 的返回值，下发被跳过却照样打
        "TriggerDelay=3500us" —— 现场会以为错峰生效了，其实 8 台仍同时推流。"""
        import fake_galaxy_sdk
        B = fake_galaxy_sdk.install()
        import gxipy as gx
        from Camera_Tool.CameraOperation import CameraOperation
        B.reset()
        B.add_camera("SNLOG", "MER2", "192.168.1.21", 32, 24,
                     fake_galaxy_sdk.PIXEL.BAYER_GB8)
        cam = CameraOperation(
            gx.DeviceManager(),
            {"index": 1, "sn": "SNLOG", "model_name": "MER2", "ip": "192.168.1.21"},
            camera_name="camera8", trigger_stagger_us=500)
        logs = []
        import Camera_Tool.CameraOperation as co
        monkeypatch.setattr(co, "info", lambda m: logs.append(m))
        assert cam.Open_device() == 0
        txt = "\n".join(logs)
        assert "未生效" in txt, txt
        # 不能出现"干净的成功串"（没有未生效标记的那种）
        assert "BufferCount=10, TriggerDelay=3500us)" not in txt, txt


# ------------------------------------------- §6 截断必须截**步长**，保持间距均匀
class TestStaggerStepClamp:
    """回归：截断单台延时会把顶部几台压成同一个值 → 间距 0 → 错峰**静默**失效。

    实测（修复前）trigger_stagger_us=4000 时：
        camera6/7/8 = 20000/20000/20000  →  间距 0，这三台又变回同时触发。
    这恰好是错峰要防的那件事，而且没有任何告警。
    """

    MAX_DELAY = 20000
    MAX_INDEX = 7
    MAX_STEP = MAX_DELAY // MAX_INDEX          # 2857

    def _delays(self, step):
        """返回 camera1..camera8 的 trigger_delay_us（不碰相机，只算）。"""
        import fake_galaxy_sdk
        B = fake_galaxy_sdk.install()
        import gxipy as gx
        from Camera_Tool.CameraOperation import CameraOperation
        B.reset()
        out = []
        for i in range(1, 9):
            B.add_camera("SN%d" % i, "MER2", "192.168.1.%d" % (10 + i),
                         32, 24, fake_galaxy_sdk.PIXEL.BAYER_GB8)
        mgr = gx.DeviceManager()
        for i in range(1, 9):
            c = CameraOperation(
                mgr, {"index": 1, "sn": "SN%d" % i, "model_name": "MER2",
                      "ip": "192.168.1.%d" % (10 + i)},
                camera_name="camera%d" % i, trigger_stagger_us=step)
            out.append(c.trigger_delay_us)
        return out

    def test_spacing_is_uniform_at_every_step(self):
        for step in (200, 500, 2000, self.MAX_STEP):
            d = self._delays(step)
            gaps = [d[i + 1] - d[i] for i in range(self.MAX_INDEX)]
            assert len(set(gaps)) == 1, f"step={step} 间距不均匀: {gaps}"
            assert gaps[0] == step, f"step={step} 间距={gaps[0]}"
            assert max(d) <= self.MAX_DELAY, f"step={step} 超上限: {d}"

    def test_oversized_step_is_reduced_not_collapsed(self):
        """⚠️ 核心回归：超配时**降步长**，绝不能让任何两台的间距变成 0。"""
        for step in (3000, 4000, 10000, 999999):
            d = self._delays(step)
            gaps = [d[i + 1] - d[i] for i in range(self.MAX_INDEX)]
            assert 0 not in gaps, f"step={step} 出现间距 0（错峰静默失效）: {d}"
            assert len(set(gaps)) == 1, f"step={step} 间距不均匀: {gaps}"
            assert gaps[0] == self.MAX_STEP, f"step={step} 未降到上限: {gaps}"
            assert max(d) <= self.MAX_DELAY

    def test_camera678_are_never_simultaneous(self):
        """专门盯住修复前出事的那三台。"""
        for step in (4000, 999999):
            d = self._delays(step)
            top3 = d[5:8]                       # camera6/7/8
            assert len(set(top3)) == 3, \
                f"step={step} camera6/7/8 延时相同={top3}，又同时触发了"

    def test_oversized_step_warns(self, monkeypatch):
        import fake_galaxy_sdk
        B = fake_galaxy_sdk.install()
        import gxipy as gx
        from Camera_Tool import CameraOperation as co
        B.reset()
        B.add_camera("SNW", "MER2", "192.168.1.30", 32, 24,
                     fake_galaxy_sdk.PIXEL.BAYER_GB8)
        logs = []
        monkeypatch.setattr(co, "warning", lambda m: logs.append(m))
        co.CameraOperation(
            gx.DeviceManager(),
            {"index": 1, "sn": "SNW", "model_name": "MER2", "ip": "192.168.1.30"},
            camera_name="camera8", trigger_stagger_us=999999)
        assert any("过大" in m for m in logs), logs

    def test_out_of_range_index_falls_back_to_zero(self, monkeypatch):
        """索引必须落在 0..7。越界一律按 0（这台不错峰），
        绝不能算出一个让相机永远不出图的延时。"""
        import fake_galaxy_sdk
        B = fake_galaxy_sdk.install()
        import gxipy as gx
        from Camera_Tool import CameraOperation as co
        B.reset()
        B.add_camera("SNX", "MER2", "192.168.1.31", 32, 24,
                     fake_galaxy_sdk.PIXEL.BAYER_GB8)
        logs = []
        monkeypatch.setattr(co, "warning", lambda m: logs.append(m))
        monkeypatch.setattr(co, "_camera_index_from_name", lambda n: 99)
        c = co.CameraOperation(
            gx.DeviceManager(),
            {"index": 1, "sn": "SNX", "model_name": "MER2", "ip": "192.168.1.31"},
            camera_name="camera9", trigger_stagger_us=2857)
        assert c.trigger_delay_us == 0, c.trigger_delay_us
        assert any("超出" in m for m in logs), logs

    def test_shipped_config_value_is_within_ceiling(self):
        """config.yaml 里出厂值必须 ≤ 步长上限，否则一开机就告警。"""
        import yaml
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "config.yaml"), encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        step = cfg["cameras"]["acquisition"]["trigger_stagger_us"]
        assert isinstance(step, int), type(step)
        assert 0 < step <= self.MAX_STEP, \
            f"trigger_stagger_us={step} 超出步长上限 {self.MAX_STEP}"
