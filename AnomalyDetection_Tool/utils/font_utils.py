#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中文字体加载与文字尺寸工具
"""

import os
from typing import Optional, Tuple

from PIL import ImageFont
from General_Tool.EnhancedLogger import info, error

FONT_SEARCH_PATHS = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
]


class FontManager:
    """字体管理器（单例缓存）"""

    _instance: Optional['FontManager'] = None
    _font_normal = None
    _font_large = None
    _loaded = False

    FONT_SIZE_NORMAL = 20
    FONT_SIZE_LARGE = 28

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def load(self) -> bool:
        if self._loaded:
            return self._font_normal is not None

        for font_path in FONT_SEARCH_PATHS:
            if os.path.exists(font_path):
                try:
                    self._font_normal = ImageFont.truetype(
                        font_path, self.FONT_SIZE_NORMAL)
                    self._font_large = ImageFont.truetype(
                        font_path, self.FONT_SIZE_LARGE)
                    self._loaded = True
                    info(f"成功加载中文字体: {font_path}")
                    return True
                except Exception:
                    continue

        self._font_normal = ImageFont.load_default()
        self._font_large = ImageFont.load_default()
        self._loaded = True
        error("无法加载中文字体，使用默认字体")
        return False

    @property
    def font(self):
        if not self._loaded:
            self.load()
        return self._font_normal

    @property
    def font_large(self):
        if not self._loaded:
            self.load()
        return self._font_large

    @staticmethod
    def get_text_size(text: str, font) -> Tuple[int, int]:
        """返回 (width, height)"""
        try:
            bbox = font.getbbox(text)
            return bbox[2] - bbox[0], bbox[3] - bbox[1]
        except AttributeError:
            return font.getsize(text)


# 便捷访问
_manager = FontManager()


def load_chinese_font() -> bool:
    return _manager.load()


def get_font():
    return _manager.font


def get_font_large():
    return _manager.font_large


def get_text_size(text: str, font) -> Tuple[int, int]:
    return FontManager.get_text_size(text, font)