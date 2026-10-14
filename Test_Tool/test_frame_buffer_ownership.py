#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""帧缓冲区所有权的回归测试。

背景：现场出现过 0xC0000005 访问违例。排查时怀疑"送进下游的图是 SDK
缓冲区的视图、回调返回后被复用" —— 查下来**这个怀疑是错的**，
但顺带发现 ``CameraOperation._to_bgr`` 的注释在撒谎：它写着
"独立副本"、"copy：既脱离 SDK 缓冲区，也让下游可写"，而实际用的
``np.ascontiguousarray`` 对连续输入根本不拷贝。

这组测试把**真实行为**钉住，免得后人再照着那段假注释做判断。
"""

import ctypes
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ======================================================================
# §1  np.ascontiguousarray 对连续输入不拷贝
# ======================================================================
class TestAsContiguousArrayIsNotACopy:

    def _contiguous_readonly(self):
        """模拟 gxipy：string_at 出 bytes，frombuffer 出视图。"""
        buf = bytes(b"\x07" * (64 * 48 * 3))
        return np.frombuffer(buf, dtype=np.uint8).reshape(48, 64, 3)

    def test_input_is_already_contiguous(self):
        arr = self._contiguous_readonly()
        assert arr.flags.c_contiguous is True

    def test_returns_the_same_object(self):
        """核心事实：同一个对象、同一块内存，没有拷贝。"""
        arr = self._contiguous_readonly()
        out = np.ascontiguousarray(arr)
        assert out is arr
        assert np.shares_memory(out, arr)

    def test_result_is_read_only(self):
        """所以"让下游可写"是假的。"""
        out = np.ascontiguousarray(self._contiguous_readonly())
        assert out.flags.writeable is False
        with pytest.raises((ValueError, TypeError)):
            out[0, 0, 0] = 1

    def test_mutating_the_source_is_visible(self):
        """不拷贝的直接证据：源头变了，"副本"跟着变。"""
        buf = bytearray(b"\x07" * (64 * 48 * 3))
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(48, 64, 3)
        out = np.ascontiguousarray(arr)
        buf[0] = 0xFF
        assert out[0, 0, 0] == 0xFF

    def test_it_does_copy_when_input_is_non_contiguous(self):
        """只有非连续输入才真拷贝 —— 所以这个 API 的名字很容易骗人。"""
        arr = self._contiguous_readonly()[:, ::2, :]
        assert arr.flags.c_contiguous is False
        out = np.ascontiguousarray(arr)
        assert not np.shares_memory(out, arr)


# ======================================================================
# §2  gxipy 的内存到底归谁
# ======================================================================
class TestGxipyBufferOwnership:
    """证明"SDK 环形缓冲复用会打穿下游"这个怀疑不成立。

    照 lib/gxipy/ImageProc.py:28 / :172 的写法：
        if image_buf is not None:
            self.__image_array = string_at(image_buf, image_size)
    ``string_at`` 返回 Python bytes —— 已经是一次拷贝，内存归 Python。
    """

    def test_string_at_produces_python_owned_bytes(self):
        native = (ctypes.c_ubyte * 12)(*range(12))
        buf = ctypes.string_at(ctypes.addressof(native), 12)
        assert isinstance(buf, bytes)

    def test_view_survives_the_native_buffer_being_overwritten(self):
        """原生内存被改写后，frombuffer 出来的数组**不受影响** ——
        因为它持有的是 string_at 拷出来的那份 bytes。
        """
        native = (ctypes.c_ubyte * 12)(*([1] * 12))
        buf = ctypes.string_at(ctypes.addressof(native), 12)
        arr = np.frombuffer(buf, dtype=np.uint8)

        # 模拟 SDK 把这块缓冲拿去做下一帧
        for i in range(12):
            native[i] = 0xEE

        assert list(arr) == [1] * 12, "竟然跟着变了，说明不是拷贝"

    def test_view_keeps_the_bytes_alive(self):
        """持有数组就等于持有底层 bytes，不会被 GC 掉。"""
        native = (ctypes.c_ubyte * 8)(*([5] * 8))
        arr = np.frombuffer(ctypes.string_at(ctypes.addressof(native), 8),
                            dtype=np.uint8)
        del native
        assert list(arr) == [5] * 8


# ======================================================================
# §3  源码里不许再出现那两句假话
# ======================================================================
class TestSourceDoesNotLie:

    @pytest.fixture(scope="class")
    def src(self):
        with open(os.path.join(ROOT, "Camera_Tool", "CameraOperation.py"),
                  encoding="utf-8") as f:
            return f.read()

    def test_no_independent_copy_claim(self, src):
        """"独立副本"是假的（ascontiguousarray 不拷贝），整个文件都不许再写。

        范围放到全文件而不是只查 _to_bgr：这句假话原先在 set_callback 的
        文档字符串里还抄了一遍。
        """
        assert "独立副本" not in src
        assert "脱离 SDK 缓冲区" not in src

    def test_docstring_states_it_is_read_only(self, src):
        """必须写明返回的是只读数组，否则后人会直接在上面画框。"""
        start = src.index("def _to_bgr")
        end = src.index("def _gray_to_array", start)
        body = src[start:end]
        assert "只读" in body
        assert "ascontiguousarray" in body      # 解释清楚为什么不拷贝

    def test_no_independent_copy_claim_anywhere_in_repo(self):
        """全仓扫描。这句假话原先在三处各抄了一遍
        （_to_bgr 文档字符串、Registration_callback、main_yolo 的
        on_camera_frame / image_control），只扫单个文件会漏。
        """
        bad = []
        for root, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs
                       if d not in (".git", "docs", "__pycache__",
                                    ".venv", "Test_Tool")]
            for f in files:
                if not f.endswith(".py"):
                    continue
                fp = os.path.join(root, f)
                try:
                    with open(fp, encoding="utf-8") as fh:
                        if "独立副本" in fh.read():
                            bad.append(os.path.relpath(fp, ROOT))
                except (OSError, UnicodeDecodeError):
                    continue
        assert bad == [], f"这些地方还在声称返回独立副本: {bad}"

    def test_callers_copy_before_drawing(self):
        """现状安全的真正前提：所有原地绘制都先 copy。"""
        checks = [
            ("AnomalyDetection_Tool/output/machine_stop_saver.py",
             "canvas = image.copy()"),
            ("AnomalyDetection_Tool/analysis/weft_skew_analyzer.py",
             "ann  = image.copy()"),
        ]
        for rel, needle in checks:
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                assert needle in f.read(), f"{rel} 里找不到 {needle!r}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
