#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
文件名: analysis/dinomaly_defect_analyzer.py
模块概述: 基于 .pth 权重 (DinoDefectClassifier) 的缺陷掩膜分析器
支持: CUDA Stream + AMP autocast（避免 Half/Float dtype 冲突）
===============================================================================
"""

import sys
import threading

import cv2
import numpy as np
from typing import Dict, Tuple
from contextlib import contextmanager
from PIL import Image

from General_Tool.EnhancedLogger import info, error
from AnomalyDetection_Tool.config.settings import PIXEL_AREA_TO_MM2

try:
    import torch
    _TORCH_AVAILABLE = True
except ImportError:
    _TORCH_AVAILABLE = False


@contextmanager
def _null_context():
    yield


class DinomalyDefectAnalyzer:
    """
    使用 .pth 权重 (DinoDefectClassifier) 生成缺陷掩膜。

    重要说明（你这次报错的根因）：
    - 模型内部存在 feat.float() 之类显式 dtype 转换
    - 如果你对整个 model 做 model.half()，会造成 Linear 权重 half，但输入被 float() 强制成 float32
    - 于是触发 RuntimeError: mat1 Float and mat2 Half
    解决：模型保持 float32，推理使用 autocast（AMP）而不是 model.half()
    """

    def __init__(
        self,
        checkpoint_path: str,
        device: str = "cuda:0",
        slice_size: int = 640,
        mask_threshold: float = 0.3,
        model_source_dir: str = None,
        use_fp16: bool = False,          #  这里的 fp16 表示 autocast，不是 model.half()
        use_cuda_stream: bool = False,
    ):
        if not _TORCH_AVAILABLE:
            raise RuntimeError("PyTorch 未安装，无法使用 DinomalyDefectAnalyzer")

        # ---- 导入模型 / 变换 ----
        if model_source_dir and model_source_dir not in sys.path:
            sys.path.insert(0, model_source_dir)
            info(f"[Dinomaly] 模型源码目录已加入 sys.path: {model_source_dir}")

        try:
            from models.model import DinoDefectClassifier
            from data.transforms import get_val_transform
        except ImportError as exc:
            raise RuntimeError(
                f"无法导入 models.model / data.transforms，"
                f"请检查 model_source_dir 是否正确: {exc}"
            ) from exc

        self.device = device
        self.slice_size = slice_size
        self.mask_threshold = mask_threshold
        self.use_fp16 = bool(use_fp16)
        self.use_cuda_stream = bool(use_cuda_stream)

        self._infer_lock = threading.Lock()

        self._is_cuda = ("cuda" in str(device)) and torch.cuda.is_available()

        #  独立 CUDA Stream（可选）
        self._cuda_stream = None
        if self._is_cuda and self.use_cuda_stream:
            device_idx = self._parse_device_idx(device)
            self._cuda_stream = torch.cuda.Stream(device=device_idx)

        # ---- 加载 checkpoint ----
        info(f"[Dinomaly] 加载权重: {checkpoint_path}")
        ckpt = torch.load(checkpoint_path, map_location=device)
        mcfg = ckpt["config"]

        self.num_classes = mcfg["num_classes"]
        self.class_names = mcfg["class_names"]
        self.img_size = mcfg["img_size"]

        #  模型保持 float32（不要 half）
        with self._get_stream_context():
            self.model = DinoDefectClassifier(
                num_classes=self.num_classes,
                encoder_name=mcfg["encoder_name"],
                selected_layers=mcfg["selected_layers"],
                decoder_dim=mcfg["decoder_dim"],
                fused_dim=mcfg["fused_dim"],
                decoder_depth=mcfg["decoder_depth"],
                decoder_heads=mcfg["decoder_heads"],
                img_size=self.img_size,
            ).to(device)

            self.model.load_state_dict(ckpt["model_state_dict"])
            self.model.eval()

        if self._cuda_stream is not None:
            self._cuda_stream.synchronize()

        self.transform = get_val_transform(self.img_size)

        feats = []
        if self._is_cuda and self.use_fp16:
            feats.append("AMP-autocast(FP16)")
        if self._cuda_stream is not None:
            feats.append(f"CUDA-Stream#{id(self._cuda_stream) & 0xFFFF:04x}")
        if not feats:
            feats.append("FP32")

        info(f"[Dinomaly] 初始化完成"
             f"\n  类别={self.class_names} img_size={self.img_size}"
             f"\n  device={device} mask_threshold={mask_threshold}"
             f"\n  模式: {', '.join(feats)}")

    @staticmethod
    def _parse_device_idx(device: str) -> int:
        if ":" in device:
            try:
                return int(str(device).split(":")[1])
            except (ValueError, IndexError):
                return 0
        return 0

    def _get_stream_context(self):
        if self._cuda_stream is not None:
            return torch.cuda.stream(self._cuda_stream)
        return _null_context()

    # ================================================================
    # 1) 提取切片
    # ================================================================
    def _extract_slice(self, image: np.ndarray, box) -> Tuple[np.ndarray, int, int]:
        h, w = image.shape[:2]
        ss = self.slice_size

        cx = (int(box.x1) + int(box.x2)) // 2
        cy = (int(box.y1) + int(box.y2)) // 2

        sx1 = cx - ss // 2
        sy1 = cy - ss // 2
        sx2 = sx1 + ss
        sy2 = sy1 + ss

        if sx1 < 0:
            sx1, sx2 = 0, min(ss, w)
        if sy1 < 0:
            sy1, sy2 = 0, min(ss, h)
        if sx2 > w:
            sx1, sx2 = max(0, w - ss), w
        if sy2 > h:
            sy1, sy2 = max(0, h - ss), h

        crop = image[sy1:sy2, sx1:sx2]

        ch, cw = crop.shape[:2]
        if ch < ss or cw < ss:
            ndim = crop.shape[2] if len(crop.shape) == 3 else None
            shape = (ss, ss, ndim) if ndim else (ss, ss)
            padded = np.zeros(shape, dtype=crop.dtype)
            padded[:ch, :cw] = crop
            crop = padded

        return crop, int(sx1), int(sy1)

    # ================================================================
    # 2) 切片推理（AMP autocast）
    # ================================================================
    @torch.no_grad()
    def _infer_slice(self, slice_bgr: np.ndarray) -> Dict:
        slice_h, slice_w = slice_bgr.shape[:2]

        rgb = cv2.cvtColor(slice_bgr, cv2.COLOR_BGR2RGB)
        pil_img = Image.fromarray(rgb)
        tensor = self.transform(pil_img).unsqueeze(0).to(self.device)  #  保持 float32

        #  模型推理（加锁保证线程安全）
        with self._infer_lock:
            with self._get_stream_context():
                if self._is_cuda and self.use_fp16:
                    #  autocast：让合适的算子用 fp16，不改权重 dtype
                    with torch.cuda.amp.autocast(dtype=torch.float16):
                        outputs = self.model(tensor)
                else:
                    outputs = self.model(tensor)

            #  等待当前 stream 完成
            if self._cuda_stream is not None:
                self._cuda_stream.synchronize()

        # ---------- 解析输出 ----------
        anomaly_score = outputs["anomaly_prob"][0, 0].item()

        amap = outputs["anomaly_map"][0].detach().float().cpu().numpy()
        hm, wm = outputs["spatial_shape"]
        amap_2d = amap.reshape(hm, wm)

        lo, hi = amap_2d.min(), amap_2d.max()
        amap_norm = ((amap_2d - lo) / (hi - lo + 1e-8)
                     if hi - lo > 1e-8 else np.zeros_like(amap_2d))
        anomaly_map_full = cv2.resize(
            amap_norm, (slice_w, slice_h), interpolation=cv2.INTER_LINEAR)

        mask_logits = outputs["mask_pred"][0, 0].detach().float().cpu().numpy()
        mask_prob = 1.0 / (1.0 + np.exp(-mask_logits))  # sigmoid
        mask_prob_full = cv2.resize(
            mask_prob, (slice_w, slice_h), interpolation=cv2.INTER_LINEAR)

        combined_prob = 0.5 * anomaly_map_full + 0.5 * mask_prob_full
        combined_binary = (combined_prob > self.mask_threshold).astype(np.uint8) * 255

        return {
            "anomaly_score": anomaly_score,
            "anomaly_map": anomaly_map_full,
            "mask_prob": mask_prob_full,
            "combined_prob": combined_prob,
            "combined_binary": combined_binary,
        }

    # ================================================================
    # 3) 主入口：提取缺陷特征
    # ================================================================
    def extract_defect_features(self, image: np.ndarray, box, cls_id: int) -> Dict:
        empty: Dict = {
            "pixel_area": 0, "heatmap": None, "mask": None,
            "num_regions": 0, "fill_ratio": 0, "anomaly_score": 0,
            "debug_info": {},
        }

        if image is None or image.size == 0:
            return empty

        try:
            slice_img, off_x, off_y = self._extract_slice(image, box)
            if slice_img.size == 0:
                return empty

            pred = self._infer_slice(slice_img)

            sh, sw = slice_img.shape[:2]
            bx1 = max(0, int(box.x1) - off_x)
            by1 = max(0, int(box.y1) - off_y)
            bx2 = min(sw, int(box.x2) - off_x)
            by2 = min(sh, int(box.y2) - off_y)

            if bx2 <= bx1 or by2 <= by1:
                return empty

            box_mask = pred["combined_binary"][by1:by2, bx1:bx2].copy()
            box_heatmap = pred["combined_prob"][by1:by2, bx1:bx2].copy()

            pixel_area = int(np.count_nonzero(box_mask))

            nlbl, _, stats, _ = cv2.connectedComponentsWithStats(
                box_mask, connectivity=8)
            num_regions = max(0, nlbl - 1)

            box_total = (bx2 - bx1) * (by2 - by1)
            defect_ratio = pixel_area / box_total if box_total > 0 else 0
            fill_ratio = 1.0 - defect_ratio

            return {
                "pixel_area": pixel_area,
                "heatmap": box_heatmap,
                "mask": box_mask,
                "num_regions": num_regions,
                "fill_ratio": fill_ratio,
                "anomaly_score": pred["anomaly_score"],
                "debug_info": {
                    "slice_offset": (off_x, off_y),
                    "box_in_slice": (bx1, by1, bx2, by2),
                    "anomaly_score": pred["anomaly_score"],
                    "pixel_area": pixel_area,
                    "num_regions": num_regions,
                    "defect_ratio": defect_ratio,
                    "mask_threshold": self.mask_threshold,
                    "slice_size": self.slice_size,
                },
            }

        except Exception as exc:
            error(f"[Dinomaly] 推理异常: {exc}")
            import traceback
            traceback.print_exc()
            return empty

    # ================================================================
    # 工具
    # ================================================================
    @staticmethod
    def calculate_real_area_mm2(pixel_area: int) -> float:
        return pixel_area * PIXEL_AREA_TO_MM2

    def release(self):
        with self._infer_lock:
            if hasattr(self, "model") and self.model is not None:
                del self.model
                self.model = None
            self._cuda_stream = None
            if _TORCH_AVAILABLE and torch.cuda.is_available():
                torch.cuda.empty_cache()
        info("[Dinomaly] 模型已释放")