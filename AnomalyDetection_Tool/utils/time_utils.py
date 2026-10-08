#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
时间相关工具函数
"""

from datetime import datetime

# 完整时间戳（到微秒，now_str 默认截断到毫秒）
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
# 短时间戳（到秒，不含小数部分）
# 注意：此前该常量的值与 TIMESTAMP_FORMAT 完全相同，
# 调用方以为选了短格式实际却拿到微秒串，属于命名与实现自相矛盾。
TIMESTAMP_FORMAT_SHORT = "%Y-%m-%d %H:%M:%S"


def now_str(fmt: str = TIMESTAMP_FORMAT, trim_us: int = 3) -> str:
    """
    当前时间字符串，默认把微秒截断到毫秒。

    仅当格式串真的包含微秒(%f)时才截断，否则传入 TIMESTAMP_FORMAT_SHORT
    这类不含 %f 的格式会被误砍掉末尾 3 个字符。
    """
    s = datetime.now().strftime(fmt)
    if trim_us > 0 and "%f" in fmt and '.' in s:
        return s[:-trim_us]
    return s


def now_ms_timestamp() -> int:
    """当前毫秒时间戳"""
    return int(datetime.now().timestamp() * 1000)


def parse_timestamp(ts_str: str, fmt: str = TIMESTAMP_FORMAT) -> float:
    """解析时间戳字符串为 epoch 秒"""
    return datetime.strptime(ts_str, fmt).timestamp()


def ts_to_ms_str(ts_str: str) -> str:
    """将 ISO 格式时间字符串转为毫秒时间戳字符串"""
    dt = datetime.fromisoformat(ts_str)
    return str(int(dt.timestamp() * 1000))


def format_numeric_timestamp(capture_timestamp: str) -> str:
    """将采集时间戳格式化为纯数字串"""
    if capture_timestamp:
        return (capture_timestamp
                .replace("-", "").replace(":", "")
                .replace(" ", "").replace(".", ""))
    return datetime.now().strftime("%Y%m%d%H%M%S%f")[:-3]


def generate_timestamp_str() -> str:
    """生成 yyyyMMddHHmmssSSS 格式字符串"""
    now = datetime.now()
    return now.strftime("%Y%m%d%H%M%S") + f"{now.microsecond // 1000:03d}"