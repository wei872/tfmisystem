#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
异步上传管理器
底层上传实现直接来自 Communication_Tool.http_client
"""

import queue
import asyncio
import threading
import time
import json
from typing import Dict, Any

from General_Tool.EnhancedLogger import info, error
from AnomalyDetection_Tool.config.settings import get_upload_config
from AnomalyDetection_Tool.upload.upload_task import UploadTask
from Communication_Tool.http_client import (
    upload_detection_info,
)


class AsyncUploadManager:
    """异步上传管理器"""

    # 连续失败达到此阈值后，后续上传直接跳过（避免网络不可用时堆积）
    _CONSECUTIVE_FAIL_THRESHOLD = 5

    def __init__(self):
        cfg = get_upload_config()

        self._num_workers = cfg.get("normal_upload_workers", 3)
        self._queue_size = cfg.get("normal_upload_queue_size", 256)
        self._normal_timeout = cfg.get("normal_upload_timeout", 10)
        self._stop_timeout = cfg.get("stop_upload_timeout", 3)
        self._max_retry = cfg.get("max_retry", 5)
        self._retry_interval = cfg.get("retry_interval", 1.0)

        self._queue: queue.Queue[UploadTask] = queue.Queue(
            maxsize=self._queue_size)
        self._workers = []
        self._stop_event = threading.Event()

        # 连续失败计数器（任意一次成功则重置）
        self._consecutive_failures = 0
        self._upload_disabled = False

        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_event_loop,
            daemon=True, name="UploadEventLoop")

        self._lock = threading.Lock()
        self._stats = {
            "normal_submitted": 0,
            "normal_uploaded": 0,
            "normal_failed": 0,
            "normal_dropped": 0,
            "normal_retried": 0,
            "stop_submitted": 0,
            "stop_uploaded": 0,
            "stop_failed": 0,
            "upload_api_false_count": 0,
        }
        self._started = False

    def _run_event_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def start(self):
        if self._started:
            return

        self._stop_event.clear()
        self._loop_thread.start()

        for i in range(self._num_workers):
            t = threading.Thread(
                target=self._worker_loop, args=(i,),
                daemon=True, name=f"UploadWorker-{i}")
            t.start()
            self._workers.append(t)

        self._started = True
        info(f"[AsyncUpload] 已启动 {self._num_workers} 个上传线程"
             f" (队列={self._queue_size},"
             f" 普通超时={self._normal_timeout}s,"
             f" 停机超时={self._stop_timeout}s,"
             f" 最大重试={self._max_retry})")
    # 普通疵点的提交走的是队列，停机疵点不走队列，检测到疵点直接插队上传
    def submit_normal(self, task: UploadTask):
        # 上传已禁用（连续失败过多），直接丢弃
        if self._upload_disabled:
            with self._lock:
                self._stats["normal_dropped"] += 1
            return

        with self._lock:
            self._stats["normal_submitted"] += 1

        try:
            self._queue.put_nowait(task)
        except queue.Full:
            try:
                dropped = self._queue.get_nowait()
                with self._lock:
                    self._stats["normal_dropped"] += 1
                info(f"[AsyncUpload] 队列满，丢弃旧任务: "
                     f"{dropped.class_name} ← {dropped.camera_name}")
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(task)
            except queue.Full:
                with self._lock:
                    self._stats["normal_dropped"] += 1

    def submit_stop(self, task: UploadTask):
        with self._lock:
            self._stats["stop_submitted"] += 1

        threading.Thread(
            target=self._do_stop_upload,
            args=(task,), daemon=True,
            name=f"StopUpload-{task.camera_name}",
        ).start()

    # ================================================================
    # Worker
    # ================================================================
    def _worker_loop(self, worker_id: int):
        count = 0
        try:
            while not self._stop_event.is_set():
                try:
                    task = self._queue.get(timeout=1.0)
                except queue.Empty:
                    continue

                success = self._do_upload(task, self._normal_timeout)
                if success:
                    with self._lock:
                        self._stats["normal_uploaded"] += 1
                    count += 1
                else:
                    task.retry_count += 1
                    if task.retry_count < self._max_retry:
                        with self._lock:
                            self._stats["normal_retried"] += 1
                        time.sleep(self._retry_interval)
                        try:
                            self._queue.put_nowait(task)
                        except queue.Full:
                            with self._lock:
                                self._stats["normal_failed"] += 1
                            error(f"[AsyncUpload] 重试队列满: "
                                  f"{task.class_name} ← {task.camera_name}")
                    else:
                        with self._lock:
                            self._stats["normal_failed"] += 1
                        error(f"[AsyncUpload] 普通上传最终失败 "
                              f"(重试{self._max_retry}次): "
                              f"{task.class_name} ← {task.camera_name}")
        finally:
            pass

        info(f"[UploadWorker-{worker_id}] 退出 (共上传 {count})")

    # ================================================================
    # 停机上传
    # ================================================================
    def _do_stop_upload(self, task: UploadTask):
        try:
            for attempt in range(self._max_retry):
                success = self._do_upload(task, self._stop_timeout)
                if success:
                    with self._lock:
                        self._stats["stop_uploaded"] += 1
                    info(f"[停机上传成功] {task.class_name} "
                         f"conf={task.confidence:.2f} "
                         f"← {task.camera_name} "
                         f"(第{attempt + 1}次尝试)")
                    return

                if attempt < self._max_retry - 1:
                    info(f"[停机上传重试] 第{attempt + 2}次 "
                         f"{task.class_name} ← {task.camera_name}")
                    time.sleep(0.5)

            with self._lock:
                self._stats["stop_failed"] += 1
            error(f"[停机上传失败] 重试{self._max_retry}次仍失败: "
                  f"{task.class_name} ← {task.camera_name}")
        except Exception as e:
            with self._lock:
                self._stats["stop_failed"] += 1
            error(f"[停机上传异常] {e}")

    # ================================================================
    # 实际上传
    # ================================================================
    def _do_upload(self, task: UploadTask,
                   timeout: float) -> bool:
        label = "停机" if task.is_stop else "普通"

        # ========== 进入上传时就打印 JSON ==========
        json_str = json.dumps(
            task.detection_info, ensure_ascii=False, indent=2)
        info(f"\n  [上传请求] [{label}] "
             f"{task.class_name} conf={task.confidence:.2f} "
             f"← {task.camera_name})"
             f"\n{json_str}")
        # ==========================================

        try:
            future = asyncio.run_coroutine_threadsafe(
                asyncio.wait_for(
                    upload_detection_info(
                        detection_info=task.detection_info),
                    timeout=timeout,
                ),
                self._loop,
            )
            result = future.result(timeout=timeout + 2)

            if not result:
                with self._lock:
                    self._stats["upload_api_false_count"] += 1
                    self._consecutive_failures += 1
                    if self._consecutive_failures >= self._CONSECUTIVE_FAIL_THRESHOLD and not self._upload_disabled:
                        self._upload_disabled = True
                        error(f"[AsyncUpload] 连续 {self._consecutive_failures} 次上传失败，后续上传将跳过")
                error(f"[AsyncUpload] [{label}] 接口返回失败: "
                      f"result={result!r}  "
                      f"{task.class_name} conf={task.confidence:.2f} "
                      f"← {task.camera_name}  "
                      f"retry={task.retry_count}/{self._max_retry}")
                return False

            # 成功：重置连续失败计数器
            with self._lock:
                self._consecutive_failures = 0
                if self._upload_disabled:
                    self._upload_disabled = False
                    info("[AsyncUpload] 上传恢复正常")
            info(f"\n [上传成功] [{label}] "
                 f"{task.class_name} ← {task.camera_name}")
            return True

        except asyncio.TimeoutError:
            with self._lock:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._CONSECUTIVE_FAIL_THRESHOLD and not self._upload_disabled:
                    self._upload_disabled = True
                    error(f"[AsyncUpload] 连续 {self._consecutive_failures} 次上传失败，后续上传将跳过")
            error(f"[AsyncUpload] [{label}] 上传超时 ({timeout}s): "
                  f"{task.class_name} ← {task.camera_name}  "
                  f"retry={task.retry_count}/{self._max_retry}")
            return False

        except Exception as e:
            with self._lock:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._CONSECUTIVE_FAIL_THRESHOLD and not self._upload_disabled:
                    self._upload_disabled = True
                    error(f"[AsyncUpload] 连续 {self._consecutive_failures} 次上传失败，后续上传将跳过")
            error(f"[AsyncUpload] [{label}] 上传异常: {e}  "
                  f"{task.class_name} ← {task.camera_name}  "
                  f"retry={task.retry_count}/{self._max_retry}")
            import traceback
            traceback.print_exc()
            return False

    # ================================================================
    # 管理
    # ================================================================
    def pending_count(self) -> int:
        return self._queue.qsize()

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            stats = self._stats.copy()
        stats["queue_size"] = self._queue.qsize()
        stats["queue_capacity"] = self._queue_size
        stats["workers"] = self._num_workers
        stats["running"] = self._started
        return stats

    def wait_until_done(self, timeout: float = 60.0) -> bool:
        t0 = time.time()
        last_log = 0
        while True:
            remaining = self._queue.qsize()
            if remaining == 0:
                return True
            elapsed = time.time() - t0
            if elapsed > timeout:
                info(f"[AsyncUpload] 等待超时，仍有 "
                     f"{remaining} 条未上传")
                return False
            if int(elapsed) - last_log >= 5:
                last_log = int(elapsed)
                info(f"[AsyncUpload] 等待中 {elapsed:.0f}s "
                     f"剩余: {remaining}")
            time.sleep(0.5)

    def shutdown(self, drain_timeout: float = 30.0):
        if not self._started:
            return

        remaining = self._queue.qsize()
        if remaining > 0:
            info(f"[AsyncUpload] 关闭中，等待 {remaining} 个任务...")

        self._stop_event.set()

        for t in self._workers:
            t.join(
                timeout=drain_timeout / max(len(self._workers), 1))

        alive = sum(1 for t in self._workers if t.is_alive())
        if alive:
            info(f"[AsyncUpload] {alive} 个线程未退出")

        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)

        self._workers.clear()
        self._started = False

        stats = self.get_stats()
        info(f"[AsyncUpload] 已关闭  统计: {stats}")