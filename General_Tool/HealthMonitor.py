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
  - **pool_nonpaged_growth_mb 一路爬升** -> 内核非分页池被驱动吃光，
    这类卡死**物理内存看起来是正常的**，两道基于物理内存的防线都不会报警。
    用 poolmon 定位是哪个驱动标签在涨。
  - queue 深度顶满        -> 下游（YOLO/GPU）吞吐跟不上采集
  - stream.lost 持续增长  -> 网络带宽/丢包，不是软件问题
  - stream.incomplete 涨但 lost 不涨 -> GigE 包长设错，不是带宽
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


# ======================================================================
#  Windows 内核池（非分页池 / 分页池）
# ======================================================================
def _define_perf_struct():
    """定义 PERFORMANCE_INFORMATION。

    字段顺序与 Win32 文档一致；ctypes 会按 C ABI 自动插入对齐填充
    （64 位下 ``cb`` 之后补 4 字节），所以不需要手写 padding。
    """
    import ctypes

    class _PERFORMANCE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("cb",                 ctypes.c_uint32),
            ("CommitTotal",        ctypes.c_size_t),
            ("CommitLimit",        ctypes.c_size_t),
            ("CommitPeak",         ctypes.c_size_t),
            ("PhysicalTotal",      ctypes.c_size_t),
            ("PhysicalAvailable",  ctypes.c_size_t),
            ("SystemCache",        ctypes.c_size_t),
            ("KernelTotal",        ctypes.c_size_t),
            ("KernelPaged",        ctypes.c_size_t),
            ("KernelNonPaged",     ctypes.c_size_t),
            ("PageSize",           ctypes.c_size_t),
            ("HandleCount",        ctypes.c_uint32),
            ("ProcessCount",       ctypes.c_uint32),
            ("ThreadCount",        ctypes.c_uint32),
        ]

    return ctypes, _PERFORMANCE_INFORMATION


def get_kernel_pool_info() -> Optional[Dict]:
    """读 Windows 内核池使用量（MB）。**非 Windows 或取不到时返回 None。**

    调用方必须把 ``None`` 当作"这项数据没有"，**绝不能当成 0**。

    为什么要单独监控它：

    ``MemoryGuard`` 和 ``rss_mb`` 看的都是**物理内存**。而网卡驱动、GigE
    过滤驱动是从**内核非分页池**里分配内存的——那是另一套资源。收包路径一堵，
    驱动持续申请非分页池，池子见底时整个 OS 失稳、只能硬重启，**而此时物理
    内存可能才用了 60%，两道基于物理内存的防线都不会触发**。

    这是"整机卡死但内存看起来不高"这一类现象最需要的观测点。

    安全性：
      - ``cb`` 传的是本进程里结构体的实际大小，结构体布局若与系统不符，
        ``GetPerformanceInfo`` 会返回 FALSE，我们返回 None（失败模式是
        "没有数据"，不是"错误数据"）；
      - 额外做一次**自证**：``PhysicalTotal × PageSize`` 应当等于 psutil
        报告的物理总内存。对不上说明字段偏移读错了，宁可返回 None。
    """
    import sys

    if sys.platform != "win32":
        return None

    try:
        ctypes, PERF = _define_perf_struct()
    except Exception:
        return None

    try:
        info_ = PERF()
        info_.cb = ctypes.sizeof(PERF)
        size = ctypes.c_uint32(info_.cb)

        fn = None
        for lib_name, fn_name in (("kernel32", "K32GetPerformanceInfo"),
                                  ("psapi", "GetPerformanceInfo")):
            try:
                fn = getattr(getattr(ctypes, "WinDLL")(lib_name), fn_name)
                break
            except Exception:
                continue
        if fn is None:
            return None

        fn.argtypes = [ctypes.POINTER(PERF), ctypes.c_uint32]
        fn.restype = ctypes.c_bool
        if not fn(ctypes.byref(info_), size):
            return None
    except Exception:
        return None

    page = int(info_.PageSize or 0)
    if page <= 0:
        return None

    # ---- 自证：物理总内存必须和 psutil 对得上，否则说明偏移读错了 ----
    if _PSUTIL:
        try:
            expect = psutil.virtual_memory().total
            got = int(info_.PhysicalTotal) * page
            if expect > 0 and abs(got - expect) > expect * 0.01:
                return None
        except Exception:
            pass

    mb = 1024.0 * 1024.0
    return {
        "pool_nonpaged_mb": round(int(info_.KernelNonPaged) * page / mb, 1),
        "pool_paged_mb":    round(int(info_.KernelPaged) * page / mb, 1),
        "pool_kernel_mb":   round(int(info_.KernelTotal) * page / mb, 1),
        "handles_system":   int(info_.HandleCount),
    }


class HealthMonitor:
    """周期性健康快照。"""

    def __init__(self,
                 interval: float = 30.0,
                 rss_warn_mb: float = 0.0,
                 thread_warn: int = 0,
                 pool_growth_warn_mb: float = 512.0,
                 providers: Optional[List[Callable[[], Dict]]] = None):
        """
        :param interval:     快照间隔（秒），<=0 表示不启动
        :param rss_warn_mb:  进程 RSS 超过此值(MB)时升级为 WARNING；0=不告警
        :param thread_warn:  线程数超过此值时升级为 WARNING；0=不告警
        :param pool_growth_warn_mb: 内核非分页池相比启动基线增长超过此值(MB)
                             时升级为 WARNING；0=不告警。这是"物理内存看起来
                             不高但整机要卡死"的唯一早期信号。
        :param providers:    额外的取数回调列表，每个返回一个 dict，
                             会被合并进快照（用于接检测队列/相机诊断）
        """
        self.interval = float(interval or 0)
        self.rss_warn_mb = float(rss_warn_mb or 0)
        self.thread_warn = int(thread_warn or 0)
        self.pool_growth_warn_mb = float(pool_growth_warn_mb or 0)
        self.providers = list(providers or [])

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._proc = None
        self._tick = 0
        self._last_gc_counts = gc.get_count()
        # 内核非分页池：耗尽是**缓慢爬升**，单点数值说明不了问题，
        # 必须记基线和峰值，卡死后翻日志看的是这条曲线。
        self._pool_baseline_mb: Optional[float] = None
        self._pool_peak_mb: float = 0.0
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

        # 内核非分页池（仅 Windows）。取不到就是 None，不写 0。
        try:
            pool = get_kernel_pool_info()
            if pool:
                snap.update(pool)
                cur = pool.get("pool_nonpaged_mb")
                if cur is not None:
                    if self._pool_baseline_mb is None:
                        self._pool_baseline_mb = cur
                    if cur > self._pool_peak_mb:
                        self._pool_peak_mb = cur
                    snap["pool_nonpaged_base_mb"] = self._pool_baseline_mb
                    snap["pool_nonpaged_peak_mb"] = self._pool_peak_mb
                    snap["pool_nonpaged_growth_mb"] = round(
                        cur - self._pool_baseline_mb, 1)
        except Exception as e:
            snap["pool_err"] = str(e)

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
                growth = snap.get("pool_nonpaged_growth_mb")
                over_pool = (self.pool_growth_warn_mb > 0
                             and growth is not None
                             and growth > self.pool_growth_warn_mb)
                if over_pool:
                    warning(
                        msg + f"  <-- 内核非分页池已比启动时涨 {growth:.0f}MB，"
                        f"超过告警线 {self.pool_growth_warn_mb:.0f}MB。"
                        f"这通常是网卡/GigE 过滤驱动在泄漏内核池，"
                        f"继续涨下去会导致整机失稳需硬重启。"
                        f"请用 poolmon 定位是哪个驱动标签在涨。")
                elif over_rss or over_thr:
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
                        pool_growth_warn_mb: float = 512.0,
                        providers: Optional[List[Callable[[], Dict]]] = None
                        ) -> Optional[HealthMonitor]:
    global _monitor
    _monitor = HealthMonitor(
        interval=interval,
        rss_warn_mb=rss_warn_mb,
        thread_warn=thread_warn,
        pool_growth_warn_mb=pool_growth_warn_mb,
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
