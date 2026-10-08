#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
纬线检测器 - GPU加速版
包含断纬线检测 + 断纬强度显示 + 中文路径保存修复
"""

import time
import cv2
import numpy as np
from pathlib import Path
from scipy import signal as scipy_signal
import threading

# cupy 为可选依赖：顶层原先还有一句无保护的 `import cupy as cp`，
# 与下面的 try 重复，且在未装 cupy 的机器上会直接导入失败。
try:
    import cupy as cp
    _CUPY_OK = cp.cuda.is_available()
except ImportError:
    cp = None
    _CUPY_OK = False

try:
    _CV_CUDA = cv2.cuda.getCudaEnabledDeviceCount() > 0
except Exception:
    _CV_CUDA = False

print(f"[GPU] CuPy={'是' if _CUPY_OK else '否'}  "
      f"OpenCV-CUDA={'是' if _CV_CUDA else '否'}")

_thread_local = threading.local()


def _get_cuda_stream():
    """每个线程持有独立的 CUDA Stream，避免上下文冲突"""
    if not hasattr(_thread_local, 'stream'):
        _thread_local.stream = cp.cuda.Stream(non_blocking=True)
    return _thread_local.stream


class WeftDetector:
    """
    纬线检测器主类

    直线方程约定（统一使用 x=0 截距）
    ------------------------------------
    y = k * x + b
    b = y|_{x=0}（左端点 y 坐标）
    y_center = k * (W/2) + b（图像中心列处的 y 坐标）
    """

    def __init__(self,
                 output_dir="output",
                 save_annotated=True,
                 target_size=400,
                 angle_filter_thresh=1.0,
                 edge_margin_ratio=0.02,
                 max_spacing_threshold=260,
                 leak_detect_ratio=0.6,
                 edge_leak_threshold=1.8,
                 # 断纬检测参数
                 broken_drop_ratio=0.28,
                 broken_min_length_ratio=0.15,
                 use_gpu=True):
        self.output_dir  = Path(output_dir)
        self.save_annotated  = save_annotated
        self.target_size  = target_size
        self.angle_filter_thresh  = angle_filter_thresh
        self.edge_margin_ratio  = edge_margin_ratio
        self.max_spacing_threshold = max_spacing_threshold
        self.leak_detect_ratio  = leak_detect_ratio
        self.edge_leak_threshold  = edge_leak_threshold

        # 断纬检测参数
        self.broken_drop_ratio = broken_drop_ratio
        self.broken_min_length_ratio = broken_min_length_ratio

        self.use_cupy   = use_gpu and _CUPY_OK
        self.use_cv_gpu = use_gpu and _CV_CUDA

        if self.use_cv_gpu:
            self._gpu_sobel = cv2.cuda.createSobelFilter(
                cv2.CV_32F, cv2.CV_32F, 0, 1, ksize=3)
            self._gpu_gauss = cv2.cuda.createGaussianFilter(
                cv2.CV_8UC1, cv2.CV_8UC1, (3, 3), 0.8)

        # 创建输出目录
        if self.save_annotated and not self.output_dir.exists():
            self.output_dir.mkdir(parents=True, exist_ok=True)

        # 缩略图形状缓存
        self._thumb_shape = (0, 0)

        # 预计算粗搜索角度表
        self._coarse_angles = np.arange(-20, 20.1, 1.5, dtype=np.float32)
        self._coarse_tans   = np.tan(np.deg2rad(self._coarse_angles)).astype(np.float32)

        # 断纬检测结果暂存
        self.broken_lines = []

    # ================================================================== #
    #  GPU 工具方法
    # ================================================================== #
    def _to_gray_blur(self, image):
        if self.use_cv_gpu:
            gpu_src  = cv2.cuda_GpuMat()
            gpu_src.upload(image)
            gpu_gray = cv2.cuda.cvtColor(gpu_src, cv2.COLOR_BGR2GRAY)
            gpu_blur = self._gpu_gauss.apply(gpu_gray)
            return gpu_blur.download()
        else:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            return cv2.GaussianBlur(gray, (3, 3), 0.8)

    def _sobel_abs(self, gray_f32):
        if self.use_cv_gpu:
            gpu_src = cv2.cuda_GpuMat()
            gpu_src.upload(gray_f32)
            gpu_sob = self._gpu_sobel.apply(gpu_src)
            gpu_abs = cv2.cuda.abs(gpu_sob)
            return gpu_abs.download()
        else:
            out = cv2.Sobel(gray_f32, cv2.CV_32F, 0, 1, ksize=3)
            np.abs(out, out=out)
            return out

    # ================================================================== #
    #  主流程
    # ================================================================== #
    def detect(self, image, image_name=None):
        t1 = time.time()
        H, W = image.shape[:2]

        # 步骤1：灰度转换 + 高斯模糊去噪
        gray_f = self._to_gray_blur(image)

        # 步骤2：在缩略图上搜索最优角度
        best_angle, row_sig_small, thumb_scale = self._analyze_thumbnail(gray_f)

        # 步骤3：FFT 自相关估计纬线周期
        period_small = self._estimate_period(row_sig_small)

        # 步骤4：在行信号上检测峰值
        peaks_small  = self._detect_peaks(row_sig_small, period_small)

        if len(peaks_small) < 2:
            print("未检测到纬线")
            self.broken_lines = []
            return False, 0.0, 0

        # 步骤5：在原图上计算 Sobel
        gray_f32   = gray_f.astype(np.float32)
        sobel_orig = self._sobel_abs(gray_f32)

        # 步骤6：坐标映射
        lines_raw = self._peaks_to_lines(peaks_small, best_angle, thumb_scale, W)

        # 步骤7：精细拟合
        period_orig = period_small / thumb_scale
        band_half   = max(4, int(period_orig * 0.30))
        refined     = self._refine_all(sobel_orig, lines_raw, band_half, n_samples=40)

        # 步骤8：后处理
        weft_lines = self._post_process(refined, period_orig, W, H)
        if len(weft_lines) < 2:
            print("有效纬线不足")
            self.broken_lines = []
            return False, 0.0, 0

        # 步骤8.5：断纬线检测（包含强度计算）
        self.broken_lines = self._detect_broken_lines(
            weft_lines, sobel_orig, period_orig, W, H
        )
        # 将断裂信息附加到对应纬线字典中
        broken_set = {id(bl['line']): bl for bl in self.broken_lines}
        for line in weft_lines:
            info = broken_set.get(id(line))
            line['is_broken'] = info is not None
            if info:
                line['break_x'] = info['break_x']
                line['broken_strength'] = info['strength']
            else:
                line['broken_strength'] = 0.0

        # 步骤9：边缘漏检补线
        weft_lines, edge_suppl_list = self._check_edge_leak(
            weft_lines, W, H, period_orig
        )

        # 步骤10：计算间距
        spacings, sides = self._calc_spacings(weft_lines, W, H)
        max_idx  = int(np.argmax(spacings))
        max_sp   = spacings[max_idx]
        max_side = sides[max_idx]

        # 步骤11：漏检/异常判断
        suppl_info  = None
        anomaly_info = None
        if max_sp > self.max_spacing_threshold:
            weft_lines, spacings, sides, suppl_info, anomaly_info = \
                self._check_leak(weft_lines, spacings, sides, max_idx, max_side,
                                 sobel_orig, W, H)
            if len(spacings) > 0:
                max_idx  = int(np.argmax(spacings))
                max_sp   = spacings[max_idx]
                max_side = sides[max_idx]

        # 步骤12：绘制标注图并保存
        if self.save_annotated and image_name:
            ann  = image.copy()
            self._draw(ann, weft_lines, best_angle, max_idx, max_side,
                       suppl_info, anomaly_info, edge_suppl_list, period_orig,
                       spacings=spacings, sides=sides)
            stem = Path(image_name).stem
            out  = self.output_dir / f"detected_{stem}.jpg"
            self._imwrite_cn(out, ann)

        # 输出摘要
        print(f"   纬线斜度: {best_angle:.2f}°")
        print(f"   检测纬线数: {len(weft_lines)}")
        print(f"   最大间隔: {int(max_sp)}px")
        # detect() 方法末尾，修改如下

        t2 = time.time() - t1

        has_broken = False
        if self.broken_lines:
            avg_str = np.mean([l['broken_strength'] for l in weft_lines
                               if l.get('is_broken')])
            if avg_str != 0 and avg_str != 1 and avg_str != 0.5:
                print(f" 检测到 {len(self.broken_lines)} 根断纬线, 平均强度: {avg_str:.2f}")
                has_broken = True

        print(f"================={t2:.2f}s==================")

        # ── 始终返回3个值，断纬信息通过 self.broken_lines 属性读取 ──
        return True, float(best_angle), int(max_sp)

    # ================================================================== #
    #  断纬线检测（计算强度）
    # ================================================================== #
    def _detect_broken_lines(self, lines, sobel, period, W, H):
        """
        沿一条纬线逐点采样 Sobel 强度，识别中段断裂，并计算断裂强度。
        :return: list of dict {'line': line_dict, 'break_x': int, 'strength': float}
        """
        broken = []
        if len(lines) == 0:
            return broken

        band_h = max(3, int(period * 0.25))          # 采样窗口半高
        step_x = max(1, W // 60)                     # 采样步长
        xs = np.arange(0, W, step_x, dtype=np.int32)

        for line in lines:
            k = line['k']
            b = line['b']
            ys = np.round(k * xs + b).astype(np.int32)

            valid = (ys - band_h >= 0) & (ys + band_h < H) & (xs >= 0) & (xs < W)
            means = []
            for i in range(len(xs)):
                if not valid[i]:
                    means.append(0.0)
                    continue
                x = xs[i]
                y = ys[i]
                y1 = max(0, y - band_h)
                y2 = min(H, y + band_h + 1)
                roi = sobel[y1:y2, x]
                means.append(float(roi.mean()))

            means = np.array(means)
            if len(means) < 5:
                continue

            # 中值滤波平滑
            means = scipy_signal.medfilt(means, kernel_size=3)
            peak = float(np.max(means))
            if peak < 1e-6:
                continue

            # 下降判定掩码
            drop_mask = means < peak * self.broken_drop_ratio
            min_len = max(3, int(len(means) * self.broken_min_length_ratio))
            break_x = -1
            for i in range(len(means)):
                if i + min_len > len(means):
                    break
                if np.all(drop_mask[i:i + min_len]):
                    idx = i + min_len // 2
                    break_x = xs[min(idx, len(xs) - 1)]
                    break

            if break_x > 0 and break_x < W - 1:
                # 计算断裂强度：右侧平均相对左侧平均的下降比例
                right_mask = xs >= break_x
                left_mean = float(means[~right_mask].mean()) if np.any(~right_mask) else peak
                right_mean = float(means[right_mask].mean())
                strength = 1.0 - (right_mean / (left_mean + 1e-6))
                strength = max(0.0, min(1.0, strength))

                broken.append({
                    'line': line,
                    'break_x': break_x,
                    'side': 'right' if break_x > W // 2 else 'left',
                    'strength': strength,
                    'left_mean': left_mean,
                    'right_mean': right_mean
                })

        return broken

    # ================================================================== #
    #  角度搜索
    # ================================================================== #
    def _analyze_thumbnail(self, gray):
        h, w  = gray.shape
        scale = min(1.0, self.target_size / max(h, w))
        th    = max(1, int(h * scale))
        tw    = max(1, int(w * scale))
        small = cv2.resize(gray, (tw, th), interpolation=cv2.INTER_AREA)

        small_f32 = small.astype(np.float32)
        sobel     = self._sobel_abs(small_f32)

        if self._thumb_shape != (th, tw):
            self._thumb_shape = (th, tw)
            self._col_f  = np.arange(tw, dtype=np.float32)
            self._col_i  = self._col_f.astype(np.int32)
            self._rows_i = np.arange(th, dtype=np.int32)
            self._center = tw / 2.0

        margin = max(1, int(th * 0.02))
        trim_l = margin
        trim_r = th - margin

        best_angle, best_var = self._angle_search_batch(
            sobel, self._coarse_tans, self._coarse_angles,
            th, tw, trim_l, trim_r, -1.0, 0.0)

        fine_angles = np.arange(best_angle - 0.9, best_angle + 0.95, 0.3,
                                dtype=np.float32)
        fine_tans   = np.tan(np.deg2rad(fine_angles)).astype(np.float32)
        best_angle, best_var = self._angle_search_batch(
            sobel, fine_tans, fine_angles,
            th, tw, trim_l, trim_r, best_var, best_angle)

        tan_a   = float(np.tan(np.deg2rad(best_angle)))
        shifts  = np.round((self._col_f - self._center) * tan_a).astype(np.int32)
        r_idx   = np.clip(self._rows_i[:, None] - shifts[None, :], 0, th - 1)
        row_sig = sobel[r_idx, self._col_i].mean(axis=1).astype(np.float64)

        return float(best_angle), row_sig, scale

    def _angle_search_batch(self, sobel, tans, angles,
                             th, tw, trim_l, trim_r,
                             init_var, init_angle):
        if self.use_cupy:
            return self._angle_search_cupy(
                sobel, tans, angles, th, tw, trim_l, trim_r, init_var, init_angle)
        else:
            return self._angle_search_cpu(
                sobel, tans, angles, th, tw, trim_l, trim_r, init_var, init_angle)

    def _angle_search_cupy(self, sobel_cpu, tans, angles,
                           th, tw, trim_l, trim_r,
                           init_var, init_angle):
        stream = _get_cuda_stream()
        with stream:  # 在独立 stream 上执行，线程间不共享
            xp = cp
            A = len(tans)
            BATCH_A = 16
            best_var = float(init_var)
            best_angle = float(init_angle)

            sob_g = xp.asarray(sobel_cpu, dtype=xp.float32)
            col_g = xp.arange(tw, dtype=xp.int32)
            rows_g = xp.arange(th, dtype=xp.int32)
            center = xp.float32(tw / 2.0)

            for start in range(0, A, BATCH_A):
                end = min(start + BATCH_A, A)
                tans_b = xp.asarray(tans[start:end], dtype=xp.float32)
                angles_b = angles[start:end]

                shifts_g = xp.round(
                    (col_g[None, :].astype(xp.float32) - center) * tans_b[:, None]
                ).astype(xp.int32)

                r_idx_g = xp.clip(
                    rows_g[None, :, None] - shifts_g[:, None, :], 0, th - 1)
                proj_g = sob_g[r_idx_g, col_g[None, None, :]]
                row_sum_g = proj_g.mean(axis=2)
                region_g = row_sum_g[:, trim_l:trim_r]
                m_g = region_g.mean(axis=1, keepdims=True)
                var_g = ((region_g - m_g) ** 2).mean(axis=1)

                best_i_b = int(xp.argmax(var_g))
                best_var_b = float(var_g[best_i_b])

                if best_var_b > best_var:
                    best_var = best_var_b
                    best_angle = float(angles_b[best_i_b])

            stream.synchronize()  # 确保计算完成再读结果
            return best_angle, best_var

    def _angle_search_cpu(self, sobel, tans, angles,
                          th, tw, trim_l, trim_r,
                          init_var, init_angle):
        """
        全批量向量化版本（替换原来的逐角度循环）
        原来：for a_i in range(len(tans)) → 逐个处理
        现在：一次性矩阵运算 → 快3-5x
        """
        col_f = self._col_f
        col_i = self._col_i
        rows_i = self._rows_i
        center = self._center
        A = len(tans)

        # [A, tw] 一次性计算所有角度的偏移
        shifts_all = np.round(
            (col_f[None, :] - center) * tans[:, None]
        ).astype(np.int32)

        # [A, th, tw] 所有角度的行索引
        r_idx_all = rows_i[None, :, None] - shifts_all[:, None, :]
        np.clip(r_idx_all, 0, th - 1, out=r_idx_all)

        # [A, th, tw] → [A, th] 行均值
        proj_all = sobel[r_idx_all, col_i[None, None, :]]
        row_sum_all = proj_all.mean(axis=2)  # [A, th]

        # [A] 各角度的方差
        region_all = row_sum_all[:, trim_l:trim_r]  # [A, trim]
        m_all = region_all.mean(axis=1, keepdims=True)
        var_all = ((region_all - m_all) ** 2).mean(axis=1)  # [A]

        best_i = int(np.argmax(var_all))
        best_var = float(var_all[best_i])

        if best_var > init_var:
            return float(angles[best_i]), best_var
        return float(init_angle), float(init_var)

    # ================================================================== #
    #  周期估计
    # ================================================================== #

    def _estimate_period(self, sig):
        n = len(sig)
        if n < 20:
            return float(n) / 3

        s   = sig - sig.mean()
        fft = np.fft.rfft(s, n=2 * n)
        acf = np.fft.irfft(fft * np.conj(fft))[:n]
        acf /= acf[0] + 1e-12

        lo, hi = max(5, n // 20), max(7, n // 3)
        search = acf[lo:hi]
        peaks, props = scipy_signal.find_peaks(
            search, height=0.01, distance=max(3, lo // 3))

        if len(peaks) == 0:
            return float(np.clip(np.argmax(search) + lo, 3, n // 2))

        best_peak = float(peaks[np.argmax(props['peak_heights'])] + lo)
        return float(np.clip(best_peak, 3.0, n // 2))

    # ================================================================== #
    #  峰值检测
    # ================================================================== #

    def _detect_peaks(self, sig, period):
        std  = float(sig.std())
        mean = float(sig.mean())
        mx   = float(sig.max())
        dist = max(3, int(period * 0.55))

        peaks, _ = scipy_signal.find_peaks(
            sig, distance=dist,
            prominence=max(std * 0.2, (mx - mean) * 0.05),
            height=mean + std * 0.05)

        if len(peaks) == 0:
            peaks, _ = scipy_signal.find_peaks(
                sig, distance=dist, prominence=std * 0.05)

        return peaks

    # ================================================================== #
    #  坐标映射
    # ================================================================== #

    def _peaks_to_lines(self, peaks, angle, scale, orig_w):
        """
        缩略图峰值 → 原图直线参数 (k, b, y_center)
        b 为标准截距（x=0 处的 y 值）
        """
        k      = -float(np.tan(np.deg2rad(angle)))
        peak_f = np.asarray(peaks, dtype=np.float64)
        y_cs   = np.round(peak_f / scale).astype(np.int32)
        bs     = y_cs.astype(np.float64) - k * (orig_w / 2.0)
        return list(zip(
            np.full(len(peaks), k).tolist(),
            bs.tolist(),
            y_cs.tolist()
        ))

    # ================================================================== #
    #  精细拟合
    # ================================================================== #

    def _refine_all(self, sobel, lines_raw, band_half, n_samples=40):
        if not lines_raw:
            return []

        H, W   = sobel.shape
        half_w = W / 2.0
        N      = len(lines_raw)

        cols = np.linspace(0, W - 1, n_samples, dtype=np.int32)
        offs = np.arange(-band_half, band_half + 1, dtype=np.int32)

        ks  = np.array([l[0] for l in lines_raw], dtype=np.float64)
        bs  = np.array([l[1] for l in lines_raw], dtype=np.float64)
        ycs = np.array([l[2] for l in lines_raw], dtype=np.int32)

        valid_line = (ycs >= -int(H * 0.25)) & (ycs <= int(H * 1.25))

        y_pred = np.round(
            ks[:, None] * cols[None, :].astype(np.float64) + bs[:, None]
        ).astype(np.int32)

        valid_col = ((y_pred - band_half) >= 0) & ((y_pred + band_half) < H)

        row_idx = np.clip(
            y_pred[:, :, None] + offs[None, None, :], 0, H - 1)

        col_bc = cols[None, :, None]
        sv     = sobel[row_idx, col_bc]
        sv[~valid_col, :] = 0.0

        col_max = sv.max(axis=2)
        best_o  = sv.argmax(axis=2)

        good_col = valid_col & (col_max > 0.3)
        good_cnt = good_col.sum(axis=1)

        pts_y   = y_pred + offs[best_o]
        cols_f  = cols.astype(np.float64)
        pts_y_f = pts_y.astype(np.float64)
        mask_f  = good_col.astype(np.float64)

        cnt = mask_f.sum(axis=1)
        sx  = (mask_f * cols_f[None, :]).sum(axis=1)
        sy  = (mask_f * pts_y_f).sum(axis=1)
        sxx = (mask_f * cols_f[None, :] ** 2).sum(axis=1)
        sxy = (mask_f * cols_f[None, :] * pts_y_f).sum(axis=1)

        denom  = cnt * sxx - sx * sx
        safe   = np.abs(denom) > 1e-12
        k_fit  = np.where(safe, (cnt * sxy - sx * sy) / (denom + 1e-300), ks)
        b_fit  = np.where(safe, (sy - k_fit * sx) / (cnt + 1e-300), bs)

        use_orig = (~valid_line) | (good_cnt < 4)
        k_fit = np.where(use_orig, ks, k_fit)
        b_fit = np.where(use_orig, bs, b_fit)

        yc_fit = np.round(k_fit * half_w + b_fit).astype(np.int32)

        result = []
        for i in range(N):
            if valid_line[i]:
                result.append((float(k_fit[i]), float(b_fit[i]), int(yc_fit[i])))
        return result

    # ================================================================== #
    #  后处理
    # ================================================================== #

    def _post_process(self, lines, period, orig_w, orig_h=None):
        if not lines:
            return []

        lines   = sorted(lines, key=lambda x: x[2])
        min_gap = period * 0.55
        merged  = [lines[0]]
        for item in lines[1:]:
            if item[2] - merged[-1][2] >= min_gap:
                merged.append(item)

        result = [{'y_center': yc, 'k': k, 'b': b} for k, b, yc in merged]
        result = self._filter_angles(result, self.angle_filter_thresh)

        if orig_h is not None:
            result = self._filter_edges(result, orig_w, orig_h, self.edge_margin_ratio)

        return result

    def _filter_angles(self, lines, thresh):
        """过滤角度偏离中位数超过阈值的纬线（静默执行，不打印）"""
        if len(lines) < 3:
            return lines

        ks  = np.array([l['k'] for l in lines], dtype=np.float64)
        ang = np.degrees(np.arctan(ks))
        med = float(np.median(ang))
        ok  = np.abs(ang - med) <= thresh
        return [l for l, v in zip(lines, ok) if v]

    def _filter_edges(self, lines, W, H, ratio):
        """过滤超出图像边界的纬线（静默执行，不打印）"""
        if not lines:
            return lines

        m  = H * ratio
        ks = np.array([l['k'] for l in lines], dtype=np.float64)
        bs = np.array([l['b'] for l in lines], dtype=np.float64)
        yl = bs
        yr = ks * (W - 1) + bs
        ok = (yl >= -m) & (yl <= H + m) & (yr >= -m) & (yr <= H + m)
        return [l for l, v in zip(lines, ok) if v]

    # ================================================================== #
    #  间距计算
    # ================================================================== #

    def _calc_spacings(self, lines, W, H):
        if len(lines) < 2:
            return np.array([], dtype=np.int32), []

        ks = np.array([l['k'] for l in lines], dtype=np.float64)
        bs = np.array([l['b'] for l in lines], dtype=np.float64)

        yl    = np.clip(bs,          0, H - 1).astype(np.int32)
        yr    = np.clip(ks*(W-1)+bs, 0, H - 1).astype(np.int32)
        dl    = np.abs(np.diff(yl))
        dr    = np.abs(np.diff(yr))
        use_l = dl >= dr
        sp    = np.where(use_l, dl, dr)
        sides = ['left' if u else 'right' for u in use_l]
        return sp, sides

    # ================================================================== #
    #  边缘漏检补线
    # ================================================================== #

    def _check_edge_leak(self, lines, W, H, avg_period):
        """
        检查首尾是否漏检并补线（静默执行，不打印）

        修复：插入顶部补线后同步重建所有派生量
        """
        if len(lines) < 2:
            return lines, []

        suppl_list = []

        def _rebuild():
            ks_ = np.array([l['k'] for l in lines], dtype=np.float64)
            bs_ = np.array([l['b'] for l in lines], dtype=np.float64)
            ym_ = ks_ * (W / 2.0) + bs_
            avg_sp_ = float(np.abs(np.diff(ym_)).mean()) if len(ym_) > 1 else avg_period
            thr_    = avg_sp_ * self.edge_leak_threshold
            return ks_, bs_, ym_, avg_sp_, thr_

        ks, bs, ym, avg_sp, thr = _rebuild()

        # 顶部
        k0, b0 = float(ks[0]), float(bs[0])
        top_ys = np.array([b0, k0*(W/2)+b0, k0*(W-1)+b0])
        if float(top_ys.min()) > thr:
            k_n  = k0
            yc_n = lines[0]['y_center'] - int(avg_sp)
            b_n  = yc_n - k_n * (W / 2.0)
            nl   = {'y_center': yc_n, 'k': k_n, 'b': b_n,
                    'is_suppl': True, 'is_edge_suppl': True, 'position': 'top'}
            lines.insert(0, nl)
            suppl_list.append(nl)
            ks, bs, ym, avg_sp, thr = _rebuild()

        # 底部
        kL, bL = float(ks[-1]), float(bs[-1])
        bot_ys = np.array([bL, kL*(W/2)+bL, kL*(W-1)+bL])
        if float((H - 1) - bot_ys.max()) > thr:
            k_n  = kL
            yc_n = lines[-1]['y_center'] + int(avg_sp)
            b_n  = yc_n - k_n * (W / 2.0)
            nl   = {'y_center': yc_n, 'k': k_n, 'b': b_n,
                    'is_suppl': True, 'is_edge_suppl': True, 'position': 'bottom'}
            lines.append(nl)
            suppl_list.append(nl)

        return lines, suppl_list

    # ================================================================== #
    #  漏检 / 异常检测
    # ================================================================== #

    def _check_leak(self, lines, spacings, sides, max_idx, max_side,
                    sobel, W, H):
        """
        判断最大间距处是否为漏检（静默执行，不打印）

        修复：right/left 截距计算逻辑
        """
        spacings = np.asarray(spacings)
        la, lb   = lines[max_idx], lines[max_idx + 1]
        ka, ba   = la['k'], la['b']
        kb, bb   = lb['k'], lb['b']

        if max_side == 'right':
            ya = float(np.clip(ka*(W-1) + ba, 0, H - 1))
            yb = float(np.clip(kb*(W-1) + bb, 0, H - 1))
        else:
            ya = float(np.clip(ba,             0, H - 1))
            yb = float(np.clip(bb,             0, H - 1))

        ref_sp  = abs(yb - ya)
        y_mid_r = (ya + yb) / 2.0

        yca, ycb = la['y_center'], lb['y_center']
        y_mid_c  = (yca + ycb) // 2
        band     = max(5, int(ref_sp * 0.30))
        mid_str  = float(sobel[max(0, y_mid_c - band):min(H, y_mid_c + band)].mean())

        ref_list = []
        if max_idx > 0:
            yp = lines[max_idx - 1]['y_center']
            b2 = max(5, int(abs(yca - yp) * 0.30))
            ref_list.append(float(sobel[max(0, yp - b2):min(H, yp + b2)].mean()))
        if max_idx + 2 < len(lines):
            yn = lines[max_idx + 2]['y_center']
            b2 = max(5, int(abs(ycb - yn) * 0.30))
            ref_list.append(float(sobel[max(0, ycb - b2):min(H, ycb + b2)].mean()))

        ref_avg = float(np.mean(ref_list)) if ref_list else float(sobel.mean()) * 2.0
        ratio   = mid_str / (ref_avg + 1e-6)
        is_leak = ratio >= self.leak_detect_ratio

        if is_leak:
            kn = (ka + kb) / 2.0
            if max_side == 'right':
                bn = y_mid_r - kn * (W - 1)
            else:
                bn = y_mid_r

            ycn = int(round(kn * (W / 2.0) + bn))
            nl  = {'y_center': ycn, 'k': kn, 'b': bn, 'is_suppl': True}
            new_lines = sorted(lines + [nl], key=lambda x: x['y_center'])
            sp2, sd2  = self._calc_spacings(new_lines, W, H)
            suppl_info = {
                'y_center':    ycn,
                'k':           kn,
                'b':           bn,
                'ref_side':    max_side,
                'ref_spacing': ref_sp,
                'ratio':       ratio,
            }
            return new_lines, sp2, sd2, suppl_info, None
        else:
            anomaly_info = {
                'idx':     max_idx,
                'spacing': int(spacings.max()),
                'side':    max_side,
                'ratio':   ratio,
            }
            return lines, spacings, sides, None, anomaly_info

    # ================================================================== #
    #  绘图（加入断纬强度显示）
    # ================================================================== #

    def _draw(self, img, lines, angle, max_idx, max_side,
              suppl_info, anomaly_info, edge_suppl_list, period,
              spacings=None, sides=None):
        H, W = img.shape[:2]
        if not lines:
            cv2.putText(img, "No weft lines", (50, 50),
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
            return

        lth = max(2, int(W / 1000))
        fsc = max(0.5, min(0.8, W / 2000))

        ks = np.array([l['k'] for l in lines], dtype=np.float64)
        bs = np.array([l['b'] for l in lines], dtype=np.float64)
        yl = np.clip(bs,           0, H - 1).astype(np.int32)
        yr = np.clip(ks*(W-1)+bs,  0, H - 1).astype(np.int32)

        # 用于计算平均断裂强度
        broken_strengths = []

        for i, l in enumerate(lines):
            yL, yR  = int(yl[i]), int(yr[i])
            is_edge = l.get('is_edge_suppl', False)
            is_mid  = l.get('is_suppl', False) and not is_edge
            is_broken = l.get('is_broken', False)
            strength = l.get('broken_strength', 0.0)

            if is_broken:
                broken_strengths.append(strength)

            if is_edge:
                self._dashed_line(img, (0, yL), (W-1, yR), (255, 0, 255), lth + 1)
                cv2.putText(img, f"L{i+1}[EdgeSuppl]", (10, yL),
                            cv2.FONT_HERSHEY_SIMPLEX, fsc, (255, 0, 255), 2)
            elif is_mid:
                self._dashed_line(img, (0, yL), (W-1, yR), (0, 215, 255), lth + 1)
                cv2.putText(img, f"L{i+1}[Suppl]", (10, yL),
                            cv2.FONT_HERSHEY_SIMPLEX, fsc, (0, 215, 255), 2)
            elif is_broken:
                bk_x = l.get('break_x', W // 2)
                bk_y = int(np.clip(l['k'] * bk_x + l['b'], 0, H - 1))
                cv2.line(img, (0, yL), (bk_x, bk_y), (0, 255, 255), lth, cv2.LINE_AA)
                if bk_x < W - 1:
                    self._dashed_line(img, (bk_x, bk_y), (W-1, yR), (0, 255, 255), lth)
                # 显示强度值
                label = f"L{i+1}[Broken {strength:.2f}]"
                cv2.putText(img, label, (10, yL),
                            cv2.FONT_HERSHEY_SIMPLEX, fsc, (0, 255, 255), 1)
                cv2.circle(img, (bk_x, bk_y), lth + 2, (0, 0, 255), 2)
            else:
                cv2.line(img, (0, yL), (W-1, yR), (0, 255, 0), lth, cv2.LINE_AA)
                cv2.putText(img, f"L{i+1}", (10, yL),
                            cv2.FONT_HERSHEY_SIMPLEX, fsc, (0, 255, 255), 1)

        # 间距标注
        if len(lines) > 1:
            if spacings is None or sides is None:
                spacings, sides = self._calc_spacings(lines, W, H)
            sp  = spacings
            sd  = sides
            axr = int(W * 0.85)
            axl = int(W * 0.05)

            for i in range(len(lines) - 1):
                is_max = (i == max_idx)
                side   = max_side if is_max else sd[i]
                color  = (0, 0, 255) if is_max else (255, 100, 0)
                thick  = 3 if is_max else 1
                label  = f"{int(sp[i])}px" + (" [MAX]" if is_max else "")

                if side == 'left':
                    mid = (int(yl[i]) + int(yl[i+1])) // 2
                    cv2.line(img, (10, int(yl[i])),   (axl, int(yl[i])),   color, thick)
                    cv2.line(img, (10, int(yl[i+1])), (axl, int(yl[i+1])), color, thick)
                    cv2.putText(img, label, (axl+5, mid),
                                cv2.FONT_HERSHEY_SIMPLEX, fsc*0.9, color,
                                2 if is_max else 1)
                else:
                    mid = (int(yr[i]) + int(yr[i+1])) // 2
                    cv2.line(img, (W-10, int(yr[i])),   (axr, int(yr[i])),   color, thick)
                    cv2.line(img, (W-10, int(yr[i+1])), (axr, int(yr[i+1])), color, thick)
                    cv2.putText(img, label, (axr+5, mid),
                                cv2.FONT_HERSHEY_SIMPLEX, fsc*0.9, color,
                                2 if is_max else 1)

        if anomaly_info:
            cv2.putText(img,
                        f"[ANOMALY] spacing={anomaly_info['spacing']}px",
                        (50, H - 50), cv2.FONT_HERSHEY_SIMPLEX,
                        0.8, (0, 0, 255), 2)

        # 信息面板
        ph = 180
        cv2.rectangle(img, (W-280, 15), (W-15, 15+ph), (50, 50, 50), -1)
        cv2.rectangle(img, (W-280, 15), (W-15, 15+ph), (0, 255, 0), 2)
        panel = [
            f"Lines:{len(lines)}",
            f"Angle:{angle:.1f}deg",
            f"Thresh:{self.max_spacing_threshold}px",
        ]
        if suppl_info:
            panel.append("Added:1 mid-suppl")
        if edge_suppl_list:
            panel.append(f"Added:{len(edge_suppl_list)} edge-suppl")
        if anomaly_info:
            panel.append("Status:ANOMALY")
        if broken_strengths:
            avg_str = np.mean(broken_strengths)
            panel.append(f"Broken:{len(broken_strengths)} lines")
            panel.append(f"AvgStr:{avg_str:.2f}")

        for j, t in enumerate(panel):
            cv2.putText(img, t, (W-265, 35 + j*22),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1)

    # ================================================================== #
    #  工具方法
    # ================================================================== #
    @staticmethod
    def _dashed_line(img, p1, p2, color, thickness=2, dash=20, gap=12):
        x1, y1, x2, y2 = p1[0], p1[1], p2[0], p2[1]
        dist = float(np.hypot(x2-x1, y2-y1))
        if dist == 0:
            return
        dx, dy = (x2-x1)/dist, (y2-y1)/dist
        step   = dash + gap
        ss     = np.arange(0, dist, step)
        es     = np.minimum(ss + dash, dist)
        for s, e in zip(ss, es):
            cv2.line(img,
                     (int(x1 + dx*s), int(y1 + dy*s)),
                     (int(x1 + dx*e), int(y1 + dy*e)),
                     color, thickness, cv2.LINE_AA)

    @staticmethod
    def _imread_cn(path):
        img = cv2.imread(str(path))
        if img is not None:
            return img
        try:
            with open(path, 'rb') as f:
                return cv2.imdecode(np.frombuffer(f.read(), np.uint8),
                                    cv2.IMREAD_COLOR)
        except Exception:
            return None

    @staticmethod
    def _imwrite_cn(path, img):
        """保存图像（兼容中文路径），始终输出成功/失败信息"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        # 尝试标准写入
        if cv2.imwrite(str(path), img, [cv2.IMWRITE_JPEG_QUALITY, 92]):
            print(f" 图像已保存: {path}")
            return True

        # 降级方案：imencode
        try:
            ext = path.suffix.lower()
            ok, buf = cv2.imencode(ext, img, [cv2.IMWRITE_JPEG_QUALITY, 92])
            if ok:
                with open(path, 'wb') as f:
                    f.write(buf.tobytes())
                print(f"图像已保存: {path}")
                return True
        except Exception as e:
            print(f"保存失败: {path}, 错误: {e}")
            return False
        return False


# ================================================================== #
#  批量处理入口
# ================================================================== #
import os
import sys
import atexit

# 离线批处理调试入口的默认目录。此前硬编码为 D:\ 开头的本机路径，
# 现改为可由命令行参数或环境变量指定。
INPUT_DIR = os.environ.get("WEFT_INPUT_DIR", "")
OUTPUT_DIR = os.environ.get("WEFT_OUTPUT_DIR", "./weft_debug_output")
IMG_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff"}


def cv_imread_cn(fp):
    try:
        return cv2.imdecode(np.fromfile(fp, dtype=np.uint8), cv2.IMREAD_COLOR)
    except Exception as e:
        print(f"读取失败: {fp}, {e}")
        return None


def batch_process(save_annotated=True, use_gpu=True,
                  input_dir=None, output_dir=None):
    input_dir  = Path(input_dir or INPUT_DIR)
    output_dir = Path(output_dir or OUTPUT_DIR)
    if not input_dir.is_dir():
        print(f"输入目录不存在: {input_dir}")
        return
    output_dir.mkdir(parents=True, exist_ok=True)

    detector = WeftDetector(
        output_dir            = str(output_dir),
        save_annotated        = save_annotated,
        angle_filter_thresh   = 1.0,
        edge_margin_ratio     = 0.02,
        max_spacing_threshold = 260,
        leak_detect_ratio     = 0.6,
        edge_leak_threshold   = 1.5,
        use_gpu               = use_gpu,
    )

    imgs = sorted([p for p in input_dir.iterdir()
                   if p.is_file() and p.suffix.lower() in IMG_SUFFIXES])
    print(f"找到 {len(imgs)} 张图片\n")

    ok = fail = 0
    t_all = []

    for i, p in enumerate(imgs, 1):
        print(f"[{i}/{len(imgs)}] {p.name}")
        img = cv_imread_cn(str(p))
        if img is None:
            fail += 1
            continue
        try:
            t0 = time.perf_counter()
            s, ang, sp = detector.detect(img, p.name)
            t1 = time.perf_counter()
            t_all.append((t1 - t0) * 1000)
            if s:
                ok += 1
            else:
                fail += 1
                print(f"   FAIL")
        except Exception as e:
            fail += 1
            print(f"   ERR: {e}")

    print(f"\n{'='*50}")
    print(f"完成! 总:{len(imgs)} 成功:{ok} 失败:{fail}")
    if t_all:
        arr = np.array(t_all)
        print(f"平均耗时: {arr.mean():.1f}ms | "
              f"最小: {arr.min():.1f}ms | 最大: {arr.max():.1f}ms")
    print(f"{'='*50}")


if __name__ == "__main__":
    # 用法: python -m AnomalyDetection_Tool.analysis.weft_skew_analyzer \
    #           <输入目录> [输出目录]
    _in  = sys.argv[1] if len(sys.argv) > 1 else None
    _out = sys.argv[2] if len(sys.argv) > 2 else None
    if not (_in or INPUT_DIR):
        print("用法: python -m AnomalyDetection_Tool.analysis."
              "weft_skew_analyzer <输入目录> [输出目录]")
        sys.exit(1)
    batch_process(save_annotated=False, use_gpu=True,
                  input_dir=_in, output_dir=_out)
