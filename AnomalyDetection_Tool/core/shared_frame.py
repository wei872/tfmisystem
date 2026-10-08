# -*- coding: utf-8 -*-
"""
shared_frame.py

零拷贝共享帧：引用计数管理，所有分析任务共享同一份图像数据。
线程安全，引用归零时自动释放 numpy 数组引用。
"""

import threading
import numpy as np
from typing import Optional

class SharedFrame:
    __slots__ = ("_image", "_ref_count", "_lock", "_owner_released")

    def __init__(self, image: np.ndarray):
        self._image: Optional[np.ndarray] = image
        self._ref_count: int = 1
        self._lock = threading.Lock()
        self._owner_released: bool = False

    @property
    def image(self) -> Optional[np.ndarray]:
        return self._image

    def acquire(self) -> "SharedFrame":
        with self._lock:
            if self._image is None:
                raise RuntimeError("SharedFrame 已释放，不可再 acquire")
            self._ref_count += 1
        return self

    def release(self) -> None:
        with self._lock:
            self._ref_count -= 1
            if self._ref_count <= 0:
                self._image = None

    def owner_release(self) -> None:
        if not self._owner_released:
            self._owner_released = True
            self.release()

    @property
    def ref_count(self) -> int:
        with self._lock:
            return self._ref_count

    # 【优化】支持 with 语法，确保异常时也能安全释放
    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()

    def __repr__(self) -> str:
        shape = self._image.shape if self._image is not None else None
        return f"SharedFrame(shape={shape}, refs={self.ref_count})"