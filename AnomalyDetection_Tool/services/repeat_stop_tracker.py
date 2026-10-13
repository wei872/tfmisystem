#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 (相机, 类别) 统计疵点重复出现的滑动窗口。

需求（现场原话）：「在一台相机上，连续几秒检测到相同类型的疵点就停机」，
例如 camera3 上 3 秒内出现 3 次「包边不良」就触发停机。

这个模块只做**计数与判定**，不做停机动作、不碰相机、不读配置 ——
配置合并在 ``settings.effective_repeat_rule``，停机动作在
``StopMachineService``。这样它能脱离现场环境单独测。

三条已经和现场确认过的语义：
  1. **按帧计数**：同一帧里同类出现 5 个框也只算 1 次。
     （按框算的话，单帧误检 3 个就会立刻误停。）
  2. **滑动窗口**：任意 window_sec 秒内累计满 count 次就触发，
     中间允许有空档。（严格连续帧的话，漏检一帧就得从头数。）
  3. 与单次停机规则**并存**：两条任一条满足就停机。

计数门槛（min_confidence）必须**独立**于单次停机门槛。
   单次规则是"这一个疵点够不够大/够不够确信"，重复规则是"这类疵点是不是
   一直在出现"。如果复用单次门槛，那些每次都差一点点的疵点永远计不进来，
   本功能就完全不起作用了。
"""

import threading
import time
from collections import deque
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from General_Tool.EnhancedLogger import info, warning


# 每个 (相机, 类别) 最多留多少个时间戳。
# 现场 10 m/min 下每台约 2.08 帧/秒，最短窗口也远用不到 64 个；
# 这个上限纯粹是防呆 —— 万一 window_sec 被配成一个天文数字，
# 也不会让 deque 无限涨。
_MAX_EVENTS_PER_KEY = 64


class RepeatStopTracker:
    """线程安全的滑动窗口计数器。

    在 post-worker 线程池里被调用（``post_workers`` 条线程并发），
    所以所有状态读写都在锁内。
    """

    def __init__(self,
                 rule_provider: Callable[[str, str], Optional[dict]],
                 clock: Callable[[], float] = time.monotonic):
        """
        :param rule_provider: ``(class_name, camera_name) -> 生效规则 dict 或 None``。
                              None = 这个 (相机, 类) 不做重复判定。
                              每次调用都重新取，这样 config.yaml 热更新能立刻生效。
        :param clock:         取时间的函数。**默认 time.monotonic 而不是
                              time.time** —— 时间窗口必须用单调时钟，
                              否则系统对时/NTP 校时会让窗口突然清空或突然塞满。
                              测试时注入假时钟。
        """
        self._rule_provider = rule_provider
        self._clock = clock
        self._lock = threading.Lock()
        # (camera_name, class_name) -> deque[float]，升序时间戳
        self._events: Dict[Tuple[str, str], deque] = {}
        self._stats = {
            "frames_seen": 0,
            "occurrences": 0,
            "triggered": 0,
            "no_rule": 0,
        }

    # ------------------------------------------------------------------
    def record_frame(self,
                     camera_name: str,
                     classes: Iterable[str],
                     confidences: Optional[Dict[str, float]] = None,
                     ) -> List[dict]:
        """记录一帧的检出结果，返回**本帧新触发**的重复停机列表。

        :param camera_name: 相机名（camera1..camera8）
        :param classes:     本帧检出的类别。可以重复，内部会去重 ——
                            这是"按帧计数"语义的实现点。
        :param confidences: ``{类名: 本帧该类的最高置信度}``。用于计数门槛。
                            没传就不做置信度过滤。
        :return: ``[{"class_name":..., "count":..., "window_sec":...,
                     "span_sec":...}, ...]``，空列表 = 本帧没触发。
        """
        if not camera_name:
            return []
        confidences = confidences or {}
        # 按帧去重：同一帧同类出现多个框只算一次
        uniq = []
        seen = set()
        for c in classes:
            if c and c not in seen:
                seen.add(c)
                uniq.append(c)
        if not uniq:
            return []

        now = self._clock()
        fired: List[dict] = []

        with self._lock:
            self._stats["frames_seen"] += 1
            for cls in uniq:
                try:
                    rule = self._rule_provider(cls, camera_name)
                except Exception as e:      # 配置读坏了不能影响检测主流程
                    warning(f"[重复停机] 取 {camera_name}/{cls} 的规则失败，"
                            f"本次跳过: {e}")
                    rule = None
                if not rule:
                    self._stats["no_rule"] += 1
                    continue

                min_conf = rule.get("min_confidence")
                if min_conf is not None:
                    got = confidences.get(cls)
                    if got is None or got < float(min_conf):
                        continue        # 这一帧的这个类不够格，不计入

                hit = self._record_locked(camera_name, cls, rule, now)
                if hit is not None:
                    fired.append(hit)
                    self._stats["triggered"] += 1

        for h in fired:
            info(f"[重复停机] {camera_name} 在 {h['window_sec']:.1f}s 内"
                 f"累计 {h['count']} 次「{h['class_name']}」"
                 f"（实际跨度 {h['span_sec']:.2f}s）→ 触发停机")
        return fired

    # ------------------------------------------------------------------
    def _record_locked(self, camera_name: str, cls: str,
                       rule: dict, now: float) -> Optional[dict]:
        """记一次出现并判断是否到阈值。**必须在锁内调用。**"""
        try:
            count = int(rule.get("count", 0))
            window = float(rule.get("window_sec", 0.0))
        except (TypeError, ValueError):
            warning(f"[重复停机] {camera_name}/{cls} 的 count/window_sec "
                    f"不是数字，已忽略该规则")
            return None
        if count <= 1 or window <= 0:
            # count<=1 就退化成"出现即停"，那是单次停机规则的职责，
            # 不在这里做 —— 否则两条路径会重复停机、日志也会打架。
            return None

        key = (camera_name, cls)
        dq = self._events.get(key)
        if dq is None:
            dq = deque(maxlen=_MAX_EVENTS_PER_KEY)
            self._events[key] = dq

        dq.append(now)
        self._stats["occurrences"] += 1

        # 滑掉窗口外的
        cutoff = now - window
        while dq and dq[0] < cutoff:
            dq.popleft()

        if len(dq) < count:
            return None

        span = (dq[-1] - dq[0]) if len(dq) > 1 else 0.0
        # 必须清空。否则窗口里一直 >= count，之后**每一帧**都会触发，
        #    停机信号会被刷爆。
        n = len(dq)
        dq.clear()
        return {"class_name": cls, "count": n,
                "window_sec": window, "span_sec": span}

    # ------------------------------------------------------------------
    def snapshot(self, camera_name: Optional[str] = None,
                 class_name: Optional[str] = None) -> List[dict]:
        """当前各 (相机, 类) 窗口里攒了几次。排查/日志用。"""
        now = self._clock()
        with self._lock:
            out = []
            for (cam, cls), dq in self._events.items():
                if camera_name and cam != camera_name:
                    continue
                if class_name and cls != class_name:
                    continue
                out.append({"camera_name": cam, "class_name": cls,
                            "pending": len(dq),
                            "oldest_age_sec": (now - dq[0]) if dq else 0.0})
            return out

    def get_stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def clear(self) -> None:
        """清空所有窗口。测试用；现场换布/重启检测时也可以调。"""
        with self._lock:
            self._events.clear()
