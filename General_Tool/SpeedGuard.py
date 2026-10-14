#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""超速守卫：车速过高时，把**接受帧率**钳到设计车速对应的触发率。

前提：**机器的实际运行速度就是 10 m/min。**

所以 ``cap_speed_mpm = 10.0`` 的含义是"按正常运行速度收图"，钳帧等于回到
正常工况；``warn_mpm = 15.0`` 是**设计速度的 1.5 倍**，一个刻意放宽的容差
带，留余量是为了不被测速抖动和加减速瞬态误触发 —— 不是可以随手往下压的
任意阈值。超过 15 已属异常；超过 20（设计速度的 2 倍）才会出现盲区，
正常运行根本不该到，所以盲区告警同时也是"机器跑飞了"的信号。

需求
----
速度超过 ``warn_mpm``（默认 15 m/min）时，不再按编码器实际触发率收图，
而是按 ``cap_speed_mpm``（默认 10 m/min）对应的触发率收图 ——
即每台相机的两帧之间至少间隔

    cap_interval = trigger_distance_mm / (cap_speed_mpm * 1000 / 60)

代入实配值：触发距离 102 × 0.78651 = 80.224 mm，10 m/min
⇒ 2.0775 Hz ⇒ **481.3 ms**。

为什么需要
----------
现场日志出现过 ``速度:41.9565m/min``。按设计点 10 m/min 算每台相机
2.078 帧/秒；42 m/min 就是 8.7 帧/秒，8 台合计约 70 帧/秒 × 15 MB
≈ **1 GB/s 内存分配**。而现有唯一的保护（``detection_worker_pool.submit()``
里的 memory_guard + 非阻塞信号量）触发在**第三步**：

    SDK 回调线程: _on_capture → _to_bgr()   ← 15MB 分配 + Bayer 转换，无条件执行
                  → image_control → submit() ← 这里才丢

最贵的那部分已经做完了才丢。本模块把闸门提到 ``_to_bgr`` **之前**。

覆盖率代价（重要）
------------------
钳帧只能落在**触发栅格**上，所以实际接受间隔被量化：

    实际接受间隔 = k × 触发距离      k = ceil(cap_interval / 触发间隔)，整数
    接受帧间距   = k × 触发距离      ← 与车速无关，只取决于 k

帧高 = 2048 px × 0.0942623 = **193.05 mm**。完整覆盖要求 k × 80.224 ≤ 193.05，
即 **k ≤ floor(193.05 / 80.224) = 2**。实测：

    车速       k   接受间隔   间距       覆盖
    10 m/min   1    481 ms    80.2 mm   完整（未钳帧）
    15 m/min   2    642 ms   160.4 mm   完整
    20 m/min   2    481 ms   160.4 mm   完整
    24 m/min   3    602 ms   240.7 mm   盲区 47.6 mm
    30 m/min   3    481 ms   240.7 mm   盲区 47.6 mm
    42 m/min   5    573 ms   401.1 mm   盲区 208.1 mm

**临界车速 = floor(帧高 / 触发距离) × cap_speed = 2 × 10 = 20.0 m/min。**
注意不是 帧高 / cap_interval（= 24.06）—— 那个式子忽略了量化，是错的，
第一版就是这么写的，靠仿真跑出来才发现。低于 20 m/min 一毫米都不漏；
高于它必然出现盲区，因此会额外告警 —— 现场定了"不接受漏检"，这告警不能省。

这是个**无法两全的权衡**：临界车速恒等于 cap_speed 的 floor(帧高/触发距离) 倍。

  * cap_speed 调**大** → 覆盖上限高，但对系统的保护弱；
  * cap_speed 调**小** → 保护强，但盲区来得更早（cap_speed=5 ⇒ 临界仅 10）。

唯一能同时改善两者的只有**缩短触发距离**（k 的上限变大）：
触发距离减半到 40.112 mm ⇒ floor=4 ⇒ 临界 40 m/min。
但那是 PLC / 编码器的改动，不在软件范围内。

实测边界（模拟真实帧流、Modbus 每 100 ms 喂速度）：16.0 ~ 19.5 m/min 都是
k=2、间距 160.4 mm、完整；20.0 起变成 k=3、间距 240.7 mm、出现盲区。
20.0 是刀刃值 —— 理论上 k = ceil(481.3/240.67) = 2，但浮点累积会让第二帧
落在 481.34 ms 刚好超阈值被拒。**稳妥按 ≤19 m/min 视为安全。**

策略取舍（现场已确认）
----------------------
**宁可漏检也要保护机器。**

超过临界车速后钳帧必然产生盲区，这个代价现场明确接受了，因此：

  * 盲区只**告警**，不触发任何额外动作（不停机、不改变钳帧力度）；
  * 但"漏了多少"必须看得见 —— ``speed_dropped`` 计数并进健康快照
    （``cameraN.speed_capped``），守卫状态报成 ``speed_guard.*``。
    接受漏检不等于可以是一笔糊涂账。

钳到 ``cap_speed_mpm`` 的触发率，等于把负载精确拉回 10 m/min 的设计点
（2.0775 帧/秒/台 × 8 台），这是已验证能长期跑的工况，所以不需要钳得更狠。

三个实现前提
------------
1. **判定用滑动中值，不用 Modbus 的 ``current_speed`` 原始值。**
   后者是单次差分 ``|Δ距离 / dt| × 60``，且 ``dt <= 0`` 时兜底成 0.001
   （单位毫秒，即 1 微秒）—— 两次采样挨得极近时，1 个脉冲
   (0.023558 mm) 就能算出 1413 m/min。拿它直接判定会疯狂误触发。

2. **限流按相机独立计。** 8 台相机各自被 Line0 触发，
   共用一个时间戳会让它们互相挤占配额。

3. **软件降不了硬件触发率。** 相机是 Line0 硬触发，帧率由编码器决定；
   这里做的是"触发了但不收"。真要降触发率得改触发距离或降车速。

失败开放（fail-open）
---------------------
拿不到速度、样本不足、或本模块自己出异常时，**一律收图**。
一个测速故障不该让整条产线瞎掉。
"""

import math
import statistics
import threading
import time
from collections import deque
from typing import Dict, Optional

from General_Tool.EnhancedLogger import info, warning


DEFAULT_WARN_MPM = 15.0            # 超过即进入钳帧模式
DEFAULT_CAP_SPEED_MPM = 10.0       # 钳到该车速对应的触发率
DEFAULT_TRIGGER_DISTANCE_MM = 102 * 0.78651   # = 80.224，见 config.yaml plc 段
DEFAULT_WINDOW = 5
DEFAULT_HYSTERESIS = 0.9           # 退出钳帧要降到 阈值×该系数，防反复横跳
DEFAULT_FRAME_HEIGHT_MM = 2048 * (23 / 2440) * 10   # = 193.05


class SpeedGuard:
    """线程安全。``update`` 在 Modbus 线程，``should_drop`` 在 8 个 SDK 回调线程。"""

    def __init__(self,
                 warn_mpm: float = DEFAULT_WARN_MPM,
                 cap_speed_mpm: float = DEFAULT_CAP_SPEED_MPM,
                 trigger_distance_mm: float = DEFAULT_TRIGGER_DISTANCE_MM,
                 frame_height_mm: float = DEFAULT_FRAME_HEIGHT_MM,
                 window: int = DEFAULT_WINDOW,
                 hysteresis: float = DEFAULT_HYSTERESIS,
                 min_samples: int = 3,
                 clock=time.monotonic):
        self.warn_mpm = max(0.1, float(warn_mpm))
        self.cap_speed_mpm = max(0.1, float(cap_speed_mpm))
        self.trigger_distance_mm = max(0.1, float(trigger_distance_mm))
        self.frame_height_mm = max(1.0, float(frame_height_mm))
        # window 允许 1（退化成不过滤，测试用）；生产建议 >= 5
        self.window = max(1, int(window))
        self.hysteresis = min(0.99, max(0.5, float(hysteresis)))
        # min_samples 必须 <= window，否则 deque 永远装不满，
        # 守卫会永远不下任何判断（第一版犯过这个错，测试当场抓到）
        self.min_samples = max(1, min(int(min_samples), self.window))
        self._clock = clock

        # 钳帧间隔：cap_speed 对应的触发周期
        fps = self.cap_speed_mpm * 1000.0 / 60.0 / self.trigger_distance_mm
        self.cap_fps = fps
        self.cap_interval_sec = 1.0 / fps
        # 超过这个车速，钳帧就会产生盲区。
        #
        # 不能用 帧高/cap_interval 算 —— 那样得到 24.06 m/min，是错的。
        # 钳帧只能落在触发栅格上，实际接受间隔被量化成 k × 触发距离
        # （k = ceil(cap_interval / 触发间隔)，整数）。间距 = k × 触发距离，
        # 与车速无关！所以完整覆盖的条件是
        #     k <= floor(帧高 / 触发距离)
        # 而 k <= K 等价于 触发间隔 >= cap_interval / K，即
        #     车速 <= K × 触发距离 × cap_fps × 60/1000 = K × cap_speed
        # 代入：floor(193.05/80.224)=2 ⇒ **临界车速 = 2 × 10 = 20.0 m/min**。
        self._max_k = max(1, int(self.frame_height_mm // self.trigger_distance_mm))
        self.blind_spot_mpm = self._max_k * self.cap_speed_mpm

        self._samples = deque(maxlen=self.window)
        self._lock = threading.Lock()
        self._filtered = 0.0
        self._capping = False
        self._blind_warned = False
        self._last_accept: Dict[str, float] = {}
        # 实测的接受间隔（秒）。**不能**用 cap_interval_sec 算盲区：
        # 钳帧只能落在触发栅格上，481 ms 的间隔遇到 240 ms 的栅格会被量化成
        # 720 ms，实际间距比理论值大得多。20 m/min 就是这么从"完整覆盖"
        # 变成 240 mm 间距的。
        self._measured_gap_sec = 0.0
        self._stats = {"updates": 0, "spikes_filtered": 0,
                       "cap_entered": 0, "frames_capped": 0,
                       "blind_warned": 0}

    # ------------------------------------------------------------------
    #  Modbus 线程：喂速度
    # ------------------------------------------------------------------
    def update(self, speed_mpm: Optional[float]) -> None:
        """喂一个速度样本（m/min）。None / NaN / inf / 负数直接忽略。"""
        try:
            if speed_mpm is None:
                return
            v = float(speed_mpm)
            if not (v == v) or v < 0 or v == float("inf"):
                return
            now = self._clock()
            with self._lock:
                self._samples.append(v)
                self._stats["updates"] += 1
                if len(self._samples) < self.min_samples:
                    return                      # 样本不足，不下任何判断
                med = statistics.median(self._samples)
                if med > 0 and v > med * 3.0:
                    self._stats["spikes_filtered"] += 1
                self._filtered = med
                self._evaluate_locked(med, now)
        except Exception as e:                  # 守卫自己出问题不能影响测速
            warning(f"[超速守卫] update 异常，本次忽略: {e}")

    def _evaluate_locked(self, med: float, now: float) -> None:
        if not self._capping and med >= self.warn_mpm:
            self._capping = True
            self._stats["cap_entered"] += 1
            self._last_accept.clear()           # 新窗口重新计时
            warning(
                f"[超速守卫] 车速 {med:.2f} m/min ≥ {self.warn_mpm:.1f}，"
                f"按 {self.cap_speed_mpm:.1f} m/min 的触发率收图"
                f"（每台相机最小帧间隔 {self.cap_interval_sec * 1000:.0f} ms，"
                f"≈{self.cap_fps:.2f} 帧/秒）")
        elif self._capping and med < self.warn_mpm * self.hysteresis:
            self._capping = False
            self._last_accept.clear()
            self._measured_gap_sec = 0.0
            info(f"[超速守卫] 车速已回落到 {med:.2f} m/min，"
                 f"恢复按编码器实际触发率收图")

        # 盲区告警：钳帧后接受帧间距 = 车速 × cap_interval，超过帧高就有盲区
        if self._capping:
            # 用实测间隔（含触发栅格量化），不用理论 cap_interval
            eff = max(self.cap_interval_sec, self._measured_gap_sec)
            gap = med * 1000.0 / 60.0 * eff
            if gap > self.frame_height_mm and not self._blind_warned:
                self._blind_warned = True
                self._stats["blind_warned"] += 1
                warning(
                    f"[超速守卫] 车速 {med:.2f} m/min 超过临界值 "
                    f"{self.blind_spot_mpm:.1f} m/min：钳帧后接受帧间距 "
                    f"{gap:.1f} mm > 帧高 {self.frame_height_mm:.1f} mm，"
                    f"布面出现 {gap - self.frame_height_mm:.1f} mm 盲区。"
                    f"这是现场已确认的取舍（宁可漏检也要保护机器），"
                    f"机器保护优先。但注意：机器设计运行速度只有 10 m/min，"
                    f"临界值是它的 2 倍 —— 跑到这里说明速度本身异常，"
                    f"该查的是为什么超速，不只是漏检。")
            elif gap <= self.frame_height_mm:
                self._blind_warned = False

    # ------------------------------------------------------------------
    #  SDK 采集线程：热路径
    # ------------------------------------------------------------------
    def should_drop(self, camera_name: str = "") -> bool:
        """本帧该不该丢。**按相机独立限流**，只在钳帧模式下生效。"""
        if not self._capping:                   # 常态：一次 bool 判断，零成本
            return False
        try:
            now = self._clock()
            key = camera_name or "_"
            with self._lock:
                last = self._last_accept.get(key)
                if last is not None and (now - last) < self.cap_interval_sec:
                    self._stats["frames_capped"] += 1
                    return True
                if last is not None:
                    # 记录量化后的真实间隔，盲区判定要用它
                    self._measured_gap_sec = max(self._measured_gap_sec,
                                                 now - last)
                self._last_accept[key] = now
            return False
        except Exception:
            return False                        # fail-open：出错就收图

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            med = self._filtered
            eff = max(self.cap_interval_sec, self._measured_gap_sec)
            gap = med * 1000.0 / 60.0 * eff if med else 0.0
            return {
                "filtered_mpm": round(med, 3),
                "last_raw_mpm": (round(self._samples[-1], 3)
                                 if self._samples else None),
                "warn_mpm": self.warn_mpm,
                "cap_speed_mpm": self.cap_speed_mpm,
                "cap_fps": round(self.cap_fps, 4),
                "cap_interval_ms": round(self.cap_interval_sec * 1000, 1),
                "measured_interval_ms": round(self._measured_gap_sec * 1000, 1),
                "blind_spot_mpm": round(self.blind_spot_mpm, 2),
                "max_k": self._max_k,
                "accept_gap_mm": round(gap, 1),
                "frame_height_mm": round(self.frame_height_mm, 2),
                "capping": self._capping,
                "window": self.window,
                "samples": len(self._samples),
                "stats": dict(self._stats),
            }

    def clear(self) -> None:
        with self._lock:
            self._samples.clear()
            self._filtered = 0.0
            self._capping = False
            self._blind_warned = False
            self._last_accept.clear()
            self._measured_gap_sec = 0.0


# ======================================================================
#  模块级单例
# ======================================================================
_guard: Optional[SpeedGuard] = None
_guard_lock = threading.Lock()


def init_speed_guard(warn_mpm: float = DEFAULT_WARN_MPM,
                     cap_speed_mpm: float = DEFAULT_CAP_SPEED_MPM,
                     trigger_distance_mm: float = DEFAULT_TRIGGER_DISTANCE_MM,
                     frame_height_mm: float = DEFAULT_FRAME_HEIGHT_MM,
                     window: int = DEFAULT_WINDOW,
                     hysteresis: float = DEFAULT_HYSTERESIS) -> SpeedGuard:
    global _guard
    with _guard_lock:
        _guard = SpeedGuard(warn_mpm=warn_mpm, cap_speed_mpm=cap_speed_mpm,
                            trigger_distance_mm=trigger_distance_mm,
                            frame_height_mm=frame_height_mm,
                            window=window, hysteresis=hysteresis)
    info(f"[超速守卫] 已就绪：车速 ≥{warn_mpm:.1f} m/min 时按 "
         f"{cap_speed_mpm:.1f} m/min 的触发率收图"
         f"（最小帧间隔 {_guard.cap_interval_sec * 1000:.0f} ms ≈ "
         f"{_guard.cap_fps:.2f} 帧/秒/台）；"
         f"临界车速 {_guard.blind_spot_mpm:.1f} m/min，超过会有盲区并告警")
    return _guard


def get_speed_guard() -> Optional[SpeedGuard]:
    return _guard
