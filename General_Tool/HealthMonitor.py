#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
健康心跳 / 卡死取证。

为什么需要它：现场的症状是"整机突然卡死，只能重启"，重启之后**什么证据都没留下**。
faulthandler 能抓 Python 层死锁，但抓不到"内存一路涨到把页面文件打爆"这类
渐进式故障 —— 那需要的是**时间序列**。

本模块每隔 N 秒往日志里写一行紧凑的健康快照：
    进程 RSS、线程数、GC 计数、检测队列深度、各相机的帧数/丢帧数。

卡死之后翻日志，看最后几行心跳：
  - RSS 一路爬升          -> 内存泄漏 / 队列积压，往上传和检测链路查
  - 线程数一路爬升        -> 线程泄漏，往 ThreadPoolExecutor / Thread() 查
  - queue 深度顶满        -> 下游（YOLO/GPU）吞吐跟不上采集
  - stream.lost 持续增长  -> 网络带宽/丢包，不是软件问题
  - 心跳本身停了但进程还在 -> 真的是整机卡死（而非某个线程慢）

设计约束：
  - 心跳线程本身绝不能变成负担：只读计数器，不做任何 I/O 之外的重活；
  - 任何一个数据源取不到都不能让心跳挂掉（逐项 try）。
"""

import gc
import os
import threading
from typing import Callable, Dict, List, Optional

from General_Tool.EnhancedLogger import info, warning, error

try:
    import psutil
    _PSUTIL = True
except ImportError:          # pragma: no cover
    _PSUTIL = False


class HealthMonitor:
    """周期性健康快照。"""

    def __init__(self,
                 interval: float = 30.0,
                 rss_warn_mb: float = 0.0,
                 thread_warn: int = 0,
                 providers: Optional[List[Callable[[], Dict]]] = None):
        """
        :param interval:     快照间隔（秒），<=0 表示不启动
        :param rss_warn_mb:  进程 RSS 超过此值(MB)时升级为 WARNING；0=不告警
        :param thread_warn:  线程数超过此值时升级为 WARNING；0=不告警
        :param providers:    额外的取数回调列表，每个返回一个 dict，
                             会被合并进快照（用于接检测队列/相机诊断）
        """
        self.interval = float(interval or 0)
        self.rss_warn_mb = float(rss_warn_mb or 0)
        self.thread_warn = int(thread_warn or 0)
        self.providers = list(providers or [])

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc = None
        self._tick = 0
        self._last_gc_counts = gc.get_count()
        self._peak_rss_mb = 0.0

        if _PSUTIL:
            try:
                self._proc = psutil.Process(os.getpid())
            except Exception:
                self._proc = None

    # ------------------------------------------------------------------
    def add_provider(self, fn: Callable[[], Dict]) -> None:
        self.providers.append(fn)

    def snapshot(self) -> Dict:
        """采一次快照。任何一项失败都不影响其余项。"""
        snap: Dict = {"tick": self._tick}

        if self._proc is not None:
            try:
                with self._proc.oneshot():
                    snap["rss_mb"] = round(
                        self._proc.memory_info().rss / 1024 / 1024, 1)
                    snap["threads"] = self._proc.num_threads()
                    snap["cpu_pct"] = round(self._proc.cpu_percent(None), 1)
                    try:
                        snap["fds"] = self._proc.num_fds()      # Linux
                    except AttributeError:
                        snap["handles"] = self._proc.num_handles()  # Windows
            except Exception as e:
                snap["proc_err"] = str(e)
        else:
            snap["threads"] = threading.active_count()

        try:
            counts = gc.get_count()
            snap["gc"] = counts
            snap["gc_delta"] = counts[0] - self._last_gc_counts[0]
            self._last_gc_counts = counts
        except Exception:
            pass

        for fn in self.providers:
            try:
                extra = fn()
                if isinstance(extra, dict):
                    snap.update(extra)
            except Exception as e:
                snap[f"provider_err_{getattr(fn, '__name__', '?')}"] = str(e)

        return snap

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        info(f"[健康心跳] 已启动，每 {self.interval:.0f}s 一次快照"
             + ("" if _PSUTIL else "（psutil 不可用，只有线程数）"))
        while not self._stop.wait(self.interval):
            try:
                self._tick += 1
                snap = self.snapshot()

                rss = snap.get("rss_mb", 0.0)
                if rss > self._peak_rss_mb:
                    self._peak_rss_mb = rss
                snap["peak_rss_mb"] = round(self._peak_rss_mb, 1)

                msg = "[健康心跳] " + " ".join(
                    f"{k}={v}" for k, v in snap.items())

                # 有明确越界就升级成 WARNING，否则 INFO
                over_rss = self.rss_warn_mb > 0 and rss > self.rss_warn_mb
                over_thr = (self.thread_warn > 0
                            and snap.get("threads", 0) > self.thread_warn)
                if over_rss or over_thr:
                    warning(msg + "  <-- 超过告警线，请检查是否有泄漏/积压")
                else:
                    info(msg)
            except Exception as e:
                error(f"[健康心跳] 快照失败: {e}")
        info("[健康心跳] 已停止")

    # ------------------------------------------------------------------
    def start(self) -> bool:
        if self.interval <= 0:
            return False
        if self._thread is not None and self._thread.is_alive():
            return True
        self._thread = threading.Thread(
            target=self._loop, name="HealthMonitor", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None


# ======================================================================
# 进程内单例
# ======================================================================
_monitor: Optional[HealthMonitor] = None


def init_health_monitor(interval: float = 30.0,
                        rss_warn_mb: float = 0.0,
                        thread_warn: int = 0,
                        providers: Optional[List[Callable[[], Dict]]] = None
                        ) -> Optional[HealthMonitor]:
    global _monitor
    _monitor = HealthMonitor(
        interval=interval,
        rss_warn_mb=rss_warn_mb,
        thread_warn=thread_warn,
        providers=providers,
    )
    _monitor.start()
    return _monitor


def get_health_monitor() -> Optional[HealthMonitor]:
    return _monitor


def shutdown_health_monitor() -> None:
    global _monitor
    if _monitor is not None:
        _monitor.stop()
        _monitor = None
