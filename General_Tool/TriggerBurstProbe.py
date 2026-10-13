# -*- coding: utf-8 -*-
"""触发突发探针：把"一次触发同时打了几台相机、峰差多少、当时内存什么状况"记下来。

为什么要有这个模块
------------------
现场的现象是**整机卡死后断电**。断电指向电源（过流保护 / 温度保护），
而 8 台相机同时开始推流正好是一个瞬时功率尖峰：

    上线 38.1 MB（8 × 4.77 MB）
    BGR  114.4 MB，在 8 条 SDK 回调线程上**同时**分配
    1GbE 单链路要 305 ms 才传得完

但"是不是真的 8 台同时到达"这件事，光看代码推不出来 —— 取决于物理接线
（8 台是不是接同一张网卡）、交换机、以及触发线本身。所以这里把**实测的
到达时间分布**记下来，下次出事时日志里最后几行就是证据。

设计纪律（和 CameraOperation 的包长路径、NicFilterCheck 同一条）
--------------------------------------------------------------
  * ``record_arrival()`` 跑在 SDK 的 C 回调线程上，**必须极便宜且永不抛异常**
  * 分析放在独立的守护线程里做，回调线程只往 deque 里塞一条
  * 任何异常都吞掉；探针的失败模式是"没有诊断数据"，绝不能是"取流挂了"
  * 关掉（``enabled=False``）时 ``record_arrival`` 直接 return，零开销
"""

import threading
import time
from collections import deque
from typing import Dict, List, Optional, Tuple

from General_Tool.EnhancedLogger import info, warning

# 到达记录环形缓冲。8 台 × 2fps × 60s ≈ 960 条，留 4096 足够覆盖一个报告周期
_MAX_RECORDS = 4096

# 两次到达之间超过这个间隔就认为是**新的一次触发**（秒）。
# 现场触发间隔约 481 ms（10 m/min、80.224 mm 触发距离），所以 100 ms 足够
# 把同一次触发的 8 台聚在一起，又不会把相邻两次触发混起来。
_BURST_GAP_SEC = 0.1


class TriggerBurstProbe:
    """单例式探针。所有方法都吞异常。"""

    def __init__(self, enabled: bool = True, report_interval: float = 30.0,
                 min_burst_cameras: int = 2, logger=None,
                 pool_info_fn=None):
        self.enabled = bool(enabled)
        self.report_interval = max(1.0, float(report_interval or 30.0))
        # 一次触发里到达 >= 这么多台才报告（1 台说明没有同时触发，不值得说）
        self.min_burst_cameras = max(2, int(min_burst_cameras or 2))
        self._log = logger or info
        self._warn = logger or warning

        self._lock = threading.Lock()
        self._buf = deque(maxlen=_MAX_RECORDS)
        self._overflow = 0          # 被 maxlen 挤掉的条数（说明报告周期太长）

        # 统计（供测试和外部读取）
        self.total_arrivals = 0
        self.total_bursts = 0
        self.max_burst_size = 0
        self.max_spread_ms = 0.0
        self.last_burst_size = 0
        self.last_spread_ms = 0.0

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pool_info_fn = pool_info_fn

    # ------------------------------------------------------------------
    #  热路径：SDK 回调线程调这个
    # ------------------------------------------------------------------
    def record_arrival(self, camera_name, ts: Optional[float] = None) -> None:
        """记一次帧到达。**极便宜、永不抛异常。**

        关掉时直接 return —— 生产环境不需要诊断时应该是零开销。
        """
        if not self.enabled:
            return
        try:
            t = float(ts) if ts is not None else time.time()
            with self._lock:
                if len(self._buf) == self._buf.maxlen:
                    self._overflow += 1
                self._buf.append((t, str(camera_name)))
                self.total_arrivals += 1
        except Exception:
            pass

    # ------------------------------------------------------------------
    #  分析（守护线程里跑，也可以在测试里直接调）
    # ------------------------------------------------------------------
    def _drain(self) -> List[Tuple[float, str]]:
        with self._lock:
            items = list(self._buf)
            self._buf.clear()
            overflow = self._overflow
            self._overflow = 0
        if overflow:
            self._warn(f"[触发突发] 报告周期内到达记录溢出 {overflow} 条"
                       f"（缓冲上限 {_MAX_RECORDS}），统计可能不完整；"
                       f"可缩短 burst_report_interval。")
        return items

    @staticmethod
    def cluster_bursts(items: List[Tuple[float, str]],
                       gap: float = _BURST_GAP_SEC
                       ) -> List[List[Tuple[float, str]]]:
        """把到达记录按时间聚成"一次触发"。**纯函数**，方便测。"""
        if not items:
            return []
        ordered = sorted(items, key=lambda x: x[0])
        bursts: List[List[Tuple[float, str]]] = [[ordered[0]]]
        for rec in ordered[1:]:
            if rec[0] - bursts[-1][-1][0] > gap:
                bursts.append([rec])
            else:
                bursts[-1].append(rec)
        return bursts

    def analyse_and_report(self, force: bool = False) -> Dict:
        """分析一个周期内的到达分布并打日志。**永不抛异常。**"""
        out: Dict = {"bursts": 0, "multi": 0, "max_size": 0,
                     "max_spread_ms": 0.0, "arrivals": 0}
        if not self.enabled:
            return out
        try:
            items = self._drain()
            out["arrivals"] = len(items)
            if not items:
                return out

            bursts = self.cluster_bursts(items)
            out["bursts"] = len(bursts)
            self.total_bursts += len(bursts)

            multi = [b for b in bursts
                     if len({c for _, c in b}) >= self.min_burst_cameras]
            out["multi"] = len(multi)
            if not multi and not force:
                return out

            worst = None
            for b in multi:
                ts = [t for t, _ in b]
                spread = (max(ts) - min(ts)) * 1000.0
                size = len({c for _, c in b})
                if worst is None or spread > worst[0]:
                    worst = (spread, size, sorted({c for _, c in b}))
                self.max_burst_size = max(self.max_burst_size, size)
                self.max_spread_ms = max(self.max_spread_ms, spread)
                # 这两个也要填进返回值 —— 只更新实例属性的话，调用方
                # 拿到的是初始化的 0，看起来像"没测到突发"。
                out["max_size"] = max(out["max_size"], size)
                out["max_spread_ms"] = max(out["max_spread_ms"], spread)

            if worst:
                spread, size, cams = worst
                self.last_burst_size = size
                self.last_spread_ms = spread
                self._log(
                    "[触发突发] 本周期 {} 次触发，其中 {} 次是 ≥{} 台同时到达；"
                    "最坏一次 {} 台、首末相差 {:.1f} ms：{}".format(
                        len(bursts), len(multi), self.min_burst_cameras,
                        size, spread, ",".join(cams)))
                # 峰差很小 = 真的同时到达 = 突发最尖锐的时候
                if spread < 5.0 and size >= 6:
                    self._warn(
                        "[触发突发] ⚠ {} 台相机在 {:.1f} ms 内**几乎同时**到达。"
                        "这一次上线突发约 {:.1f} MB、BGR 分配约 {:.1f} MB，"
                        "全部压在 8 条 SDK 回调线程上 —— 如果卡死/断电就发生在这"
                        "个时刻附近，基本可以锁定是同时推流造成的瞬时尖峰。"
                        "对策见 config.yaml 的 cameras.acquisition."
                        "trigger_stagger_us。".format(
                            size, spread, size * 4.77, size * 14.30))
                self._append_pool_state()
        except Exception:
            pass
        return out

    def _append_pool_state(self) -> None:
        """顺带记当时的内存 / 内核非分页池状况。读不到就跳过，不报错。"""
        try:
            parts = []
            try:
                import psutil
                vm = psutil.virtual_memory()
                parts.append(f"RAM {vm.percent:.0f}%"
                             f"(可用 {vm.available / 1048576:.0f}MB)")
                parts.append(f"进程RSS {psutil.Process().memory_info().rss / 1048576:.0f}MB")
            except Exception:
                pass
            fn = self._pool_info_fn
            if fn is None:
                try:
                    from General_Tool.HealthMonitor import get_kernel_pool_info
                    fn = get_kernel_pool_info
                except Exception:
                    fn = None
            if fn is not None:
                try:
                    pool = fn()
                    if pool and pool.get("pool_nonpaged_mb"):
                        parts.append(
                            f"内核非分页池 {pool['pool_nonpaged_mb']:.0f}MB")
                except Exception:
                    pass
            if parts:
                self._log("[触发突发] 同时刻资源: " + " | ".join(parts))
        except Exception:
            pass

    # ------------------------------------------------------------------
    #  生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动后台报告线程。幂等；失败只告警。"""
        if not self.enabled or self._thread is not None:
            return
        try:
            self._thread = threading.Thread(
                target=self._loop, name="TriggerBurstProbe", daemon=True)
            self._thread.start()
            self._log(f"[触发突发] 探针已启动（每 {self.report_interval:.0f}s 报告一次；"
                      f"≥{self.min_burst_cameras} 台同时到达才报）")
        except Exception as e:
            self._thread = None
            try:
                self._warn(f"[触发突发] 探针启动失败（已忽略，不影响运行）: {e!r}")
            except Exception:
                pass

    def _loop(self) -> None:
        while not self._stop.wait(self.report_interval):
            try:
                self.analyse_and_report()
            except Exception:
                pass

    def stop(self) -> None:
        self._stop.set()
        t, self._thread = self._thread, None
        if t is not None:
            try:
                t.join(timeout=2.0)
            except Exception:
                pass


# ----------------------------------------------------------------------
#  模块级单例
# ----------------------------------------------------------------------
_probe: Optional[TriggerBurstProbe] = None
_probe_lock = threading.Lock()


def get_probe() -> Optional[TriggerBurstProbe]:
    return _probe


def init_probe(enabled: bool = True, report_interval: float = 30.0,
               min_burst_cameras: int = 2) -> Optional[TriggerBurstProbe]:
    """初始化并启动探针。幂等。"""
    global _probe
    with _probe_lock:
        if _probe is None:
            _probe = TriggerBurstProbe(
                enabled=enabled, report_interval=report_interval,
                min_burst_cameras=min_burst_cameras)
            _probe.start()
        return _probe


def record_arrival(camera_name, ts: Optional[float] = None) -> None:
    """给 ``_on_capture`` 调的模块级入口。探针没初始化时是空操作。"""
    p = _probe
    if p is None:
        return
    try:
        p.record_arrival(camera_name, ts)
    except Exception:
        pass


def shutdown() -> None:
    global _probe
    with _probe_lock:
        p, _probe = _probe, None
    if p is not None:
        try:
            p.stop()
        except Exception:
            pass
