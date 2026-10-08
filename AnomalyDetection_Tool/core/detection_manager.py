#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YOLO 检测管理器 — 线程池隔离优化版
职责:
  1. 协调检测流水线 / 线程池
  2. 检测后处理（竖线过滤 → 面积分析 → 保存 → 上传）
  3. 统计汇总

已抽离:
  - 停机分析/保存/继电器 → StopMachineService
  - 通信模块 → CommService
  - 性能统计 → PerformanceTracker
  - 上传管理 → AsyncUploadManager
  - 路径/时间工具 → utils
"""

import os
import time
import threading
from datetime import datetime
from typing import Dict, Optional
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from General_Tool.EnhancedLogger import info, error

from AnomalyDetection_Tool.config.settings import (
    MACHINE_STOP_RULES, SaveConfig, SIMULATION_MODE,
    VERTICAL_LINE_FILTER, STOP_ANALYSIS_MODE, CONCURRENCY_CONFIG,
)
from AnomalyDetection_Tool.models.data_types import (
    DetectionResult, DetectionTask, BoxAnalysisEntry,
)
from AnomalyDetection_Tool.core.detection_pipeline import (
    DetectionPipeline,
)
from AnomalyDetection_Tool.core.multi_model_detector import MultiModelYOLODetector
from AnomalyDetection_Tool.core.detection_worker_pool import (
    DetectionWorkerPool,
)
from AnomalyDetection_Tool.output.result_saver import ResultSaver
from AnomalyDetection_Tool.output.detection_info_formatter import (
    format_detection_info,
)
from AnomalyDetection_Tool.upload.async_upload_manager import (
    AsyncUploadManager,
)
from AnomalyDetection_Tool.upload.upload_task import UploadTask
from AnomalyDetection_Tool.services.stop_machine_service import (
    StopMachineService,
)
from AnomalyDetection_Tool.services.comm_service import comm_service
from AnomalyDetection_Tool.stats.performance_tracker import (
    PerformanceTracker,
)
from AnomalyDetection_Tool.filter.vertical_line_filter import (
    VerticalLineFilter,
)
from AnomalyDetection_Tool.utils.path_utils import (
    to_upload_image_url,
)
from AnomalyDetection_Tool.utils.time_utils import (
    now_str, parse_timestamp, TIMESTAMP_FORMAT,
)
import cv2
from AnomalyDetection_Tool.services.WebSocketImageServer import get_websocket_server
try:
    import torch

    _TORCH_AVAILABLE = True
except ImportError:
    _TORCH_AVAILABLE = False


class YOLODetectionManager:
    """YOLO 检测管理器"""

    _LAUNCH_TAG: str = datetime.now().strftime("%Y%m%d%H%M%S")

    def __init__(self, config: dict, save_path: str,
                 save_config: SaveConfig,
                 class_thresholds: Dict[str, float] = None,
                 class_thresholds_by_id: Dict[int, float] = None,
                 default_class_threshold: float = 0.25,
                 machine_stop_path: str = None):

        self.config = config
        self.save_path = save_path
        self.save_config = save_config
        self.machine_stop_path = machine_stop_path
        self.launch_tag = self._LAUNCH_TAG

        # 用 `is None` 而非 `or {}`，避免把 settings 的热更新字典
        # 在启动瞬间为空时替换成普通 dict（那样就丧失热更新能力了）
        self.class_thresholds = (
            class_thresholds if class_thresholds is not None else {})
        self.class_thresholds_by_id = (
            class_thresholds_by_id if class_thresholds_by_id is not None else {})
        self.default_class_threshold = default_class_threshold

        # 子组件（延迟初始化）
        self.pipeline: Optional[DetectionPipeline] = None
        self.worker_pool: Optional[DetectionWorkerPool] = None
        self.result_saver: Optional[ResultSaver] = None
        self._upload_manager: Optional[AsyncUploadManager] = None
        self._stop_service: Optional[StopMachineService] = None
        self._perf_tracker: Optional[PerformanceTracker] = None
        self._vertical_filter: Optional[VerticalLineFilter] = None

        # 【优化】分离线程池：推理与 I/O 隔离
        self.infer_pool: Optional[ThreadPoolExecutor] = None
        self.io_pool: Optional[ThreadPoolExecutor] = None

        # 异步保存跟踪
        self._pending_async = 0
        self._pending_lock = threading.Lock()
        self._async_done = threading.Event()
        self._async_done.set()

        # 异步保存/上传线程池（避免每帧 new thread）
        # 规模由 config.yaml 的 concurrency.async_save_workers 控制
        self._async_executor = ThreadPoolExecutor(
            max_workers=max(1, int(
                CONCURRENCY_CONFIG.get("async_save_workers", 4))),
            thread_name_prefix="AsyncSave")

        # 统计
        self._lock = threading.Lock()
        self._stats = {
            "total_images": 0,
            "total_detections": 0,
            "total_boxes": 0,
            "upload_skipped_no_comm": 0,
            "upload_skipped_no_upload": 0,
            "upload_skipped_no_path": 0,
        }

        self._initialized = False

    # ================================================================
    # 配置读取便捷属性
    # ================================================================
    def _cfg(self, key, default=None):
        return self.config.get(key, default)

    # ================================================================
    # 初始化
    # ================================================================
    def initialize(self) -> bool:
        try:
            self._optimize_gpu()

            model_path = self._cfg("model_path")
            if not os.path.exists(model_path):
                error(f"YOLO 模型不存在: {model_path}")
                return False

            # 结果保存器
            self.result_saver = ResultSaver(
                self.save_path, self.save_config,
                self._cfg("patch_size", 640),
                launch_tag=self.launch_tag)

            # 停机服务
            self._stop_service = StopMachineService(
                machine_stop_path=self.machine_stop_path or "",
                launch_tag=self.launch_tag,
                patch_size=self._cfg("patch_size", 640),
                analysis_mode=STOP_ANALYSIS_MODE,
                pth_checkpoint=self._cfg("pth_checkpoint"),
                pth_device=self._cfg("pth_device"),
                pth_model_source_dir=self._cfg("pth_model_source_dir"),
                mask_threshold=self._cfg("mask_threshold", 0.3),
                dinomaly_use_fp16=self._cfg("dinomaly_use_fp16", False),
                use_cuda_stream=self._cfg("use_cuda_stream", False),
            )
            self._stop_service.initialize()

            # 竖线过滤
            if VERTICAL_LINE_FILTER.get("enabled"):
                self._vertical_filter = VerticalLineFilter(
                    target_cameras=set(
                        VERTICAL_LINE_FILTER["target_cameras"]),
                    target_classes=set(
                        VERTICAL_LINE_FILTER["target_classes"]),
                    max_x_deviation=(
                        VERTICAL_LINE_FILTER["max_x_deviation"]),
                    min_accumulate_count=(
                        VERTICAL_LINE_FILTER.get(
                            "min_accumulate_count", 3)),
                )

            # 性能追踪
            num_workers = self._cfg("num_workers", 1)
            self._perf_tracker = PerformanceTracker(
                num_workers=num_workers,
                analysis_mode=self._stop_service.analysis_mode,
            )

            # 上传管理器
            self._upload_manager = AsyncUploadManager()
            self._upload_manager.start()

            # 【优化】初始化线程池
            self.infer_pool = ThreadPoolExecutor(
                max_workers=max(1, int(
                    CONCURRENCY_CONFIG.get("infer_pool_workers", 2))),
                thread_name_prefix="Infer")
            self.io_pool = ThreadPoolExecutor(
                max_workers=max(1, int(
                    CONCURRENCY_CONFIG.get("io_pool_workers", 4))),
                thread_name_prefix="PostIO")

            # 检测流水线/线程池
            if num_workers >= 1:
                self._init_worker_pool()
            else:
                self._init_single_pipeline()

            self._initialized = True
            self._print_init_info()
            return True

        except Exception as e:
            error(f"初始化失败: {e}")
            import traceback
            traceback.print_exc()
            return False

    def _optimize_gpu(self):
        if not _TORCH_AVAILABLE or not torch.cuda.is_available():
            return
        if self._cfg("cuda_benchmark"):
            torch.backends.cudnn.benchmark = True
        if hasattr(torch.backends.cuda, 'matmul'):
            torch.backends.cuda.matmul.allow_tf32 = True
        if hasattr(torch.backends.cudnn, 'allow_tf32'):
            torch.backends.cudnn.allow_tf32 = True

    def _speed_detail(self) -> str:
        """
        取当前 worker 的推理分段耗时，附加到计时日志后面。

        "推理" 这个数字包含了 CPU 预处理 + GPU 推理 + NMS 三段，
        只看总数无法判断变慢发生在哪一侧，所以这里把明细带出来。
        """
        try:
            pool = self.worker_pool
            pipelines = pool.iter_pipelines() if pool else (
                [self.pipeline] if self.pipeline else [])
            for p in pipelines:
                det = getattr(p, "detector", None)
                s = det.speed_str() if det is not None else ""
                if s:
                    return f" [{s}]"
        except Exception:
            pass
        return ""

    def _get_device(self, worker_id: int = 0) -> str:
        """
        解析推理设备。

        取值来源为配置项 `yolo.pth_device`（此前这里硬编码 "cuda:0"，
        导致多卡机器无法分卡、配置改了不生效）。

        支持的配置写法:
          - "cpu"        → 强制 CPU
          - "cuda"       → 按 worker_id 轮询分配到各张卡（多卡负载均衡）
          - "cuda:N"     → 固定使用第 N 张卡
          - 未配置/None  → 等价于 "cuda"
        CUDA 不可用时一律回退到 "cpu"。
        """
        configured = (self._cfg("pth_device") or "cuda")
        configured = str(configured).strip().lower()

        if configured == "cpu":
            return "cpu"

        if not _TORCH_AVAILABLE or not torch.cuda.is_available():
            if configured.startswith("cuda"):
                error(f"[设备] 配置要求 {configured} 但 CUDA 不可用，回退到 CPU")
            return "cpu"

        device_count = torch.cuda.device_count()

        if configured.startswith("cuda:"):
            try:
                idx = int(configured.split(":", 1)[1])
            except ValueError:
                error(f"[设备] pth_device='{configured}' 格式非法，回退到 cuda:0")
                return "cuda:0"
            if idx >= device_count:
                error(f"[设备] 配置的 {configured} 超出可见 GPU 数量"
                      f"({device_count})，回退到 cuda:0")
                return "cuda:0"
            return f"cuda:{idx}"

        # "cuda"：按 worker 轮询分卡
        return f"cuda:{worker_id % device_count}"

    def _make_pipeline(self, worker_id: int = 0):
        detector = MultiModelYOLODetector(
            default_model_path=self._cfg("model_path"),
            with_edge_model_path=self._cfg("with_edge_model_path"),
            without_edge_model_path=self._cfg("without_edge_model_path"),
            confidence=self._cfg("confidence"),
            device=self._get_device(worker_id),
            image_size=self._cfg("image_size", 640),
            iou_threshold=self._cfg("iou_threshold", 0.5),
            worker_id=worker_id,
            use_cuda_stream=self._cfg("use_cuda_stream", True),
        )

        return DetectionPipeline(
            detector=detector,
            output_dir=self.save_path,
            save_config=self.save_config,
            class_thresholds=self.class_thresholds,
            class_thresholds_by_id=self.class_thresholds_by_id,
            default_class_threshold=self.default_class_threshold,
            external_saver=self.result_saver,
            worker_id=worker_id,
            patch_size=self._cfg("patch_size", 640),
            launch_tag=self.launch_tag,
            machine_stop_path=self.machine_stop_path or self.save_path,
            upload_manager=self._upload_manager,
        )

    def _init_worker_pool(self):
        self.worker_pool = DetectionWorkerPool(
            num_workers=self._cfg("num_workers", 1),
            pipeline_factory=self._make_pipeline,
            result_callback=self._on_worker_result,
            max_queue_size=self._cfg("queue_size", 32),
            skip_old_frames=not SIMULATION_MODE,
            post_workers=self._cfg("post_workers", 2),
            max_inflight_frames=self._cfg("max_inflight_frames", 12),
            memory_high_watermark=self._cfg("memory_high_watermark", 90),
            memory_critical_watermark=self._cfg(
                "memory_critical_watermark", 95),
            blocking_submit=SIMULATION_MODE,
            blocking_timeout=30.0,
        )
        self.worker_pool.start()

    def _init_single_pipeline(self):
        self.pipeline = self._make_pipeline(0)

    def _print_init_info(self):
        mode = self._stop_service.analysis_mode
        comm = comm_service.status_summary()
        info(
            f"YOLO 管理器初始化成功"
            f"\n  模型: {os.path.basename(self._cfg('model_path'))}"
            f"\n  分析模式: {mode}"
            f"\n  启动标签: {self.launch_tag}"
            f"\n  通信: {comm}"
            f"\n  Workers: {self._cfg('num_workers', 1)}"
        )

    # ================================================================
    # 外部入口
    # ================================================================
    def detect(self, image, filename, camera_name,
               camera_sn="", capture_timestamp=None,
               metadata=None):
        if not self._initialized:
            error("管理器未初始化")
            return None

        # ========== 时间追踪1：开始检测（perf_counter 在 DetectionTask 构造时自动记录）==========
        info(f'[时间追踪1] 开始检测 | {camera_name} | {filename} | '
             f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]}')
        # ========================================

        ts = capture_timestamp or now_str()

        if self.worker_pool is not None:
            task = DetectionTask(
                image=image, filename=filename,
                camera_name=camera_name, camera_sn=camera_sn,
                capture_timestamp=ts, metadata=metadata)
            ok = self.worker_pool.submit(task)
            if not ok:
                task.image = None
            return None

        # 【优化】异步提交推理任务，不阻塞回调
        self.infer_pool.submit(self._run_inference_task, image, filename, camera_name, camera_sn, ts, metadata)
        return None

    def _run_inference_task(self, image, filename, camera_name, camera_sn, ts, metadata):
        """在 infer_pool 中执行推理"""
        t_enter = time.time()
        try:
            result = self.pipeline.detect_image(image, filename, capture_timestamp=ts, camera_sn=camera_sn)

            # 【优化】将后处理交给 io_pool，实现 I/O 与计算隔离
            self.io_pool.submit(self._process_result, result, image, camera_name, camera_sn, filename, ts, metadata,
                                t_enter)
        except Exception as e:
            error(f"推理任务失败: {e}")

    def _on_worker_result(self, result, task, worker_id):
        try:
            if result is None:
                error(f"[_on_worker_result] result=None [{task.filename}]")
                return

            self._process_result(
                result=result, image=task.image,
                camera_name=task.camera_name,
                camera_sn=task.camera_sn,
                filename=task.filename,
                capture_timestamp=task.capture_timestamp,
                metadata=task.metadata,
                t_enter=task.submit_time,
                t_infer_start=task.infer_start,
                t_infer_end=task.infer_end,
                perf_submit=task.perf_submit,
                perf_infer_start=task.perf_infer_start,
                perf_infer_end=task.perf_infer_end)
        except Exception as e:
            import traceback
            error(f"后处理失败 [{task.filename}]: {e}")
            error(traceback.format_exc())  # ← 加这一行，看完整堆栈

    def _detect_sync(self, image, filename, camera_name,
                     camera_sn, capture_timestamp, metadata):
        t_enter = time.time()
        try:
            t_start = time.time()
            result = self.pipeline.detect_image(
                image, filename,
                capture_timestamp=capture_timestamp, camera_sn=camera_sn)
            t_end = time.time()
            self._process_result(
                result=result, image=image,
                camera_name=camera_name, camera_sn=camera_sn,
                filename=filename,
                capture_timestamp=capture_timestamp,
                metadata=metadata, t_enter=t_enter,
                t_infer_start=t_start, t_infer_end=t_end)
            return result
        except Exception as e:
            error(f"检测失败 [{filename}]: {e}")
            return None

    # ================================================================
    # 后处理
    # ================================================================
    def _process_result(self, result, image, camera_name,
                        camera_sn, filename, capture_timestamp,
                        metadata, t_enter,
                        t_infer_start=None, t_infer_end=None,
                        perf_submit=0.0, perf_infer_start=0.0, perf_infer_end=0.0):
        t_post_begin = time.time()
        perf_post_begin = time.perf_counter()

        # 计时
        if t_infer_start and t_infer_end and t_infer_start > 0:
            queue_wait = t_infer_start - t_enter
            yolo_time = t_infer_end - t_infer_start
        else:
            queue_wait = 0.0
            yolo_time = t_post_begin - t_enter

        capture_delay = 0.0
        try:
            t_cap = parse_timestamp(capture_timestamp)
            capture_delay = t_enter - t_cap
        except Exception:
            self._perf_tracker.record_parse_fail()

        with self._lock:
            self._stats["total_images"] += 1
            if result.has_detection:
                self._stats["total_detections"] += 1
                self._stats["total_boxes"] += result.total_boxes

        if not result.has_detection:
            perf_end = time.perf_counter()
            e2e_ms = (perf_end - perf_submit) * 1000 if perf_submit > 0 else 0
            infer_ms = (perf_infer_end - perf_infer_start) * 1000 if perf_infer_start > 0 else 0
            self._perf_tracker.record(
                capture_delay, queue_wait, yolo_time, 0.0, 0.0)
            # ========== 无检测完成 ==========
            info(f'无检测处理完成 | {camera_name} | {filename} | '
                 f'端到端={e2e_ms:.1f}ms 推理={infer_ms:.1f}ms'
                 f'{self._speed_detail()}')
            # ========================================

            # ====== 无缺陷：推送原图 ======
            ws = get_websocket_server()
            if ws is not None and image is not None:
                ws.push_image(camera_name, image)
            # ========================================
            return

        t_post_start = time.time()

        # 竖线过滤
        if self._vertical_filter and result.boxes:
            filtered_idx = self._vertical_filter.check_and_filter(
                camera_name, result.boxes)
            if filtered_idx:
                result.boxes = [
                    b for i, b in enumerate(result.boxes)
                    if i not in filtered_idx]
                result.total_boxes = len(result.boxes)
                result.has_detection = result.total_boxes > 0
                result.max_confidence = max(
                    (b.confidence for b in result.boxes), default=0)
                if not result.has_detection:
                    self._perf_tracker.record(
                        capture_delay, queue_wait, yolo_time,
                        0.0, time.time() - t_post_start)
                    return

        # 面积分析
        t_analysis_start = time.time()
        ba_list = []
        frame_stopped = False
        log_lines = []  # 收集每帧的分析日志，统一输出

        for i, box in enumerate(result.boxes):
            if frame_stopped:
                ba_list.append(BoxAnalysisEntry.empty(box, i))
                log_lines.append(f"  [跳过] box[{i}] {box.class_name} 帧内已停机")
                continue

            # ── 按 trigger_mode 路由到不同分析方法 ───────────────
            rule = StopMachineService.get_stop_rule(box.class_name)
            trigger_mode = (rule or {}).get("trigger_mode", "area")

            if trigger_mode == "dimension":
                # 折边不入等：纯框坐标换算，无需 GPU / 分析器
                ar = self._stop_service.analyze_defect_dimension(
                    image, box, camera_name, filename,
                    camera_sn=camera_sn,
                    capture_timestamp=capture_timestamp,
                    frame_already_stopped=frame_stopped,
                    log_lines=log_lines,
                    box_index=i,
                )

            else:
                # 默认 area 模式：毛羽 / 纬线污染 等，走原有面积分析
                ar = self._stop_service.analyze_defect_area(
                    image, box, camera_name, filename,
                    camera_sn=camera_sn,
                    capture_timestamp=capture_timestamp,
                    frame_already_stopped=frame_stopped,
                    log_lines=log_lines,
                    box_index=i,
                )

            if ar.triggered:
                frame_stopped = True

            entry = BoxAnalysisEntry(
                box=box, index=i,
                is_stop=ar.triggered,
                analyzed=ar.analyzed,
                area_result=ar,
            )
            ba_list.append(entry)

        t_analysis_end = time.time()
        perf_analysis_end = time.perf_counter()
        analysis_time = t_analysis_end - t_analysis_start
        postprocess_time = (time.time() - t_post_start) - analysis_time

        # ========== 端到端耗时统计（submit -> 停机判定完成）==========
        e2e_ms = (perf_analysis_end - perf_submit) * 1000 if perf_submit > 0 else 0
        infer_ms = (perf_infer_end - perf_infer_start) * 1000 if perf_infer_start > 0 else 0
        queue_ms = (perf_infer_start - perf_submit) * 1000 if perf_infer_start > 0 else 0
        analysis_ms = (perf_analysis_end - perf_post_begin) * 1000
        info(f'[计时] {camera_name} | {filename} | '
             f'端到端={e2e_ms:.1f}ms '
             f'(排队={queue_ms:.1f} 推理={infer_ms:.1f} 后处理={analysis_ms:.1f}) | '
             f'boxes={result.total_boxes} stop={frame_stopped}'
             f'{self._speed_detail()}')
        # ========================================

        self._perf_tracker.record(
            capture_delay, queue_wait, yolo_time,
            analysis_time, postprocess_time)

        # 异步保存+上传
        self._submit_async(
            self._async_save_and_upload,
            result, image, camera_name, camera_sn,
            filename, ba_list)

    # ================================================================
    # 异步保存+上传
    # ================================================================
    def _submit_async(self, fn, *args):
        """将异步任务提交到专用线程池，实现 I/O 与计算隔离"""
        with self._pending_lock:
            self._pending_async += 1
            self._async_done.clear()

        def _runner():
            try:
                fn(*args)
            finally:
                with self._pending_lock:
                    self._pending_async -= 1
                    if self._pending_async <= 0:
                        self._pending_async = 0
                        self._async_done.set()

        self._async_executor.submit(_runner)

    def _async_save_and_upload(self, result, image,
                               camera_name, camera_sn,
                               filename, ba_list):
        """后台线程：保存 + 上传分发"""
        saved = {}
        if self.result_saver:
            normal_boxes = [e.box for e in ba_list if not e.is_stop]
            normal_result = DetectionResult(
                filename=result.filename,
                original_width=result.original_width,
                original_height=result.original_height,
                has_detection=len(normal_boxes) > 0,
                total_boxes=len(normal_boxes),
                max_confidence=max(
                    (b.confidence for b in normal_boxes), default=0),
                boxes=normal_boxes,
                capture_timestamp=result.capture_timestamp,
                detection_timestamp=result.detection_timestamp,
                process_time_ms=result.process_time_ms,
                raw_boxes_count=result.raw_boxes_count,
                filtered_count=result.filtered_count,
            )
            saved = self.result_saver.save(
                result=normal_result,
                original_image=image,
                camera_name=camera_name,
                camera_sn=camera_sn,
                capture_timestamp=result.capture_timestamp,
            )
        else:
            info("[上传诊断] result_saver 为 None，跳过保存")

        anomaly_path = saved.get("anomaly_image")

        if not comm_service.thrift_available:
            with self._lock:
                self._stats["upload_skipped_no_comm"] += 1
            info(f"[上传跳过] ThriftControl 不可用  "
                 f"camera={camera_name} file={filename}")
            return

        if not comm_service.upload_available:
            with self._lock:
                self._stats["upload_skipped_no_upload"] += 1
            info(f"[上传跳过] http_client 不可用  "
                 f"camera={camera_name} file={filename}")
            return

        # 兜底保存原图
        if not anomaly_path:
            has_stop = any(e.is_stop for e in ba_list)
            if not has_stop and self.result_saver:
                try:
                    anomaly_path = self.result_saver.ensure_anomaly_image(
                        image, camera_name, result.filename)
                except Exception as e:
                    error(f"[上传诊断] 兜底保存失败: {e}")

        info(f"[上传分发] camera={camera_name} file={filename} "
             f"boxes={len(ba_list)} normal_path={anomaly_path}")

        self._dispatch_uploads(
            ba_list, anomaly_path, camera_sn,
            camera_name, filename, image)

    def _dispatch_uploads(self, ba_list, anomaly_path,
                          camera_sn, camera_name,
                          filename, image):
        if not self._upload_manager:
            return

        for entry in ba_list:
            box = entry.box
            ar = entry.area_result

            # 这个就是我们上传给http的信息
            det_info = format_detection_info(
                camera_sn=camera_sn,
                camera_name=camera_name,
                image=image, box=box,
                analysis_result={
                    'real_area_mm2': ar.real_area_mm2,
                    'fill_ratio': ar.fill_ratio,
                    'debug_info': ar.debug_info,
                },
                should_stop=entry.is_stop,
                frame_number=filename,
            )

            real_path = (ar.stop_image_path
                         if entry.is_stop
                         else (anomaly_path or ""))
            if not real_path:
                with self._lock:
                    self._stats["upload_skipped_no_path"] += 1
                continue

            # 统一走 to_upload_image_url：相对路径 + launch_tag 前缀补齐
            upload_path = to_upload_image_url(
                real_path, self.save_path, self.launch_tag)

            det_info["imageUrl"] = upload_path

            task = UploadTask(
                image_path=upload_path,
                camera_sn=camera_sn,
                camera_name=camera_name,
                frame_number=filename,
                class_name=box.class_name,
                class_id=box.class_id,
                confidence=box.confidence,
                x1=int(box.x1), y1=int(box.y1),
                x2=int(box.x2), y2=int(box.y2),
                is_stop=entry.is_stop,
                real_area_mm2=ar.real_area_mm2,
                fill_ratio=ar.fill_ratio,
                detection_info=det_info,
            )

            if entry.is_stop:
                self._upload_manager.submit_stop(task)
            else:
                self._upload_manager.submit_normal(task)

    # ================================================================
    # 等待 / 统计 / 关闭
    # ================================================================
    def wait_until_done(self, timeout: float = 300.0) -> bool:
        ok = True
        if self.worker_pool:
            ok = self.worker_pool.wait_until_done(timeout)

        if not self._async_done.wait(timeout=min(timeout, 30)):
            ok = False

        if self._upload_manager:
            ok = self._upload_manager.wait_until_done(
                timeout=min(timeout, 60)) and ok

        return ok

    # ================================================================
    # 批次标签刷新（前端点「开始」时调用）
    # ================================================================
    def reset_launch_tag(self, wait_inflight: bool = True) -> str:
        """重新生成本次批次的 launch_tag，并就地刷新所有依赖它
        决定保存子目录的组件：ResultSaver / StopMachineService /
        MachineStopSaver / 各 pipeline 内 StopHandler。

        场景：前端点「开始」开启一次新生产批次，让本批次的疵点、
        停机图写入一个独立目录（base_path/<launch_tag>/...），
        不再与上一批混在同一启动标签下。

        若 wait_inflight=True（推荐），切换 tag 前会先阻塞等待当前
        inflight 任务（worker_pool + 异步保存 + upload_manager）完成，
        避免旧帧已写盘但走新 tag，造成上传相对路径错位。
        """
        if wait_inflight:
            try:
                self.wait_until_done(timeout=15)
            except Exception as e:
                error(f"[reset_launch_tag] wait inflight 失败: {e}（继续刷新 tag）")

        new_tag = datetime.now().strftime("%Y%m%d%H%M%S")
        old_tag = self.launch_tag
        self.launch_tag = new_tag

        # —— 一般疵点：ResultSaver（Normal 目录）——
        if self.result_saver is not None:
            self.result_saver.launch_tag = new_tag

        # —— 疵点框停机：StopMachineService + MachineStopSaver ——
        if self._stop_service is not None:
            self._stop_service.launch_tag = new_tag
            # 同步惰性创建用的标签，否则「启动时停机规则关闭、
            # 运行中打开」时新建的 saver 会沿用上一批次的旧标签
            if hasattr(self._stop_service, "_saver_launch_tag"):
                self._stop_service._saver_launch_tag = new_tag
            saver = getattr(self._stop_service, "machine_stop_saver", None)
            if saver is not None:
                saver.launch_tag = new_tag

        # —— 经纬线停机：每个 pipeline 内的 StopHandler ——
        pipelines = []
        if self.worker_pool is not None:
            pipelines = self.worker_pool.iter_pipelines()
        elif self.pipeline is not None:
            pipelines = [self.pipeline]
        for p in pipelines:
            stop_handler = getattr(p, "_stop_handler", None)
            if stop_handler is not None:
                stop_handler.launch_tag = new_tag

        info(f"[reset_launch_tag] 批次标签刷新: {old_tag} -> {new_tag}")
        return new_tag

    def get_stats(self) -> dict:
        with self._lock:
            s = dict(self._stats)

        s["analysis_mode"] = (
            self._stop_service.analysis_mode
            if self._stop_service else "unknown")
        s["launch_tag"] = self.launch_tag

        if self._stop_service:
            s["stop_service"] = self._stop_service.get_stats()
        if self.worker_pool:
            s["pool"] = self.worker_pool.get_stats()
        if self._upload_manager:
            s["upload"] = self._upload_manager.get_stats()
        if self._perf_tracker:
            s["timing"] = self._perf_tracker.get_stats()

        with self._pending_lock:
            s["pending_async"] = self._pending_async

        return s

    def shutdown(self):
        self.wait_until_done(timeout=120)

        if self.worker_pool:
            self.worker_pool.shutdown(drain=False)
            self.worker_pool = None
        elif self.pipeline:
            self.pipeline.release()
            self.pipeline = None

        # 关闭异步保存线程池
        if self._async_executor:
            self._async_executor.shutdown(wait=True, cancel_futures=False)
            self._async_executor = None

        if self.infer_pool:
            self.infer_pool.shutdown(wait=True)
            self.infer_pool = None

        if self.io_pool:
            self.io_pool.shutdown(wait=True)
            self.io_pool = None

        if self._upload_manager:
            self._upload_manager.shutdown(drain_timeout=30)
            self._upload_manager = None

        if self._stop_service:
            self._stop_service.release()
            self._stop_service = None

        # 停止全局定时上报线程。
        # 它是 daemon 线程，不显式停会在关闭过程中继续发 HTTP 请求，
        # 打印出一堆"连接被拒绝"的噪声日志，并可能拖慢进程退出。
        try:
            from AnomalyDetection_Tool.services.periodic_report_service import (
                get_global_scheduler,
            )
            get_global_scheduler().stop()
        except Exception as e:
            error(f"[关闭] 停止定时上报调度器失败: {e}")

        self._initialized = False
        info("YOLO 管理器已关闭")
