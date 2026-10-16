#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""后端下发的检测配置（阈值 + 停机规则）的运行时覆盖层。

为什么需要单独一层
------------------
阈值和停机规则原本只有两个来源：``machine_stop_rules`` / ``class_confidence_thresholds``
（全局）和 ``camera_stop_rule_overrides`` / ``camera_threshold_overrides``（按相机）。
现在后端按**当前白布的材质+规格**下发一份配置，它是业务上的权威来源，
优先级必须高于本地 yaml。

但后端可能不可达、可能返回业务错误（没有运行中的白布等）。所以这一层的
语义是"**有就用，没有就当不存在**"：

  * 存储为空（从未成功拉取过）→ 两个解析入口完全走原有逻辑，行为不变；
  * 某台相机某个类没有下发 → 该类退回本地配置；
  * 拉取失败 → **保留上一次成功的结果**，绝不清空。
    拉取失败时把配置清掉，等于让整条产线突然换回另一套阈值，
    这比用一份稍旧的配置危险得多。

优先级
------
后端下发  >  按相机覆盖(camera_*_overrides)  >  全局(machine_stop_rules /
class_confidence_thresholds)

唯一的例外：``machine_stop_rules.enabled`` 这个**总开关**仍然最高。
它在本地 yaml 里，是现场的急停手段，后端不该能绕过它。

键为什么是 (camera, class)
--------------------------
后端只返回具体相机（不返回 ALL、不与"全部相机"合并），所以每条配置都
天然带相机维度。用 (camera, class) 做键，查询时不需要再做合并推断。
"""

import threading
import time
from typing import Dict, Optional, Tuple

# 这个模块本身不打日志：它是纯数据容器，日志由 material_config_client
# 统一输出，避免同一次刷新出现两处不同措辞的记录。


_Key = Tuple[str, str]        # (camera_name, class_name)


class RemoteConfigStore:
    """线程安全。写在拉取线程，读在检测 worker 线程。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._thresholds: Dict[_Key, float] = {}
        self._stop_rules: Dict[_Key, Dict] = {}
        self._meta: Dict[str, object] = {}
        self._applied_at = 0.0
        self._apply_count = 0

    # ------------------------------------------------------------------
    #  写入（整份替换，不做增量合并）
    # ------------------------------------------------------------------
    def apply(self, thresholds: Dict[_Key, float],
              stop_rules: Optional[Dict[_Key, Dict]] = None,
              meta: Optional[Dict] = None) -> None:
        """整份替换。**不做增量合并** —— 后端返回的是完整配置，
        增量合并会让后端删掉的一条永远留在本地。"""
        with self._lock:
            self._thresholds = dict(thresholds)
            self._stop_rules = {k: dict(v) for k, v in (stop_rules or {}).items()}
            self._meta = dict(meta or {})
            self._applied_at = time.time()
            self._apply_count += 1

    # ------------------------------------------------------------------
    #  读取
    # ------------------------------------------------------------------
    def get_threshold(self, camera_name: Optional[str],
                      class_name: str) -> Optional[float]:
        if not camera_name:
            return None
        with self._lock:
            return self._thresholds.get((str(camera_name), class_name))

    def get_stop_rule(self, camera_name: Optional[str],
                      class_name: str) -> Optional[Dict]:
        """返回**副本**。调用方改它不会污染存储。"""
        if not camera_name:
            return None
        with self._lock:
            rule = self._stop_rules.get((str(camera_name), class_name))
            return dict(rule) if rule is not None else None

    def is_empty(self) -> bool:
        with self._lock:
            return not self._thresholds and not self._stop_rules

    def snapshot(self) -> dict:
        with self._lock:
            cams = sorted({k[0] for k in
                           list(self._thresholds) + list(self._stop_rules)})
            return {
                "applied": self._apply_count > 0,
                "apply_count": self._apply_count,
                "age_sec": (round(time.time() - self._applied_at, 1)
                            if self._applied_at else None),
                "cameras": cams,
                "threshold_count": len(self._thresholds),
                "stop_rule_count": len(self._stop_rules),
                "thresholds": {f"{c}/{n}": v
                               for (c, n), v in sorted(self._thresholds.items())},
                "stop_rules": {f"{c}/{n}": dict(v)
                               for (c, n), v in sorted(self._stop_rules.items())},
                "meta": dict(self._meta),
            }


# ======================================================================
#  模块级单例
# ======================================================================
_store = RemoteConfigStore()


def get_remote_config_store() -> RemoteConfigStore:
    return _store


def reset_remote_config_store() -> None:
    """仅供测试：换成一个空存储。"""
    global _store
    _store = RemoteConfigStore()
