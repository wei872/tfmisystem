#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``YOLODetectionManager._speed_detail`` 的归因测试。

现场日志里 camera2/3/4/6 四行的 `预处理=74.0 GPU推理=18.9 NMS=13.2`
一字不差。原因是旧实现"遍历所有 pipeline，返回第一个有值的"，
于是不管在记哪台相机，附带的都是同一个 worker 的数字。
这组测试锁住"必须按 worker_id 取，取不到就不显示"。
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from Test_Tool import fake_galaxy_sdk  # noqa: E402
fake_galaxy_sdk.install()

from AnomalyDetection_Tool.core.detection_manager import (  # noqa: E402
    YOLODetectionManager)


class _Det:
    def __init__(self, s):
        self._s = s

    def speed_str(self):
        return self._s


class _Pipeline:
    def __init__(self, wid, s):
        self.worker_id = wid
        self.detector = _Det(s)


class _Pool:
    def __init__(self, pipelines):
        self._p = pipelines

    def iter_pipelines(self):
        return list(self._p)


def _dm(pipelines):
    dm = YOLODetectionManager.__new__(YOLODetectionManager)
    dm.worker_pool = _Pool(pipelines)
    dm.pipeline = None
    return dm


class TestAttribution:

    def test_picks_the_worker_that_produced_the_frame(self):
        """W0 和 W1 数字不同，问 W1 就必须拿到 W1 的。"""
        dm = _dm([_Pipeline(0, "预处理=74.0 GPU推理=18.9 NMS=13.2"),
                  _Pipeline(1, "预处理=11.1 GPU推理=22.2 NMS=33.3")])
        assert "预处理=11.1" in dm._speed_detail(1)
        assert "预处理=74.0" not in dm._speed_detail(1)

    def test_old_behaviour_always_returned_the_first(self):
        """钉住旧 bug 的样子：不给 worker_id 时只会拿第一个。"""
        dm = _dm([_Pipeline(0, "预处理=74.0"), _Pipeline(1, "预处理=11.1")])
        assert "预处理=74.0" in dm._speed_detail()

    def test_output_names_the_worker(self):
        """必须打出 worker 编号，否则读者无从判断这是谁的数据。"""
        dm = _dm([_Pipeline(0, "预处理=74.0")])
        assert dm._speed_detail(0).strip().startswith("[W0")

    def test_unknown_worker_returns_empty_not_someone_elses_number(self):
        """关键：该 worker 没上报过就返回空，绝不能拿别人的数字顶上。"""
        dm = _dm([_Pipeline(0, "预处理=74.0")])
        assert dm._speed_detail(7) == ""

    def test_worker_without_speed_string_returns_empty(self):
        dm = _dm([_Pipeline(0, "")])
        assert dm._speed_detail(0) == ""

    def test_no_pipelines_is_tolerated(self):
        dm = _dm([])
        assert dm._speed_detail(0) == ""
        assert dm._speed_detail() == ""

    def test_no_pool_is_tolerated(self):
        dm = YOLODetectionManager.__new__(YOLODetectionManager)
        dm.worker_pool = None
        dm.pipeline = None
        assert dm._speed_detail(0) == ""

    def test_broken_detector_does_not_raise(self):
        """日志辅助函数绝不能把检测主流程带崩。"""
        class Boom:
            worker_id = 0

            @property
            def detector(self):
                raise RuntimeError("炸了")

        dm = YOLODetectionManager.__new__(YOLODetectionManager)
        dm.worker_pool = _Pool([Boom()])
        dm.pipeline = None
        assert dm._speed_detail(0) == ""


class TestCallSitesPassWorkerId:
    """两个调用点都必须把 worker_id 传进去。"""

    @pytest.fixture(scope="class")
    def src(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "AnomalyDetection_Tool", "core",
                               "detection_manager.py"), encoding="utf-8") as f:
            return f.read()

    def test_no_bare_call_left(self, src):
        assert "{self._speed_detail()}" not in src

    def test_both_sites_pass_worker_id(self, src):
        n = src.count('_speed_detail(getattr(result, "worker_id", None))')
        assert n == 2, f"应有 2 处传 worker_id，实际 {n}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
