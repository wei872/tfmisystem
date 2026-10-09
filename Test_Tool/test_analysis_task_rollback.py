# -*- coding: utf-8 -*-
"""
经纬线后台任务提交失败时的资源回滚测试
================================================

覆盖 detection_pipeline._submit_analysis_tasks() 里"已预留、未移交"窗口：

    try_mark_inflight()  →  in_flight = True
    _weft_queue_depth += 1
    shared.acquire()     →  引用计数 +1
    executor.submit(...) →  ← 这里抛异常的话，任务体永远不会运行

shared.release() / reset_inflight() / _weft_queue_depth -= 1 三处归还
全部只写在任务体的 finally 里。submit() 一抛异常就都归还不了，而代码里
没有任何超时看门狗能把卡住的 in_flight 救回来。

后果分级（已核对作用域）：
  * _weft_queue_depth 是**模块级全局**，只有 4 个槽位、8 台相机共用
    → 泄漏满 4 次，整个进程的纬线分析永久停摆
  * _warp_cache.in_flight / _weft_cache.in_flight 是**每 pipeline 一份**
    → 卡住则该 pipeline 的对应分析永久停摆

这里不构造完整的 DetectionPipeline（构造要拉起 GPU/模型），而是直接调用
真实的 DetectionPipeline._submit_analysis_tasks 函数对象，用一个只提供该
方法实际用到的属性的轻量替身作 self。跑的是仓库里那份代码，不是复制品。
"""

import sys
from pathlib import Path

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "lib") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "lib"))

from AnomalyDetection_Tool.core import detection_pipeline as dp  # noqa: E402
from AnomalyDetection_Tool.core.shared_frame import SharedFrame  # noqa: E402
from AnomalyDetection_Tool.core.warp_weft_analysis_cache import (  # noqa: E402
    WarpCache,
    WeftCache,
)

# 不在 CAMERAS_WITHOUT_WEFT（{'camera1', 'camera8'}）里，纬线任务才会提交
CAMERA = "camera3"


class FailingExecutor:
    """submit() 直接抛异常的假线程池，模拟 shutdown / BrokenThreadPool"""

    def __init__(self, exc=None):
        self.exc = exc if exc is not None else RuntimeError(
            "cannot schedule new futures after shutdown"
        )
        self.calls = 0

    def submit(self, fn, *args, **kwargs):
        self.calls += 1
        raise self.exc


class RecordingExecutor:
    """submit() 正常受理但不真正执行，用于验证成功路径"""

    def __init__(self):
        self.calls = 0

    def submit(self, fn, *args, **kwargs):
        self.calls += 1
        return None


class PipelineStub:
    """只提供 _submit_analysis_tasks 实际访问的属性的轻量替身"""

    def __init__(self, warp=True, weft=True):
        import threading

        self._frame_counter = 0
        self.enable_warp_density = warp
        self.enable_weft_density = weft
        self._warp_interval = 1
        self._weft_interval = 1
        self._warp_detector = object()  # 非 None 即视为已初始化
        self._warp_cache = WarpCache()
        self._weft_cache = WeftCache()
        self._lock = threading.Lock()
        self._stats = dp.DetectionPipeline._init_stats()
        # submit() 的第一个实参就是这两个方法引用。不提供的活，AttributeError
        # 会在求值实参时就抛出来（同样落在 try 里、同样被回滚），但那样测的就
        # 不是"executor.submit() 失败"这条路径了，所以必须给上。
        self._warp_task_shared = lambda *a, **k: None
        self._weft_task_shared_guarded = lambda *a, **k: None


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """每个用例前后把模块级 _weft_queue_depth 归零，避免用例间串味"""
    monkeypatch.setattr(dp, "_weft_queue_depth", 0)
    yield


def _make_frame():
    return SharedFrame(np.zeros((64, 64, 3), dtype=np.uint8))


def _submit(stub, shared):
    """调用真实的 DetectionPipeline._submit_analysis_tasks"""
    return dp.DetectionPipeline._submit_analysis_tasks(
        stub, shared, CAMERA, "test.png", "SN-001"
    )


# ────────────────────────────────────────────────────────────────────
#  纬线：_weft_queue_depth 回滚（模块级全局，最严重）
# ────────────────────────────────────────────────────────────────────


def test_weft_submit_failure_rolls_back_queue_depth(monkeypatch):
    weft_ex = FailingExecutor()
    monkeypatch.setattr(dp, "_weft_executor", weft_ex)
    monkeypatch.setattr(dp, "_warp_executor", FailingExecutor())
    stub = PipelineStub(warp=False, weft=True)
    shared = _make_frame()

    _submit(stub, shared)

    # 确认异常确实来自 submit()，而不是实参求值阶段的意外错误
    assert weft_ex.calls == 1, "必须真的走到 executor.submit() 才算测到这条路径"
    assert dp._weft_queue_depth == 0, "队列深度必须归还，否则槽位被永久占用"
    assert stub._stats["weft_task_submit_failed"] == 1


def test_repeated_weft_failures_do_not_exhaust_queue_depth(monkeypatch):
    """
    核心回归：连续失败远超 4 个槽位，纬线分析也不能停摆。

    修复前 _weft_queue_depth 每次失败泄漏 1，满 4 次之后所有后续帧都走
    depth_dropped 分支直接 return，全进程 8 台相机的纬线分析永久静默停止。
    """
    weft_ex = FailingExecutor()
    monkeypatch.setattr(dp, "_weft_executor", weft_ex)
    monkeypatch.setattr(dp, "_warp_executor", FailingExecutor())
    stub = PipelineStub(warp=False, weft=True)

    rounds = dp.WEFT_MAX_QUEUE_DEPTH * 5  # 20 次，远超 4 个槽位
    for _ in range(rounds):
        _submit(stub, _make_frame())

    assert weft_ex.calls == rounds, "每一帧都必须走到 submit()，不能被深度检查提前拦掉"
    assert dp._weft_queue_depth == 0, "20 次失败后深度仍应为 0"
    assert stub._stats["weft_task_depth_dropped"] == 0, (
        "一次都不该因为深度耗尽而丢帧——那说明槽位被泄漏占满了"
    )
    assert stub._stats["weft_task_submit_failed"] == rounds


def test_weft_submit_failure_resets_inflight(monkeypatch):
    monkeypatch.setattr(dp, "_weft_executor", FailingExecutor())
    monkeypatch.setattr(dp, "_warp_executor", FailingExecutor())
    stub = PipelineStub(warp=False, weft=True)

    _submit(stub, _make_frame())

    assert stub._weft_cache.in_flight is False, "in_flight 卡 True 会让后续每帧都跳过"
    # 且下一帧仍能成功占用（证明没有留下"永久占用"的残留）
    assert stub._weft_cache.try_mark_inflight() is True


def test_weft_submit_failure_releases_acquired_ref(monkeypatch):
    monkeypatch.setattr(dp, "_weft_executor", FailingExecutor())
    monkeypatch.setattr(dp, "_warp_executor", FailingExecutor())
    stub = PipelineStub(warp=False, weft=True)
    shared = _make_frame()

    _submit(stub, shared)

    assert shared.ref_count == 1, "acquire 成功后 submit 失败，必须 release 回去"
    assert shared.image is not None


# ────────────────────────────────────────────────────────────────────
#  经线：in_flight 回滚
# ────────────────────────────────────────────────────────────────────


def test_warp_submit_failure_resets_inflight(monkeypatch):
    warp_ex = FailingExecutor()
    monkeypatch.setattr(dp, "_warp_executor", warp_ex)
    stub = PipelineStub(warp=True, weft=False)

    _submit(stub, _make_frame())

    assert warp_ex.calls == 1
    assert stub._warp_cache.in_flight is False
    assert stub._stats["warp_task_submit_failed"] == 1
    assert stub._stats["warp_task_skipped"] == 0


def test_warp_submit_failure_releases_acquired_ref(monkeypatch):
    monkeypatch.setattr(dp, "_warp_executor", FailingExecutor())
    stub = PipelineStub(warp=True, weft=False)
    shared = _make_frame()

    _submit(stub, shared)

    assert shared.ref_count == 1
    assert shared.image is not None


def test_repeated_warp_failures_keep_warp_enabled(monkeypatch):
    """经线 in_flight 是每 pipeline 一份，卡住则该 pipeline 经线分析停摆"""
    warp_ex = FailingExecutor()
    monkeypatch.setattr(dp, "_warp_executor", warp_ex)
    stub = PipelineStub(warp=True, weft=False)

    for _ in range(10):
        _submit(stub, _make_frame())

    assert warp_ex.calls == 10
    assert stub._stats["warp_task_submit_failed"] == 10
    assert stub._stats["warp_task_skipped"] == 0, (
        "不应有任何一帧是因为 in_flight 卡住而被跳过的"
    )


# ────────────────────────────────────────────────────────────────────
#  acquire() 自己抛异常：不能多扣一次引用计数
# ────────────────────────────────────────────────────────────────────


def test_weft_acquire_failure_does_not_over_release(monkeypatch):
    """
    SharedFrame.acquire() 在 _image 为 None 时会在 _ref_count += 1 **之前**
    抛 RuntimeError。此时若照样 release()，引用计数会被扣穿成 0，
    _image 被置空，主链路的 owner_release() 再扣一次就变负数。
    """
    monkeypatch.setattr(dp, "_weft_executor", RecordingExecutor())
    monkeypatch.setattr(dp, "_warp_executor", RecordingExecutor())
    stub = PipelineStub(warp=False, weft=True)
    shared = _make_frame()

    # SharedFrame 用 __slots__，实例上不能挂属性，只能打类方法
    def _boom(self):
        raise RuntimeError("SharedFrame 已释放，不可再 acquire")

    monkeypatch.setattr(SharedFrame, "acquire", _boom)

    _submit(stub, shared)

    assert shared.ref_count == 1, "acquire 没成功就不该 release"
    assert dp._weft_queue_depth == 0
    assert stub._weft_cache.in_flight is False
    assert stub._stats["weft_task_submit_failed"] == 1


# ────────────────────────────────────────────────────────────────────
#  KeyboardInterrupt / SystemExit 必须继续上抛
# ────────────────────────────────────────────────────────────────────


def test_keyboard_interrupt_propagates_but_still_rolls_back(monkeypatch):
    """吞掉 KeyboardInterrupt 会让 Ctrl+C 失灵，所以只回滚不吞"""
    monkeypatch.setattr(dp, "_weft_executor", FailingExecutor(KeyboardInterrupt()))
    monkeypatch.setattr(dp, "_warp_executor", FailingExecutor(KeyboardInterrupt()))
    stub = PipelineStub(warp=False, weft=True)

    with pytest.raises(KeyboardInterrupt):
        _submit(stub, _make_frame())

    assert dp._weft_queue_depth == 0, "即使上抛，回滚也必须已经发生"
    assert stub._weft_cache.in_flight is False


def test_system_exit_propagates_but_still_rolls_back(monkeypatch):
    monkeypatch.setattr(dp, "_weft_executor", FailingExecutor(SystemExit(1)))
    monkeypatch.setattr(dp, "_warp_executor", FailingExecutor(SystemExit(1)))
    stub = PipelineStub(warp=False, weft=True)

    with pytest.raises(SystemExit):
        _submit(stub, _make_frame())

    assert dp._weft_queue_depth == 0
    assert stub._weft_cache.in_flight is False


# ────────────────────────────────────────────────────────────────────
#  成功路径不能被改坏
# ────────────────────────────────────────────────────────────────────


def test_success_path_still_counts_and_holds_refs(monkeypatch):
    warp_ex, weft_ex = RecordingExecutor(), RecordingExecutor()
    monkeypatch.setattr(dp, "_warp_executor", warp_ex)
    monkeypatch.setattr(dp, "_weft_executor", weft_ex)
    stub = PipelineStub(warp=True, weft=True)
    shared = _make_frame()

    _submit(stub, shared)

    assert warp_ex.calls == 1 and weft_ex.calls == 1
    assert stub._stats["warp_task_submitted"] == 1
    assert stub._stats["weft_task_submitted"] == 1
    assert stub._stats["warp_task_submit_failed"] == 0
    assert stub._stats["weft_task_submit_failed"] == 0
    # 任务体尚未执行，两个引用都还被持有：owner(1) + warp(1) + weft(1)
    assert shared.ref_count == 3
    # 深度已占用，等任务体 finally 归还
    assert dp._weft_queue_depth == 1


def test_submit_failure_does_not_raise_into_main_path(monkeypatch):
    """
    后台分析提交失败不该把本帧已经算完的 YOLO 结果一起丢掉，
    所以 _submit_analysis_tasks 必须正常返回而不是抛出去。
    """
    monkeypatch.setattr(dp, "_weft_executor", FailingExecutor())
    monkeypatch.setattr(dp, "_warp_executor", FailingExecutor())
    stub = PipelineStub(warp=True, weft=True)

    _submit(stub, _make_frame())  # 不应抛异常

    assert stub._stats["warp_task_submit_failed"] == 1
    assert stub._stats["weft_task_submit_failed"] == 1
