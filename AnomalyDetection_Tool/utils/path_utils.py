#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
路径相关工具函数

上传相对路径的**唯一实现**在 `to_upload_relative_path()`。

历史背景：此前本模块有两个独立实现 —— `to_upload_relative_path()`（基于
`os.path.relpath`）与 `build_relative_image_path()`（基于 Normal/StopMachine
目录名反查），二者对同一个绝对路径会产出不同结果，导致同一张图在不同上传
链路里的 `imageUrl` 字段不一致。现在 `build_relative_image_path()` 已改为
调用同一套核心逻辑，仅在拿不到 base_path 时才退化到目录名启发式。

后端约定的格式（Windows 反斜杠、以反斜杠开头）：
    \\<launch_tag>\\<camera_name>\\<Normal|StopMachine>\\<filename>
"""

import os
from typing import Optional

# 保存目录中标识图片类别的目录名
_CATEGORY_DIRS = ("Normal", "StopMachine")

# 后端约定的路径分隔符（与运行平台无关，服务端固定按 Windows 风格解析）
UPLOAD_SEP = "\\"


def safe_filename(name: str) -> str:
    """移除文件名中的非法字符"""
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, '_')
    return name.strip()


def _normalize_upload_path(rel: str) -> str:
    """把任意分隔符的相对路径规整为 `\\a\\b\\c` 形式"""
    rel = rel.replace("/", UPLOAD_SEP).replace(os.sep, UPLOAD_SEP)
    rel = rel.strip(UPLOAD_SEP)
    return UPLOAD_SEP + rel if rel else ""


def _split_tokens(path: str) -> list:
    """把任意分隔符的路径切成组件列表"""
    unified = path.replace("\\", "/").replace(os.sep, "/")
    return [t for t in unified.split("/") if t not in ("", ".")]


def _strip_base(full_tokens: list, base_tokens: list) -> Optional[list]:
    """
    若 full 位于 base 之下，返回去掉 base 前缀后的组件列表，否则返回 None。

    不使用 os.path.relpath：它的行为依赖运行平台，在 Linux 上解析
    "D:\\a\\b" 这类 Windows 路径会把整串当成一个文件名，导致同一份代码
    在开发机（Linux/CI）与产线机（Windows）上产出不同的 imageUrl。
    这里改为纯字符串比较，且大小写不敏感（贴合 Windows 文件系统语义）。
    """
    if len(base_tokens) > len(full_tokens):
        return None
    for a, b in zip(full_tokens, base_tokens):
        if a.lower() != b.lower():
            return None
    return full_tokens[len(base_tokens):]


def _fallback_by_category_dir(full_path: str) -> str:
    """
    兜底策略：从右往左找 Normal / StopMachine 目录名，
    取其前两级（launch_tag、camera_name）拼出相对路径。
    仅在无法通过 base_path 计算相对路径时使用。
    """
    parts = _split_tokens(full_path)

    for i in range(len(parts) - 1, -1, -1):
        if parts[i] in _CATEGORY_DIRS and i + 1 < len(parts):
            start = max(0, i - 2)
            return _normalize_upload_path(UPLOAD_SEP.join(parts[start:i + 2]))

    return _normalize_upload_path(os.path.basename(full_path.replace("\\", "/")))


def to_upload_relative_path(full_path: str,
                            base_path: Optional[str] = None,
                            launch_tag: str = "") -> str:
    """
    将绝对保存路径转换为上传用相对路径（**全系统唯一实现**）。

    例如:
      full_path = D:\\results\\20260330143025\\camera1\\Normal\\a.png
      base_path = D:\\results
      -> \\20260330143025\\camera1\\Normal\\a.png

    Args:
        full_path:  图片的完整保存路径
        base_path:  保存根目录；为空或不匹配时退化到目录名启发式
        launch_tag: 本次批次标签，用于校验/补齐前缀

    Returns:
        形如 `\\20260330143025\\camera1\\Normal\\a.png` 的字符串；
        入参为空时返回空串。
    """
    if not full_path:
        return ""

    if base_path:
        rel_tokens = _strip_base(_split_tokens(full_path),
                                 _split_tokens(base_path))
        if rel_tokens:
            return _normalize_upload_path(UPLOAD_SEP.join(rel_tokens))

    return _fallback_by_category_dir(full_path)


def to_upload_image_url(full_path: str,
                        base_path: Optional[str],
                        launch_tag: str = "") -> str:
    """
    生成最终上传用的 imageUrl：相对路径 + 保证带上 launch_tag 前缀。

    此前这段"补前缀"逻辑在 detection_manager 和 warp_weft_stop_handler
    里各抄了一份，现收敛到这里。
    """
    rel = to_upload_relative_path(full_path, base_path, launch_tag)
    if not rel or not launch_tag:
        return rel

    prefix = UPLOAD_SEP + launch_tag + UPLOAD_SEP
    if not rel.startswith(prefix):
        rel = UPLOAD_SEP + launch_tag + rel
    return rel


def build_relative_image_path(image_path: str,
                              base_path: Optional[str] = None) -> str:
    """
    兼容旧调用点的别名。

    未显式给出 base_path 时，使用配置中的检测结果根目录，
    从而与 `to_upload_relative_path()` 产出完全一致的结果。
    """
    if not image_path:
        return ""

    if base_path is None:
        try:
            from AnomalyDetection_Tool.config.settings import (
                DETECTION_BASE_PATH,
            )
            base_path = DETECTION_BASE_PATH
        except Exception:
            base_path = None

    return to_upload_relative_path(image_path, base_path)
