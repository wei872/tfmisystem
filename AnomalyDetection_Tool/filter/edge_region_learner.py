#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""边缘区自学习过滤器（当前用于多股线，结构上可扩展到其他类别）。

要解决的问题
------------
布卷开头一段，camera2 图像的**右侧**、camera7 图像的**左侧**会稳定出现
多股线检出。那是布边/接布的固有特征，不是疵点，但每卷都在同一个横向位置
反复报。做法：每卷开头**自己学出**那条横向带，学出来之后本卷永久过滤。

两个区，别混
------------
**学习区（learning zone）** —— 固定的，配置写死：
    camera2 图像右侧 350 px，camera7 图像左侧 350 px。
    只有落在这里的框才会被降阈值、被采样。

**生效区（effective zone）** —— 算出来的，每卷不同：
    学习期结束后，用学习区里采到的框统计 [min(x1), max(x2)]，
    左右各外扩 ``region_padding_px``，夹到图像边界内。
    这才是真正用来过滤的区域。

生效区由学习区统计而来，但**不等于**学习区：它可能比 350 px 窄
（布边实际只占一小段），也可能因为外扩而略微越出 350 px。

阈值只在学习区内降低（关键）
---------------------------
学习期需要把目标类的阈值从正常的 0.65 降到 ``learning_threshold``（0.1），
才能把"看得见但够不到阈值"的布边检出捞进来采样。

这个降阈值**必须限定在学习区内**。第一版把整台相机的阈值都降了，
结果学习区外也冒出 0.1~0.65 的框，只能再额外加一条"区外低置信度框
也丢掉"的补救规则 —— 那是把两个问题搅在一起。正确做法是区外**照旧**
用正常阈值：区外根本不会产生低置信度框，也就不需要任何补救规则。

生命周期
--------
学习期**只由米长决定**：从窗口起点算，累计增长到 ``window_length_mm``
（默认 10 m）为止。到点即结算出生效区，学习期结束。

前端「停止」表示**这批布结束了**，它不是学习期的终点：
    * 它不会用半截数据去结算生效区（没到 10 m 就是没学成）；
    * 它只负责清场 —— 生效区是本卷的，布都下机了就该失效。

窗口起点有两个，取先到者：前端下发「开始」、程序启动。

计数与生成
----------
按 **(相机, 类别)** 分别计数和分别生成生效区：
    * 记录数 > ``count_threshold``（默认 5，即 ≥6 个）才生成；
    * camera2 学的是右侧、camera7 学的是左侧，两张不同的图，
      横向坐标没有可比性，绝不能共用一条区间；
    * 不同类别的布边位置也没有关系，同样各存各的。

线程模型
--------
``observe()`` / ``should_filter()`` / ``learning_threshold_for()`` 在多个
检测 worker 线程上调用，``begin_window()`` / ``end_roll()`` 在 Thrift
线程和主线程上调用。全程一把锁，临界区只做字典和列表操作，不做 I/O。
"""

import threading
from typing import Dict, List, Optional, Tuple

from General_Tool.EnhancedLogger import info, warning


# 默认学习区：camera2 看右侧、camera7 看左侧
DEFAULT_TARGETS: Dict[str, Dict] = {
    "camera2": {"side": "right", "band_px": 350},
    "camera7": {"side": "left", "band_px": 350},
}

# 当前只处理多股线；以后要加类别，往配置里添一项即可，代码不用动。
DEFAULT_TARGET_CLASSES: Tuple[str, ...] = ("多股线",)


class EdgeRegionLearner:
    """边缘区自学习。线程安全。"""

    def __init__(self,
                 targets: Optional[Dict[str, Dict]] = None,
                 target_classes=None,
                 learning_threshold: float = 0.1,
                 window_length_mm: float = 10000.0,
                 count_threshold: int = 5,
                 region_padding_px: int = 50,
                 image_width_px: int = 2440,
                 distance_provider=None):
        """
        :param targets: ``{camera_name: {"side": "left"|"right", "band_px": int}}``
                        None = 用默认；``{}`` = 显式禁用（**不**回落默认）
        :param target_classes: 类别列表；None = 用默认（多股线）
        :param count_threshold: 记录数**超过**这个值才生成生效区（>5 即 ≥6）
        :param distance_provider: 无参可调用，返回当前累计米长(mm)
        """
        # 三态，别写 `targets or DEFAULT_TARGETS`：
        #   None  = 没配，用默认
        #   {}    = 显式配成空的，**等于禁用**，绝不能回落默认
        #   {...} = 用配的那份
        # （T33 的白名单踩过同一个坑。）
        self.targets: Dict[str, Dict] = {}
        source = DEFAULT_TARGETS if targets is None else targets
        for cam, spec in source.items():
            side = str(spec.get("side", "")).lower()
            if side not in ("left", "right"):
                warning(f"[边缘区学习] {cam} 的 side={spec.get('side')!r} "
                        f"不是 left/right，已忽略这台相机")
                continue
            try:
                band = int(spec.get("band_px", 0))
            except (TypeError, ValueError):
                warning(f"[边缘区学习] {cam} 的 band_px="
                        f"{spec.get('band_px')!r} 不是整数，已忽略")
                continue
            if band <= 0:
                warning(f"[边缘区学习] {cam} 的 band_px={band} <= 0，已忽略")
                continue
            self.targets[cam] = {"side": side, "band_px": band}

        if target_classes is None:
            classes = list(DEFAULT_TARGET_CLASSES)
        elif isinstance(target_classes, str):
            classes = [target_classes]        # 容错：配成单个字符串
        else:
            classes = [str(c) for c in target_classes if str(c).strip()]
        self.target_classes: Tuple[str, ...] = tuple(dict.fromkeys(classes))

        self.learning_threshold = float(learning_threshold)
        self.window_length_mm = max(0.0, float(window_length_mm))
        self.count_threshold = int(count_threshold)
        self.region_padding_px = max(0, int(region_padding_px))
        self.image_width_px = max(1, int(image_width_px))
        self._distance_provider = distance_provider

        self._lock = threading.RLock()
        self._window_open = False
        self._start_distance = 0.0
        # 学习期采样：{(camera, class): [(x1, x2), ...]}
        self._samples: Dict[Tuple[str, str], List[Tuple[int, int]]] = {}
        # 生效区：{(camera, class): (x_left, x_right)}
        self._regions: Dict[Tuple[str, str], Tuple[int, int]] = {}
        self._stats = {
            "windows": 0, "recorded": 0, "regions_learned": 0,
            "filtered_by_region": 0, "settled_by_distance": 0,
            "rolls_ended": 0, "not_enough_samples": 0,
        }
        self._reset_samples_locked()

    # ------------------------------------------------------------------
    #  生命周期
    # ------------------------------------------------------------------
    def _reset_samples_locked(self) -> None:
        self._samples = {(c, cls): []
                         for c in self.targets for cls in self.target_classes}

    def begin_window(self, reason: str = "") -> None:
        """开一个新布卷的学习窗口（前端「开始」/ 程序启动）。

        清掉上一卷的生效区 —— 生效区是**本卷**的，跨卷沿用会拿上一卷的
        布边去屏蔽这一卷的真缺陷。
        """
        with self._lock:
            if not self.targets or not self.target_classes:
                return
            self._start_distance = self._read_distance()
            self._window_open = True
            self._reset_samples_locked()
            self._regions = {}
            self._stats["windows"] += 1
            start = self._start_distance
        info(f"[边缘区学习] 新布卷窗口开启"
             f"{'（' + reason + '）' if reason else ''}："
             f"起始米长 {start / 1000:.3f} m，学习长度 "
             f"{self.window_length_mm / 1000:.1f} m，"
             f"类别 {list(self.target_classes)}，目标 {list(self.targets)}")

    def end_roll(self, reason: str = "") -> None:
        """前端「停止」：这批布结束了。

        **不结算**。学习期是否结束、生效区生不生成，只由 10 m 米长判断；
        「停止」只负责清场 —— 没学到 10 m 就是没学成，不能拿半截数据
        凑一个生效区出来。
        """
        with self._lock:
            self._window_open = False
            self._regions = {}
            self._reset_samples_locked()
            self._stats["rolls_ended"] += 1
        info(f"[边缘区学习] 本卷结束"
             f"{'（' + reason + '）' if reason else ''}："
             f"生效区已清空，下一卷重新学习")

    def check_distance(self) -> None:
        """米长到点就结算 —— 这是学习期**唯一**的终点。"""
        if not self._window_open or self.window_length_mm <= 0:
            return
        try:
            grown = self._read_distance() - self._start_distance
        except Exception:
            return
        if grown >= self.window_length_mm:
            self._settle(grown)

    def _settle(self, grown: float) -> None:
        with self._lock:
            if not self._window_open:
                return
            self._window_open = False
            self._stats["settled_by_distance"] += 1
            learned = []
            for key, samples in self._samples.items():
                n = len(samples)
                if n <= self.count_threshold:
                    if n:
                        self._stats["not_enough_samples"] += 1
                        warning(
                            f"[边缘区学习] {key[0]}/{key[1]} 本卷只采样到 "
                            f"{n} 个，未超过 {self.count_threshold} 个，"
                            f"不生成生效区（该类检测恢复原阈值，照常报出）")
                    continue
                x_min = min(s[0] for s in samples)
                x_max = max(s[1] for s in samples)
                left = max(0, x_min - self.region_padding_px)
                right = min(self.image_width_px, x_max + self.region_padding_px)
                if right <= left:
                    continue
                self._regions[key] = (left, right)
                self._stats["regions_learned"] += 1
                learned.append((key, n, x_min, x_max, left, right))
        for (cam, cls), n, x_min, x_max, left, right in learned:
            warning(
                f"[边缘区学习] {cam}/{cls} 本卷采样到 {n} 个"
                f"（> {self.count_threshold}），学习区统计出生效区："
                f"原始 x∈[{x_min}, {x_max}] 外扩 {self.region_padding_px} px "
                f"=> x∈[{left}, {right}]。本卷内落在该区的 {cls} 不再报出。")
        if not learned:
            info(f"[边缘区学习] 学习期结束（走了 {grown / 1000:.2f} m），"
                 f"未生成任何生效区")

    # ------------------------------------------------------------------
    #  查询
    # ------------------------------------------------------------------
    def is_learning(self, camera_name: str) -> bool:
        """这台相机当前是否处于学习期。"""
        if camera_name not in self.targets:
            return False
        with self._lock:
            return self._window_open

    def in_learning_zone(self, camera_name: str, x1: int, x2: int) -> bool:
        """框是否落在这台相机的学习区（固定的 350 px 边带）内。"""
        spec = self.targets.get(camera_name)
        if not spec:
            return False
        if spec["side"] == "right":
            return x2 >= self.image_width_px - spec["band_px"]
        return x1 <= spec["band_px"]

    def learning_threshold_for(self, camera_name: str, class_name: str,
                               x1: int, x2: int) -> Optional[float]:
        """学习期 + 目标类 + **框在学习区内** => 返回临时低阈值；否则 None。

        降阈值必须限定在学习区内：区外照旧用正常阈值，就不会产生
        "只因降阈值才存在的区外噪声框"，也就不需要任何补救性的抑制规则。
        """
        if class_name not in self.target_classes:
            return None
        if not self.in_learning_zone(camera_name, x1, x2):
            return None
        if not self.is_learning(camera_name):
            return None
        return self.learning_threshold

    def region_of(self, camera_name: str,
                  class_name: str) -> Optional[Tuple[int, int]]:
        with self._lock:
            return self._regions.get((camera_name, class_name))

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "window_open": self._window_open,
                "start_distance_mm": round(self._start_distance, 1),
                "grown_mm": round(self._read_distance() - self._start_distance, 1)
                              if self._window_open else 0.0,
                "window_length_mm": self.window_length_mm,
                "target_classes": list(self.target_classes),
                "learning_threshold": self.learning_threshold,
                "count_threshold": self.count_threshold,
                "samples": {f"{c}/{cls}": len(v)
                            for (c, cls), v in self._samples.items()},
                "regions": {f"{c}/{cls}": list(v)
                            for (c, cls), v in self._regions.items()},
                "targets": {c: dict(v) for c, v in self.targets.items()},
                "stats": dict(self._stats),
            }

    # ------------------------------------------------------------------
    #  热路径
    # ------------------------------------------------------------------
    def should_filter(self, camera_name: str, class_name: str,
                      x1: int, x2: int) -> bool:
        """落在本卷生效区内的目标类，一律滤掉。

        只对 ``target_classes`` 里的类生效 —— 生效区是**按类**存的，
        绝不会拿多股线的布边去屏蔽别的疵点。
        """
        if class_name not in self.target_classes:
            return False
        with self._lock:
            reg = self._regions.get((camera_name, class_name))
            if reg is None:
                return False
            # 有重叠就算命中：这类疵点是横向长线，
            # 只要有一部分在生效区里，报出来就是同一条布边。
            if x2 >= reg[0] and x1 <= reg[1]:
                self._stats["filtered_by_region"] += 1
                return True
            return False

    def observe(self, camera_name: str, class_name: str,
                x1: int, x2: int) -> bool:
        """学习期记录一个落在学习区内的框。返回 True 表示该框要被抑制。

        区外的框返回 False —— 它们用的是正常阈值，是真检出，照常报出。
        """
        if class_name not in self.target_classes:
            return False
        if camera_name not in self.targets:
            return False
        if not self.in_learning_zone(camera_name, x1, x2):
            return False                     # 区外：正常阈值下的真检出，放行
        with self._lock:
            if not self._window_open:
                return False
            self._samples[(camera_name, class_name)].append(
                (int(x1), int(x2)))
            self._stats["recorded"] += 1
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
_learner: Optional[EdgeRegionLearner] = None
_learner_lock = threading.Lock()


def init_edge_learner(**kwargs) -> EdgeRegionLearner:
    global _learner
    with _learner_lock:
        _learner = EdgeRegionLearner(**kwargs)
    return _learner


def get_edge_learner() -> Optional[EdgeRegionLearner]:
    return _learner
