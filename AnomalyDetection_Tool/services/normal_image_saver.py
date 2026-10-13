#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定时保存**正常图片**（本帧未检出任何缺陷）。

需求：前端下发一个「开始时刻 + 持续时长」，在这段时间内定时保存正常图片，
**每台相机最多 10 张**。

设计要点
--------
* **窗口用墙上时钟**（``time.time``）而不是单调时钟。因为开始时刻是前端给的
  绝对时间，必须和系统时间比。这点和 TriggerBurstProbe / RepeatStopTracker
  相反 —— 那两个测的是"间隔"，用单调钟；这里比的是"时刻"，只能用墙上钟。
  代价：现场对时会让窗口整体平移。可接受，因为窗口本来就是人给的。

* **判定在保存之前**（``should_save``）。热路径上每帧都要问一次
  （8 台 × 约 2 帧/秒 = 16 次/秒），所以只做锁 + 字典查，不做任何 I/O。

* **落盘在独立线程**。绝不在检测线程里写磁盘 —— 那会把推理节拍拖垮。

* **每台相机的 10 张是硬上限**，按窗口计。窗口重新开始才清零。
  这个上限存在的理由：一次窗口 8 台 × 10 张 = 80 张，
  2440x2048 的 JPEG 约 1 MB 一张，最多 80 MB，不会把盘写满。
"""

import os
import threading
import time
from datetime import datetime
from typing import Optional

from General_Tool.EnhancedLogger import info, warning


# 每台相机每个窗口最多存多少张（需求写死的上限）
DEFAULT_MAX_PER_CAMERA = 10
# 同一台相机两次保存的最小间隔（秒）
DEFAULT_INTERVAL_SEC = 5.0
DEFAULT_JPEG_QUALITY = 90


class NormalImageSaver:
    """线程安全的"正常图片定时保存"窗口管理器。"""

    def __init__(self,
                 base_path: str,
                 launch_tag: str = "",
                 interval_sec: float = DEFAULT_INTERVAL_SEC,
                 max_per_camera: int = DEFAULT_MAX_PER_CAMERA,
                 jpeg_quality: int = DEFAULT_JPEG_QUALITY,
                 save_subdir: str = "NormalSave",
                 clock=time.time):
        self.base_path = base_path
        self.launch_tag = launch_tag or ""
        self.interval_sec = max(0.1, float(interval_sec))
        self.max_per_camera = max(1, int(max_per_camera))
        self.jpeg_quality = int(jpeg_quality)
        self.save_subdir = save_subdir or "NormalSave"
        self._clock = clock

        self._lock = threading.Lock()
        self._win_start: Optional[float] = None      # epoch 秒
        self._win_end: Optional[float] = None
        self._counts = {}        # camera -> 本窗口已存张数
        self._last_at = {}       # camera -> 上次保存的 epoch 秒
        self._ended_logged = False
        self._stats = {"windows_started": 0, "saved": 0,
                       "skipped_outside_window": 0,
                       "skipped_capped": 0, "skipped_interval": 0,
                       "save_failed": 0}

    # ------------------------------------------------------------------
    #  前端下发的控制接口
    # ------------------------------------------------------------------
    def start_window(self, start=None, duration_sec: float = 0.0) -> dict:
        """开启一个保存窗口。

        :param start: 开始时刻。支持三种写法：
                      * ``None``          -> 立即开始
                      * ``float``/``int`` -> epoch 秒（前端最常用）
                      * ``"HH:MM"`` / ``"HH:MM:SS"`` -> 今天的该时刻；
                        若已过则顺延到明天
        :param duration_sec: 持续时长（秒）。<= 0 视为无效，直接拒绝 ——
                             否则窗口会立刻结束，前端还以为开成功了。
        :return: ``get_status()`` 的结果。
        """
        dur = float(duration_sec or 0)
        if dur <= 0:
            warning(f"[正常图保存] 持续时长 {duration_sec!r} 无效（必须 > 0），"
                    f"未开启窗口")
            return self.get_status()

        now = self._clock()
        start_ts = self._parse_start(start, now)
        if start_ts is None:
            warning(f"[正常图保存] 开始时刻 {start!r} 解析不了，未开启窗口")
            return self.get_status()

        with self._lock:
            self._win_start = start_ts
            self._win_end = start_ts + dur
            self._counts.clear()
            self._last_at.clear()
            self._ended_logged = False
            self._stats["windows_started"] += 1

        when = ("立即" if start_ts <= now + 0.5
                else datetime.fromtimestamp(start_ts).strftime("%H:%M:%S"))
        info(f"[正常图保存] 窗口已开启：{when} 开始，持续 {dur:.0f} 秒"
             f"（到 {datetime.fromtimestamp(self._win_end).strftime('%H:%M:%S')}），"
             f"每台相机最多 {self.max_per_camera} 张，"
             f"间隔 {self.interval_sec:.1f} 秒")
        return self.get_status()

    def stop_window(self) -> dict:
        """提前结束窗口。"""
        with self._lock:
            had = self._win_start is not None
            self._win_start = None
            self._win_end = None
        if had:
            info(f"[正常图保存] 窗口已手动结束，共保存 "
                 f"{self._stats['saved']} 张")
        return self.get_status()

    def get_status(self) -> dict:
        now = self._clock()
        with self._lock:
            active = self._is_active_locked(now)
            return {
                "enabled": self._win_start is not None,
                "active": active,
                "start": self._win_start,
                "end": self._win_end,
                "remaining_sec": (max(0.0, self._win_end - now)
                                  if self._win_end else 0.0),
                "interval_sec": self.interval_sec,
                "max_per_camera": self.max_per_camera,
                "per_camera": dict(self._counts),
                "stats": dict(self._stats),
            }

    # ------------------------------------------------------------------
    #  热路径：每帧问一次，必须便宜
    # ------------------------------------------------------------------
    def should_save(self, camera_name: str) -> bool:
        """本帧该不该存。**不做任何 I/O。**"""
        if not camera_name:
            return False
        now = self._clock()
        with self._lock:
            if not self._is_active_locked(now):
                self._stats["skipped_outside_window"] += 1
                self._maybe_log_end_locked(now)
                return False
            if self._counts.get(camera_name, 0) >= self.max_per_camera:
                self._stats["skipped_capped"] += 1
                return False
            last = self._last_at.get(camera_name)
            if last is not None and (now - last) < self.interval_sec:
                self._stats["skipped_interval"] += 1
                return False
            # 先占位再落盘：否则同一台相机的两帧可能在落盘完成前都通过判定，
            # 突破 10 张上限。
            self._counts[camera_name] = self._counts.get(camera_name, 0) + 1
            self._last_at[camera_name] = now
            idx = self._counts[camera_name]
        self._pending_idx = idx        # 仅供 save() 命名，非并发安全依赖
        return True

    def save(self, camera_name: str, image, now: Optional[float] = None) -> str:
        """在独立线程里落盘。返回预期路径（失败时为空串）。

        注意：必须在 ``should_save()`` 返回 True 之后调用 —— 序号取自它。
        """
        if image is None:
            return ""
        now = now if now is not None else self._clock()
        with self._lock:
            idx = self._counts.get(camera_name, 0)
        try:
            d = os.path.join(self.base_path, self.launch_tag,
                             camera_name, self.save_subdir)
            os.makedirs(d, exist_ok=True)
            ts = datetime.fromtimestamp(now).strftime("%Y%m%d_%H%M%S_%f")[:-3]
            path = os.path.join(d, f"{ts}_{camera_name}_{idx:02d}.jpg")
        except Exception as e:
            with self._lock:
                self._stats["save_failed"] += 1
            warning(f"[正常图保存] {camera_name} 建目录/命名失败: {e}")
            return ""

        threading.Thread(target=self._write, args=(path, image, camera_name),
                         daemon=True).start()
        return path

    # ------------------------------------------------------------------
    def _write(self, path: str, image, camera_name: str) -> None:
        try:
            import cv2
            ok = cv2.imwrite(path, image,
                             [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])
            if not ok:
                raise RuntimeError("cv2.imwrite 返回 False")
            with self._lock:
                self._stats["saved"] += 1
        except Exception as e:
            with self._lock:
                self._stats["save_failed"] += 1
                # 落盘失败要把占位退回去，否则这台相机会白白少一张配额
                if self._counts.get(camera_name, 0) > 0:
                    self._counts[camera_name] -= 1
            warning(f"[正常图保存] {camera_name} 写盘失败 {path}: {e}")

    # ------------------------------------------------------------------
    def _is_active_locked(self, now: float) -> bool:
        return (self._win_start is not None and self._win_end is not None
                and self._win_start <= now < self._win_end)

    def _maybe_log_end_locked(self, now: float) -> None:
        """窗口自然到期时打一条汇总，只打一次。"""
        if (self._win_end is not None and now >= self._win_end
                and not self._ended_logged):
            self._ended_logged = True
            info(f"[正常图保存] 窗口已结束，共保存 {self._stats['saved']} 张："
                 f"{dict(self._counts)}")

    @staticmethod
    def _parse_start(start, now: float) -> Optional[float]:
        if start is None:
            return now
        if isinstance(start, (int, float)):
            return float(start)
        s = str(start).strip()
        if not s:
            return now
        # 纯数字字符串当 epoch
        try:
            return float(s)
        except ValueError:
            pass
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                t = datetime.strptime(s, fmt)
            except ValueError:
                continue
            base = datetime.fromtimestamp(now).replace(
                hour=t.hour, minute=t.minute, second=t.second, microsecond=0)
            ts = base.timestamp()
            # 已经过了就顺延到明天
            return ts if ts >= now else ts + 86400.0
        return None


# ======================================================================
#  模块级单例
# ======================================================================
_saver: Optional[NormalImageSaver] = None
_saver_lock = threading.Lock()


def init_normal_saver(base_path: str, launch_tag: str = "",
                      interval_sec: float = DEFAULT_INTERVAL_SEC,
                      max_per_camera: int = DEFAULT_MAX_PER_CAMERA,
                      jpeg_quality: int = DEFAULT_JPEG_QUALITY,
                      save_subdir: str = "NormalSave") -> NormalImageSaver:
    global _saver
    with _saver_lock:
        _saver = NormalImageSaver(
            base_path=base_path, launch_tag=launch_tag,
            interval_sec=interval_sec, max_per_camera=max_per_camera,
            jpeg_quality=jpeg_quality, save_subdir=save_subdir)
    info(f"[正常图保存] 已就绪：每台最多 {max_per_camera} 张，"
         f"间隔 {interval_sec:.1f} 秒，目录 <save_path>/<launch_tag>/"
         f"<相机>/{save_subdir}/。等待前端下发窗口。")
    return _saver


def get_normal_saver() -> Optional[NormalImageSaver]:
    return _saver
