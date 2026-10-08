#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
经纱密度检测器
核心思路：宽度不压缩防混叠，高度取窄条带防倾斜，一阶差分去低频，ACF多倍插值提精度
目标耗时：< 5ms
"""
import time
import numpy as np
import cv2
import logging
from dataclasses import dataclass
import warnings
from AnomalyDetection_Tool.config.settings import REAL_WIDTH_CM, IMAGE_WIDTH_PX
from General_Tool.EnhancedLogger import info

warnings.filterwarnings('ignore')

PIXEL_PER_CM = IMAGE_WIDTH_PX / REAL_WIDTH_CM
PIXEL_TO_MM  = (REAL_WIDTH_CM / IMAGE_WIDTH_PX) * 10

logger = logging.getLogger('WarpDensityDetector')

# ================================================================
#  关键常量：投影目标长度
#  经纱周期最小3px，在缩放后的坐标系中也需要至少3px采样
#  设定投影长度上限，超过则等比缩放频率
# ================================================================
_PROJ_TARGET_LEN = 1024   # 投影向量目标长度，控制FFT规模


# ================================================================
#  工具函数
# ================================================================

def _np_detrend(x: np.ndarray) -> np.ndarray:
    """线性去趋势，闭合解析解"""
    n = len(x)
    mean_t = (n - 1) * 0.5
    mean_x = x.mean()
    # Σt² 的闭合公式
    sum_t2 = n * (n - 1) * (2 * n - 1) / 6.0
    # Σt·x 用 dot 加速
    t = np.arange(n, dtype=x.dtype)
    sum_tx = np.dot(t, x)
    denom = sum_t2 - n * mean_t ** 2
    if abs(denom) < 1e-12:
        return x - mean_x
    slope = (sum_tx - n * mean_t * mean_x) / denom
    intercept = mean_x - slope * mean_t
    return x - (intercept + slope * t)


def _next_power_of_2(n: int) -> int:
    """返回 >= n 的最小2的幂次"""
    return 1 << (n - 1).bit_length()


def _find_peaks_vec(x: np.ndarray,
                    prominence_ratio: float = 0.05,
                    min_distance: int = 3) -> np.ndarray:
    """全向量化峰值检测"""
    if len(x) < 3:
        return np.array([], dtype=int)

    thr = prominence_ratio * x.max()
    # 局部极大值（向量化）
    is_peak = (x[1:-1] > x[:-2]) & (x[1:-1] > x[2:]) & (x[1:-1] >= thr)
    raw = np.where(is_peak)[0] + 1

    if len(raw) == 0 or min_distance <= 1:
        return raw

    # min_distance 抑制（向量化版本）
    # 按幅值降序，标记已占用区间
    order = np.argsort(x[raw])[::-1]
    raw = raw[order]
    kept = np.zeros(len(x), dtype=bool)

    result = []
    for idx in raw:
        # 早退：当前峰值已被更高峰占用，直接跳过
        if kept[idx]:
            continue
        lo = max(0, idx - min_distance)
        hi = min(len(x), idx + min_distance + 1)
        kept[lo:hi] = True
        result.append(idx)

    return np.array(result, dtype=int)


def _build_acf(proj_norm: np.ndarray) -> np.ndarray:
    """FFT 自相关，补零到2的幂次"""
    n = len(proj_norm)
    n_fft = _next_power_of_2(2 * n)
    F = np.fft.rfft(proj_norm, n=n_fft)
    acf = np.fft.irfft(F * np.conj(F), n=n_fft)[:n].real
    acf /= (acf[0] + 1e-12)
    return acf


def _acf_score_at_period(acf: np.ndarray, period: float, n: int) -> float:
    """计算给定周期在 ACF 前3倍处的得分"""
    hw = max(2, int(period * 0.25))
    vals = []
    for k in range(1, 4):
        c = int(round(k * period))
        lo = max(1, c - hw)
        hi = min(n - 1, c + hw)
        if hi > lo and c < n:
            vals.append(acf[lo: hi + 1].max())
    return float(np.mean(vals)) if vals else -1.0


# ================================================================
#  数据类
# ================================================================

@dataclass
class WarpDensityResult:
    density_per_cm: float = 0.0
    density_per_10cm: float = 0.0
    period_px: float = 0.0
    period_mm: float = 0.0
    confidence: float = 0.0
    uniformity: float = 0.0
    pixel_per_cm: float = PIXEL_PER_CM
    cut_ratio: float = 0.0
    cut_side: str = ""
    original_width: int = 0
    cropped_width: int = 0

    def to_dict(self):
        return {
            'density_per_cm': round(self.density_per_cm, 2),
            'density_per_10cm': round(self.density_per_10cm, 1),
            'period_px': round(self.period_px, 2),
            'period_mm': round(self.period_mm, 3),
            'confidence': round(self.confidence, 3),
            'uniformity': round(self.uniformity, 3),
            'cut_ratio': round(self.cut_ratio, 3),
            'cut_side': self.cut_side,
            'original_width': self.original_width,
            'cropped_width': self.cropped_width,
        }


# ================================================================
#  检测器
# ================================================================

class WarpDensityDetector:
    """经纱密度检测器（终极性能与精度版）"""

    PROJ_LEN = _PROJ_TARGET_LEN

    def __init__(self,
                 pixel_per_cm: float = PIXEL_PER_CM,
                 use_clahe: bool = True,
                 roi_ratio: float = 0.8,
                 min_period_px: int = 3,
                 max_period_px: int = 250,
                 # 新增参数
                 auto_correct_low_density: bool = True,
                 low_density_min: float = 50.0,
                 low_density_max: float = 60.0
                 ):

        self.pixel_per_cm = pixel_per_cm
        self.use_clahe = use_clahe
        self.roi_ratio = roi_ratio
        self.min_period_px = min_period_px
        self.max_period_px = max_period_px

        # 低密度修正配置
        self.auto_correct_low_density = auto_correct_low_density
        self.low_density_min = low_density_min
        self.low_density_max = low_density_max

        self._clahe = (
            cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 4))
            if use_clahe else None
        )

        logger.info(
            f"经纱密度检测器初始化 | 标定: {pixel_per_cm:.2f} px/cm | "
            f"周期范围: [{min_period_px}, {max_period_px}] px | "
            # f"低密度修正: {auto_correct_low_density} [{low_density_min}-{low_density_max}]"
        )

    # ----------------------------------------------------------------
    #  主入口
    # ----------------------------------------------------------------
    def detect(self,
               image: np.ndarray,
               cut_ratio: float = 0.0,
               cut_side: str = "none") -> WarpDensityResult:

        result = WarpDensityResult(
            pixel_per_cm=self.pixel_per_cm,
            cut_ratio=cut_ratio,
            cut_side=cut_side,
        )
        t1 = time.perf_counter()

        try:
            h_orig, w_orig = image.shape[:2]
            result.original_width = w_orig

            if cut_ratio > 0 and cut_side in ("left", "right"):
                image = self._crop_image(image, cut_ratio, cut_side)
                logger.info(
                    f"[经纱密度] 切除{cut_side}侧 {cut_ratio:.1%} "
                    f"({w_orig}px → {image.shape[1]}px)"
                )
            result.cropped_width = image.shape[1]

            projection, scale = self._preprocess_to_projection(image)
            freq_scaled, uniformity = self._analyze(projection, scale)

            if freq_scaled <= 0:
                logger.warning("未检测到有效经纱频率")
                return result

            frequency = freq_scaled * scale

            result.period_px = 1.0 / frequency
            result.period_mm = result.period_px * PIXEL_TO_MM
            result.density_per_cm = frequency * self.pixel_per_cm
            result.density_per_10cm = result.density_per_cm * 10
            result.uniformity = uniformity

            # ---- 低密度自动修正 ----
            if (self.auto_correct_low_density and
                    self.low_density_min <= result.density_per_10cm <= self.low_density_max):
                original_density = result.density_per_cm
                original_density_10cm = result.density_per_10cm

                result.density_per_cm *= 2.0
                result.density_per_10cm *= 2.0
                result.period_px /= 2.0
                result.period_mm /= 2.0

                logger.warning(
                    f"[经纱密度修正] {original_density:.2f} → {result.density_per_cm:.2f} 根/cm "
                    f"({original_density_10cm:.1f} → {result.density_per_10cm:.1f} 根/10cm) | "
                    f"检测到密度在 {self.low_density_min}-{self.low_density_max} 根/10cm 范围内，自动×2"
                )
            # -------------------------

            result.confidence = self._compute_confidence(result)

            elapsed = (time.perf_counter() - t1) * 1000
            info(f"经线检测用时: {elapsed:.1f}ms")

        except Exception as e:
            logger.error(f"经纱密度检测失败: {e}", exc_info=True)
            result.confidence = 0.0

        return result

    # ----------------------------------------------------------------
    #  核心：预处理 → 投影
    # ----------------------------------------------------------------

    def _preprocess_to_projection(
            self, image: np.ndarray) -> tuple[np.ndarray, float]:
        """
        宽度不压缩，高度取中间窄条带。
        核心进化：使用列标准差投影(std)替代均值投影(mean)，彻底解决纱线微小倾斜导致的周期涂抹拉长问题。
        """
        h, w = image.shape[:2]

        # 1. 灰度化
        if len(image.shape) == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:
            gray = image

        # 2. 高度取中间窄条带
        strip_h = 64
        y_center = h // 2
        y1 = max(0, y_center - strip_h // 2)
        y2 = min(h, y_center + strip_h // 2)
        roi = gray[y1:y2, :]

        # 3. x方向轻量高斯模糊去噪，提升std投影信噪比 (仅1行高时极快)
        roi = cv2.GaussianBlur(roi, (5, 1), 0)

        # 4. 列标准差投影（核心修改！防倾斜涂抹）
        projection = roi.std(axis=0).astype(np.float64)

        # 5. scale = 1.0，因为宽度未缩放
        col_scale = 1.0

        return projection, col_scale

    # ----------------------------------------------------------------
    #  裁剪
    # ----------------------------------------------------------------

    def _crop_image(self, image: np.ndarray,
                    cut_ratio: float, cut_side: str) -> np.ndarray:
        """视图切片，零拷贝"""
        h, w = image.shape[:2]
        cut_pixels = int(w * cut_ratio)
        if cut_side == "left":
            return image[:, cut_pixels:]
        elif cut_side == "right":
            return image[:, :w - cut_pixels]
        return image

    # ----------------------------------------------------------------
    #  频率分析 + 均匀性（ACF共享）
    # ----------------------------------------------------------------

    def _analyze(self,
                 projection: np.ndarray,
                 scale: float = 1.0) -> tuple[float, float]:
        """
        在投影向量上做分析。
        """
        n = len(projection)
        if n < 16:
            return 0.0, 0.5

        # 去趋势
        proj_dt = _np_detrend(projection)

        # 一阶差分：强力高通滤波，彻底无非线性光照渐变对频谱的干扰
        # 这能确保 FFT 锁定在真实的纱线高频周期，而不被低频“肩膀”拉偏
        # proj_dt = np.diff(proj_dt, append=proj_dt[-1])

        min_period_scaled = max(2.0, self.min_period_px * scale)
        max_period_scaled = min(float(n // 2), self.max_period_px * scale)

        min_freq = 1.0 / max_period_scaled
        max_freq = min(0.5, 1.0 / min_period_scaled)

        if min_freq >= max_freq:
            logger.warning(
                f"[经纱密度] 频率范围无效: min_freq={min_freq:.4f} >= "
                f"max_freq={max_freq:.4f}，scale={scale:.4f}"
            )
            return 0.0, 0.5

        # ---- FFT ----
        proj_win = proj_dt * np.hanning(n)
        n_fft = _next_power_of_2(n * 2)
        fft_vals = np.abs(np.fft.rfft(proj_win, n=n_fft))
        freq_axis = np.fft.rfftfreq(n_fft, d=1.0)

        valid_mask = (freq_axis > min_freq) & (freq_axis <= max_freq)

        if not valid_mask.any():
            logger.warning(
                f"[经纱密度] 有效频率范围内无FFT分量"
            )
            return 0.0, 0.5

        fft_masked = np.where(valid_mask, fft_vals, 0.0)

        # 修复：基于频点索引平滑，避免物理频率衰减导致主峰偏移
        kernel = np.ones(5) / 5.0
        fft_smooth = np.convolve(fft_masked, kernel, mode='same')

        peaks = _find_peaks_vec(fft_smooth,
                                prominence_ratio=0.05,
                                min_distance=3)

        if len(peaks) == 0:
            best_freq = float(freq_axis[fft_smooth.argmax()])
            sorted_freqs = np.array([best_freq])
        else:
            order = np.argsort(fft_smooth[peaks])[::-1]
            sorted_peaks = peaks[order]
            sorted_freqs = freq_axis[sorted_peaks]
            best_freq = float(sorted_freqs[0])

            # 谐波检测
            for cf in sorted_freqs[1:]:
                if cf <= 0:
                    continue
                ratio = best_freq / cf
                ni = round(ratio)
                if ni >= 2 and abs(ratio - ni) < 0.15:
                    best_freq = float(cf)
                    break

        # ---- ACF（只算一次）----
        proj_norm = proj_dt - proj_dt.mean()
        std = proj_norm.std()
        uniformity = 0.5

        if std > 1e-10:
            proj_norm /= std
            acf = _build_acf(proj_norm)

            # 用 ACF 选最佳基频
            if len(sorted_freqs) > 1:
                candidates = list(sorted_freqs[:min(5, len(sorted_freqs))])
                for f in list(candidates):
                    hf = f / 2.0
                    if hf > min_freq:
                        candidates.append(hf)

                best_val = -1.0
                best_freq_acf = best_freq
                for freq in candidates:
                    if freq <= 0:
                        continue
                    s = _acf_score_at_period(acf, 1.0 / freq, n)
                    if s > best_val:
                        best_val = s
                        best_freq_acf = freq
                best_freq = best_freq_acf

            # 均匀性
            if best_freq > 0:
                period = 1.0 / best_freq
                if n >= period * 3:
                    hw = max(3, int(period * 0.25))
                    acf_vals = []
                    acf_weights = []
                    for k in range(1, 6):
                        c = int(round(k * period))
                        lo = max(1, c - hw)
                        hi = min(n - 1, c + hw)
                        if hi > lo and c < n:
                            acf_vals.append(float(acf[lo: hi + 1].max()))
                            acf_weights.append(1.0 / k)
                    if acf_vals:
                        uniformity = float(
                            np.clip(
                                np.average(acf_vals, weights=acf_weights),
                                0.0, 1.0
                            )
                        )

            # ---- 亚像素周期校准（多倍周期联合插值） ----
            if best_freq > 0:
                period_est = 1.0 / best_freq
                refined_periods = []

                # 在 1, 2, 3, 4 倍周期处分别做抛物线插值
                for k in range(1, 5):
                    c = int(round(k * period_est))
                    if 1 < c < n - 1:
                        y0 = acf[c]
                        y1 = acf[c - 1]
                        y2 = acf[c + 1]
                        denom = y1 - 2 * y0 + y2
                        if abs(denom) > 1e-8:
                            delta = 0.5 * (y1 - y2) / denom
                            delta = np.clip(delta, -0.5, 0.5)
                            p_k = c + delta
                            refined_periods.append(p_k / k)

                if refined_periods:
                    # 取多倍周期插值的均值作为最终精确周期
                    period_true = np.mean(refined_periods)
                    best_freq = 1.0 / period_true
            # --------------------------------------------

        return float(best_freq), float(uniformity)

    # ----------------------------------------------------------------
    #  置信度
    # ----------------------------------------------------------------

    def _compute_confidence(self, result: WarpDensityResult) -> float:
        d = result.density_per_10cm
        scores = [
            1.0 if 10 <= d <= 300 else (0.6 if 5 <= d <= 400 else 0.1),
            result.uniformity,
            1.0 if self.min_period_px <= result.period_px <= self.max_period_px else 0.0,
        ]
        return float(np.clip(np.mean(scores), 0.0, 1.0))


# ================================================================
#  测试入口（仅供离线调试，生产链路不会执行到这里）
#
#  用法: python -m AnomalyDetection_Tool.analysis.warp_density_detector <图片目录>
#  目录也可用环境变量 WARP_TEST_DIR 指定；此前这里硬编码了一个 D:\ 开头的
#  本机路径，换台机器就跑不了。
# ================================================================

import os
import sys

if __name__ == "__main__":
    FOLDER_PATH = (sys.argv[1] if len(sys.argv) > 1
                   else os.environ.get("WARP_TEST_DIR", ""))
    if not FOLDER_PATH or not os.path.isdir(FOLDER_PATH):
        print("用法: python -m AnomalyDetection_Tool.analysis."
              "warp_density_detector <图片目录>")
        sys.exit(1)

    VALID_EXT = ('.png', '.jpg', '.jpeg', '.bmp', '.tiff', '.tif')

    # 只实例化一次检测器
    detector = WarpDensityDetector(
        pixel_per_cm=PIXEL_PER_CM,
        use_clahe=True,
        roi_ratio=0.6,
        min_period_px=3,
        max_period_px=250,
        # 新增配置
        auto_correct_low_density=True,  # 启用自动修正
        low_density_min=50.0,  # 修正范围下限
        low_density_max=60.0,  # 修正范围上限
    )

    # 获取所有图片文件
    image_files = [
        f for f in os.listdir(FOLDER_PATH)
        if f.lower().endswith(VALID_EXT)
    ]
    if not image_files:
        print(f"文件夹中未找到图片: {FOLDER_PATH}")
        exit(1)

    for filename in image_files:
        img_path = os.path.join(FOLDER_PATH, filename)

        # ------ 兼容中文路径的读取方式 ------
        try:
            with open(img_path, 'rb') as f:
                data = np.frombuffer(f.read(), dtype=np.uint8)
            image = cv2.imdecode(data, cv2.IMREAD_COLOR)
        except Exception as e:
            print(f"读取文件失败: {img_path}，错误: {e}")
            continue

        if image is None:
            print(f" 图像解码失败，跳过: {img_path}")
            continue

        # 每张图片重新定义测试方案（避免 pop 破坏字典）
        test_cases = [
            dict(cut_ratio=0.0, cut_side="none", label="不裁剪"),
            dict(cut_ratio=0.30, cut_side="right", label="切除右侧 30%"),
            dict(cut_ratio=0.303, cut_side="left", label="切除左侧 30%"),
        ]

        print(f"\n{'=' * 60}")
        print(f"  正在处理: {filename}")
        print(f"{'=' * 60}")

        for case in test_cases:
            label = case.pop("label")
            result = detector.detect(image, **case)
            d = result.to_dict()

            print(f"\n{'─' * 55}")
            print(f"  方案: {label}")
            print(f"{'─' * 55}")
            print(f"  经纱密度          : {d['density_per_cm']:.2f}  根/cm")
            print(f"  经纱密度 (10cm)   : {d['density_per_10cm']:.1f}  根/10cm")
            print(f"  纱线周期          : {d['period_px']:.2f}  px  |  {d['period_mm']:.3f}  mm")
            print(f"  均匀性 (autocorr) : {d['uniformity']:.3f}")
            print(f"  置信度            : {d['confidence']:.3f}")
            print(f"  原始宽度          : {d['original_width']}  px")
            print(f"  裁剪后宽度        : {d['cropped_width']}  px")

    print(f"\n{'=' * 60}\n 所有图片处理完成。")