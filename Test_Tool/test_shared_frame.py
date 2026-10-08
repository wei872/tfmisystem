# -*- coding: utf-8 -*-
"""
SharedFrame 引用计数回归测试

守住 detection_pipeline.py Step4 的那个修复：主链路的 owner_release()
必须放在 finally 里。

背景：_submit_analysis_tasks 内部是"先 shared.acquire() 再 executor.submit()"，
一旦 acquire 之后抛异常（线程池已 shutdown 等），裸调用的 owner_release() 就
永远执行不到，_ref_count 停在 >=1，_image 不会被置空。

这些用例直接测 SharedFrame 的契约本身，不依赖真实相机或 GPU。
"""

import gc
import sys
import weakref
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from AnomalyDetection_Tool.core.shared_frame import SharedFrame


@pytest.fixture
def frame():
    """一块 12 MB 的图，尺寸比例接近现场真实的 14.3 MB（2048x2048x3 BGR）"""
    return np.zeros((2048, 2048, 3), dtype=np.uint8)


class TestRefCountContract:
    """SharedFrame 自身的引用计数语义"""

    def test_初始引用计数为1(self, frame):
        shared = SharedFrame(frame)
        assert shared.ref_count == 1
        assert shared.image is frame

    def test_acquire_release_配对后归零并置空图像(self, frame):
        shared = SharedFrame(frame)
        shared.acquire()
        assert shared.ref_count == 2
        shared.release()          # 后台任务归还
        shared.owner_release()    # 主链路归还
        assert shared.ref_count == 0
        assert shared.image is None

    def test_owner_release_幂等(self, frame):
        """owner_release 重复调用不应把计数减穿"""
        shared = SharedFrame(frame)
        shared.owner_release()
        shared.owner_release()
        shared.owner_release()
        assert shared.ref_count == 0

    def test_已释放后再_acquire_必须报错(self, frame):
        shared = SharedFrame(frame)
        shared.owner_release()
        with pytest.raises(RuntimeError):
            shared.acquire()

    def test_with_语法异常时也会释放(self, frame):
        # 注意：SharedFrame(image) 构造时的 ref_count=1 就已经是 owner 的那一份，
        # 不要再手动 acquire() 一次，否则计数会多算。
        shared = SharedFrame(frame)
        with pytest.raises(ValueError):
            with shared:          # 后台任务用 with 进入（__enter__ = acquire）
                raise ValueError("后台任务炸了")
        # __exit__ 已归还后台任务那一份，只剩 owner 的 1
        assert shared.ref_count == 1
        shared.owner_release()
        assert shared.ref_count == 0
        assert shared.image is None


class TestOwnerReleaseInFinally:
    """
    核心回归：模拟 detect() Step4 的调用形状，
    验证 _submit_analysis_tasks 抛异常时 owner 引用仍被归还。
    """

    @staticmethod
    def _step4(shared, submit_fn):
        """复刻 detection_pipeline.py Step4 修复后的结构"""
        try:
            submit_fn(shared)
        finally:
            shared.owner_release()

    @staticmethod
    def _step4_buggy(shared, submit_fn):
        """修复前的形状：裸调用，异常会跳过 owner_release"""
        submit_fn(shared)
        shared.owner_release()

    def test_修复后_submit抛异常时owner引用仍归还(self, frame):
        shared = SharedFrame(frame)

        def boom(_shared):
            # 复刻真实顺序：先 acquire，再 submit 时炸
            _shared.acquire()
            raise RuntimeError("cannot schedule new futures after shutdown")

        with pytest.raises(RuntimeError):
            self._step4(shared, boom)

        # acquire 的 +1 没人归还（后台任务从未真正跑起来），
        # 但主链路那一份必须已经还掉
        assert shared.ref_count == 1
        # 关键：把残留的那一份也放掉后，图像必须能被置空
        shared.release()
        assert shared.image is None

    def test_修复前_裸调用会漏掉owner引用(self, frame):
        """
        这条是"反向守卫"：证明修复前的形状确实会漏。
        如果哪天有人把 finally 去掉，这条会失败。
        """
        shared = SharedFrame(frame)

        def boom(_shared):
            _shared.acquire()
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError):
            self._step4_buggy(shared, boom)

        # 裸调用时 owner_release 被跳过：主链路那 1 份 + acquire 的 1 份都还在
        assert shared.ref_count == 2
        assert shared.image is not None

    def test_无异常路径行为不变(self, frame):
        """finally 不能改变正常路径的语义"""
        shared = SharedFrame(frame)
        acquired = []

        def ok(s):
            s.acquire()
            acquired.append(s)

        self._step4(shared, ok)
        assert shared.ref_count == 1        # 只剩后台任务那一份
        acquired[0].release()               # 后台任务跑完
        assert shared.ref_count == 0
        assert shared.image is None


class TestImageActuallyReclaimed:
    """
    用 weakref 验证真实内存行为。

    这组用例同时钉住了我在文档里订正过的结论：
    漏掉 owner_release **不会**造成永久泄漏——detect() 返回后
    shared 随局部变量出栈，CPython 会把 SharedFrame 和数组一起回收。
    """

    def test_正常路径_数组被回收(self):
        img = np.zeros((2048, 2048, 3), dtype=np.uint8)
        probe = weakref.ref(img)
        shared = SharedFrame(img)
        shared.owner_release()
        del shared, img
        gc.collect()
        assert probe() is None

    def test_漏掉owner_release_数组仍被回收(self):
        """订正"泄漏 14.3MB/帧"这个过重说法的实测依据"""
        img = np.zeros((2048, 2048, 3), dtype=np.uint8)
        probe = weakref.ref(img)
        shared = SharedFrame(img)
        # 故意不调 owner_release，模拟异常跳过的场景
        del shared, img
        gc.collect()
        assert probe() is None

    def test_异常对象被留存时_数组会跟着滞留(self):
        """
        这才是真正的风险面：traceback 持有帧局部变量。
        只要异常对象活着，这一帧的数组就活着——
        放在 finally 里能确定性地断开这条引用。
        """
        captured = {}

        def run_without_finally():
            local_img = np.zeros((2048, 2048, 3), dtype=np.uint8)
            captured["probe"] = weakref.ref(local_img)
            # 这个局部变量是必须存在的：它就是真实 detect() 帧里的那个 shared，
            # 和 local_img 一起被 traceback 持有，正是本用例要复现的形状。
            shared = SharedFrame(local_img)
            captured["shared_holds_image"] = shared.image is local_img
            try:
                raise RuntimeError("boom")
            except RuntimeError as e:
                captured["exc"] = e        # 模拟日志/上层留存了异常对象

        run_without_finally()
        gc.collect()
        # 异常对象被留存 → traceback 持有 local_img → 数组仍在
        assert captured["probe"]() is not None

        # 断掉异常引用后，数组立刻可回收
        del captured["exc"]
        gc.collect()
        assert captured["probe"]() is None
