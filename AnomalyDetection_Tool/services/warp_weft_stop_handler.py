# -*- coding: utf-8 -*-
"""
经纬线停机统一处理器
职责：停机标记 → 继电器触发 → 图片保存 → 上传任务构造
"""

import os
import time
from datetime import datetime
from typing import Optional

import cv2
import numpy as np

from General_Tool.EnhancedLogger import info, error
from AnomalyDetection_Tool.config.settings import (
    MACHINE_STOP_RULES,
    REAL_WIDTH_CM,
    IMAGE_WIDTH_PX,
)
from AnomalyDetection_Tool.services.relay_service import relay_service
from AnomalyDetection_Tool.upload.upload_task import UploadTask
from AnomalyDetection_Tool.output.encoder_state_service import get_encoder_state

# ──────────────────────────────────────────────────────────────────────────────
#  stop_type（内部）→ defect_type（上传字段）映射表
# ──────────────────────────────────────────────────────────────────────────────
STOP_TYPE_TO_DEFECT: dict = {
    "weft":        "missing_latitude",  # 纬线间距过大 → 缺纬
    "broken_weft": "weft_broken",       # 断纬
    "warp":        "thin_seam",         # 经线不均匀 → 稀缝
    "lat_slope":   "lat_slope",         # 纬斜
}


class StopHandler:
    """
    经纬线停机统一处理器（可独立测试）。

    参数
    ----
    machine_stop_path : 停机图片根目录
    launch_tag        : 本次启动标签（用于子目录隔离）
    upload_manager    : 上传管理器，None 时跳过上传
    """

    # ── 停机类型优先级（数字越小越优先，决定主 stop_type 和图片文件名）──
    _PRIORITY: dict = {
        "broken_weft": 0,
        "weft":        1,
        "lat_slope":   2,
        "warp":        3,
    }

    # ──────────────────────────────────────────────────────
    #  初始化
    # ──────────────────────────────────────────────────────
    def __init__(
        self,
        machine_stop_path: str,
        launch_tag: str,
        upload_manager=None,
    ):
        self.machine_stop_path = machine_stop_path
        self.launch_tag = launch_tag
        self.upload_manager = upload_manager

    # ──────────────────────────────────────────────────────
    #  公共入口 1：单事件停机
    # ──────────────────────────────────────────────────────
    def trigger(
        self,
        image: np.ndarray,
        camera_name: str,
        filename: str,
        stop_type: str,
        stop_reason: str = "",
        camera_sn: str = "",
        weft_angle: Optional[float] = None,
        weft_max_spacing: Optional[int] = None,
        weft_broken_intensity: Optional[float] = None,
        warp_uniformity: Optional[float] = None,
    ) -> None:
        """
        停机全流程：
          1. setDefectStop()
          2. 触发继电器
          3. 保存停机图片
          4. 提交上传任务

        人为停止期间直接返回：经纬分析跑在后台线程池里，不经过相机回调，
        入口处的停止判断挡不住它，否则会出现"点了停止仍然触发继电器、
        仍然写停机图"的情况。
        """
        if self._detection_paused():
            return

        self._set_defect_stop()
        self._trigger_relay(camera_name, stop_type)

        if not MACHINE_STOP_RULES.get("save_to_folder", False):
            return

        save_path = self._save_image(
            image, camera_name, filename, stop_type, stop_reason
        )

        if self.upload_manager is not None:
            self._submit_upload(
                save_path=save_path,
                camera_name=camera_name,
                camera_sn=camera_sn,
                filename=filename,
                stop_type=stop_type,
                image=image,
                weft_angle=weft_angle,
                weft_max_spacing=weft_max_spacing,
                weft_broken_intensity=weft_broken_intensity,
                warp_uniformity=warp_uniformity,
            )

    # ──────────────────────────────────────────────────────
    #  公共入口 2：多事件聚合停机
    # ──────────────────────────────────────────────────────
    def trigger_aggregated(
        self,
        image: np.ndarray,
        camera_name: str,
        filename: str,
        camera_sn: str = "",
        stop_events: Optional[list] = None,
    ) -> None:
        """
        将同一帧多个停机事件聚合为一次上传。
        继电器 / setDefectStop 仍各自执行（每个事件均需触发）。
        上传仅提交一次，携带所有字段的聚合值。

        stop_events 字段结构：
        [
            {
                "stop_type":             str,
                "stop_reason":           str,
                "weft_angle":            float | None,
                "weft_max_spacing":      int   | None,
                "weft_broken_intensity": float | None,
                "warp_uniformity":       float | None,
            },
            ...
        ]
        """
        if not stop_events:
            return

        # 人为停止期间完全静默（理由同 trigger()）
        if self._detection_paused():
            return

        # ① 每个事件独立执行停机动作（继电器等）
        for evt in stop_events:
            self._set_defect_stop()
            self._trigger_relay(camera_name, evt.get("stop_type", "unknown"))

        if not MACHINE_STOP_RULES.get("save_to_folder", False):
            return

        # ② 只保存一张图片（避免重复写盘），stop_type 取优先级最高的事件
        primary = self._pick_primary_event(stop_events)
        save_path = self._save_image(
            image,
            camera_name,
            filename,
            primary.get("stop_type", ""),
            primary.get("stop_reason", ""),
        )

        if self.upload_manager is None:
            return

        # ③ 聚合所有事件字段 → 一次上传
        merged = self._merge_events(stop_events)
        self._submit_upload(
            save_path=save_path,
            camera_name=camera_name,
            camera_sn=camera_sn,
            filename=filename,
            stop_type=primary.get("stop_type", ""),
            image=image,
            weft_angle=merged["weft_angle"],
            weft_max_spacing=merged["weft_max_spacing"],
            weft_broken_intensity=merged["weft_broken_intensity"],
            warp_uniformity=merged["warp_uniformity"],
        )

    # ──────────────────────────────────────────────────────
    #  私有步骤
    # ──────────────────────────────────────────────────────
    @staticmethod
    def _detection_paused() -> bool:
        """人为停止检测期间，经纬停机也应当完全静默"""
        try:
            import Communication_Tool.ThriftControl as TC
            return TC.isDetectionPaused()
        except Exception:
            return False

    @staticmethod
    def _set_defect_stop() -> None:
        # 统一走 CommService（内部已做可用性检查与异常兜底），
        # 不再直接 import ThriftControl 绕过抽象层。
        from AnomalyDetection_Tool.services.comm_service import comm_service
        comm_service.set_defect_stop()

    @staticmethod
    def _trigger_relay(camera_name: str, stop_type: str) -> None:
        try:
            if MACHINE_STOP_RULES.get("trigger_relay"):
                relay_service.trigger(camera_name, stop_type)
        except Exception as e:
            error(f"[停机] relay_service.trigger 失败: {e}")

    def _save_image(
        self,
        image: np.ndarray,
        camera_name: str,
        filename: str,
        stop_type: str,
        stop_reason: str,
    ) -> str:
        """保存停机图片，返回完整路径；失败时返回空字符串。"""
        try:
            stop_dir = os.path.join(
                self.machine_stop_path,
                self.launch_tag,
                camera_name,
                "StopMachine",
            )
            os.makedirs(stop_dir, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:18]
            safe_reason = stop_reason.replace("/", "_").replace("\\", "_")
            save_path = os.path.join(
                stop_dir,
                f"{camera_name}_{filename}_{stop_type}_{safe_reason}_{timestamp}.jpg",
            )
            cv2.imwrite(save_path, image)
            info(f"[停机] 图片已保存: {save_path}")
            return save_path
        except Exception as e:
            error(f"[停机] 保存图片失败: {e}")
            return ""

    def _submit_upload(
        self,
        save_path: str,
        camera_name: str,
        camera_sn: str,
        filename: str,
        stop_type: str,
        image: np.ndarray,
        weft_angle: Optional[float],
        weft_max_spacing: Optional[int],
        weft_broken_intensity: Optional[float],
        warp_uniformity: Optional[float],
    ) -> None:
        """构造 UploadTask 并提交，失败时仅记录日志。"""
        try:
            detection_info = self._build_detection_info(
                save_path=save_path,
                camera_name=camera_name,
                camera_sn=camera_sn,
                stop_type=stop_type,
                image=image,
                weft_angle=weft_angle,
                weft_max_spacing=weft_max_spacing,
                weft_broken_intensity=weft_broken_intensity,
                warp_uniformity=warp_uniformity,
            )

            task = UploadTask(
                image_path=detection_info["imageUrl"],
                camera_sn=camera_sn,
                camera_name=camera_name,
                frame_number=filename,
                class_name=stop_type,
                class_id=-1,
                confidence=1.0,
                x1=0, y1=0, x2=0, y2=0,
                is_stop=True,
                real_area_mm2=0.0,
                fill_ratio=0.0,
                detection_info=detection_info,
            )

            self.upload_manager.submit_stop(task)
            info(
                f"[停机] 上传任务已提交 "
                f"defect_type={detection_info['defectType']} "
                f"camera={camera_name} "
                f"angle={detection_info['latSlopeAngle']}° "
                f"intensity={detection_info['weftBroken']} "
                f"max_spacing={detection_info['maxLatInterval']}px "
                f"uniformity={detection_info['thinSeamUniformity']}"
            )
        except Exception as e:
            error(f"[停机] 构造上传任务失败: {e}")

    def _build_detection_info(
        self,
        save_path: str,
        camera_name: str,
        camera_sn: str,
        stop_type: str,
        image: np.ndarray,
        weft_angle: Optional[float],
        weft_max_spacing: Optional[int],
        weft_broken_intensity: Optional[float],
        warp_uniformity: Optional[float],
    ) -> dict:
        """
        组装上传 JSON。

        格式：
        {
          "machineId":          "094",
          "cameraId":           "camera3",
          "cameraSn":           "DA9670307",
          "imageSize":          [2436, 1148],
          "imageType":          "jpg",
          "colorSpace":         "BGR",
          "imageUrl":           "\\20260604164935\\...",
          "defectType":         "weft_broken",
          "confidence":         1.0,
          "timestamp":          1780562977736,
          "isStop":             1,
          "weftBroken":         0.35,
          "maxLatInterval":     260,
          "latSlopeAngle":      -1.2,
          "thinSeamUniformity": 32.5,
          "defectLength":       120.5,
          "realLength":         2400,
          "pictureLength":      2436
        }
        """
        from AnomalyDetection_Tool.utils.path_utils import to_upload_image_url

        defect_type = STOP_TYPE_TO_DEFECT.get(stop_type, stop_type)

        # 上传相对路径
        upload_path = ""
        if save_path:
            upload_path = to_upload_image_url(
                save_path, self.machine_stop_path, self.launch_tag
            )

        img_h, img_w = image.shape[:2] if image is not None else (0, 0)

        enc = get_encoder_state()
        defect_length = round(enc.cumulative_distance, 2)

        return {
            "machineId":          "094",
            "cameraId":           camera_name,
            "cameraSn":           camera_sn,
            "imageSize":          [img_w, img_h],
            "imageType":          "jpg",
            "colorSpace":         "BGR",
            "imageUrl":           upload_path,
            "defectType":         defect_type,
            "confidence":         1.0,
            "timestamp":          int(time.time() * 1000),
            "isStop":             1,
            # ── 经纬线专属字段 ────────────────────────────────
            # 断纬平均强度 [0,1]，非断纬填 0.0
            "weftBroken": (
                round(float(weft_broken_intensity), 4)
                if weft_broken_intensity is not None else 0.0
            ),
            # 纬线最大间距(px)，无纬线检测填 0
            "maxLatInterval": (
                int(weft_max_spacing)
                if weft_max_spacing is not None else 0
            ),
            # 纬线斜度角(°)，纬斜停机时为关键字段
            "latSlopeAngle": (
                round(float(weft_angle), 2)
                if weft_angle is not None else 0.0
            ),
            # 经线均匀度，无经线检测填 0.0
            "thinSeamUniformity": (
                round(float(warp_uniformity), 4)
                if warp_uniformity is not None else 0.0
            ),
            # 编码器累计距离
            "defectLength":  defect_length,
            "realLength":    REAL_WIDTH_CM * 10,
            "pictureLength": IMAGE_WIDTH_PX,
        }

    # ──────────────────────────────────────────────────────
    #  聚合辅助
    # ──────────────────────────────────────────────────────
    def _pick_primary_event(self, events: list) -> dict:
        """按优先级选出主事件。"""
        return min(
            events,
            key=lambda e: self._PRIORITY.get(e.get("stop_type", ""), 99),
        )

    @staticmethod
    def _merge_events(events: list) -> dict:
        """
        将多个事件的关键字段合并：
        - weft_angle             取绝对值最大的（最严重的纬斜）
        - weft_max_spacing       取最大值
        - weft_broken_intensity  取最大值
        - warp_uniformity        取最小值（越小越差）
        """
        angles      = [e["weft_angle"]            for e in events if e.get("weft_angle")            is not None]
        spacings    = [e["weft_max_spacing"]       for e in events if e.get("weft_max_spacing")      is not None]
        intensities = [e["weft_broken_intensity"]  for e in events if e.get("weft_broken_intensity") is not None]
        uniformities= [e["warp_uniformity"]        for e in events if e.get("warp_uniformity")       is not None]

        return {
            "weft_angle":            max(angles,       key=abs) if angles       else None,
            "weft_max_spacing":      max(spacings)              if spacings     else None,
            "weft_broken_intensity": max(intensities)           if intensities  else None,
            "warp_uniformity":       min(uniformities)          if uniformities else None,
        }