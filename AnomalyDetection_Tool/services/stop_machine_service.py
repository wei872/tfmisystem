#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
停机处理服务
将停机判断、保存、继电器触发等逻辑从 Manager 中抽离
"""

import os
import time
import threading
from typing import Optional, List, Dict
from datetime import datetime
import numpy as np

from General_Tool.EnhancedLogger import info, error

from AnomalyDetection_Tool.config.settings import (
    MACHINE_STOP_RULES, STOP_ANALYSIS_MODE, PIXEL_TO_MM,
    MACHINE_STOP_PATH, validate_analysis_mode, effective_stop_rule,
    effective_repeat_rule, repeat_stop_allowed,
)
from AnomalyDetection_Tool.models.data_types import (
    DetectionBox, AreaAnalysisResult,
)
from AnomalyDetection_Tool.output.machine_stop_saver import (
    MachineStopSaver,
)
from AnomalyDetection_Tool.services.comm_service import comm_service
from AnomalyDetection_Tool.services.relay_service import relay_service
from AnomalyDetection_Tool.services.repeat_stop_tracker import RepeatStopTracker


class StopMachineService:
    """停机分析与处理服务"""

    def __init__(self, machine_stop_path: str,
                 launch_tag: str,
                 patch_size: int = 640,
                 analysis_mode: str = None,
                 pth_checkpoint: str = None,
                 pth_device: str = None,
                 pth_model_source_dir: str = None,
                 mask_threshold: float = 0.3,
                 dinomaly_use_fp16: bool = False,
                 use_cuda_stream: bool = False):

        self.analysis_mode = validate_analysis_mode(
            analysis_mode or STOP_ANALYSIS_MODE)
        self.patch_size = patch_size
        self.launch_tag = launch_tag

        # 分析器
        self.defect_analyzer = None
        # 允许 2 个 post-worker 并发做面积分析
        # coarse 模式纯 CPU(OpenCV)，天然可并行；
        # dinomaly 模式若共享同一 GPU 也可并发（torch 内部有流调度）
        self._dinomaly_semaphore = threading.Semaphore(2)

        # 停机保存器（惰性创建）
        #
        # 原实现只在 __init__ 时按 MACHINE_STOP_RULES["enabled"] 判断一次。
        # 停机规则支持热更新后会出现这种情况：启动时 enabled=false，
        # 运行中改成 true —— 规则立刻生效并触发继电器/停机，
        # 但保存器还是 None，导致"停机了却没有留下证据图"。
        # 改为首次使用时按当时的开关状态创建。
        self._saver_path = machine_stop_path
        self._saver_launch_tag = launch_tag
        self._saver_lock = threading.Lock()
        self.machine_stop_saver: Optional[MachineStopSaver] = None
        self._ensure_stop_saver()

        # 统计
        self._lock = threading.Lock()
        self._stats = {
            "area_analysis_triggered": 0,
            "area_analysis_skipped_no_model": 0,
            "machine_stop_triggered": 0,
            "dinomaly_infer_count": 0,
            "dinomaly_infer_time_total": 0.0,
            "repeat_stop_triggered": 0,
            # 攒够了但这台相机没有停机权限（只计数、只打日志）。
            # 这个数涨得快，说明该考虑给这台相机开权限了。
            "repeat_stop_no_authority": 0,
        }

        # 【三】重复停机：同一台相机上同类疵点在时间窗口内反复出现。
        # 规则每次现取（effective_repeat_rule），所以 config.yaml 热更新立刻生效。
        self._repeat_tracker = RepeatStopTracker(effective_repeat_rule)

        # 延迟初始化参数
        self._pth_checkpoint = pth_checkpoint
        self._pth_device = pth_device
        self._pth_model_source_dir = pth_model_source_dir
        self._mask_threshold = mask_threshold
        self._dinomaly_use_fp16 = dinomaly_use_fp16
        self._use_cuda_stream = use_cuda_stream

    def initialize(self) -> bool:
        """初始化分析器"""
        try:
            self._init_analyzer()
            return True
        except Exception as e:
            error(f"[StopService] 初始化失败: {e}")
            return False

    def _init_analyzer(self):
        if self.analysis_mode == "dinomaly":
            self._init_dinomaly()
        elif self.analysis_mode == "coarse":
            self._init_coarse()
        else:
            self.defect_analyzer = None
            info("[分析模式] none — 不做面积分析")

    def _init_dinomaly(self):
        if not (self._pth_checkpoint
                and os.path.exists(self._pth_checkpoint)):
            error("[Dinomaly] 权重文件不可用，回退到 none")
            self.analysis_mode = "none"
            return

        from AnomalyDetection_Tool.analysis.dinomaly_defect_analyzer import (
            DinomalyDefectAnalyzer,
        )
        try:
            self.defect_analyzer = DinomalyDefectAnalyzer(
                checkpoint_path=self._pth_checkpoint,
                device=self._pth_device or "cpu",
                slice_size=self.patch_size,
                mask_threshold=self._mask_threshold,
                model_source_dir=self._pth_model_source_dir,
                use_fp16=self._dinomaly_use_fp16,
                use_cuda_stream=self._use_cuda_stream,
            )
            info(f"[分析模式] dinomaly OK (device={self._pth_device})")
        except Exception as e:
            error(f"[Dinomaly] 初始化失败: {e}，回退到 none")
            self.analysis_mode = "none"
            self.defect_analyzer = None

    def _init_coarse(self):
        from AnomalyDetection_Tool.analysis.coarse_defect_analyzer import (
            CoarseDefectAnalyzer,
        )
        try:
            self.defect_analyzer = CoarseDefectAnalyzer(
                slice_size=self.patch_size)
            info("[分析模式] coarse OK")
        except Exception as e:
            error(f"[CoarseAnalyzer] 初始化失败: {e}，回退到 none")
            self.analysis_mode = "none"
            self.defect_analyzer = None

    # ================================================================
    # 面积分析
    # ================================================================
    def analyze_defect_area(
            self, image: np.ndarray,
            box: DetectionBox,
            camera_name: str,
            filename: str,
            camera_sn: str = "",
            capture_timestamp: str = "",
            frame_already_stopped: bool = False,
            log_lines: Optional[List[str]] = None,
            box_index: int = 0) -> AreaAnalysisResult:
        """分析单个框的缺陷面积，判断是否停机"""

        default = AreaAnalysisResult()

        rule = self.get_stop_rule(box.class_name, camera_name)
        if rule is None:
            if log_lines is not None:
                log_lines.append(
                    f"  [跳过] {box.class_name} 无停机规则")
            return default

        min_conf = rule.get("min_confidence", 0.85)
        if box.confidence < min_conf:
            if log_lines is not None:
                log_lines.append(
                    f"  [跳过] {box.class_name} 置信度 "
                    f"{box.confidence:.2f} < {min_conf}")
            return default

        with self._lock:
            self._stats["area_analysis_triggered"] += 1

        if self.analysis_mode == "none" or self.defect_analyzer is None:
            with self._lock:
                self._stats["area_analysis_skipped_no_model"] += 1
            if log_lines is not None:
                log_lines.append(
                    f"  [跳过] 分析器未加载 (mode={self.analysis_mode})")
            return default

        t0 = time.time()
        with self._dinomaly_semaphore:
            feat = self.defect_analyzer.extract_defect_features(
                image, box, box.class_name)
        dt = time.time() - t0

        with self._lock:
            self._stats["dinomaly_infer_count"] += 1
            self._stats["dinomaly_infer_time_total"] += dt

        px_area = feat["pixel_area"]
        real_area = self.defect_analyzer.calculate_real_area_mm2(px_area)
        anomaly_score = feat.get("anomaly_score", 0)
        min_area = rule.get("min_area_mm2", 80)

        is_stop = real_area >= min_area and not frame_already_stopped

        if log_lines is not None:
            tag = (" 触发停机" if is_stop
                   else ("帧内已停机,跳过"
                         if frame_already_stopped
                         else "未达阈值"))
            log_lines.append(
                f"  [{tag}] {box.class_name} "
                f"面积={real_area:.2f}mm² (阈值={min_area}mm²) "
                f"conf={box.confidence:.2f} 耗时={dt * 1000:.1f}ms")

        result = AreaAnalysisResult(
            triggered=is_stop,
            analyzed=True,
            real_area_mm2=real_area,
            fill_ratio=feat["fill_ratio"],
            debug_info=feat["debug_info"],
            mask=feat.get("mask"),
            heatmap=feat.get("heatmap"),
            rule_used=rule,
            anomaly_score=anomaly_score,
        )

        if is_stop:
            # ========== 时间追踪2：停机判定完成 ==========
            info(f'[时间追踪2] 停机判定完成 | '
                 f'{camera_name} | {filename} | '
                 f'{box.class_name} conf={box.confidence:.2f} | '
                 f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]}')
            # ========================================
            stop_path = self._handle_stop(
                image, box, camera_name, filename,
                result, rule,
                camera_sn=camera_sn,
                capture_timestamp=capture_timestamp,
                box_index=box_index)
            result.stop_image_path = stop_path

        return result

    # ================================================================
    # 维度触发判断（折边不入等基于长宽的规则）
    # ================================================================
    def analyze_defect_dimension(
            self,
            image: np.ndarray,
            box: DetectionBox,
            camera_name: str,
            filename: str,
            camera_sn: str = "",
            capture_timestamp: str = "",
            frame_already_stopped: bool = False,
            log_lines: Optional[List[str]] = None,
            box_index: int = 0) -> AreaAnalysisResult:
        """
        基于检测框长宽（像素 → mm）判断是否触发停机。
        适用于 trigger_mode='dimension' 的规则（如折边不入）。
        换算直接使用 settings.PIXEL_TO_MM，无需分析器。
        """
        default = AreaAnalysisResult()

        # ── 1. 取规则（按相机）─────────────────────────────────────
        rule = self.get_stop_rule(box.class_name, camera_name)
        if rule is None:
            if log_lines is not None:
                log_lines.append(
                    f"  [跳过] {box.class_name} 无停机规则")
            return default

        trigger_mode = rule.get("trigger_mode", "area")
        if trigger_mode not in ("dimension", "any"):
            if log_lines is not None:
                log_lines.append(
                    f"  [跳过-维度] {box.class_name} "
                    f"trigger_mode={trigger_mode} 非维度模式")
            return default

        # ── 2. 置信度门槛 ──────────────────────────────────────────
        min_conf = rule.get("min_confidence", 0.5)
        if box.confidence < min_conf:
            if log_lines is not None:
                log_lines.append(
                    f"  [跳过] {box.class_name} 置信度 "
                    f"{box.confidence:.2f} < {min_conf}")
            return default

        # ── 3. 像素 → mm（直接用 settings 中已有常量，无需分析器）──
        h_img, w_img = image.shape[:2]
        clamped = box.clamp(w_img, h_img)

        px_w = float(clamped.x2 - clamped.x1)
        px_h = float(clamped.y2 - clamped.y1)

        real_w_mm = px_w * PIXEL_TO_MM  # PIXEL_TO_MM = REAL_WIDTH_CM*10 / IMAGE_WIDTH_PX
        real_h_mm = px_h * PIXEL_TO_MM

        # ── 4. 与阈值比较 ──────────────────────────────────────────
        min_w = rule.get("min_width_mm", 0.0)
        min_h = rule.get("min_height_mm", 0.0)

        # 设为 0 表示该方向不参与判断
        w_triggered = (min_w > 0) and (real_w_mm >= min_w)
        h_triggered = (min_h > 0) and (real_h_mm >= min_h)

        # 宽或高任一超阈值即触发；两者均为 0 时不触发
        dim_triggered = w_triggered or h_triggered
        is_stop = dim_triggered and not frame_already_stopped

        # ── 5. 日志 ────────────────────────────────────────────────
        if log_lines is not None:
            tag = (
                "触发停机" if is_stop
                else ("帧内已停机,跳过" if frame_already_stopped
                      else "未达阈值")
            )
            parts = []
            if min_w > 0:
                parts.append(
                    f"宽={real_w_mm:.2f}mm(阈值={min_w}mm,"
                    f"{'✓' if w_triggered else '✗'})")
            if min_h > 0:
                parts.append(
                    f"高={real_h_mm:.2f}mm(阈值={min_h}mm,"
                    f"{'✓' if h_triggered else '✗'})")
            log_lines.append(
                f"  [{tag}] {box.class_name} "
                + " ".join(parts)
                + f" conf={box.confidence:.2f}"
            )

        # ── 6. 构造结果 ────────────────────────────────────────────
        result = AreaAnalysisResult(
            triggered=is_stop,
            analyzed=True,
            real_area_mm2=max(real_w_mm ,real_h_mm),  # 框面积，仅供参考
            fill_ratio=0.0,
            debug_info={
                "mode": "dimension",
                "real_w_mm": round(real_w_mm, 3),
                "real_h_mm": round(real_h_mm, 3),
                "px_w": px_w,
                "px_h": px_h,
                "pixel_to_mm": PIXEL_TO_MM,
                "w_triggered": w_triggered,
                "h_triggered": h_triggered,
            },
            rule_used=rule,
            anomaly_score=box.confidence,
        )

        if is_stop:
            with self._lock:
                self._stats["machine_stop_triggered"] += 1

            info(
                f'[时间追踪2] 停机判定完成(维度) | '
                f'{camera_name} | {filename} | '
                f'{box.class_name} conf={box.confidence:.2f} | '
                f'宽={real_w_mm:.2f}mm 高={real_h_mm:.2f}mm | '
                f'{datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]}'
            )
            stop_path = self._handle_stop(
                image, box, camera_name, filename,
                result, rule,
                camera_sn=camera_sn,
                capture_timestamp=capture_timestamp,
                box_index=box_index,
            )
            result.stop_image_path = stop_path

        return result

    # ================================================================
    # 停机处理
    # ================================================================
    def _handle_stop(self, image, box, camera_name,
                     filename, result, rule,
                     camera_sn="", capture_timestamp="",
                     box_index=0) -> str:
        # 人为停止期间不再触发任何停机动作（继电器/落盘/上传）。
        # 在途帧已在线程池入口被丢弃，这里是最后一道防线。
        try:
            import Communication_Tool.ThriftControl as TC
            if TC.isDetectionPaused():
                return ""
        except Exception:
            pass

        with self._lock:
            self._stats["machine_stop_triggered"] += 1

        # 明确标记为缺陷停机（可自动恢复），区别于手动暂停。
        # 走 CommService 而非直接 import ThriftControl，
        # 以复用其可用性检查与异常兜底。
        comm_service.set_defect_stop()

        # 2. 立刻触发继电器（内部已是异步线程）
        if MACHINE_STOP_RULES["trigger_relay"]:
            relay_service.trigger(camera_name, box.class_name)

        # 3. 异步保存停机文件（不阻塞停机响应链）
        stop_save_path = ""
        if (MACHINE_STOP_RULES["save_to_folder"]
                and self._ensure_stop_saver()):
            h, w = image.shape[:2]
            clamped = box.clamp(w, h)
            bi = {
                "class_name": box.class_name,
                "class_id": box.class_id,
                "confidence": box.confidence,
                "x1": clamped.x1, "y1": clamped.y1,
                "x2": clamped.x2, "y2": clamped.y2,
            }
            # 预生成保存路径，立刻返回给上层用于上传引用
            safe_sn = camera_sn or "unknown"
            safe_cls = box.class_name.replace("/", "_").replace("\\", "_")
            ts = (capture_timestamp
                  or datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])
            try:
                dt = datetime.fromisoformat(ts)
            except Exception:
                dt = datetime.now()
            ts_str = str(int(dt.timestamp() * 1000))
            stop_dir = os.path.join(
                self.machine_stop_saver.base_path if self.machine_stop_saver else MACHINE_STOP_PATH,
                self.machine_stop_saver.launch_tag if self.machine_stop_saver else "",
                camera_name, "StopMachine")
            os.makedirs(stop_dir, exist_ok=True)
            stop_save_path = os.path.join(stop_dir, f"{safe_sn}_{ts_str}_{safe_cls}_{box_index}.png")

            # 异步写入磁盘
            saver = self.machine_stop_saver
            _save_args = dict(
                image=image.copy(),  # 拷贝避免被后续帧覆盖
                camera_name=camera_name,
                filename=filename,
                box_info=bi,
                analysis_result=result.to_dict(),
                rule_used=rule,
                camera_sn=camera_sn,
                capture_timestamp=capture_timestamp,
                box_index=box_index,
            )
            threading.Thread(
                target=self._async_save_stop_image,
                args=(saver, _save_args),
                daemon=True
            ).start()

        return stop_save_path

    @staticmethod
    def _async_save_stop_image(saver, save_args):
        """异步保存停机图片（在独立线程中执行）"""
        try:
            saver.save(**save_args)
        except Exception as e:
            error(f"[停机保存] 异步写入失败: {e}")

    # ================================================================
    # 工具方法
    # ================================================================
    def _ensure_stop_saver(self) -> Optional[MachineStopSaver]:
        """
        按需创建停机保存器。

        支持 machine_stop_rules.enabled 在运行中由 false 改为 true 的场景。
        """
        if self.machine_stop_saver is not None:
            return self.machine_stop_saver
        if not (self._saver_path and self._saver_launch_tag):
            return None
        if not MACHINE_STOP_RULES.get("enabled", False):
            return None

        with self._saver_lock:
            if self.machine_stop_saver is None:
                self.machine_stop_saver = MachineStopSaver(
                    self._saver_path, launch_tag=self._saver_launch_tag)
                info(f"[停机保存] 已创建保存器: {self._saver_path}")
        return self.machine_stop_saver

    # ================================================================
    #  【三】重复触发停机（按相机 + 按类，时间窗口内反复出现）
    # ================================================================
    def check_repeat_stops(
            self,
            image: np.ndarray,
            camera_name: str,
            filename: str,
            boxes_by_class: Dict[str, DetectionBox],
            camera_sn: str = "",
            capture_timestamp: str = "",
            frame_already_stopped: bool = False,
            log_lines: Optional[List[str]] = None) -> bool:
        """记录本帧检出，判断有没有哪个类在窗口内攒够了次数。

        与 ``analyze_defect_area`` / ``analyze_defect_dimension`` **并存**：
        那两条是"这一个疵点够不够严重"，这条是"这类疵点是不是一直在出现"。
        任一条满足就停机。

        :param boxes_by_class: ``{类名: 本帧该类的代表框}``。**必须已经按帧
                               去重**（每类一个框）—— 计数是"按帧"的，
                               同一帧同类出现 5 个框也只算 1 次。
                               代表框用于停机落盘/继电器，取置信度最高的那个。
        :param frame_already_stopped: 本帧是否已由单次规则停过机。
                                      为 True 时**只计数不再停**（避免同一帧
                                      连发两次停机信号），但历史照样记，
                                      否则窗口会缺一帧、后面很难攒够。
        :return: 本帧是否由重复规则触发了停机。
        """
        if not boxes_by_class:
            return False

        classes = list(boxes_by_class.keys())
        confs = {c: b.confidence for c, b in boxes_by_class.items()}

        hits = self._repeat_tracker.record_frame(camera_name, classes, confs)
        if not hits:
            return False

        # 8 台都参与计数，但只有被授权的相机（现场是 camera1/camera8）
        # 允许**真的**停机。没授权的照样打日志 —— 中间相机的重复情况
        # 要看得见，否则无从判断该不该给它开权限。
        # ⚠️ 这只管【三】重复触发，【二】单次停机在 8 台上的行为没动。
        allowed = repeat_stop_allowed(camera_name)

        stopped = False
        for h in hits:
            cls = h["class_name"]
            box = boxes_by_class.get(cls)
            if box is None:
                continue
            if log_lines is not None:
                log_lines.append(
                    f"  [重复触发] {cls} 在 {h['window_sec']:.1f}s 内累计 "
                    f"{h['count']} 次（跨度 {h['span_sec']:.2f}s）")

            if not allowed:
                with self._lock:
                    self._stats["repeat_stop_no_authority"] += 1
                info(f"[重复停机] {camera_name} 已达阈值（{cls} "
                     f"{h['count']} 次 / {h['window_sec']:.1f}s），"
                     f"但该相机不在 stop_allowed_cameras 里，**不停机**。"
                     f"若这类疵点确实该停，请把 {camera_name} 加进白名单。")
                if log_lines is not None:
                    log_lines.append(
                        f"  [无停机权限] {cls} 已攒够 {h['count']} 次，"
                        f"但 {camera_name} 不在白名单")
                continue

            if frame_already_stopped or stopped:
                # 本帧已经停过了：计数照记（上面已经记完），但不再发第二次
                # 停机动作 —— 继电器和落盘重复触发只会制造混乱。
                if log_lines is not None:
                    log_lines.append(f"  [跳过动作] {cls} 本帧已停机")
                continue

            rule = effective_repeat_rule(cls, camera_name) or {}
            result = AreaAnalysisResult(
                triggered=True,
                analyzed=False,
                rule_used=rule,
                debug_info={
                    "stop_reason": "repeat",
                    "repeat_count": h["count"],
                    "repeat_window_sec": h["window_sec"],
                    "repeat_span_sec": h["span_sec"],
                },
            )
            with self._lock:
                self._stats["repeat_stop_triggered"] += 1
            self._handle_stop(
                image, box, camera_name, filename, result, rule,
                camera_sn=camera_sn,
                capture_timestamp=capture_timestamp,
                box_index=0)
            stopped = True

        return stopped

    @property
    def repeat_tracker(self) -> RepeatStopTracker:
        """暴露给上层做统计/排查（snapshot 里能看到各窗口攒了几次）。"""
        return self._repeat_tracker

    @staticmethod
    def get_stop_rule(class_name: str,
                      camera_name: Optional[str] = None) -> Optional[dict]:
        """取某个类**在某台相机上实际生效**的停机规则。

        合并逻辑全部在 ``settings.effective_stop_rule`` 里——那边不依赖
        libgxiapi，能脱离现场环境单独测。这里只保留原有入口。

        :param class_name:  类别名
        :param camera_name: 相机名。**传 None 时行为与改动前完全一致**
                            （只看全局 machine_stop_rules），所以没升级的
                            老调用点不受影响。
        :return: 生效规则的**副本**，或 None（= 这台相机上这个类不停机）。
                 是副本，改它不会影响配置本体。
        """
        return effective_stop_rule(class_name, camera_name)

    def get_stats(self) -> dict:
        with self._lock:
            stats = self._stats.copy()
        stats["analysis_mode"] = self.analysis_mode
        stats["analyzer_available"] = self.defect_analyzer is not None
        if self.machine_stop_saver:
            stats["saved_count"] = (
                self.machine_stop_saver.get_save_count())
            stats["by_class"] = (
                self.machine_stop_saver.get_class_counts())
        return stats

    def release(self):
        if self.defect_analyzer is not None:
            self.defect_analyzer.release()
            self.defect_analyzer = None
        info("[StopService] 已释放")