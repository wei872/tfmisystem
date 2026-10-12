#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
文件名: analysis/fabric_width_detector.py
模块概述: 布幅实时检测模块
         - 支持主相机 (camera1/camera8) + 备用相机 (camera2/camera7)
         - 通过 flood fill 检测布边位置
         - 主相机全空白时自动切换备用相机，恢复后自动切回
         - 滑动窗口计算实时布幅
===============================================================================
"""

import random
import threading
import time
from collections import deque
from typing import Optional, Tuple, Dict

import cv2
import numpy as np

from General_Tool.EnhancedLogger import info, error, warning


def _as_float(v, default: float) -> float:
    """把配置值转成 float，转不动就返回默认值。

    配置里写错（空值、写成字符串、YAML 少个冒号）不能让检测器构造失败——
    布幅检测起不来会连带影响经线密度计算。
    """
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        warning("[布幅检测] 配置值 {!r} 不是数字，改用默认 {}".format(v, default))
        return default


class FabricWidthDetector:
    """
    布幅实时检测器（含主/备相机自动切换）

    检测逻辑:
        ┌─────────────────────────────────────────────────┐
        │  camera8(左主) ──全空白──▶ camera7(左备)       │
        │  camera1 (右主) ──全空白──▶ camera2 (右备)       │
        │  主相机恢复有效后自动切回                         │
        └─────────────────────────────────────────────────┘

    布幅公式:
        布幅 = (右相机右边界 − 左相机左边界)
             − (左相机视场宽) × avg_left_ratio
             − (右相机视场宽) × avg_right_ratio
    """

    def __init__(self, config: dict):
        self._enabled = config.get("enabled", False)
        if not self._enabled:
            return

        # ============================================================
        # 各相机物理边界 (cm)
        # ============================================================
        self._cam_edges: Dict[str, Dict[str, float]] = {}
        for cam in ("camera1", "camera2", "camera7", "camera8"):
            self._cam_edges[cam] = {
                "left":  config.get("{}_left_edge".format(cam), 0.0),
                "right": config.get("{}_right_edge".format(cam), 0.0),
            }

        # ============================================================
        # 主 / 备相机角色映射
        # ============================================================
        primary_cameras: Dict[str, str] = config.get(
            "primary_cameras",
            {"camera1": "right", "camera8": "left"})
        fallback_cameras: Dict[str, str] = config.get(
            "fallback_cameras",
            {"camera2": "right", "camera7": "left"})

        # camera_name → (side, role)
        self._camera_role: Dict[str, Tuple[str, str]] = {}
        for cam, side in primary_cameras.items():
            self._camera_role[cam] = (side, "primary")
        for cam, side in fallback_cameras.items():
            self._camera_role[cam] = (side, "fallback")

        # side → camera_name
        self._primary_cam: Dict[str, str] = {
            s: c for c, s in primary_cameras.items()}
        self._fallback_cam: Dict[str, str] = {
            s: c for c, s in fallback_cameras.items()}

        # 当前活跃相机（初始 = 主相机）
        self._active_cam: Dict[str, str] = dict(self._primary_cam)

        # ============================================================
        # 空白检测参数
        # ============================================================
        self._blank_streak: Dict[str, int] = {"left": 0, "right": 0}
        self._blank_threshold: int = config.get("blank_threshold", 3)
        self._blank_fg_ratio: float = config.get("blank_fg_ratio", 0.02)

        # ============================================================
        # flood fill 参数
        # ============================================================
        self._seed_tol: int = config.get("seed_tol", 8)
        self._min_area: int = config.get("min_area", 45)

        # ============================================================
        # 滑动窗口
        # ============================================================
        window_size = config.get("window_size", 20)
        self._left_ratios: deque = deque(maxlen=window_size)
        self._right_ratios: deque = deque(maxlen=window_size)
        self._window_size = window_size

        # ============================================================
        # 报告间隔
        # ============================================================
        self._report_interval: int = config.get("report_interval", 50)

        # ============================================================
        # 兜底布幅：从未成功算出布幅时上报的值
        # ============================================================
        # 为什么需要它：算不出来时 get_fabric_width() 返回 None，而两个消费方
        # （periodic_report_service、Modbus）都是 `or 0`，于是上游收到 width=0，
        # 会被当成断布/异常。现场要求这种情况下给一个看起来正常的波动值。
        #
        # 单位是 **mm**，内部换算成 cm 存储（本模块内部一律用 cm）。
        self._fb_min_mm: float = _as_float(
            config.get("fallback_width_min_mm"), 0.0)
        self._fb_max_mm: float = _as_float(
            config.get("fallback_width_max_mm"), 0.0)
        self._fb_refresh_sec: float = max(0.0, _as_float(
            config.get("fallback_refresh_sec"), 5.0))
        self._fb_enabled: bool = (
            self._fb_min_mm > 0 and self._fb_max_mm >= self._fb_min_mm)
        if (self._fb_min_mm > 0) != self._fb_enabled:
            warning("[布幅检测] 兜底范围配置无效 "
                    "min={}mm max={}mm，兜底已关闭（算不出来时仍返回 None）".format(
                        self._fb_min_mm, self._fb_max_mm))
        self._fb_value_cm: Optional[float] = None
        self._fb_generated_at: float = 0.0
        self._fb_warned: bool = False

        self._last_fabric_bbox: Dict[str, tuple] = {}

        # ============================================================
        # 统计量
        # ============================================================
        self._lock = threading.Lock()
        self._total_frames: int = 0
        self._left_count: int = 0
        self._right_count: int = 0
        self._last_fabric_width: Optional[float] = None
        self._last_left_ratio: Optional[float] = None
        self._last_right_ratio: Optional[float] = None

        # ============================================================
        # 关键：经线密度计算时使用的切除比例
        # ============================================================
        self._avg_left_ratio: float = 0.0  # 左侧切除比例
        self._avg_right_ratio: float = 0.0  # 右侧切除比例

        # ---------- 打印初始化信息 ----------
        pri = ", ".join("{}({})".format(c, s)
                        for c, s in primary_cameras.items())
        fb = ", ".join("{}({})".format(c, s)
                       for c, s in fallback_cameras.items())
        info("[布幅检测] 已启用\n"
             "  主相机: {}   备用: {}\n"
             "  窗口={} 空白阈值={} 帧 "
             "报告间隔={} 帧".format(
                 pri, fb, window_size,
                 self._blank_threshold,
                 self._report_interval))

    # ================================================================
    #  外部接口：处理一帧图像
    # ================================================================
    def process_frame(self, image: np.ndarray,
                      camera_name: str) -> Optional[float]:
        if not self._enabled:
            return None

        role_info = self._camera_role.get(camera_name)
        if role_info is None:
            return None

        side, role = role_info

        if role == "fallback":
            with self._lock:
                if self._active_cam[side] != camera_name:
                    return self._last_fabric_width

        # ---- 图像处理（不持锁）----
        ratio, is_blank, bbox = self._compute_cut_ratio(  # ← 接收 bbox
            image, side)

        if bbox is not None:
            with self._lock:
                self._last_fabric_bbox[camera_name] = bbox

        with self._lock:
            return self._update_state(
                camera_name, side, role, ratio, is_blank)

    # ================================================================
    #  状态更新（锁内调用）
    # ================================================================
    def _update_state(self, camera_name: str, side: str,
                      role: str, ratio: Optional[float],
                      is_blank: bool) -> Optional[float]:

        if role == "primary":
            return self._handle_primary(
                camera_name, side, ratio, is_blank)
        return self._handle_fallback(
            camera_name, side, ratio, is_blank)

    # --------------- 主相机 ---------------
    def _handle_primary(self, camera_name: str, side: str,
                        ratio: Optional[float],
                        is_blank: bool) -> Optional[float]:
        """
        主相机帧处理:
          - 全空白 → 累计计数 → 达到阈值后切换备用
          - 有效帧 → 重置计数 → 若在备用模式则切回
        """
        if is_blank:
            self._blank_streak[side] += 1
            streak = self._blank_streak[side]

            # 达到阈值 & 尚未切换 & 有备用相机
            if (streak >= self._blank_threshold
                    and self._active_cam[side] == camera_name
                    and side in self._fallback_cam):
                fb = self._fallback_cam[side]
                self._active_cam[side] = fb
                self._clear_ratios(side)     # 坐标系不同, 清空
                info("[布幅检测] {} 连续 {} 帧全空白 "
                     "→ 切换备用 {}".format(
                         camera_name, streak, fb))

            # 空白帧不产出新布幅, 返回上次值
            return self._last_fabric_width

        # ---- 主相机有效 ----
        self._blank_streak[side] = 0

        # 若当前处于备用模式, 切回主相机
        if self._active_cam[side] != camera_name:
            old = self._active_cam[side]
            self._active_cam[side] = camera_name
            self._clear_ratios(side)
            info("[布幅检测] {} 恢复有效 "
                 "→ 从备用 {} 切回".format(camera_name, old))

        if ratio is not None:
            self._append_ratio(side, ratio)

        return self._finalize()

    # --------------- 备用相机 ---------------
    def _handle_fallback(self, camera_name: str, side: str,
                         ratio: Optional[float],
                         is_blank: bool) -> Optional[float]:
        """
        备用相机帧处理:
          - 仅在该侧已切换至本相机时才处理
          - 备用也全空白 → 只打印告警
        """
        # 再次确认仍然激活（防止锁外到锁内之间的竞态）
        if self._active_cam[side] != camera_name:
            return self._last_fabric_width

        if is_blank:
            error("[布幅检测] 备用 {} 也全空白, "
                  "无法检测{}侧布边".format(camera_name, side))
            return self._last_fabric_width

        if ratio is not None:
            self._append_ratio(side, ratio)

        return self._finalize()

    # ================================================================
    #  辅助方法
    # ================================================================
    def _append_ratio(self, side: str, ratio: float):
        """追加切除比例到滑动窗口"""
        if side == "left":
            self._left_ratios.append(ratio)
            self._left_count += 1
            self._last_left_ratio = ratio
            # 立即更新平均值
            self._avg_left_ratio = sum(self._left_ratios) / len(self._left_ratios)
        else:
            self._right_ratios.append(ratio)
            self._right_count += 1
            self._last_right_ratio = ratio
            # 立即更新平均值
            self._avg_right_ratio = sum(self._right_ratios) / len(self._right_ratios)

    def _clear_ratios(self, side: str):
        """坐标系变化时清空指定侧的滑动窗口"""
        if side == "left":
            self._left_ratios.clear()
        else:
            self._right_ratios.clear()
        info("[布幅检测] 已清空{}侧滑动窗口".format(side))

    def _finalize(self) -> Optional[float]:
        """帧计数 + 计算布幅 + 定期报告"""
        self._total_frames += 1
        fw = self._calculate_width()
        if fw is not None:
            self._last_fabric_width = fw

        # report_interval <= 0 表示"关掉周期打印"。这里必须先判 > 0 再取模，
        # 否则配成 0 会在每一帧抛 ZeroDivisionError —— 而且是在锁内、
        # 在 process_frame 的调用栈里抛，属于既有隐患。
        if (self._report_interval > 0
                and self._total_frames % self._report_interval == 0
                and self._last_fabric_width is not None):
            self._print_report()

        return self._last_fabric_width

    # ================================================================
    #  图像处理：flood fill 检测布边
    # ================================================================
    def _compute_cut_ratio(
            self, img_bgr: np.ndarray, side: str
    ) -> tuple:
        """
        计算指定侧的切除宽度占比。

        Returns
        -------
        (ratio, is_blank, bbox)
            ratio    : 切除比例 [0, 1]，None 表示无法计算
            is_blank : True = 图像全空白
            bbox     : (x, y, w, h) 或 None
        """
        try:
            bg = self._border_floodfill(img_bgr)
            fg = cv2.bitwise_not(bg)
            fg = self._clean_mask(fg)

            fg_pixels = int(np.count_nonzero(fg))
            total_pixels = fg.shape[0] * fg.shape[1]

            if fg_pixels < total_pixels * self._blank_fg_ratio:
                return None, True, None

            bbox = self._tight_bbox(fg)
            if bbox is None:
                return None, True, None

            x, _, w, _ = bbox
            orig_w = img_bgr.shape[1]
            cut = x if side == "left" else orig_w - (x + w)
            return cut / orig_w, False, bbox

        except Exception as e:
            error("[布幅检测] 图像处理异常: {}".format(e))
            return None, False, None

    def _border_floodfill(self, img_bgr: np.ndarray
                          ) -> np.ndarray:
        """边界泛洪提取背景"""
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        H, W = gray.shape[:2]
        mask = np.zeros((H + 2, W + 2), np.uint8)
        flags = 4 | cv2.FLOODFILL_MASK_ONLY | (1 << 8)
        # 中值滤波来进行区分布匹和空白
        blur = cv2.medianBlur(gray, 3)
        tol = self._seed_tol

        border_pts = (
            [(0, x) for x in range(W)]
            + [(H - 1, x) for x in range(W)]
            + [(y, 0) for y in range(H)]
            + [(y, W - 1) for y in range(H)])

        for (y, x) in border_pts[::5]:
            cv2.floodFill(blur, mask, (x, y), 0,
                          (tol,) * 3, (tol,) * 3, flags)

        return (mask[1:H + 1, 1:W + 1] > 0).astype(
            np.uint8) * 255

    def _clean_mask(self, mask: np.ndarray) -> np.ndarray:
        """移除小面积噪点连通域"""
        n, lab, stats, _ = cv2.connectedComponentsWithStats(
            (mask > 0).astype(np.uint8))
        out = np.zeros_like(mask)
        for i in range(1, n):
            if stats[i, cv2.CC_STAT_AREA] >= self._min_area:
                out[lab == i] = 255
        return out

    @staticmethod
    def _tight_bbox(mask: np.ndarray
                    ) -> Optional[Tuple[int, int, int, int]]:
        """前景最小外接矩形 (x, y, w, h)"""
        ys, xs = np.where(mask > 0)
        if xs.size == 0:
            return None
        return (int(xs.min()), int(ys.min()),
                int(xs.max() - xs.min() + 1),
                int(ys.max() - ys.min() + 1))

    # ================================================================
    #  布幅计算（动态使用活跃相机参数）
    # ================================================================
    def _calculate_width(self) -> Optional[float]:
        """计算布幅并更新切除比例"""
        # 即使窗口未满，也更新现有数据的平均值
        if self._left_ratios:
            self._avg_left_ratio = sum(self._left_ratios) / len(self._left_ratios)
        if self._right_ratios:
            self._avg_right_ratio = sum(self._right_ratios) / len(self._right_ratios)

        # 布幅计算需要两侧都有数据
        if not self._left_ratios or not self._right_ratios:
            return None

        avg_left = self._avg_left_ratio
        avg_right = self._avg_right_ratio

        le = self._cam_edges[self._active_cam["left"]]
        re = self._cam_edges[self._active_cam["right"]]

        full = re["right"] - le["left"]
        left_cut = (le["right"] - le["left"]) * avg_left
        right_cut = (re["right"] - re["left"]) * avg_right

        return full - left_cut - right_cut

    # ================================================================
    #  报告 & 对外查询
    # ================================================================
    def _print_report(self):
        """打印布幅统计报告"""
        avg_l = self._avg_left_ratio
        avg_r = self._avg_right_ratio
        fw = self._last_fabric_width
        if fw is None:
            fb = self._fallback_width_cm()
            fw_txt = ("无实测值 → 兜底 {:.2f} cm ({:.0f} mm)".format(fb, fb * 10)
                      if fb is not None else "无（未算出，且兜底已关闭）")
        else:
            fw_txt = "{:.2f} cm".format(fw)
        l_cam = self._active_cam["left"]
        r_cam = self._active_cam["right"]

        # 标注是否使用备用
        l_tag = " [备用]" if l_cam != self._primary_cam.get("left") else ""
        r_tag = " [备用]" if r_cam != self._primary_cam.get("right") else ""

        info("\n" + "=" * 58
             + "\n[布幅检测] 第 {} 帧统计".format(
                 self._total_frames)
             + "\n  布幅: {}".format(fw_txt)
             + "\n  活跃相机  左={}{} 右={}{}".format(
                 l_cam, l_tag, r_cam, r_tag)
             + "\n  左侧({}) 切除比 {:.2%}  "
               "样本={}".format(l_cam, avg_l,
                                len(self._left_ratios))
             + "\n  右侧({}) 切除比 {:.2%}  "
               "样本={}".format(r_cam, avg_r,
                                len(self._right_ratios))
             + "\n  累计 left={} right={} total={}".format(
                 self._left_count, self._right_count,
                 self._total_frames)
             + "\n" + "=" * 58)

    def get_stats(self) -> dict:
        """获取统计信息（线程安全）"""
        if not self._enabled:
            return {"enabled": False}
        with self._lock:
            is_fb = (self._last_fabric_width is None and self._fb_enabled)
            return {
                "enabled": True,
                "total_frames": self._total_frames,
                "left_count": self._left_count,
                "right_count": self._right_count,
                # last_fabric_width 保持**原始测量值**：没算出来就是 None。
                # 兜底值单独放 fabric_width_effective，不要把合成值混进测量字段。
                "last_fabric_width": self._last_fabric_width,
                "fabric_width_effective": (
                    self._fallback_width_cm()
                    if self._last_fabric_width is None
                    else self._last_fabric_width),
                "fabric_width_is_fallback": is_fb,
                "fallback_width_range_mm": (
                    [self._fb_min_mm, self._fb_max_mm]
                    if self._fb_enabled else None),
                "last_left_ratio": self._last_left_ratio,
                "last_right_ratio": self._last_right_ratio,
                "left_window_size": len(self._left_ratios),
                "right_window_size": len(self._right_ratios),
                "active_left_camera": self._active_cam.get("left"),
                "active_right_camera": self._active_cam.get("right"),
                "left_using_fallback":
                    self._active_cam["left"] != self._primary_cam.get("left"),
                "right_using_fallback":
                    self._active_cam["right"] != self._primary_cam.get("right"),
                "avg_left_ratio": self._avg_left_ratio,
                "avg_right_ratio": self._avg_right_ratio,
            }

    def _fallback_width_cm(self) -> Optional[float]:
        """从未成功算出布幅时，返回一个在配置范围内波动的兜底值（cm）。

        一个刷新周期内保持同一个值：Modbus 上报和定时上报跑在不同线程、
        不同时刻，每次读都随机会让同一批数据的两个上报口径对不上。
        **锁内调用。**
        """
        if not self._fb_enabled:
            return None
        now = time.monotonic()
        if (self._fb_value_cm is None
                or now - self._fb_generated_at >= self._fb_refresh_sec):
            mm = random.uniform(self._fb_min_mm, self._fb_max_mm)
            self._fb_value_cm = mm / 10.0      # mm → cm
            self._fb_generated_at = now
            if not self._fb_warned:
                self._fb_warned = True
                warning("[布幅检测] 尚未成功算出布幅，改用兜底值 "
                        "{}~{}mm 波动上报（每 {}s 重新取一次）。"
                        "这是**合成值不是测量值**，"
                        "请检查主/备相机是否都能看到布边。".format(
                            self._fb_min_mm, self._fb_max_mm,
                            self._fb_refresh_sec))
        return self._fb_value_cm

    def get_fabric_width(self) -> Optional[float]:
        """获取最新布幅值（cm，线程安全）。

        **从未成功算出过**布幅时返回兜底值（见 _fallback_width_cm），
        避免上游收到 width=0。一旦算出过真值，之后即使帧全空白也继续沿用
        上次真值，不会再退回兜底。

        是否兜底由 is_fallback_width() / get_stats() 的
        ``fabric_width_is_fallback`` 判断。
        """
        if not self._enabled:
            return None
        with self._lock:
            if self._last_fabric_width is not None:
                return self._last_fabric_width
            return self._fallback_width_cm()

    def is_fallback_width(self) -> bool:
        """当前 get_fabric_width() 返回的是兜底值还是真实测量值"""
        if not self._enabled:
            return False
        with self._lock:
            return self._last_fabric_width is None and self._fb_enabled

    def get_active_cameras(self) -> Dict[str, str]:
        """获取当前活跃相机 {"left": "camera8", "right": "camera1"}"""
        if not self._enabled:
            return {}
        with self._lock:
            return dict(self._active_cam)

    # ════════════════════════════════════════════
    #  获取缓存的布面 bbox（推理线程调用）
    # ════════════════════════════════════════════
    def get_last_fabric_bbox(
            self, camera_name: str
    ) -> Optional[Tuple[int, int, int, int]]:
        """
        获取指定相机最近一次 process_frame 缓存的布面 bbox

        - 零计算开销，直接取缓存
        - 线程安全
        - 未缓存时返回 None（edge filter 自动跳过）

        Parameters
        ----------
        camera_name : 相机名称

        Returns
        -------
        (x, y, w, h) 或 None
        """
        if not self._enabled:
            return None
        with self._lock:
            return self._last_fabric_bbox.get(camera_name)

    def get_cut_ratio_for_camera(self, camera_name: str):
        """获取指定相机的切除比例和侧边，返回 (ratio, side)"""
        if not self._enabled:
            return 0.0, None
        with self._lock:
            if camera_name == self._active_cam.get("left"):
                return self._avg_left_ratio, "left"
            elif camera_name == self._active_cam.get("right"):
                return self._avg_right_ratio, "right"
            else:
                return 0.0, None