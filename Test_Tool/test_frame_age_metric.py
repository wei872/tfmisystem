#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""健康快照里 cameraN.frame_age_s 的回归测试。

这个字段是为了排查"突然没有图片进来"加的。那次的现场特征是：
Modbus 侧 [触发] 日志一路正常、速度正常、触发源仍是 Line0，
但 8 台相机的 frames 同时冻结 —— 断点在 PLC→Line0→相机 这一段。

frames 是累计值，光看一条心跳看不出涨没涨；frame_age_s 是瞬时值，
一条心跳就能定位。所以这个字段**必须始终出现**（哪怕相机从没出过图，
也要报 -1），否则最需要它的时候反而看不到。
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _FakeCam:
    def __init__(self, diag):
        self._d = diag

    def get_diag_info(self):
        return self._d


@pytest.fixture()
def wired(monkeypatch):
    import fake_galaxy_sdk
    fake_galaxy_sdk.install()
    import main_yolo
    return main_yolo


def _install(monkeypatch, m, cams):
    class _Reg:
        @staticmethod
        def get_all_cameras():
            return cams
    monkeypatch.setattr(m, "CameraRegistry", _Reg)


class TestFrameAge:

    def test_fresh_frame_reports_a_small_age(self, wired, monkeypatch):
        _install(monkeypatch, wired,
                 {"camera1": _FakeCam({"frame_count": 10, "bad_frame_count": 0,
                                       "last_frame_time": time.time() - 0.4})})
        out = wired._camera_diag_provider()
        assert out["camera1.frames"] == 10
        assert 0.0 <= out["camera1.frame_age_s"] <= 2.0

    def test_stale_frame_reports_a_large_age(self):
        """相机断了 40 秒，必须一眼看得出来。"""
        import fake_galaxy_sdk
        fake_galaxy_sdk.install()
        import main_yolo as m

        class _Reg:
            @staticmethod
            def get_all_cameras():
                return {"camera1": _FakeCam({
                    "frame_count": 712, "bad_frame_count": 0,
                    "last_frame_time": time.time() - 40.0})}
        m.CameraRegistry = _Reg
        out = m._camera_diag_provider()
        assert out["camera1.frames"] == 712, "累计值冻结时看不出异常"
        assert out["camera1.frame_age_s"] >= 39.0, "瞬时值必须暴露出来"

    def test_never_delivered_reports_minus_one(self, wired, monkeypatch):
        """相机从没出过图时 last_frame_time=0，必须报 -1 而不是一个巨大的数。"""
        _install(monkeypatch, wired,
                 {"camera1": _FakeCam({"frame_count": 0, "bad_frame_count": 0,
                                       "last_frame_time": 0.0})})
        out = wired._camera_diag_provider()
        assert out["camera1.frame_age_s"] == -1.0

    def test_missing_key_reports_minus_one(self, wired, monkeypatch):
        """字段缺失不能抛，也不能算出一个假的大数。"""
        _install(monkeypatch, wired,
                 {"camera1": _FakeCam({"frame_count": 5, "bad_frame_count": 0})})
        out = wired._camera_diag_provider()
        assert out["camera1.frame_age_s"] == -1.0

    def test_all_eight_cameras_reported_independently(self, wired, monkeypatch):
        """8 台同时冻结是"上游共因"的特征，必须每台都能看到。"""
        cams = {f"camera{i}": _FakeCam({
            "frame_count": 712, "bad_frame_count": 0,
            "last_frame_time": time.time() - 30.0}) for i in range(1, 9)}
        _install(monkeypatch, wired, cams)
        out = wired._camera_diag_provider()
        ages = [out[f"camera{i}.frame_age_s"] for i in range(1, 9)]
        assert len(ages) == 8
        assert all(a >= 29.0 for a in ages)

    def test_broken_camera_does_not_kill_the_snapshot(self, wired, monkeypatch):
        class _Boom:
            def get_diag_info(self):
                raise RuntimeError("设备已断开")
        _install(monkeypatch, wired,
                 {"camera1": _Boom(),
                  "camera2": _FakeCam({"frame_count": 3, "bad_frame_count": 0,
                                       "last_frame_time": time.time()})})
        out = wired._camera_diag_provider()
        assert "camera1.frames" not in out
        assert out["camera2.frames"] == 3


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
