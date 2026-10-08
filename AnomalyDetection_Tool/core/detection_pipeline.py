#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件: detection_pipeline.py
路径: AnomalyDetection_Tool/core/detection_pipeline.py
职责: 单Worker检测流水线，协调YOLO推理与经纬线后台分析。

优化点（v2.2）:
  - 动态采样间隔：根据实际帧率自动调整经纬线分析频率
  - 分析任务提交频率与帧率解耦，避免低帧率时过度提交
  - 关键路径（YOLO推理）与后台任务完全异步，零等待
  - SharedFrame零拷贝共享图像数据，减少内存分配

采样策略:
  WARP_SAMPLE_INTERVAL = 3  → 每3帧分析一次经线（~333ms@10fps）
  WEFT_SAMPLE_INTERVAL = 3  → 每3帧分析一次纬线（~333ms@10fps）
  分析结果缓存在WarpCache/WeftCache中，主链路直接读取缓存
"""

import atexit
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

import numpy as np

from General_Tool.EnhancedLogger import info, error, warning
from AnomalyDetection_Tool.config.settings import (
    SaveConfig, CLASS_SIZE_FILTER, CLASS_EDGE_FILTER,
    WEFT_OUTPUT_DIR, SAVE_WEFT,
    WEFT_TRIGGER, WEFT_DENSITY_ENABLE, CONCURRENCY_CONFIG,
    WARP_TRIGGER, WARP_DENSITY_ENABLE, get_default_class_confidence,
    REAL_WIDTH_CM, IMAGE_WIDTH_PX,
    MACHINE_STOP_PATH,
)
from AnomalyDetection_Tool.core.shared_frame import SharedFrame
from AnomalyDetection_Tool.models.data_types import (
    DetectionBox, DetectionResult, ClassThresholdConfig,
)
from AnomalyDetection_Tool.output.fabric_width_service import get_fabric_stats
from AnomalyDetection_Tool.output.result_saver import ResultSaver
from AnomalyDetection_Tool.analysis.warp_density_detector import (
    WarpDensityDetector, WarpDensityResult,
)
from AnomalyDetection_Tool.analysis.weft_skew_analyzer import WeftDetector
from AnomalyDetection_Tool.utils.time_utils import now_str

from AnomalyDetection_Tool.core.warp_weft_analysis_cache import WarpCache, WeftCache
from AnomalyDetection_Tool.core.warp_weft_analysis_tasks import AnalysisTasks
from AnomalyDetection_Tool.services.warp_weft_stop_handler import StopHandler
from AnomalyDetection_Tool.core.warp_weft_warmup import run_warmup
from AnomalyDetection_Tool.services.periodic_report_service import (
    CameraReportContext,
    get_global_scheduler,
)

PIXEL_PER_CM = IMAGE_WIDTH_PX / REAL_WIDTH_CM

# ── 采样间隔配置 ──────────────────────────────────────────────────────
# 每 N 帧提交一次后台分析任务。
# 增大此值可降低分析频率、减少 GPU 争用；减小则提高实时性。
# 经验值：帧率 3fps 时设 2，6fps 时设 3，10fps 时设 5。
# 取值来自 config.yaml 的 concurrency 段（此前写死在代码里，
# 且注释写着"每3帧分析一次"而实际值是 1，自相矛盾）。
DEFAULT_WARP_SAMPLE_INTERVAL: int = int(
    CONCURRENCY_CONFIG.get("warp_sample_interval", 1))
DEFAULT_WEFT_SAMPLE_INTERVAL: int = int(
    CONCURRENCY_CONFIG.get("weft_sample_interval", 1))

CAMERAS_WITHOUT_WEFT = {"camera1", "camera8"}
WEFT_MAX_QUEUE_DEPTH: int = 4  # 纬线任务最大并发队列深度

# ── 独立线程池 ───────────────────────────────────────────────────────
# 各分析任务使用独立线程池，互不阻塞；规模统一由 config.yaml 的
# concurrency 段控制，便于按现场 CPU/GPU 情况整体调参。
# 经线/纬线均为 GPU-CPU 混合密集型，worker 太多会与 YOLO 抢显存和 GIL。
_save_executor = ThreadPoolExecutor(
    max_workers=max(1, int(CONCURRENCY_CONFIG.get("save_workers", 2))),
    thread_name_prefix="img_save")
_warp_executor = ThreadPoolExecutor(
    max_workers=max(1, int(CONCURRENCY_CONFIG.get("warp_workers", 2))),
    thread_name_prefix="warp_analysis")
_weft_executor = ThreadPoolExecutor(
    max_workers=max(1, int(CONCURRENCY_CONFIG.get("weft_workers", 2))),
    thread_name_prefix="weft_analysis")
_warmup_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="warmup")

atexit.register(_save_executor.shutdown,  wait=False)
atexit.register(_warp_executor.shutdown,  wait=False)
atexit.register(_weft_executor.shutdown,  wait=False)
atexit.register(_warmup_executor.shutdown, wait=False)

# 全局纬线队列深度计数器（跨Pipeline共享，防止整体过载）
_weft_queue_depth: int = 0
_weft_queue_lock = threading.Lock()


class DetectionPipeline:
    """
    单Worker检测流水线。

    主链路（同步）:
      detect_image() → YOLO推理 → 过滤 → 返回DetectionResult

    后台任务（异步，不阻塞主链路）:
      _submit_analysis_tasks() → 提交经纬线分析到独立线程池
      分析结果异步写入WarpCache/WeftCache
    """

    def __init__(
        self,
        detector,
        output_dir: str,
        save_config: SaveConfig = None,
        filter_classes: List[str] = None,
        class_thresholds: Dict[str, float] = None,
        class_thresholds_by_id: Dict[int, float] = None,
        default_class_threshold: float = 0.25,
        external_saver: ResultSaver = None,
        worker_id: int = 0,
        patch_size: int = 640,
        enable_warp_density: bool = True,
        warp_density_config: Dict = None,
        warp_sample_interval: int = DEFAULT_WARP_SAMPLE_INTERVAL,
        weft_sample_interval: int = DEFAULT_WEFT_SAMPLE_INTERVAL,
        launch_tag: str = "",
        machine_stop_path: str = "",
        upload_manager=None,
        camera_sn: str = "",
        camera_name: str = "",
    ):
        self.detector = detector
        self.image_size = getattr(detector, "image_size", 640)
        self.patch_size = patch_size
        self.worker_id = worker_id
        self.camera_sn = camera_sn
        self.camera_name = camera_name

        self.saver = external_saver or ResultSaver(output_dir, save_config, patch_size)

        self.filter_classes = set(filter_classes) if filter_classes else set()
        # 注意这里用 `is None` 判断而不是 `or {}`：
        # class_thresholds 通常是 settings 的热更新字典，若它在启动瞬间
        # 恰好为空，`or {}` 会把它替换成普通空 dict，从此失去热更新能力。
        self.threshold_config = ClassThresholdConfig(
            thresholds_by_name=(
                class_thresholds if class_thresholds is not None else {}),
            thresholds_by_id=(
                class_thresholds_by_id
                if class_thresholds_by_id is not None else {}),
            default_threshold=default_class_threshold,
            # 🔥 默认阈值改为动态读取，支持热更新
            default_threshold_provider=get_default_class_confidence,
        )

        self._frame_counter: int = 0
        # 采样间隔：每N帧提交一次后台分析
        self._warp_interval: int = max(1, warp_sample_interval)
        self._weft_interval: int = max(1, weft_sample_interval)

        self._stats: Dict = self._init_stats()
        self._lock = threading.Lock()

        self._warp_cache = WarpCache()
        self._weft_cache = WeftCache()

        _machine_stop_path = machine_stop_path or MACHINE_STOP_PATH
        self._stop_handler = StopHandler(
            machine_stop_path=_machine_stop_path,
            launch_tag=launch_tag,
            upload_manager=upload_manager,
        )

        # 经线密度：同时受配置文件开关和参数双重控制
        _enable_warp = enable_warp_density and WARP_DENSITY_ENABLE
        self.enable_warp_density = _enable_warp
        self._warp_detector = self._init_warp_detector(
            _enable_warp, warp_density_config, worker_id
        )

        # 纬线检测：受配置文件开关控制
        _enable_weft = WEFT_DENSITY_ENABLE
        self.enable_weft_density = _enable_weft
        if _enable_weft:
            self._weft_detector = WeftDetector(
                output_dir=WEFT_OUTPUT_DIR,
                save_annotated=SAVE_WEFT,
            )
            info(f"[WeftDetector] 实例级检测器已创建 (worker_id={worker_id})")
        else:
            self._weft_detector = None
            info(f"[WeftDetector] WEFT_DENSITY_ENABLE=False，跳过创建 (worker_id={worker_id})")

        # 任务执行器：注入所有依赖，包括低优先级Stream池
        self._tasks = AnalysisTasks(
            warp_cache=self._warp_cache,
            weft_cache=self._weft_cache,
            stop_handler=self._stop_handler,
            warp_detector=self._warp_detector,
            weft_detector=self._weft_detector,
            stats=self._stats,
            stats_lock=self._lock,
            calculate_cut_ratio_fn=self._calculate_cut_ratio,
        )

        if not camera_name:
            info("[DetectionPipeline] camera_name为空，定时上传服务将延迟启动")

        if camera_name:
            if camera_name in CAMERAS_WITHOUT_WEFT:
                info(f"[DetectionPipeline] {camera_name} 禁用纬线检测，初始化缓存为零")
                self._weft_cache.write(False, 0.0, 0)

            ctx = CameraReportContext(
                camera_sn=camera_sn or "UNKNOWN",
                camera_name=camera_name,
                warp_cache=self._warp_cache,
                weft_cache=self._weft_cache,
            )
            get_global_scheduler().register(ctx)
            self._report_ctx = ctx
        else:
            self._report_ctx = None

        self._camera_name_for_report = camera_name
        self._report_init_lock = threading.Lock()

        # 只有启用时才进行模型预热
        if _enable_weft:
            _warmup_executor.submit(run_warmup, worker_id)
        else:
            info(f"[WeftDetector] 跳过预热 WEFT_DENSITY_ENABLE=False (worker_id={worker_id})")

    # ──────────────────────────────────────────────────────────────────
    #  初始化辅助
    # ──────────────────────────────────────────────────────────────────

    @staticmethod
    def _init_stats() -> Dict:
        return {
            "total_images": 0,
            "detection_images": 0,
            "total_boxes": 0,
            "raw_boxes": 0,
            "filtered_by_threshold": 0,
            "filtered_by_class": 0,
            "filtered_by_size": 0,
            "filtered_by_edge_distance": 0,
            "filtered_by_machine": 0,
            "class_counts": {},
            "warp_density_count": 0,
            "warp_density_avg": 0.0,
            "warp_task_submitted": 0,
            "warp_task_skipped": 0,
            "weft_task_submitted": 0,
            "weft_task_skipped": 0,
            "weft_task_depth_dropped": 0,
            "stop_weft_count": 0,
            "stop_broken_weft_count": 0,
            "stop_warp_count": 0,
            "stop_lat_slope_count": 0,
            "yolo_total_ms": 0.0,
            "total_process_ms": 0.0,
        }

    @staticmethod
    def _init_warp_detector(
        enable: bool,
        cfg: Optional[Dict],
        worker_id: int,
    ) -> Optional[WarpDensityDetector]:
        if not enable:
            info(f"[经线密度] WARP_DENSITY_ENABLE=False，跳过初始化 (worker_id={worker_id})")
            return None
        cfg = cfg or {}
        det = WarpDensityDetector(
            pixel_per_cm=cfg.get("pixel_per_cm", PIXEL_PER_CM),
            use_clahe=cfg.get("use_clahe", True),
            roi_ratio=cfg.get("roi_ratio", 0.6),
            min_period_px=cfg.get("min_period_px", 3),
            max_period_px=cfg.get("max_period_px", 250),
            auto_correct_low_density=True,
            low_density_min=50.0,
            low_density_max=60.0,
        )
        info(f"[经线密度] 检测器已初始化 (worker_id={worker_id})")
        return det

    # ──────────────────────────────────────────────────────────────────
    #  切除比例计算
    # ──────────────────────────────────────────────────────────────────

    def _calculate_cut_ratio(
        self, camera_name: str, image_width_px: int
    ) -> Tuple[float, str]:
        fabric_stats = get_fabric_stats()
        if not fabric_stats.get("enabled"):
            return 0.0, "none"

        from AnomalyDetection_Tool.output.fabric_width_service import get_fabric_detector
        fabric_detector = get_fabric_detector()
        if fabric_detector is None:
            return 0.0, "none"

        cut_ratio, cut_side = fabric_detector.get_cut_ratio_for_camera(camera_name)
        if cut_ratio <= 0:
            return 0.0, "none"
        if cut_ratio > 0.5:
            info(f"[经线密度] {camera_name} 切除比例异常({cut_ratio:.1%})，跳过")
            return 0.0, "none"

        return cut_ratio, cut_side

    # ──────────────────────────────────────────────────────────────────
    #  核心检测方法（主链路）
    # ──────────────────────────────────────────────────────────────────

    def detect_image(
        self,
        image: np.ndarray,
        filename: str = "",
        camera_name: str = "",
        capture_timestamp: str = None,
        fabric_bbox: Optional[Tuple[int, int, int, int]] = None,
        camera_sn: str = "",
    ) -> DetectionResult:
        """
        主链路检测方法（同步执行，尽量短）。

        流程：
          1. YOLO推理（高优先级GPU Stream）
          2. 检测框过滤
          3. 提交经纬线后台任务（异步，不等待）
          4. 返回DetectionResult（后台任务结果通过缓存在下一帧读取）
        """
        t0 = time.perf_counter()
        capture_timestamp = capture_timestamp or now_str()
        h, w = image.shape[:2]
        machine_id = filename.split("_")[0] if "_" in filename else filename

        # ── Step1: YOLO推理（高优先级GPU Stream中执行）───────────────
        t_yolo = time.perf_counter()
        raw_boxes = self.detector.detect(image)
        yolo_ms = (time.perf_counter() - t_yolo) * 1000

        # ── Step2: 检测框过滤 ─────────────────────────────────────────
        filtered, f_cls, f_thr, f_size, f_edge, f_machine = self._filter_boxes(
            raw_boxes, fabric_bbox, machine_id
        )
        max_conf = max((b.confidence for b in filtered), default=0.0)

        # ── Step3: 读取缓存的经线分析结果（上一轮的结果，无等待）──────
        warp_result, _, _, _ = self._read_analysis_cache()

        # ── Step4: 创建SharedFrame并提交后台任务（异步，立即返回）──────
        # SharedFrame实现零拷贝共享，所有后台任务读取同一份图像数据
        shared = SharedFrame(image)
        try:
            self._submit_analysis_tasks(shared, camera_name, filename, camera_sn)
        finally:
            # 主链路释放对 SharedFrame 的持有，后台任务各自维护引用计数。
            #
            # 必须放在 finally 里：_submit_analysis_tasks 内部先 shared.acquire()
            # 再 executor.submit()，如果 acquire 之后抛异常（线程池已 shutdown、
            # RuntimeError 等），下面这行就永远执行不到，_ref_count 停在 >=1，
            # _image 不会被置空。
            #
            # 注意这不会造成永久泄漏——detect() 返回后 shared 随局部变量一起被
            # GC（已用 weakref 实测确认）。真正的风险是异常传播路径：Python 的
            # traceback 会持有帧的局部变量，只要异常对象被留存（日志 exc_info、
            # sys.last_traceback、上层 except 里存了引用），这一帧的 14.3 MB
            # 数组就会跟着 traceback 一起滞留。放在 finally 里可以确定性地断开
            # SharedFrame 对数组的引用。
            shared.owner_release()

        # ── Step5: 延迟初始化定时上报上下文 ──────────────────────────
        if self._report_ctx is None and camera_name:
            with self._report_init_lock:
                if self._report_ctx is None:
                    if camera_name in CAMERAS_WITHOUT_WEFT:
                        self._weft_cache.write(False, 0.0, 0)
                    ctx = CameraReportContext(
                        camera_sn=camera_sn or camera_name,
                        camera_name=camera_name,
                        warp_cache=self._warp_cache,
                        weft_cache=self._weft_cache,
                    )
                    get_global_scheduler().register(ctx)
                    self._report_ctx = ctx

        total_ms = (time.perf_counter() - t0) * 1000

        self._update_stats(
            len(raw_boxes), filtered,
            f_cls, f_thr, f_size, f_edge, f_machine,
            yolo_ms=yolo_ms, total_ms=total_ms,
        )

        result = DetectionResult(
            filename=filename,
            original_width=w,
            original_height=h,
            has_detection=len(filtered) > 0,
            total_boxes=len(filtered),
            max_confidence=max_conf,
            boxes=filtered,
            capture_timestamp=capture_timestamp,
            detection_timestamp=now_str(),
            process_time_ms=total_ms,
            raw_boxes_count=len(raw_boxes),
            filtered_count=f_cls + f_thr + f_size + f_edge + f_machine,
        )

        # 将最新经线结果附加到result元数据（供后续分析使用）
        if warp_result is not None:
            if not hasattr(result, "metadata") or result.metadata is None:
                result.metadata = {}
            result.metadata["warp_density"] = warp_result.to_dict()

        return result

    # ──────────────────────────────────────────────────────────────────
    #  后台任务提交（异步，不阻塞主链路）
    # ──────────────────────────────────────────────────────────────────

    def _submit_analysis_tasks(
        self,
        shared: SharedFrame,
        camera_name: str,
        filename: str,
        camera_sn: str = "",
    ) -> None:
        """
        提交经纬线后台分析任务。

        开关职责分离:
          WARP_DENSITY_ENABLE → 是否提交经线分析任务
          WARP_TRIGGER        → 经线异常是否触发停机（AnalysisTasks中判断）
          WEFT_DENSITY_ENABLE → 是否提交纬线分析任务
          WEFT_TRIGGER        → 纬线异常是否触发停机（AnalysisTasks中判断）

        采样控制:
          每N帧（_warp_interval/_weft_interval）才提交一次任务。
          N由配置决定，默认为3，即每3帧分析一次，降低GPU争用频率。
        """
        self._frame_counter += 1

        # ── 经线任务：受enable_warp_density和采样间隔双重控制 ─────────
        need_warp = (
            self.enable_warp_density
            and camera_name
            and self._warp_detector is not None
            and (self._frame_counter % self._warp_interval == 0)
        )
        if need_warp:
            if self._warp_cache.try_mark_inflight():
                # 消费者获取SharedFrame引用（引用计数+1）
                shared.acquire()
                _warp_executor.submit(
                    self._warp_task_shared,
                    shared, camera_name, camera_sn,
                )
                with self._lock:
                    self._stats["warp_task_submitted"] += 1
            else:
                # 上一帧的经线任务尚未完成，跳过本帧（避免堆积）
                with self._lock:
                    self._stats["warp_task_skipped"] += 1

        # ── 纬线任务：受enable_weft_density和采样间隔双重控制 ─────────
        if not self.enable_weft_density:
            return
        if camera_name in CAMERAS_WITHOUT_WEFT:
            return
        if self._frame_counter % self._weft_interval != 0:
            return

        # ── 纬线队列深度保护：防止分析任务积压导致内存占用过高 ────────
        global _weft_queue_depth
        with _weft_queue_lock:
            if _weft_queue_depth >= WEFT_MAX_QUEUE_DEPTH:
                # 队列已满，丢弃本帧纬线任务
                with self._lock:
                    self._stats["weft_task_skipped"] += 1
                    self._stats["weft_task_depth_dropped"] += 1
                return
            _weft_queue_depth += 1

        if self._weft_cache.try_mark_inflight():
            shared.acquire()
            _weft_executor.submit(
                self._weft_task_shared_guarded,
                shared, filename, camera_name, camera_sn,
            )
            with self._lock:
                self._stats["weft_task_submitted"] += 1
        else:
            # 归还队列深度计数（任务未实际提交）
            with _weft_queue_lock:
                _weft_queue_depth = max(0, _weft_queue_depth - 1)
            with self._lock:
                self._stats["weft_task_skipped"] += 1

    # ──────────────────────────────────────────────────────────────────
    #  后台任务执行体（在线程池中运行）
    # ──────────────────────────────────────────────────────────────────

    def _warp_task_shared(
        self, shared: SharedFrame, camera_name: str, camera_sn: str
    ) -> None:
        """经线分析任务（线程池Worker中执行）"""
        try:
            image = shared.image
            if image is None:
                error(f"[经线密度] SharedFrame已释放 camera={camera_name}")
                self._warp_cache.reset_inflight()
                return
            # GPU操作在低优先级Stream中执行（见AnalysisTasks.bg_detect_warp）
            self._tasks.bg_detect_warp(image, camera_name, camera_sn)
        except Exception as e:
            self._warp_cache.reset_inflight()
            error(f"[经线密度/shared] {camera_name} 失败: {e}")
        finally:
            # 必须释放引用，防止SharedFrame无法回收
            shared.release()

    def _weft_task_shared_guarded(
        self,
        shared: SharedFrame,
        filename: str,
        camera_name: str,
        camera_sn: str,
    ) -> None:
        """纬线分析任务（线程池Worker中执行，含队列深度守护）"""
        global _weft_queue_depth
        try:
            image = shared.image
            if image is None:
                error(f"[纬线检测] SharedFrame已释放 file={filename}")
                self._weft_cache.reset_inflight()
                return
            # GPU操作在低优先级Stream中执行（见AnalysisTasks.bg_detect_weft）
            self._tasks.bg_detect_weft(image, filename, camera_name, camera_sn)
        except Exception as e:
            self._weft_cache.reset_inflight()
            error(f"[纬线检测/shared] [{filename}] 失败: {e}")
        finally:
            # 归还队列深度（无论成功与否）
            with _weft_queue_lock:
                _weft_queue_depth = max(0, _weft_queue_depth - 1)
            shared.release()

    # ──────────────────────────────────────────────────────────────────
    #  缓存读取（主链路无锁，读旧值可接受）
    # ──────────────────────────────────────────────────────────────────

    def _read_analysis_cache(self):
        """读取经纬线最新分析结果（异步缓存，主链路零等待）"""
        warp_result, warp_ts = self._warp_cache.read()
        weft_ok, weft_angle, weft_max_sp, weft_ts = self._weft_cache.read()
        return warp_result, weft_ok, weft_angle, weft_max_sp

    # ──────────────────────────────────────────────────────────────────
    #  检测框过滤
    # ──────────────────────────────────────────────────────────────────

    def _filter_boxes(self, raw_boxes, fabric_bbox, machine_id=""):
        filtered = []
        f_cls = f_thr = f_size = f_edge = f_machine = 0

        for box in raw_boxes:
            # 过滤指定类别
            if box.class_name in self.filter_classes:
                f_cls += 1
                continue

            # 置信度过滤
            thr = self.threshold_config.get_threshold(box.class_name, box.class_id)
            if float(box.confidence) < float(thr):
                f_thr += 1
                continue

            # 尺寸过滤（宽度/高度最小值限制）
            size_rule = CLASS_SIZE_FILTER.get(box.class_name)
            if size_rule:
                min_w = size_rule.get("min_width")
                min_h = size_rule.get("min_height")
                too_narrow = (min_w is not None) and (int(box.width) < int(min_w))
                too_short  = (min_h is not None) and (int(box.height) < int(min_h))
                if too_narrow or too_short:
                    f_size += 1
                    continue

            # 边缘距离过滤（距布面太远的检测框）
            edge_rule = CLASS_EDGE_FILTER.get(box.class_name)
            if edge_rule and fabric_bbox is not None:
                safe_bbox = tuple(int(v) for v in fabric_bbox)
                gap = self._gap_to_fabric(box, safe_bbox)
                if gap > float(edge_rule["max_distance_to_fabric"]):
                    f_edge += 1
                    info(
                        f"[Pipeline] {box.class_name} 过滤: "
                        f"离布面{gap:.0f}px > {edge_rule['max_distance_to_fabric']}px"
                    )
                    continue

            box.threshold_used = thr
            filtered.append(box)

        return filtered, f_cls, f_thr, f_size, f_edge, f_machine

    @staticmethod
    def _gap_to_fabric(box, fabric_bbox) -> float:
        """计算检测框到布面区域的最短距离"""
        fx, fy, fw, fh = fabric_bbox
        fabric_right  = fx + fw
        fabric_bottom = fy + fh
        dx = max(0, fx - box.x2, box.x1 - fabric_right)
        dy = max(0, fy - box.y2, box.y1 - fabric_bottom)
        if dy == 0:
            return float(dx)
        if dx == 0:
            return float(dy)
        return float((dx ** 2 + dy ** 2) ** 0.5)

    # ──────────────────────────────────────────────────────────────────
    #  统计
    # ──────────────────────────────────────────────────────────────────

    def _update_stats(self, raw_count, filtered,
                      f_cls, f_thr, f_size, f_edge, f_machine,
                      yolo_ms=0.0, total_ms=0.0):
        with self._lock:
            s = self._stats
            s["total_images"]              += 1
            s["raw_boxes"]                 += raw_count
            s["filtered_by_class"]         += f_cls
            s["filtered_by_threshold"]     += f_thr
            s["filtered_by_size"]          += f_size
            s["filtered_by_edge_distance"] += f_edge
            s["filtered_by_machine"]       += f_machine
            s["yolo_total_ms"]             += yolo_ms
            s["total_process_ms"]          += total_ms
            if filtered:
                s["detection_images"] += 1
                s["total_boxes"]      += len(filtered)
                cc = s["class_counts"]
                for b in filtered:
                    cc[b.class_name] = cc.get(b.class_name, 0) + 1

    def get_stats(self) -> Dict:
        with self._lock:
            stats = self._stats.copy()

        n = stats["total_images"] or 1

        if stats["warp_density_count"] > 0:
            stats["warp_density_summary"] = {
                "count":                stats["warp_density_count"],
                "avg_density_per_10cm": round(stats["warp_density_avg"], 1),
            }

        stats["async_analysis"] = {
            "warp_enabled":         self.enable_warp_density,
            "weft_enabled":         self.enable_weft_density,
            "warp_interval":        self._warp_interval,   # 采样间隔（每N帧）
            "weft_interval":        self._weft_interval,
            "warp_submitted":       stats["warp_task_submitted"],
            "warp_skipped":         stats["warp_task_skipped"],
            "warp_cache_updates":   self._warp_cache.total,
            "warp_cache_hits":      self._warp_cache.hit,
            "weft_submitted":       stats["weft_task_submitted"],
            "weft_skipped":         stats["weft_task_skipped"],
            "weft_depth_dropped":   stats["weft_task_depth_dropped"],
            "weft_cache_updates":   self._weft_cache.total,
            "weft_cache_hits":      self._weft_cache.hit,
            "weft_queue_depth_now": _weft_queue_depth,
            "weft_queue_depth_max": WEFT_MAX_QUEUE_DEPTH,
        }

        stats["stop_summary"] = {
            "weft_stop":        stats["stop_weft_count"],
            "broken_weft_stop": stats["stop_broken_weft_count"],
            "warp_stop":        stats["stop_warp_count"],
            "lat_slope_stop":   stats["stop_lat_slope_count"],
        }

        stats["performance"] = {
            "avg_yolo_ms":  round(stats["yolo_total_ms"] / n, 1),
            "avg_total_ms": round(stats["total_process_ms"] / n, 1),
        }

        return stats

    def release(self) -> None:
        """释放资源，注销定时上报上下文"""
        if self._report_ctx is not None:
            get_global_scheduler().unregister(self._report_ctx.camera_sn)
        self.detector.release()