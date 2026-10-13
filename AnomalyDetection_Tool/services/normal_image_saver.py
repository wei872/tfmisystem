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

* **存 PNG（无损）**。体积代价要心里有数 —— 实测 2440x2048 的真实纹理图：
      PNG c1  13.02 MB / 488 ms      JPEG q90  2.96 MB / 32 ms
  一次窗口 8 台 × 10 张 = 80 张，PNG 约 **1.0 GB**，JPEG 只要 0.23 GB。
  所以 10 张这个上限不是随手定的，是拿磁盘换无损：80 张封顶约 1 GB，
  一天开几十次窗口就是几十 GB，**现场要有人清盘**。
  编码 488 ms 走独立线程，不占检测节拍；但 8 台同时开存时是 8 个线程
  各占 0.5 s CPU，比 JPEG 重 15 倍。
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
# PNG 压缩级别 0-9。实测压缩级别对布面这种高频纹理几乎没用：
#   c1 = 13.02 MB / 488 ms，c9 = 12.99 MB / 509 ms —— 多花 20 ms 只省 0.03 MB。
# 所以默认给 1（最快的那一档里体积已经到底）。
# 磁盘 I/O 是瓶颈时可降到 0：143 ms / 14.32 MB，快 3.4 倍，每张只大 1.3 MB。
DEFAULT_PNG_COMPRESSION = 1


class NormalImageSaver:
    """线程安全的"正常图片定时保存"窗口管理器。"""

    def __init__(self,
                 base_path: str,
                 launch_tag: str = "",
                 interval_sec: float = DEFAULT_INTERVAL_SEC,
                 max_per_camera: int = DEFAULT_MAX_PER_CAMERA,
                 png_compression: int = DEFAULT_PNG_COMPRESSION,
                 save_subdir: str = "NormalSave",
                 clock=time.time):
        self.base_path = base_path
        self.launch_tag = launch_tag or ""
        self.interval_sec = max(0.1, float(interval_sec))
        self.max_per_camera = max(1, int(max_per_camera))
        # 0-9 夹住：越界值 cv2 会当成别的压缩策略，行为不可预期
        self.png_compression = min(9, max(0, int(png_compression)))
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
            path = os.path.join(d, f"{ts}_{camera_name}_{idx:02d}.png")
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
            # 先写 .tmp 再原子改名。**不要**直接写目标名：
            # cv2.imwrite 一创建文件，os.path.exists() 就为真了，但字节还没
            # 写完 —— 现场有人边存边翻文件夹就会看到半个 PNG。
            # os.replace 在同一文件系统上是原子的，读者要么看不到、要么看到完整的。
            # 临时名必须以 .png 结尾 —— cv2.imwrite 是**靠扩展名选编码器**的，
            # 叫 foo.png.tmp 它会认不出格式直接返回 False。
            tmp = path + ".part.png"
            ok = cv2.imwrite(tmp, image,
                             [int(cv2.IMWRITE_PNG_COMPRESSION),
                              self.png_compression])
            if not ok:
                raise RuntimeError("cv2.imwrite 返回 False")
            os.replace(tmp, path)
            with self._lock:
                self._stats["saved"] += 1
        except Exception as e:
            try:
                if os.path.exists(path + ".part.png"):
                    os.remove(path + ".part.png")   # 别把半成品留在盘上
            except OSError:
                pass
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
                      png_compression: int = DEFAULT_PNG_COMPRESSION,
                      save_subdir: str = "NormalSave") -> NormalImageSaver:
    global _saver
    with _saver_lock:
        _saver = NormalImageSaver(
            base_path=base_path, launch_tag=launch_tag,
            interval_sec=interval_sec, max_per_camera=max_per_camera,
            png_compression=png_compression, save_subdir=save_subdir)
    info(f"[正常图保存] 已就绪：每台最多 {max_per_camera} 张，"
         f"间隔 {interval_sec:.1f} 秒，目录 <save_path>/<launch_tag>/"
         f"<相机>/{save_subdir}/。等待前端下发窗口。")
    return _saver


def get_normal_saver() -> Optional[NormalImageSaver]:
    return _saver
