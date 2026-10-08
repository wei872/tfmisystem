#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
检测线程池（内存安全版）
"""

import queue
import threading
import time
import traceback
from typing import Optional, Callable, Dict, List, Any
from concurrent.futures import ThreadPoolExecutor

from General_Tool.EnhancedLogger import info, error

from AnomalyDetection_Tool.models.data_types import (
    DetectionTask, PostProcessTask,
)
from AnomalyDetection_Tool.core.memory_guard import MemoryGuard

from AnomalyDetection_Tool.output.fabric_width_service import get_fabric_detector


class DetectionWorkerPool:
    """检测线程池"""

    def __init__(self, num_workers: int,
                 pipeline_factory: Callable[[int], Any],
                 result_callback: Callable,
                 max_queue_size: int = 32,
                 skip_old_frames: bool = True,
                 post_workers: int = 2,
                 max_inflight_frames: int = 12,
                 memory_high_watermark: float = 90.0,
                 memory_critical_watermark: float = 95.0,
                 blocking_submit: bool = False,
                 blocking_timeout: float = 30.0):

        self.num_workers = max(1, int(num_workers))
        self.pipeline_factory = pipeline_factory
        self._callback = result_callback
        self.skip_old_frames = skip_old_frames

        self._blocking_submit = blocking_submit
        self._blocking_timeout = blocking_timeout

        self._inflight_sem = threading.Semaphore(max_inflight_frames)
        self._max_inflight = max_inflight_frames

        self._memory_guard = MemoryGuard(
            high_watermark=memory_high_watermark,
            critical_watermark=memory_critical_watermark,
            max_growth_percent=10.0,
        )

        self._infer_queue: queue.Queue = queue.Queue(
            maxsize=max_queue_size)
        self._infer_executor = ThreadPoolExecutor(
            max_workers=self.num_workers,
            thread_name_prefix="Infer")

        self._post_queue: queue.Queue = queue.Queue(
            maxsize=max_queue_size * 2)
        self._post_executor = ThreadPoolExecutor(
            max_workers=max(1, post_workers),
            thread_name_prefix="Post")

        self._stop = threading.Event()
        self._dispatcher = threading.Thread(
            target=self._dispatch_loop, name="Dispatcher",
            daemon=True)
        self._post_dispatcher = threading.Thread(
            target=self._post_dispatch_loop,
            name="PostDispatcher", daemon=True)

        self._tls = threading.local()
        self._pipeline_lock = threading.Lock()
        self._pipeline_count = 0
        self._pipelines: List[Any] = []

        self._lock = threading.Lock()
        self._counters = {
            "submitted": 0, "processed": 0,
            "dropped": 0, "skipped": 0, "errors": 0,
            "post_processed": 0,
            "memory_dropped": 0, "semaphore_dropped": 0,
            "semaphore_waited": 0,
        }

        self._pending_count = 0
        self._pending_lock = threading.Lock()
        self._all_done = threading.Event()
        self._all_done.set()

    # ================================================================
    # 启动
    # ================================================================
    def start(self):
        self._dispatcher.start()
        self._post_dispatcher.start()
        self._warmup()

    def _warmup(self, timeout: float = 300.0):
        """
        预热所有 worker。

        注意：仅仅创建 pipeline（加载模型）是**不够的**。TensorRT/cuDNN 在
        第一次真实推理时才会做上下文初始化、显存分配与 kernel 选择，
        每个 worker 的首帧因此会明显偏慢。原实现只 new 了对象就算预热完成，
        于是这笔开销被推迟到产线上的前 N 帧才付，表现为"刚开始几帧忽快忽慢"。
        这里补一次真实的空跑推理，把它前移到启动阶段。
        """
        info(f"[Pool] 预热 {self.num_workers} 个 pipeline...")
        done = threading.Event()
        lock = threading.Lock()
        remaining = [self.num_workers]

        def _warm():
            pipeline, wid = self._get_or_create_pipeline()
            self._warm_infer(pipeline, wid)
            with lock:
                remaining[0] -= 1
                if remaining[0] <= 0:
                    done.set()

        for _ in range(self.num_workers):
            self._infer_executor.submit(_warm)

        if done.wait(timeout=timeout):
            info("[Pool] 预热完成")
            self._memory_guard.update_baseline()
        else:
            info("[Pool] 预热超时")

    # ================================================================
    # 提交
    # ================================================================
    def submit(self, task: DetectionTask,
               blocking: Optional[bool] = None,
               timeout: Optional[float] = None) -> bool:
        if blocking is None:
            blocking = self._blocking_submit
        if timeout is None:
            timeout = self._blocking_timeout

        with self._lock:
            self._counters["submitted"] += 1

        if self._memory_guard.should_drop_frame():
            with self._lock:
                self._counters["memory_dropped"] += 1
                self._counters["dropped"] += 1
            return False

        acquired = self._inflight_sem.acquire(
            blocking=blocking,
            timeout=timeout if blocking else None)
        if not acquired:
            with self._lock:
                self._counters["semaphore_dropped"] += 1
                self._counters["dropped"] += 1
            return False

        if blocking:
            with self._lock:
                self._counters["semaphore_waited"] += 1

        with self._pending_lock:
            self._pending_count += 1
            self._all_done.clear()

        try:
            self._infer_queue.put_nowait(task)
        except queue.Full:
            try:
                old = self._infer_queue.get_nowait()
                old.image = None
                self._release_frame()
            except queue.Empty:
                pass

            with self._lock:
                self._counters["dropped"] += 1

            try:
                self._infer_queue.put_nowait(task)
            except queue.Full:
                task.image = None
                self._release_frame()
                return False

        return True

    @property
    def qsize(self) -> int:
        return self._infer_queue.qsize()

    # ================================================================
    # 帧释放
    # ================================================================
    def _release_frame(self):
        self._inflight_sem.release()
        with self._pending_lock:
            self._pending_count -= 1
            if self._pending_count <= 0:
                self._pending_count = 0
                self._all_done.set()

    @staticmethod
    def _warm_infer(pipeline, worker_id: int) -> None:
        """用一张空图跑一次推理，触发 TensorRT/cuDNN 的一次性初始化"""
        try:
            import numpy as np

            detector = getattr(pipeline, "detector", None)
            if detector is None:
                return
            size = int(getattr(detector, "image_size", 640) or 640)
            dummy = np.zeros((size, size, 3), dtype=np.uint8)

            t0 = time.time()
            detector.detect(dummy)
            dt = (time.time() - t0) * 1000
            info(f"[Pool] Worker-{worker_id} 空跑预热完成 ({dt:.0f}ms)")
        except Exception as e:
            # 预热失败不影响启动，只是首帧会慢一点
            info(f"[Pool] Worker-{worker_id} 空跑预热跳过: {e}")

    # ================================================================
    # 推理调度
    # ================================================================
    def _dispatch_loop(self):
        while not self._stop.is_set():
            try:
                task = self._infer_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            if self.skip_old_frames:
                drained = []
                while True:
                    try:
                        drained.append(
                            self._infer_queue.get_nowait())
                    except queue.Empty:
                        break

                all_tasks = [task] + drained
                latest: Dict[str, DetectionTask] = {}
                skipped = 0

                for t in all_tasks:
                    cam = t.camera_name
                    if cam in latest:
                        latest[cam].image = None
                        self._release_frame()
                        skipped += 1
                    latest[cam] = t

                if skipped:
                    with self._lock:
                        self._counters["skipped"] += skipped

                for t in latest.values():
                    self._infer_executor.submit(self._run_infer, t)
            else:
                self._infer_executor.submit(self._run_infer, task)

    # ================================================================
    # 推理执行
    # ================================================================
    def _get_or_create_pipeline(self):
        if getattr(self._tls, "pipeline", None) is not None:
            return self._tls.pipeline, self._tls.worker_id

        with self._pipeline_lock:
            wid = self._pipeline_count
            self._pipeline_count += 1

        t0 = time.time()
        p = self.pipeline_factory(wid)
        dt = time.time() - t0

        self._tls.pipeline = p
        self._tls.worker_id = wid

        with self._pipeline_lock:
            self._pipelines.append(p)

        info(f"[Pool] Pipeline-{wid} 绑定 "
             f"{threading.current_thread().name} ({dt:.1f}s)")
        return p, wid

    @staticmethod
    def _get_fabric_bbox(camera_name: str):
        """
        从布幅检测器获取缓存的布面 bbox（零计算开销）

        process_frame 已在 image_control 线程完成洪泛填充并缓存，
        这里只是读取缓存值。

        Returns: (x, y, w, h) 或 None
        """
        detector = get_fabric_detector()
        if detector is None:
            return None
        return detector.get_last_fabric_bbox(camera_name)

    @staticmethod
    def _detection_paused() -> bool:
        """人为停止检测（前端点了"停止"/"暂停"）"""
        try:
            import Communication_Tool.ThriftControl as TC
            return TC.isDetectionPaused()
        except Exception:
            return False

    def _run_infer(self, task: DetectionTask):
        # 人为停止后，队列里"在途"的帧不该再被检测。
        # 入口 image_control() 只挡得住新到的帧，已经排进队列和线程池的
        # 帧仍会跑完，并可能触发继电器、写停机图、发上传 —— 表现为
        # "点了停止，画面却还在报疵点"。这里直接丢弃。
        # 只对手动停止生效；缺陷停机属于系统自身行为，在途帧要正常处理完。
        if self._detection_paused():
            task.image = None
            self._release_frame()
            with self._lock:
                self._counters["skipped"] += 1
            return

        pipeline, wid = self._get_or_create_pipeline()
        task.infer_start = time.time()
        task.perf_infer_start = time.perf_counter()

        try:
            fabric_bbox = self._get_fabric_bbox(task.camera_name)

            result = pipeline.detect_image(
                task.image, task.filename, task.camera_name,
                capture_timestamp=task.capture_timestamp,
                fabric_bbox=fabric_bbox,
                camera_sn=task.camera_sn,
            )
            task.infer_end = time.time()
            task.perf_infer_end = time.perf_counter()

            post = PostProcessTask(
                result=result, task=task,
                worker_id=wid,
                yolo_finish_time=task.infer_end,
            )

            try:
                self._post_queue.put(post, timeout=5.0)
            except queue.Full:
                error(f"[Pool] 后处理队列满: {task.filename}")
                task.image = None
                self._release_frame()

            with self._lock:
                self._counters["processed"] += 1

        except Exception as e:
            task.infer_end = time.time()
            # ✅ 打印完整堆栈，方便定位根因
            error(
                f"[Pool] 推理失败: {e}\n"
                f"{traceback.format_exc()}"
            )
            with self._lock:
                self._counters["errors"] += 1
            task.image = None
            self._release_frame()

    # ================================================================
    # 后处理
    # ================================================================
    def _post_dispatch_loop(self):
        while not self._stop.is_set():
            try:
                pt = self._post_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._post_executor.submit(self._run_post, pt)

        while not self._post_queue.empty():
            try:
                pt = self._post_queue.get_nowait()
                self._post_executor.submit(self._run_post, pt)
            except queue.Empty:
                break

    def _run_post(self, pt: PostProcessTask):
        try:
            self._callback(pt.result, pt.task, pt.worker_id)
            with self._lock:
                self._counters["post_processed"] += 1
        except Exception as e:
            error(f"[Pool] 后处理失败: {e}")
        finally:
            pt.task.image = None
            pt.result = None
            self._release_frame()

    # ================================================================
    # 等待 / 统计 / 关闭
    # ================================================================
    def wait_until_done(self, timeout: float = 300.0) -> bool:
        with self._pending_lock:
            if self._pending_count <= 0:
                return True
        info(f"[Pool] 等待 {self._pending_count} 个任务...")
        return self._all_done.wait(timeout=timeout)

    def get_stats(self) -> Dict:
        with self._lock:
            stats = dict(self._counters)
        stats["workers"] = self.num_workers
        stats["infer_queue"] = self._infer_queue.qsize()
        stats["post_queue"] = self._post_queue.qsize()
        stats["pending"] = self._pending_count
        stats["max_inflight"] = self._max_inflight
        stats["blocking_mode"] = self._blocking_submit
        stats["memory"] = self._memory_guard.get_stats()
        return stats

    def get_all_pipeline_stats(self) -> List[Dict]:
        with self._pipeline_lock:
            return [p.get_stats() for p in self._pipelines]

    def iter_pipelines(self) -> List[Any]:
        """返回当前已创建的 pipeline 列表快照（线程安全）。

        供上层在不重建 pipeline 的前提下，就地刷新其内部引用
        （例如经纬线 StopHandler 的 launch_tag）。
        """
        with self._pipeline_lock:
            return list(self._pipelines)

    def shutdown(self, timeout: float = 30.0, drain: bool = True):
        if drain:
            self.wait_until_done(timeout=max(timeout * 3, 60))

        self._stop.set()

        for t in [self._dispatcher, self._post_dispatcher]:
            if t.is_alive():
                t.join(timeout=5)

        # 清空队列
        for q in [self._infer_queue, self._post_queue]:
            while not q.empty():
                try:
                    item = q.get_nowait()
                    if hasattr(item, 'image'):
                        item.image = None
                    if hasattr(item, 'task'):
                        item.task.image = None
                except queue.Empty:
                    break

        self._infer_executor.shutdown(
            wait=drain, cancel_futures=not drain)
        self._post_executor.shutdown(
            wait=drain, cancel_futures=not drain)

        with self._pipeline_lock:
            for i, p in enumerate(self._pipelines):
                try:
                    p.release()
                except Exception as e:
                    error(f"Pipeline-{i} 释放失败: {e}")

        info(f"[Pool] 已关闭 | {self.get_stats()}")