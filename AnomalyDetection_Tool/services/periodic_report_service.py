# -*- coding: utf-8 -*-
"""
AnomalyDetection_Tool/services/global_report_scheduler.py

全局定时上传调度器（单例）。

设计要点：
  - 进程级单例，所有 DetectionPipeline 注册到此处
  - 单一后台线程，约每 5s 遍历所有相机，逐个构建 payload 并上传
  - 各相机数据读取完全独立，某台失败不影响其他
  - 密度接口失败进入各相机独立的重试队列
  - 全局 500 退避机制，避免无效重试
"""

import json
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import requests

from General_Tool.EnhancedLogger import info, error, warning
from AnomalyDetection_Tool.output.encoder_state_service import get_encoder_state
from AnomalyDetection_Tool.output.fabric_width_service import get_fabric_width

# ══════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════
REPORT_INTERVAL: float = 5.0  # 约 5s 触发一次
DATA_VALIDITY_SECONDS: float = 30.0  # 缓存数据有效期
UPLOAD_TIMEOUT: float = 5.0  # 单次 HTTP 超时
MAX_UPLOAD_RETRIES: int = 2  # 密度接口最大重试次数
MAX_RETRY_QUEUE_SIZE: int = 20  # 每台相机重试队列上限

# ── Payload 合理性边界 ────────────────────────
# width / length 允许为 0（未就绪时传 0，由服务端处理）
# 只拦截明显异常值（负数、单位错误导致的超大值）
MAX_WIDTH_MM: float = 5000.0  # 大于 5000mm 认为单位换算异常

# ── 缓存时间戳判断 ─────────────────────────────
# cache 初始 timestamp 为 0.0（从未更新），用此阈值区分
CACHE_NEVER_UPDATED_TS: float = 1.0

# ── 全局 500 退避 ──────────────────────────────
CONSECUTIVE_500_THRESHOLD: int = 3  # 连续 3 次 500 触发退避
BACKOFF_BASE_SECONDS: float = 5.0  # 退避基础时间
BACKOFF_MAX_SECONDS: float = 60.0  # 退避上限

# ── 后端接口地址 ──────────────────────────────
# 统一由 settings.get_api_base_url() 提供（唯一来源，随 config.yaml 生效），
# 不再在此硬编码 http://localhost:8890/api。
DENSITY_API_PATH = "/v1/defect/density"


def get_density_url() -> str:
    """密度上报接口完整地址（每次按当前配置解析，支持 yaml 热更新）"""
    from AnomalyDetection_Tool.config.settings import get_api_base_url
    return get_api_base_url() + DENSITY_API_PATH


# ══════════════════════════════════════════════
#  单台相机的注册信息
# ══════════════════════════════════════════════
@dataclass
class CameraReportContext:
    """
    单台相机上报所需的全部上下文。
    由 DetectionPipeline 构造后注册到调度器。
    """
    camera_sn: str
    camera_name: str
    warp_cache: object  # WarpCache
    weft_cache: object  # WeftCache

    # 各相机独立的重试队列
    retry_queue: deque = field(
        default_factory=lambda: deque(maxlen=MAX_RETRY_QUEUE_SIZE)
    )
    queue_lock: threading.Lock = field(default_factory=threading.Lock)

    # 各相机独立的统计
    stats: Dict = field(default_factory=lambda: {
        "total": 0,  # 实际发起 HTTP 请求次数
        "success": 0,  # 上传成功次数
        "failed": 0,  # 最终失败次数（含重试耗尽）
        "retried_success": 0,  # 队列重试成功次数
        "queue_overflows": 0,  # 重试队列溢出次数
        "skipped": 0,  # payload 校验不通过，跳过次数
        "client_errors": 0,  # 4xx 错误次数（payload 问题）
    })


# ══════════════════════════════════════════════
#  全局调度器（进程级单例）
# ══════════════════════════════════════════════
class GlobalReportScheduler:
    """
    全局定时上传调度器（单例）。

    使用方式：
        # 在 DetectionPipeline.__init__ 中注册（只调用一次）
        get_global_scheduler().register(ctx)

        # 程序退出时停止
        get_global_scheduler().stop()
    """

    def __init__(
            self,
            report_interval: float = REPORT_INTERVAL,
            density_url: Optional[str] = None,
    ):
        self._interval = report_interval
        # None 表示跟随 config.yaml（推荐）；显式传入则固定为该地址（测试用）
        self._density_url_override = density_url

        # 注册表
        self._cameras: Dict[str, CameraReportContext] = {}
        self._reg_lock = threading.Lock()

        # 线程控制
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._ctl_lock = threading.Lock()

        # ── 停止期间暂停上报的日志节流 ──────────
        self._paused_log_ts: float = 0.0
        self._PAUSED_LOG_INTERVAL: float = 60.0

        # ── 全局 500 退避状态 ──────────────────
        self._consecutive_500_count = 0
        self._backoff_until: float = 0.0
        self._backoff_lock = threading.Lock()

        info(f"[GlobalScheduler] 初始化完成，间隔≈{self._interval}s")
        info(f"[GlobalScheduler] 密度接口: {self.density_url}")
        info(
            f"[GlobalScheduler] Payload 边界: "
            f"width ≥ 0mm（允许0）且 ≤ {MAX_WIDTH_MM}mm | "
            f"length ≥ 0m（允许0）"
        )

    @property
    def density_url(self) -> str:
        """密度上报地址：默认跟随 config.yaml，构造时显式指定则固定不变"""
        return self._density_url_override or get_density_url()
        info(
            f"[GlobalScheduler] 500 退避: "
            f"连续{CONSECUTIVE_500_THRESHOLD}次触发，"
            f"退避{BACKOFF_BASE_SECONDS}~{BACKOFF_MAX_SECONDS}s"
        )

    # ──────────────────────────────────────────
    #  注册 / 注销
    # ──────────────────────────────────────────
    def register(self, ctx: CameraReportContext) -> None:
        """
        注册一台相机。
        幂等：相同 camera_sn 已存在时直接返回，不覆盖。
        若需强制更新上下文，请先 unregister 再 register。
        """
        with self._reg_lock:
            if ctx.camera_sn in self._cameras:
                return  # 静默忽略重复注册
            self._cameras[ctx.camera_sn] = ctx
            count = len(self._cameras)

        info(
            f"[GlobalScheduler] 注册 {ctx.camera_name}({ctx.camera_sn})，"
            f"当前共 {count} 台相机"
        )
        self._ensure_started()

    def unregister(self, camera_sn: str) -> None:
        """注销一台相机"""
        with self._reg_lock:
            removed = self._cameras.pop(camera_sn, None)
            count = len(self._cameras)
        if removed:
            info(f"[GlobalScheduler] 注销 {camera_sn}，剩余 {count} 台")

    def camera_count(self) -> int:
        with self._reg_lock:
            return len(self._cameras)

    # ──────────────────────────────────────────
    #  生命周期
    # ──────────────────────────────────────────
    def _ensure_started(self) -> None:
        """保证调度线程已启动（幂等）"""
        with self._ctl_lock:
            if self._running:
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="GlobalReportScheduler",
                daemon=True,
            )
            self._thread.start()
            self._running = True
            info("[GlobalScheduler] 调度线程已启动")

    def stop(self, timeout: float = 10.0) -> None:
        """停止调度线程"""
        with self._ctl_lock:
            if not self._running:
                return
            self._stop_event.set()
            self._running = False

        if self._thread:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                error("[GlobalScheduler] 停止超时，线程可能卡死")
            else:
                info("[GlobalScheduler] 调度线程已停止")

        self._print_all_stats()

    def is_running(self) -> bool:
        with self._ctl_lock:
            return self._running

    # ──────────────────────────────────────────
    #  全局 500 退避
    # ──────────────────────────────────────────
    def _is_in_backoff(self) -> bool:
        """
        当前是否处于全局退避状态。
        线程安全，外部无需额外加锁。
        """
        with self._backoff_lock:
            return time.time() < self._backoff_until

    def _record_500(self) -> None:
        """
        记录一次 500 错误，达到阈值后启动退避。
        """
        with self._backoff_lock:
            self._consecutive_500_count += 1
            if self._consecutive_500_count >= CONSECUTIVE_500_THRESHOLD:
                k = self._consecutive_500_count - CONSECUTIVE_500_THRESHOLD + 1
                wait = min(
                    BACKOFF_BASE_SECONDS * (2 ** (k - 1)),
                    BACKOFF_MAX_SECONDS,
                )
                self._backoff_until = time.time() + wait
                error(
                    f"[GlobalScheduler] 连续 {self._consecutive_500_count} 次 500，"
                    f"进入退避 {wait:.0f}s，至 "
                    f"{time.strftime('%H:%M:%S', time.localtime(self._backoff_until))}"
                )

    def _record_success(self) -> None:
        """
        记录一次成功上传，重置 500 计数。
        """
        with self._backoff_lock:
            if self._consecutive_500_count > 0:
                info(
                    f"[GlobalScheduler] 上传成功，"
                    f"重置连续500计数（原={self._consecutive_500_count}）"
                )
            self._consecutive_500_count = 0
            self._backoff_until = 0.0

    # ──────────────────────────────────────────
    #  主循环
    # ──────────────────────────────────────────
    def _loop(self) -> None:
        """
        定时策略：
            每 0.5s 检查一次是否到达间隔；
            到达后快照当前相机列表，逐台上传。
            实际间隔 ≈ REPORT_INTERVAL + 所有相机上传总耗时。
        """
        info("[GlobalScheduler] 主循环已启动")
        last_run = time.time() - self._interval  # 启动后立即触发第一次

        while not self._stop_event.is_set():
            now = time.time()
            elapsed = now - last_run

            if elapsed < self._interval:
                self._stop_event.wait(timeout=0.5)
                continue

            last_run = time.time()

            # 检查全局退避
            if self._is_in_backoff():
                with self._backoff_lock:
                    remain = self._backoff_until - time.time()
                info(
                    f"[GlobalScheduler] 全局退避中（剩余 {remain:.0f}s），"
                    f"跳过本轮上传"
                )
                continue

            # 前端下发停止后，暂停一切上报。
            # 此时织机不出布，经纬密度是上一批的残留、布长布幅都是 0，
            # 继续每 5s 推一次只会往服务端灌无效数据。
            # 重试队列原样保留，恢复运行后照常补传。
            if self._detection_paused():
                self._log_paused_throttled()
                continue

            # 快照相机列表（避免上传期间注册/注销导致 dict 变化）
            with self._reg_lock:
                cameras = list(self._cameras.values())

            if not cameras:
                info("[GlobalScheduler] 暂无注册相机，跳过本轮")
                continue

            info(f"[GlobalScheduler] 开始本轮上传，共 {len(cameras)} 台相机")
            t_round = time.time()
            round_has_success = False

            for ctx in cameras:
                try:
                    # ① 补传历史失败
                    self._process_retry_queue(ctx)

                    # ② 密度数据上传
                    ok = self._do_density_upload(ctx)
                    if ok:
                        round_has_success = True

                    # 发现退避时提前终止本轮
                    if self._is_in_backoff():
                        info("[GlobalScheduler] 退避生效，提前终止本轮")
                        break

                except Exception as e:
                    error(f"[GlobalScheduler] {ctx.camera_name} 上传异常: {e}")
                    traceback.print_exc()

            if round_has_success:
                self._record_success()

            cost_ms = (time.time() - t_round) * 1000
            info(
                f"[GlobalScheduler] 本轮完成，耗时 {cost_ms:.0f}ms，"
                f"下次约 {self._interval:.0f}s 后"
            )

        info("[GlobalScheduler] 主循环已退出")

    # ══════════════════════════════════════════
    #  停止期间的暂停
    # ══════════════════════════════════════════
    @staticmethod
    def _detection_paused() -> bool:
        """前端是否已下发停止（手动停止期间不上报）"""
        try:
            import Communication_Tool.ThriftControl as TC
            return TC.isDetectionPaused()
        except Exception:
            return False

    def _log_paused_throttled(self) -> None:
        """暂停期间节流打印，避免每 5s 刷屏但又能看出上报确实停了"""
        now = time.time()
        if now - self._paused_log_ts >= self._PAUSED_LOG_INTERVAL:
            self._paused_log_ts = now
            info("[GlobalScheduler] 前端已下发停止，暂停密度上报"
                 "（重试队列保留，恢复运行后继续）")

    # ══════════════════════════════════════════
    #  经纬密度上传
    # ══════════════════════════════════════════
    def _do_density_upload(self, ctx: CameraReportContext) -> bool:
        """
        构建 payload → 校验 → 上传。
        返回 True 表示本次上传成功，False 表示失败或跳过。
        """
        payload = self._build_density_payload(ctx)

        # ── 合理性校验（只拦截明显异常值） ──
        valid, reason = _validate_payload(payload)
        if not valid:
            ctx.stats["skipped"] += 1
            warning(
                f"[GlobalScheduler] {ctx.camera_name} "
                f"payload 校验不通过，跳过上传 → {reason}"
            )
            return False

        # ── 业务校验（等待必要字段就绪） ──
        has_data, reason = _has_meaningful_data(payload)
        if not has_data:
            ctx.stats["skipped"] += 1
            info(
                f"[GlobalScheduler] {ctx.camera_name} "
                f"数据未就绪，跳过上传 → {reason}"
            )
            return False

        return self._upload_density_with_retry(ctx, payload)

    # _build_density_payload 方法（完整版）

    def _build_density_payload(self, ctx: CameraReportContext) -> dict:
        now = time.time()
        camera_name = ctx.camera_name

        # ── 经线 ──────────────────────────────────
        warp_result, warp_ts = ctx.warp_cache.read()
        lng_density = 0.0
        evenness_value = 0.0

        if warp_ts < CACHE_NEVER_UPDATED_TS:
            pass  # 从未更新过，静默跳过
        else:
            warp_age = now - warp_ts
            if warp_age <= DATA_VALIDITY_SECONDS:
                if warp_result is not None and warp_result.confidence >= 0.5:
                    lng_density = round(float(warp_result.density_per_10cm), 2)
                    evenness_value = round(float(warp_result.uniformity), 4)
            else:
                info(
                    f"[GlobalScheduler] {camera_name} "
                    f"经线数据过期(age={warp_age:.1f}s)"
                )

        # ── 纬线 ──────────────────────────────────
        lat_density = 0.0
        lat_slope = 0.0

        # ★★★ 修改点 4：camera1/camera8 强制跳过纬线检测 ★★★
        if camera_name not in ("camera1", "camera8"):
            weft_ok, weft_angle, weft_max_spacing, weft_ts = ctx.weft_cache.read()

            if weft_ts >= CACHE_NEVER_UPDATED_TS:
                weft_age = now - weft_ts
                if weft_age <= DATA_VALIDITY_SECONDS:
                    if weft_ok:
                        lat_density = round(_spacing_to_density(weft_max_spacing), 2)
                        lat_slope = round(float(weft_angle), 3)
                else:
                    info(
                        f"[GlobalScheduler] {camera_name} "
                        f"纬线数据过期(age={weft_age:.1f}s)"
                    )
        else:
            # camera1/camera8 不读取纬线缓存，纬线数据强制为 0
            pass

        # ── 编码器 ────────────────────────────────
        enc = get_encoder_state()
        length_m = round(enc.cumulative_distance / 1000.0, 3)

        # ── 布幅 ──────────────────────────────────
        fabric_width_cm = get_fabric_width() or 0.0
        width_mm = round(fabric_width_cm * 10.0, 1)

        # ★★★ 修改点 5：添加布幅调试日志 ★★★
        if width_mm == 0.0:
            warning(
                f"[GlobalScheduler] {camera_name} "
                f"布幅为零：get_fabric_width()={fabric_width_cm}"
            )

        return {
            "cameraSn": ctx.camera_sn,
            "cameraName": camera_name,
            "latDensity": lat_density,  # camera1/camera8 始终为 0
            "lngDensity": lng_density,  # 所有相机正常上报
            "latSlope": lat_slope,  # camera1/camera8 始终为 0
            "length": length_m,  # 所有相机正常上报
            "width": width_mm,  # 所有相机正常上报
            "evennessValue": evenness_value,  # 所有相机正常上报
        }

    def _upload_density_with_retry(
            self,
            ctx: CameraReportContext,
            payload: dict,
    ) -> bool:
        """
        重试策略：
          · 4xx（_ClientError）→ payload 有问题，不重试，直接丢弃
          · 5xx / 网络异常     → 最多重试 MAX_UPLOAD_RETRIES 次，
                                 仍失败则入队等待下一轮补传
        返回 True 表示成功，False 表示最终失败。
        """
        for attempt in range(MAX_UPLOAD_RETRIES + 1):
            try:
                self._upload_density(ctx, payload)
                ctx.stats["success"] += 1
                self._record_success()
                return True

            except _ClientError as e:
                error(
                    f"[GlobalScheduler] {ctx.camera_name} "
                    f"客户端错误({e.status_code})，payload 有误，"
                    f"丢弃此条不重试"
                )
                ctx.stats["client_errors"] += 1
                ctx.stats["failed"] += 1
                return False

            except Exception as e:
                if attempt < MAX_UPLOAD_RETRIES:
                    warning(
                        f"[GlobalScheduler] {ctx.camera_name} "
                        f"第{attempt + 1}次失败，1s后重试: {e}"
                    )
                    time.sleep(1)
                else:
                    error(
                        f"[GlobalScheduler] {ctx.camera_name} "
                        f"上传失败（共{MAX_UPLOAD_RETRIES + 1}次），"
                        f"加入重试队列"
                    )
                    self._add_to_retry_queue(ctx, payload)
                    ctx.stats["failed"] += 1
                    return False

    def _upload_density(
            self,
            ctx: CameraReportContext,
            payload: dict,
    ) -> None:
        ctx.stats["total"] += 1
        json_str = json.dumps(payload, ensure_ascii=False, indent=2)
        info(
            f"\n[GlobalScheduler/密度] {ctx.camera_name}({ctx.camera_sn})\n"
            f"URL: {self.density_url}\n{json_str}"
        )

        resp = requests.post(
            self.density_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=UPLOAD_TIMEOUT,
        )

        # ── 响应处理 ──────────────────────────────
        if not resp.ok:
            body_preview = resp.text[:500] if resp.text else "(空响应体)"
            error(
                f"[GlobalScheduler] {ctx.camera_name} 响应异常\n"
                f"  Status : {resp.status_code}\n"
                f"  Body   : {body_preview}\n"
                f"  Payload: {json_str}"
            )
            if 400 <= resp.status_code < 500:
                raise _ClientError(resp.status_code, body_preview)
            if resp.status_code == 500:
                self._record_500()
            resp.raise_for_status()

        info(
            f"[GlobalScheduler] {ctx.camera_name} 密度上传成功 | "
            f"lng={payload['lngDensity']} lat={payload['latDensity']} "
            f"slope={payload['latSlope']} "
            f"len={payload['length']}m width={payload['width']}mm"
        )

    # ──────────────────────────────────────────
    #  重试队列
    # ──────────────────────────────────────────
    def _add_to_retry_queue(
            self,
            ctx: CameraReportContext,
            payload: dict,
    ) -> None:
        with ctx.queue_lock:
            if len(ctx.retry_queue) >= MAX_RETRY_QUEUE_SIZE:
                ctx.stats["queue_overflows"] += 1
                warning(
                    f"[GlobalScheduler] {ctx.camera_name} "
                    f"重试队列已满（上限{MAX_RETRY_QUEUE_SIZE}），丢弃最旧数据"
                )
            ctx.retry_queue.append(payload)

    def _process_retry_queue(self, ctx: CameraReportContext) -> None:
        """
        逐条消费重试队列：
          · 退避中不消费
          · 4xx → 丢弃此条，继续消费
          · 5xx / 网络异常 → 放回队头，本轮停止重试
        """
        if self._is_in_backoff():
            return

        while True:
            with ctx.queue_lock:
                if not ctx.retry_queue:
                    break
                payload = ctx.retry_queue.popleft()

            try:
                self._upload_density(ctx, payload)
                ctx.stats["retried_success"] += 1
                info(f"[GlobalScheduler] {ctx.camera_name} 重试成功")

            except _ClientError as e:
                error(
                    f"[GlobalScheduler] {ctx.camera_name} "
                    f"重试时遇到客户端错误({e.status_code})，丢弃此条"
                )
                ctx.stats["client_errors"] += 1

            except Exception as e:
                with ctx.queue_lock:
                    ctx.retry_queue.appendleft(payload)
                warning(f"[GlobalScheduler] {ctx.camera_name} 重试失败: {e}")
                break

    # ──────────────────────────────────────────
    #  统计
    # ──────────────────────────────────────────
    def get_stats(self) -> Dict:
        with self._reg_lock:
            cameras = list(self._cameras.values())
        with self._backoff_lock:
            backoff_info = {
                "consecutive_500": self._consecutive_500_count,
                "backoff_until": self._backoff_until,
            }
        return {
            "cameras": {ctx.camera_name: dict(ctx.stats) for ctx in cameras},
            "backoff": backoff_info,
        }

    def _print_all_stats(self) -> None:
        stats = self.get_stats()
        lines = ["[GlobalScheduler] 停止统计:"]
        for name, s in stats["cameras"].items():
            lines.append(
                f"  {name}: "
                f"总={s['total']} "
                f"成功={s['success']} "
                f"失败={s['failed']} "
                f"重试成功={s['retried_success']} "
                f"客户端错误={s['client_errors']} "
                f"跳过={s['skipped']} "
                f"队列溢出={s['queue_overflows']}"
            )
        b = stats["backoff"]
        lines.append(
            f"  退避: 连续500={b['consecutive_500']} "
            f"退避至="
            f"{time.strftime('%H:%M:%S', time.localtime(b['backoff_until'])) if b['backoff_until'] else '无'}"
        )
        info("\n".join(lines))


# ══════════════════════════════════════════════
#  自定义异常
# ══════════════════════════════════════════════
class _ClientError(Exception):
    """HTTP 4xx 错误，payload 本身有问题，不应重试。"""

    def __init__(self, status_code: int, body: str = ""):
        super().__init__(f"HTTP {status_code}: {body}")
        self.status_code = status_code
        self.body = body


# ══════════════════════════════════════════════
#  工具函数
# ══════════════════════════════════════════════
def _validate_payload(payload: dict) -> Tuple[bool, str]:
    """
    合理性校验，只拦截明显异常值：
      · width 为负数
      · width 超过上限（单位错误）
    width=0 / length=0 均允许（未就绪时传 0，由服务端处理）。
    """
    width = payload.get("width", 0.0)

    if width < 0.0:
        return False, f"width={width}mm 为负数，数据异常"

    if width > MAX_WIDTH_MM:
        return False, (
            f"width={width}mm > 上限 {MAX_WIDTH_MM}mm，"
            f"疑似单位错误（应为 mm）"
        )

    return True, ""


def _has_meaningful_data(payload: dict) -> Tuple[bool, str]:
    """
    业务校验：
      · length/width 允许为0（未就绪时传0，由服务端处理）
      · camera1/camera8（无纬线检测）：
          - lngDensity、evennessValue 必须 > 0
          - latDensity、latSlope 允许为0
      · 其他相机（有纬线检测）：
          - lngDensity、latDensity、evennessValue 必须 > 0
          - latSlope 跟随 latDensity（不单独检查）

    返回：(是否通过, 跳过原因)
    """
    lng_density = payload.get("lngDensity", 0)
    lat_density = payload.get("latDensity", 0)
    evenness_value = payload.get("evennessValue", 0)
    camera_name = payload.get("cameraName", "")

    # ① 经线密度必须有值（所有相机）
    if lng_density <= 0:
        return False, f"lngDensity={lng_density}，经线密度未就绪"

    # ② 均匀度必须有值（所有相机）
    if evenness_value <= 0:
        return False, f"evennessValue={evenness_value}，均匀度未就绪"

    # ③ camera1 和 camera8 跳过纬线检查（允许 latDensity=0, latSlope=0）
    if camera_name in ("camera1", "camera8"):
        return True, ""

    # ④ 其他相机必须纬线密度也有值（latSlope 跟随 latDensity，不单独检查）
    if lat_density <= 0:
        return False, (
            f"latDensity={lat_density}，纬线密度未就绪"
            f"（非camera1/camera8需等待纬线检测）"
        )

    return True, ""


def _spacing_to_density(max_spacing_px: int) -> float:
    from AnomalyDetection_Tool.config.settings import REAL_WIDTH_CM, IMAGE_WIDTH_PX
    if max_spacing_px <= 0:
        return 0.0
    pixel_per_cm = IMAGE_WIDTH_PX / REAL_WIDTH_CM
    return (pixel_per_cm / max_spacing_px) * 10.0


# ══════════════════════════════════════════════
#  进程级单例
# ══════════════════════════════════════════════
_scheduler_instance: Optional[GlobalReportScheduler] = None
_singleton_lock = threading.Lock()


def get_global_scheduler() -> GlobalReportScheduler:
    """获取全局调度器单例（线程安全）"""
    global _scheduler_instance
    if _scheduler_instance is None:
        with _singleton_lock:
            if _scheduler_instance is None:
                _scheduler_instance = GlobalReportScheduler()
    return _scheduler_instance