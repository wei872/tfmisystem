#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""多股线边缘区自学习过滤器。

要解决的问题
------------
布卷开头一段，camera2 图像的**右侧**、camera7 图像的**左侧**会稳定出现
多股线检出。那是布边/接布的固有特征，不是疵点，但每卷都在同一个横向位置
反复报，既污染记录又可能误停机。

做法：每卷开头**自己学出**那条横向带，学出来之后本卷永久过滤。

生命周期（"当前布料"= 一个布卷）
--------------------------------
窗口起点有两个，取先到者：
  * 前端下发「开始」（ThriftControl 的 ``register_on_start`` 回调）
  * 程序启动（main_yolo 初始化时调一次）
窗口终点也有两个，取先到者：
  * 前端下发「停止」
  * 米长从窗口起点累计增长超过 ``window_length_mm``（默认 10 m）

窗口内（学习期）：
  * 对目标相机的多股线**临时**用 ``learning_threshold``（0.1）——
    比正常的 0.65 低得多，目的是把那些"看得见但够不到阈值"的边缘检出也捞进来；
  * 落在目标边带内（camera2 右 350 px / camera7 左 350 px）的框
    **只记录，不保存、不上传**；
  * 落在边带外、且置信度低于正常阈值的框，同样**不保存、不上传**。
    这一条是必须的：阈值降到 0.1 之后，边带外会冒出一堆低置信度噪声，
    它们只是因为临时降阈值才存在的，绝不能当成真缺陷报出去。
    置信度**达到**正常阈值的框是真检出，照常走保存/上传/停机。

窗口结束：
  * 记录数 > ``count_threshold``（默认 5，即 ≥6 个）才生成区域；
  * 区域 = 所有记录框的 [min(x1), max(x2)]，左右各外扩 ``region_padding_px``
    （默认 50 px），并夹到图像边界内；
  * 该区域成为**本卷**的多股线永久过滤区，落在里面的多股线一律滤掉；
  * 下一卷「开始」时清空重学。

为什么区域要按相机分开存
------------------------
camera2 学的是右侧带、camera7 学的是左侧带，是两张不同的图，
横向坐标没有可比性。共用一条区间会让一台相机的布边去屏蔽另一台的真缺陷。

线程模型
--------
``observe()`` / ``should_filter()`` 在多个检测 worker 线程上调用，
``begin_window()`` / ``end_window()`` 在 Thrift 线程和主线程上调用。
全程一把锁，临界区只做字典和列表操作，不做 I/O、不打日志。
"""

import threading
from typing import Dict, List, Optional, Tuple

from General_Tool.EnhancedLogger import info, warning


# 默认目标：camera2 看右侧、camera7 看左侧
DEFAULT_TARGETS: Dict[str, Dict] = {
    "camera2": {"side": "right", "band_px": 350},
    "camera7": {"side": "left", "band_px": 350},
}


class MultiLineEdgeLearner:
    """多股线边缘区自学习。线程安全。"""

    def __init__(self,
                 targets: Optional[Dict[str, Dict]] = None,
                 target_class: str = "多股线",
                 learning_threshold: float = 0.1,
                 window_length_mm: float = 10000.0,
                 count_threshold: int = 5,
                 region_padding_px: int = 50,
                 image_width_px: int = 2440,
                 distance_provider=None):
        """
        :param targets: ``{camera_name: {"side": "left"|"right", "band_px": int}}``
        :param count_threshold: 记录数**超过**这个值才生成区域（>5 即 ≥6）
        :param distance_provider: 无参可调用，返回当前累计米长(mm)。
                                  传 None 时只能靠 begin/end 显式控制窗口。
        """
        # 三态，别用 `targets or DEFAULT_TARGETS`：
        #   None  = 没配，用默认的 camera2/camera7
        #   {}    = 显式配成空的，**等于禁用**，绝不能回落到默认
        #   {...} = 用配的那份
        # （T33 的白名单踩过同一个坑：`x.get(k) or default` 把"配了但为空"
        #   和"没配"混为一谈。）
        self.targets: Dict[str, Dict] = {}
        source = DEFAULT_TARGETS if targets is None else targets
        for cam, spec in source.items():
            side = str(spec.get("side", "")).lower()
            if side not in ("left", "right"):
                warning(f"[多股线学习] {cam} 的 side={spec.get('side')!r} "
                        f"不是 left/right，已忽略这台相机")
                continue
            try:
                band = int(spec.get("band_px", 0))
            except (TypeError, ValueError):
                warning(f"[多股线学习] {cam} 的 band_px="
                        f"{spec.get('band_px')!r} 不是整数，已忽略")
                continue
            if band <= 0:
                warning(f"[多股线学习] {cam} 的 band_px={band} <= 0，已忽略")
                continue
            self.targets[cam] = {"side": side, "band_px": band}

        self.target_class = str(target_class)
        self.learning_threshold = float(learning_threshold)
        self.window_length_mm = max(0.0, float(window_length_mm))
        self.count_threshold = int(count_threshold)
        self.region_padding_px = max(0, int(region_padding_px))
        self.image_width_px = max(1, int(image_width_px))
        self._distance_provider = distance_provider

        self._lock = threading.RLock()
        self._window_open = False
        self._start_distance = 0.0
        # 学习期收集的框坐标：{camera: [(x1, x2), ...]}
        self._samples: Dict[str, List[Tuple[int, int]]] = {
            c: [] for c in self.targets}
        # 学成的永久过滤区：{camera: (x_left, x_right)}
        self._regions: Dict[str, Tuple[int, int]] = {}
        self._stats = {
            "windows": 0, "recorded": 0, "suppressed_out_of_band": 0,
            "regions_learned": 0, "filtered_by_region": 0,
            "closed_by_distance": 0, "closed_by_stop": 0,
            "not_enough_samples": 0,
        }

    # ------------------------------------------------------------------
    #  窗口生命周期
    # ------------------------------------------------------------------
    def begin_window(self, reason: str = "") -> None:
        """开一个新布卷的学习窗口。会清掉上一卷学成的区域。"""
        with self._lock:
            if not self.targets:
                return
            self._start_distance = self._read_distance()
            self._window_open = True
            self._samples = {c: [] for c in self.targets}
            # 上一卷的区域必须清空：区域是"当前布卷"的，跨卷沿用会拿
            # 上一卷的布边去屏蔽这一卷的真缺陷。
            self._regions = {}
            self._stats["windows"] += 1
        info(f"[多股线学习] 新布卷窗口开启"
             f"{'（' + reason + '）' if reason else ''}："
             f"起始米长 {self._start_distance / 1000:.3f} m，"
             f"学习长度 {self.window_length_mm / 1000:.1f} m，"
             f"目标 {list(self.targets.keys())}")

    def end_window(self, reason: str = "") -> None:
        """关闭学习窗口并结算区域。"""
        with self._lock:
            if not self._window_open:
                return
            self._window_open = False
            if reason == "distance":
                self._stats["closed_by_distance"] += 1
            else:
                self._stats["closed_by_stop"] += 1
            self._settle_locked()
        for cam, reg in sorted(self._regions.items()):
            info(f"[多股线学习] {cam} 本卷永久过滤区 x∈[{reg[0]}, {reg[1]}]"
                 f"（宽 {reg[1] - reg[0]} px）")

    def check_distance(self) -> None:
        """米长超过窗口长度就自动结算。由检测路径顺带调用。"""
        if not self._window_open or self.window_length_mm <= 0:
            return
        try:
            grown = self._read_distance() - self._start_distance
        except Exception:
            return
        if grown >= self.window_length_mm:
            self.end_window(reason="distance")

    def _settle_locked(self) -> None:
        """结算：记录数超过阈值才生成区域。必须在持锁状态下调用。"""
        for cam, samples in self._samples.items():
            n = len(samples)
            if n <= self.count_threshold:
                if n:
                    self._stats["not_enough_samples"] += 1
                    warning(
                        f"[多股线学习] {cam} 本卷只记录到 {n} 个多股线，"
                        f"未超过 {self.count_threshold} 个，不生成过滤区"
                        f"（多股线检测恢复原阈值，照常保存/上传）")
                continue
            x_min = min(s[0] for s in samples)
            x_max = max(s[1] for s in samples)
            left = max(0, x_min - self.region_padding_px)
            right = min(self.image_width_px, x_max + self.region_padding_px)
            if right <= left:
                continue
            self._regions[cam] = (left, right)
            self._stats["regions_learned"] += 1
            warning(
                f"[多股线学习] {cam} 本卷记录到 {n} 个多股线"
                f"（> {self.count_threshold}），生成永久过滤区："
                f"原始 x∈[{x_min}, {x_max}] 外扩 {self.region_padding_px} px "
                f"=> x∈[{left}, {right}]。本卷内落在该区的多股线不再报出。")

    # ------------------------------------------------------------------
    #  查询
    # ------------------------------------------------------------------
    def is_learning(self, camera_name: str) -> bool:
        """这台相机当前是否处于学习期（需要用低阈值捞框）。"""
        if camera_name not in self.targets:
            return False
        with self._lock:
            return self._window_open

    def learning_threshold_for(self, camera_name: str,
                               class_name: str) -> Optional[float]:
        """学习期该相机该类的临时阈值；不适用时返回 None。"""
        if class_name != self.target_class:
            return None
        if not self.is_learning(camera_name):
            return None
        return self.learning_threshold

    def has_region(self, camera_name: str) -> bool:
        with self._lock:
            return camera_name in self._regions

    def region_of(self, camera_name: str) -> Optional[Tuple[int, int]]:
        with self._lock:
            return self._regions.get(camera_name)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "window_open": self._window_open,
                "start_distance_mm": round(self._start_distance, 1),
                "grown_mm": round(self._read_distance() - self._start_distance, 1)
                              if self._window_open else 0.0,
                "window_length_mm": self.window_length_mm,
                "target_class": self.target_class,
                "learning_threshold": self.learning_threshold,
                "count_threshold": self.count_threshold,
                "samples": {c: len(v) for c, v in self._samples.items()},
                "regions": {c: list(v) for c, v in self._regions.items()},
                "targets": {c: dict(v) for c, v in self.targets.items()},
                "stats": dict(self._stats),
            }

    # ------------------------------------------------------------------
    #  热路径：判定 + 记录
    # ------------------------------------------------------------------
    def _in_band(self, camera_name: str, x1: int, x2: int) -> bool:
        spec = self.targets.get(camera_name)
        if not spec:
            return False
        if spec["side"] == "right":
            return x2 >= self.image_width_px - spec["band_px"]
        return x1 <= spec["band_px"]

    def should_filter(self, camera_name: str, class_name: str,
                      x1: int, x2: int) -> bool:
        """本卷已学成的永久过滤区：落在里面的多股线一律滤掉。"""
        if class_name != self.target_class:
            return False
        with self._lock:
            reg = self._regions.get(camera_name)
            if reg is None:
                return False
            # 与区间有重叠就算命中：多股线是横向的长线，
            # 只要有一部分在过滤区里，报出来就是同一条布边。
            if x2 >= reg[0] and x1 <= reg[1]:
                self._stats["filtered_by_region"] += 1
                return True
            return False

    def observe(self, camera_name: str, class_name: str,
                x1: int, x2: int, confidence: float,
                normal_threshold: float) -> bool:
        """学习期观察一个框。返回 True 表示**该框要被抑制**（不保存/不上传）。

        只在 ``is_learning()`` 为真时有意义；不在学习期直接返回 False。
        """
        if class_name != self.target_class:
            return False
        if camera_name not in self.targets:
            return False
        # 达到正常阈值的是真检出，学习期也照常放行
        if float(confidence) >= float(normal_threshold):
            return False
        with self._lock:
            if not self._window_open:
                return False
            if self._in_band(camera_name, x1, x2):
                self._samples[camera_name].append((int(x1), int(x2)))
                self._stats["recorded"] += 1
                return True
            # 边带外的低置信度框：只是因为临时降阈值才存在，丢掉
            self._stats["suppressed_out_of_band"] += 1
            return True

    # ------------------------------------------------------------------
    def _read_distance(self) -> float:
        if self._distance_provider is None:
            return 0.0
        try:
            return float(self._distance_provider() or 0.0)
        except Exception:
            return 0.0


# ======================================================================
#  模块级单例
# ======================================================================
_learner: Optional[MultiLineEdgeLearner] = None
_learner_lock = threading.Lock()


def init_multiline_learner(**kwargs) -> MultiLineEdgeLearner:
    global _learner
    with _learner_lock:
        _learner = MultiLineEdgeLearner(**kwargs)
    return _learner


def get_multiline_learner() -> Optional[MultiLineEdgeLearner]:
    return _learner
