#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
detection/upload/upload_client.py -- 上传客户端（转发到 comms.http_client）
保留此文件为兼容层，所有实际 HTTP 调用已迁移到 comms/http_client.py
"""

# 从统一 HTTP 模块重新导出（保持现有 import 路径兼容）
from Communication_Tool.http_client import upload_detection_info  # noqa: F401
