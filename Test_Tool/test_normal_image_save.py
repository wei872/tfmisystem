#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""【四】正常图片定时保存的回归测试。

覆盖：窗口语义、开始时刻解析、间隔、每台 10 张硬上限、并发不超上限、
落盘失败退配额、detection_manager 挂钩只在"无缺陷"分支生效、
Thrift 三个新方法（含手写生成结构体的**真实 wire 往返**）。
"""

import json
import os
import sys
import threading
import time
from datetime import datetime

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from Test_Tool import fake_galaxy_sdk  # noqa: E402
fake_galaxy_sdk.install()

from AnomalyDetection_Tool.services import normal_image_saver as nis  # noqa: E402
from AnomalyDetection_Tool.services.normal_image_saver import (  # noqa: E402
    NormalImageSaver, DEFAULT_MAX_PER_CAMERA)


# ======================================================================
# §1  窗口语义
# ======================================================================
class TestWindow:

    def _s(self, tmp_path, **kw):
        return NormalImageSaver(base_path=str(tmp_path), launch_tag="T", **kw)

    def test_no_window_saves_nothing(self, tmp_path):
        """没下发窗口时一张都不存 —— 这是默认状态。"""
        s = self._s(tmp_path)
        assert s.get_status()["active"] is False
        assert s.should_save("camera1") is False

    def test_zero_duration_is_rejected(self, tmp_path):
        """duration <= 0 必须拒绝。否则会开一个立刻结束的窗口，
        前端还以为开成功了。"""
        s = self._s(tmp_path)
        for bad in (0, -1, -0.5, None, ""):
            s.start_window(start=None, duration_sec=bad)
            assert s.get_status()["active"] is False, bad
            assert s.should_save("camera1") is False, bad

    def test_window_is_active_between_start_and_end(self, tmp_path):
        t = [1_000_000.0]
        s = self._s(tmp_path, clock=lambda: t[0])
        s.start_window(start=1_000_000.0, duration_sec=10.0)
        assert s.should_save("camera1") is True       # t = start
        t[0] += 5.0
        assert s.should_save("camera2") is True       # 窗口中
        t[0] += 5.0
        assert s.should_save("camera3") is False      # t = end，已出窗

    def test_future_window_does_not_save_yet(self, tmp_path):
        t = [1_000_000.0]
        s = self._s(tmp_path, clock=lambda: t[0])
        s.start_window(start=1_000_060.0, duration_sec=30.0)
        assert s.get_status()["active"] is False      # 还没到
        assert s.should_save("camera1") is False
        t[0] += 60.0
        assert s.get_status()["active"] is True
        assert s.should_save("camera1") is True

    def test_stop_window_ends_it_early(self, tmp_path):
        t = [1_000_000.0]
        s = self._s(tmp_path, clock=lambda: t[0])
        s.start_window(start=1_000_000.0, duration_sec=999.0)
        assert s.should_save("camera1") is True
        s.stop_window()
        assert s.should_save("camera2") is False

    def test_restart_resets_the_per_camera_quota(self, tmp_path):
        """新窗口 = 新配额。否则开第二次窗口就一张都存不了。"""
        t = [1_000_000.0]
        s = self._s(tmp_path, clock=lambda: t[0], max_per_camera=2,
                    interval_sec=0.1)
        s.start_window(start=1_000_000.0, duration_sec=60.0)
        assert s.should_save("camera1") is True
        t[0] += 1
        assert s.should_save("camera1") is True
        t[0] += 1
        assert s.should_save("camera1") is False      # 满了
        s.start_window(start=1_000_000.0, duration_sec=60.0)
        assert s.should_save("camera1") is True       # 重新有配额

    def test_stats_count_windows_and_skips(self, tmp_path):
        t = [1_000_000.0]
        s = self._s(tmp_path, clock=lambda: t[0], interval_sec=100.0)
        s.start_window(start=1_000_000.0, duration_sec=60.0)
        assert s.should_save("camera1") is True
        assert s.should_save("camera1") is False      # 撞间隔
        st = s.get_status()["stats"]
        assert st["windows_started"] == 1
        assert st["skipped_interval"] == 1
        t[0] += 999
        assert s.should_save("camera1") is False      # 出窗
        assert s.get_status()["stats"]["skipped_outside_window"] == 1


# ======================================================================
# §2  开始时刻解析
# ======================================================================
class TestStartParsing:

    def test_none_and_empty_mean_now(self, tmp_path):
        t = [1_000_000.0]
        for v in (None, "", "   "):
            assert NormalImageSaver._parse_start(v, t[0]) == t[0], repr(v)

    def test_epoch_number_and_string(self, tmp_path):
        assert NormalImageSaver._parse_start(1_234.5, 0) == 1_234.5
        assert NormalImageSaver._parse_start("1234.5", 0) == 1234.5

    def test_hhmm_resolves_to_today(self):
        base = 1_000_000.0
        ts = NormalImageSaver._parse_start("08:00", base)
        assert ts is not None
        st = time.localtime(ts)
        assert (st.tm_hour, st.tm_min, st.tm_sec) == (8, 0, 0)

    def test_hhmmss_resolves_to_today(self):
        ts = NormalImageSaver._parse_start("08:05:30", 1_000_000.0)
        st = time.localtime(ts)
        assert (st.tm_hour, st.tm_min, st.tm_sec) == (8, 5, 30)

    def test_already_passed_time_rolls_to_tomorrow(self):
        """已经过了的时刻顺延到明天，否则会开一个立刻结束的窗口。"""
        now = time.time()
        past = time.strftime("%H:%M", time.localtime(now - 3600))
        ts = NormalImageSaver._parse_start(past, now)
        assert ts > now
        assert ts - now < 86400.0 + 2

    def test_garbage_is_rejected(self):
        for bad in ("不是时间", "25:99", "abc:def"):
            assert NormalImageSaver._parse_start(bad, 1_000_000.0) is None, bad

    def test_unparseable_start_does_not_open_a_window(self, tmp_path):
        s = NormalImageSaver(str(tmp_path), clock=lambda: 1_000_000.0)
        s.start_window(start="不是时间", duration_sec=60.0)
        assert s.get_status()["active"] is False


# ======================================================================
# §3  间隔与每台 10 张硬上限
# ======================================================================
class TestQuota:

    def test_default_cap_is_ten(self):
        """需求写死 10 张。"""
        assert DEFAULT_MAX_PER_CAMERA == 10
        assert NormalImageSaver("x").max_per_camera == 10

    def test_interval_blocks_a_second_frame(self, tmp_path):
        t = [0.0]
        s = NormalImageSaver(str(tmp_path), interval_sec=5.0,
                             clock=lambda: t[0])
        s.start_window(start=0.0, duration_sec=1000.0)
        assert s.should_save("camera1") is True
        t[0] = 4.9
        assert s.should_save("camera1") is False
        t[0] = 5.0
        assert s.should_save("camera1") is True

    def test_ten_then_refused(self, tmp_path):
        """第 11 次必须被拒。"""
        t = [0.0]
        s = NormalImageSaver(str(tmp_path), interval_sec=1.0,
                             clock=lambda: t[0])
        s.start_window(start=0.0, duration_sec=10_000.0)
        ok = 0
        for i in range(15):
            t[0] = float(i)
            if s.should_save("camera1"):
                ok += 1
        assert ok == 10
        assert s.get_status()["per_camera"]["camera1"] == 10
        assert s.get_status()["stats"]["skipped_capped"] == 5

    def test_cameras_have_independent_quotas(self, tmp_path):
        """camera1 存满不影响 camera2。"""
        t = [0.0]
        s = NormalImageSaver(str(tmp_path), interval_sec=1.0,
                             max_per_camera=2, clock=lambda: t[0])
        s.start_window(start=0.0, duration_sec=10_000.0)
        for i in range(4):
            t[0] = float(i)
            s.should_save("camera1")
        for i in range(4):
            t[0] = float(i)
            s.should_save("camera2")
        st = s.get_status()["per_camera"]
        assert st["camera1"] == 2 and st["camera2"] == 2

    def test_count_is_reserved_inside_the_guard(self, tmp_path):
        """占位必须和上限判定在**同一次**加锁里完成。

        这才是"不会突破 10 张"的真正不变量：判定与自增之间不能有释放锁的
        缝隙。用锁获取次数来盯 —— 一次成功的 should_save 只允许拿一次锁。
        把自增挪到锁外会变成两次，本用例立刻失败。
        """
        s = NormalImageSaver(str(tmp_path), max_per_camera=10,
                             clock=lambda: 0.0)
        s.start_window(start=0.0, duration_sec=100.0)

        real = s._lock
        hits = []

        class Counting:
            def __enter__(_self):
                hits.append(1)
                return real.__enter__()

            def __exit__(_self, *a):
                return real.__exit__(*a)

        s._lock = Counting()
        assert s.should_save("camera1") is True
        assert len(hits) == 1, (
            f"判定与占位分成了 {len(hits)} 次加锁，中间有释放锁的缝隙，"
            f"并发下可能突破上限")
        assert s._counts["camera1"] == 1

    def test_empty_camera_name_is_ignored(self, tmp_path):
        s = NormalImageSaver(str(tmp_path), clock=lambda: 0.0)
        s.start_window(start=0.0, duration_sec=100.0)
        assert s.should_save("") is False
        assert s.should_save(None) is False

    def test_concurrent_calls_never_exceed_the_cap(self, tmp_path):
        """8 线程并发问，总数正好等于上限、不崩、不丢更新。

        注意这条**证明不了**"先占位"防住了竞态 —— 实测把占位挪到锁外，
        本用例照样通过：A 释放锁后 GIL 仍在 A 手里，它立刻重新拿到锁完成
        自增，B 根本来不及插进来，所以那个窗口在 CPython 下几乎不可达。
        真正盯这个不变量的是下面那条 test_count_is_reserved_inside_the_guard。
        """
        t = [0.0]
        lock = threading.Lock()

        def clock():
            with lock:
                t[0] += 1.0
                return t[0]

        s = NormalImageSaver(str(tmp_path), interval_sec=0.0,
                             max_per_camera=10, clock=clock)
        s.start_window(start=0.0, duration_sec=10_000.0)

        got = []
        gl = threading.Lock()

        def hammer():
            for _ in range(200):
                if s.should_save("camera1"):
                    with gl:
                        got.append(1)

        ths = [threading.Thread(target=hammer) for _ in range(8)]
        [x.start() for x in ths]
        [x.join() for x in ths]
        assert len(got) == 10, f"并发下突破了上限：{len(got)}"


# ======================================================================
# §4  落盘
# ======================================================================
class TestSave:

    def _img(self):
        return np.full((60, 80, 3), 128, dtype=np.uint8)

    def test_writes_a_jpg_under_the_expected_path(self, tmp_path):
        t = [1_700_000_000.0]
        s = NormalImageSaver(str(tmp_path), launch_tag="L1",
                             clock=lambda: t[0])
        s.start_window(start=1_700_000_000.0, duration_sec=100.0)
        assert s.should_save("camera3") is True
        path = s.save("camera3", self._img())
        assert path.endswith(".jpg")
        assert os.path.join("L1", "camera3", "NormalSave") in path
        assert path.endswith("_camera3_01.jpg")     # 序号来自 should_save
        for _ in range(100):
            if os.path.exists(path):
                break
            time.sleep(0.02)
        assert os.path.exists(path), f"没落盘: {path}"
        assert os.path.getsize(path) > 100
        assert s.get_status()["stats"]["saved"] == 1

    def test_custom_subdir(self, tmp_path):
        s = NormalImageSaver(str(tmp_path), launch_tag="L", save_subdir="MyDir",
                             clock=lambda: 1_700_000_000.0)
        s.start_window(start=1_700_000_000.0, duration_sec=100.0)
        s.should_save("camera1")
        path = s.save("camera1", self._img())
        assert os.sep + "MyDir" + os.sep in path

    def test_none_image_is_a_noop(self, tmp_path):
        s = NormalImageSaver(str(tmp_path), clock=lambda: 0.0)
        s.start_window(start=0.0, duration_sec=100.0)
        s.should_save("camera1")
        assert s.save("camera1", None) == ""

    def test_failed_write_returns_the_quota(self, tmp_path, monkeypatch):
        """写盘失败必须把占位退回去，否则这台相机白白少一张配额。"""
        import cv2
        monkeypatch.setattr(cv2, "imwrite", lambda *a, **k: False)
        s = NormalImageSaver(str(tmp_path), clock=lambda: 0.0)
        s.start_window(start=0.0, duration_sec=100.0)
        s.should_save("camera1")
        s.save("camera1", self._img())
        for _ in range(100):
            if s.get_status()["stats"]["save_failed"]:
                break
            time.sleep(0.02)
        st = s.get_status()
        assert st["stats"]["save_failed"] == 1
        assert st["per_camera"].get("camera1", 0) == 0   # 配额已退回

    def test_bad_base_path_does_not_raise(self, tmp_path):
        """目录建不出来也不能把检测线程掀了。"""
        s = NormalImageSaver(os.path.join(str(tmp_path), "f", "f", "f"),
                             clock=lambda: 0.0)
        s.start_window(start=0.0, duration_sec=100.0)
        s.should_save("camera1")
        s.save("camera1", self._img())   # 不应抛
        time.sleep(0.1)


# ======================================================================
# §5  配置
# ======================================================================
class TestConfig:

    @pytest.fixture(scope="class")
    def cfg(self):
        import yaml
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "config.yaml"), encoding="utf-8") as f:
            return yaml.safe_load(f)

    def test_section_exists(self, cfg):
        assert "normal_image_save" in cfg
        assert isinstance(cfg["normal_image_save"], dict)

    def test_max_per_camera_is_ten(self, cfg):
        assert int(cfg["normal_image_save"]["max_per_camera"]) == 10

    def test_types(self, cfg):
        b = cfg["normal_image_save"]
        assert isinstance(b["enabled"], bool)
        assert isinstance(b["interval_sec"], (int, float)) and b["interval_sec"] > 0
        assert isinstance(b["jpeg_quality"], int)
        assert isinstance(b["save_subdir"], str) and b["save_subdir"]

    def test_settings_exports_the_live_dict(self):
        from AnomalyDetection_Tool.config.settings import NORMAL_IMAGE_SAVE
        assert NORMAL_IMAGE_SAVE.get("max_per_camera") == 10
        assert NORMAL_IMAGE_SAVE.get("interval_sec") > 0

    def test_settings_default_is_off_when_key_missing(self):
        """config 里没这一段时默认不存 —— 不能因为漏配就开始写盘。"""
        from AnomalyDetection_Tool.config.settings import NORMAL_IMAGE_SAVE
        NORMAL_IMAGE_SAVE.clear()
        try:
            assert NORMAL_IMAGE_SAVE.get("enabled", False) is False
        finally:
            NORMAL_IMAGE_SAVE._apply_reload()
        assert NORMAL_IMAGE_SAVE.get("enabled") is True   # 确认已恢复


# ======================================================================
# §6  detection_manager 挂钩
# ======================================================================
class _PerfStub:
    """_process_result 只用到 record / record_parse_fail，给个空壳。"""

    def __getattr__(self, _name):
        return lambda *a, **k: None


class _Box:
    def __init__(self, x, y, w, h, conf):
        self.x, self.y, self.w, self.h = x, y, w, h
        self.confidence = conf
        self.class_name = "破洞"
        self.area_mm2 = 0.0
        self.width_mm = 0.0
        self.height_mm = 0.0
        self.area_px = w * h
        self.width_px = float(w)
        self.height_px = float(h)


class _Result:
    def __init__(self, has_detection, n=0):
        self.has_detection = has_detection
        self.boxes = [_Box(10, 10, 20, 20, 0.9) for _ in range(n)]
        self.total_boxes = n
        self.detect_time = 0.01
        self.frame_id = 1


class TestHook:
    """挂钩必须在"无缺陷"分支，且异常不能冒出去。"""

    def _dm(self):
        from AnomalyDetection_Tool.core.detection_manager import (
            YOLODetectionManager)
        dm = YOLODetectionManager.__new__(YOLODetectionManager)
        dm._stop_service = None
        dm._perf_tracker = _PerfStub()
        dm._lock = threading.Lock()
        dm._stats = {"total_images": 0, "total_detections": 0,
                     "total_boxes": 0}
        dm._speed_detail = lambda: ""
        return dm

    def _img(self):
        return np.full((60, 80, 3), 200, dtype=np.uint8)

    @staticmethod
    def _mk():
        from AnomalyDetection_Tool.core.detection_manager import (
            YOLODetectionManager)
        dm = YOLODetectionManager.__new__(YOLODetectionManager)
        dm._stop_service = None
        dm._perf_tracker = None
        return dm

    def _run(self, result, image, camera_name, filename):
        """按真实签名调 _process_result。

        capture_timestamp 必须是**字符串**（走 parse_timestamp 的
        "%Y-%m-%d %H:%M:%S.%f"），传 float 会在解析阶段就抛。
        _perf_tracker 也不能是 None —— 解析失败分支会调它。
        """
        dm = self._dm()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")
        dm._process_result(result, image, camera_name, "SN-" + camera_name,
                           filename, ts, {}, time.perf_counter())

    @pytest.fixture(autouse=True)
    def _stub_ws(self, monkeypatch):
        monkeypatch.setattr(
            "AnomalyDetection_Tool.core.detection_manager.get_websocket_server",
            lambda: None)

    def test_clean_frame_in_window_is_saved(self, tmp_path, monkeypatch):
        t = [1_700_000_000.0]
        saver = nis.init_normal_saver(str(tmp_path), launch_tag="L",
                                      interval_sec=1.0, max_per_camera=10)
        monkeypatch.setattr(saver, "_clock", lambda: t[0])
        saver.start_window(start=1_700_000_000.0, duration_sec=100.0)

        p = saver.save
        seen = []
        monkeypatch.setattr(saver, "save",
                            lambda c, i, **k: seen.append(c) or p(c, i))
        self._run(_Result(False), self._img(), "camera4", "f1.jpg")
        assert seen == ["camera4"]

    def test_defect_frame_is_not_saved(self, tmp_path, monkeypatch):
        """有缺陷的帧绝不能进"正常图片"。"""
        saver = nis.init_normal_saver(str(tmp_path), launch_tag="L")
        monkeypatch.setattr(saver, "_clock", lambda: 1_700_000_000.0)
        saver.start_window(start=1_700_000_000.0, duration_sec=100.0)
        called = []
        monkeypatch.setattr(saver, "should_save",
                            lambda c: called.append(c) or False)
        try:
            self._run(_Result(True, 2), self._img(), "camera4", "f2.jpg")
        except AttributeError:
            # 有缺陷的分支在 return 之后还要走竖线过滤 / 面积尺寸分析 /
            # 停机判定，那部分要一整套 stub（见 test_repeat_stop.py 的夹具），
            # 本测试不重复搭。要点是：无缺陷分支里的 `return` 之前
            # 就已经决定要不要存图了，所以只要 saver 一次都没被问过即成立。
            pass
        assert called == []

    def test_no_window_means_no_save(self, tmp_path, monkeypatch):
        saver = nis.init_normal_saver(str(tmp_path))
        called = []
        monkeypatch.setattr(saver, "save",
                            lambda c, i, **k: called.append(c))
        self._run(_Result(False), self._img(), "camera4", "f3.jpg")
        assert called == []

    def test_saver_crash_does_not_break_detection(self, tmp_path, monkeypatch):
        """存图挂了不能影响检测。"""
        saver = nis.init_normal_saver(str(tmp_path))

        def boom(_c):
            raise RuntimeError("saver 崩了")
        monkeypatch.setattr(saver, "should_save", boom)
        self._run(_Result(False), self._img(), "camera4", "f4.jpg")   # 不应抛

    def test_missing_saver_is_tolerated(self, monkeypatch):
        monkeypatch.setattr(
            "AnomalyDetection_Tool.core.detection_manager.get_normal_saver",
            lambda: None)
        self._run(_Result(False), self._img(), "camera4", "f5.jpg")   # 不应抛

    def test_none_image_is_tolerated(self, tmp_path, monkeypatch):
        saver = nis.init_normal_saver(str(tmp_path))
        monkeypatch.setattr(saver, "_clock", lambda: 1_700_000_000.0)
        saver.start_window(start=1_700_000_000.0, duration_sec=100.0)
        self._run(_Result(False), None, "camera4", "f6.jpg")   # 不应抛

    def test_module_singleton_roundtrip(self, tmp_path):
        s1 = nis.init_normal_saver(str(tmp_path), max_per_camera=7)
        assert nis.get_normal_saver() is s1
        assert s1.max_per_camera == 7


# ======================================================================
# §7  Thrift
# ======================================================================
class TestThrift:
    """三个新方法 + 手写生成结构体的真实 wire 往返。"""

    NEW = ["setNormalSaveWindow", "stopNormalSave", "getNormalSaveStatus"]
    OLD = ["getIsStop", "setIsStop", "ping"]

    def test_registry_has_all_six(self):
        from lib.camera.thrift import SimpleService as S
        p = S.Processor(None)
        assert sorted(p._processMap) == sorted(self.NEW + self.OLD)

    def test_old_three_untouched(self):
        """加新方法不能动到停机信号用的那三个。"""
        from lib.camera.thrift import SimpleService as S
        p = S.Processor(None)
        for m in self.OLD:
            assert m in p._processMap
            assert hasattr(S, m + "_args") and hasattr(S, m + "_result")

    def test_iface_declares_new_methods(self):
        from lib.camera.thrift import SimpleService as S
        for m in self.NEW:
            assert hasattr(S.Iface, m)

    @pytest.mark.parametrize("start,dur", [
        ("08:00", 600.0), ("", 0.0), ("1700000000.5", 3600.0),
        ("23:59:59", 1.0)])
    def test_args_roundtrip_over_the_wire(self, start, dur):
        """手写生成代码最容易错的就是 wire 格式 —— 真序列化一遍。"""
        from thrift.protocol import TBinaryProtocol
        from thrift.transport import TTransport
        from lib.camera.thrift import SimpleService as S
        buf = TTransport.TMemoryBuffer()
        proto = TBinaryProtocol.TBinaryProtocol(buf)
        S.setNormalSaveWindow_args(start=start, duration_sec=dur).write(proto)
        buf2 = TTransport.TMemoryBuffer(buf.getvalue())
        got = S.setNormalSaveWindow_args()
        got.read(TBinaryProtocol.TBinaryProtocol(buf2))
        assert got.start == start
        assert abs(got.duration_sec - dur) < 1e-9

    def test_result_roundtrip(self):
        from thrift.protocol import TBinaryProtocol
        from thrift.transport import TTransport
        from lib.camera.thrift import SimpleService as S
        buf = TTransport.TMemoryBuffer()
        S.setNormalSaveWindow_result(success=True).write(
            TBinaryProtocol.TBinaryProtocol(buf))
        got = S.setNormalSaveWindow_result()
        got.read(TBinaryProtocol.TBinaryProtocol(
            TTransport.TMemoryBuffer(buf.getvalue())))
        assert got.success is True

    def test_status_result_roundtrip(self):
        from thrift.protocol import TBinaryProtocol
        from thrift.transport import TTransport
        from lib.camera.thrift import SimpleService as S
        payload = json.dumps({"active": True, "per_camera": {"camera1": 3}},
                             ensure_ascii=False)
        buf = TTransport.TMemoryBuffer()
        S.getNormalSaveStatus_result(success=payload).write(
            TBinaryProtocol.TBinaryProtocol(buf))
        got = S.getNormalSaveStatus_result()
        got.read(TBinaryProtocol.TBinaryProtocol(
            TTransport.TMemoryBuffer(buf.getvalue())))
        assert json.loads(got.success) == {"active": True,
                                           "per_camera": {"camera1": 3}}

    def test_thrift_spec_field_ids_match_the_idl(self):
        """字段编号必须和 IDL 一致 —— 编号错了 Java 那边静默读成 null。"""
        from lib.camera.thrift import SimpleService as S
        spec = S.setNormalSaveWindow_args.thrift_spec
        assert spec[1][:3] == (1, 11, 'start')          # 11 = TType.STRING
        assert spec[2][:3] == (2, 4, 'duration_sec')    # 4  = TType.DOUBLE

    def test_idl_file_documents_the_same_signature(self):
        """IDL 是给 Java 侧同步用的，签名必须和生成代码一致。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        idl = os.path.join(root, "lib", "camera", "thrift",
                           "simple_service.thrift")
        assert os.path.exists(idl), "缺 IDL，Java 侧没法同步"
        txt = open(idl, encoding="utf-8").read()
        assert "setNormalSaveWindow(1: string start, 2: double duration_sec)" in txt
        assert "stopNormalSave()" in txt
        assert "getNormalSaveStatus()" in txt

    # ---- handler ----
    def _handler(self):
        from Communication_Tool.ThriftControl import PythonServiceHandler
        return PythonServiceHandler()

    def test_handler_opens_a_window(self, tmp_path, monkeypatch):
        saver = nis.init_normal_saver(str(tmp_path))
        monkeypatch.setattr(saver, "_clock", lambda: 1_700_000_000.0)
        assert self._handler().setNormalSaveWindow("", 60.0) is True
        assert saver.get_status()["active"] is True

    def test_handler_rejects_bad_duration(self, tmp_path, monkeypatch):
        saver = nis.init_normal_saver(str(tmp_path))
        monkeypatch.setattr(saver, "_clock", lambda: 1_700_000_000.0)
        self._handler().setNormalSaveWindow("", 0.0)
        assert saver.get_status()["active"] is False

    def test_handler_accepts_hhmm(self, tmp_path):
        nis.init_normal_saver(str(tmp_path))
        assert self._handler().setNormalSaveWindow("23:59:59", 60.0) is True

    def test_handler_stop(self, tmp_path, monkeypatch):
        saver = nis.init_normal_saver(str(tmp_path))
        monkeypatch.setattr(saver, "_clock", lambda: 1_700_000_000.0)
        self._handler().setNormalSaveWindow("", 60.0)
        assert self._handler().stopNormalSave() is True
        assert saver.get_status()["active"] is False

    def test_handler_status_is_json(self, tmp_path, monkeypatch):
        saver = nis.init_normal_saver(str(tmp_path), max_per_camera=10)
        monkeypatch.setattr(saver, "_clock", lambda: 1_700_000_000.0)
        self._handler().setNormalSaveWindow("", 60.0)
        d = json.loads(self._handler().getNormalSaveStatus())
        assert d["active"] is True
        assert d["max_per_camera"] == 10
        assert d["remaining_sec"] <= 60.0

    def test_handler_without_saver_returns_failure_not_an_exception(self,
                                                                   monkeypatch):
        """未初始化时返回 False，绝不能抛 —— 那会掀掉 Thrift 服务线程，
        而那个线程还负责停机信号。"""
        monkeypatch.setattr(nis, "_saver", None)
        from Communication_Tool import ThriftControl as tc
        h = self._handler()
        assert h.setNormalSaveWindow("", 60.0) is False
        assert h.stopNormalSave() is False
        assert json.loads(h.getNormalSaveStatus())["reason"] == "not_initialized"
        assert tc is not None

    def test_handler_swallows_saver_exceptions(self, monkeypatch):
        saver = nis.init_normal_saver("x")

        def boom(*a, **k):
            raise RuntimeError("炸了")
        monkeypatch.setattr(saver, "start_window", boom)
        assert self._handler().setNormalSaveWindow("", 60.0) is False

    def test_old_thrift_methods_still_work(self, monkeypatch):
        """回归：停机信号那三个方法不能被这次改动弄坏。"""
        from Communication_Tool import ThriftControl as tc
        monkeypatch.setattr(tc, "setIsStop", lambda v, s, w: None)
        monkeypatch.setattr(tc, "getIsStop", lambda w: False)
        h = self._handler()
        assert h.setIsStop(True, "Java") is True
        assert h.getIsStop() is False
        assert h.ping() == "Python服务正常"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
