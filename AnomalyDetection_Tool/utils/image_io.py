#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图像读写工具（支持中文路径）
"""

from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np


class ImageIO:
    """图像读写工具类"""

    @staticmethod
    def read(path: str) -> Optional[np.ndarray]:
        try:
            img_data = np.fromfile(path, dtype=np.uint8)
            if img_data.size == 0:
                return None
            return cv2.imdecode(img_data, cv2.IMREAD_COLOR)
        except Exception:
            return None

    @staticmethod
    def encode(image: np.ndarray, ext: str = ".jpg",
               quality: int = 95) -> Optional[bytes]:
        """
        编码为字节。与 save() 分离，便于"编码一次、写多个文件"。

        编码是这条链路上最贵的一步：2440x2048 的图 PNG 约 179ms，
        JPEG(q95) 约 16ms，相差一个数量级。
        """
        try:
            params = []
            e = ext.lower()
            if e in (".jpg", ".jpeg"):
                params = [cv2.IMWRITE_JPEG_QUALITY, int(quality)]
            ok, encoded = cv2.imencode(ext, image, params)
            if not ok:
                return None
            return encoded.tobytes()
        except Exception:
            return None

    @staticmethod
    def write_bytes(path: str, data: bytes) -> bool:
        """把已编码的字节写入文件（支持中文路径）"""
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            return True
        except Exception:
            return False

    @staticmethod
    def save(path: str, image: np.ndarray, quality: int = 95) -> bool:
        try:
            p = Path(path)
            ext = p.suffix or ".png"
            data = ImageIO.encode(image, ext, quality)
            if data is None:
                return False
            return ImageIO.write_bytes(path, data)
        except Exception:
            return False

    @staticmethod
    def list_images(
        directory: str,
        extensions: Tuple[str, ...] = (
            ".jpg", ".png", ".bmp", ".jpeg", ".tif", ".tiff"),
    ) -> List[str]:
        try:
            dir_path = Path(directory)
            if not dir_path.exists() or not dir_path.is_dir():
                return []
            files = set()
            for ext in extensions:
                files.update(f.name for f in dir_path.glob(f"*{ext}"))
                files.update(
                    f.name for f in dir_path.glob(f"*{ext.upper()}"))
            return sorted(files)
        except Exception:
            return []

    @staticmethod
    def ensure_bgr(image: np.ndarray) -> np.ndarray:
        """确保图像为 BGR 格式"""
        if len(image.shape) == 2:
            return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        return image