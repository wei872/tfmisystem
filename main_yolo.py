#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
===============================================================================
主程序 —— 相机采集 + YOLO 检测 + 布幅检测
SDK: 大恒图像 Galaxy (GxIAPI / gxipy) SDK

这是系统的**唯一入口**。此前 main_yolo.py / main_test.py 是两份复制粘贴的
副本（716/729 行，仅差 99 行），修一处必漏另一处；现已合并，main_test.py
仅保留为转发到本文件的兼容壳。

运行模式由 config.yaml 决定，不需要改代码：
  - simulation.mode: true   → 读取 simulation.folder 里的图片跑仿真
  - simulation.mode: false  → 连接大恒相机跑生产流程
===============================================================================
"""

import logging
import threading
import time
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

import cv2
import faulthandler

from Communication_Tool.Modbus import TriggerControlSystem, SystemConfig
from Communication_Tool.ThriftControl import PythonJavaClient, start_python_server
import Communication_Tool.ThriftControl
from Test_Tool.TestTool import press_any_key_exit
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from General_Tool.EnhancedLogger import init_logger, info, debug, error
from General_Tool.HealthMonitor import (
    init_health_monitor, shutdown_health_monitor,
)
from General_Tool.BackgroundTaskManager import BackgroundTaskManager
from General_Tool.RunningSystemRegistry import get_running_system

from AnomalyDetection_Tool.config.settings import (
    DETECTION_SAVE_PATH, MACHINE_STOP_PATH,
    YOLO_CONFIG, SAVE_CONFIG, MACHINE_STOP_RULES,
    CLASS_CONFIDENCE_THRESHOLDS, DEFAULT_CLASS_CONFIDENCE,
    SIMULATION_MODE, SIMULATION_FOLDER,
    FABRIC_WIDTH_CONFIG, CAMERA_SN_MAP, CAMERA_ACQ_CONFIG,
    SAVE_PATH, SEAVEIMAGE, PUSH,
    PLC_CONFIG, WEBSOCKET_CONFIG, LOGGING_CONFIG, CONCURRENCY_CONFIG,
)
from AnomalyDetection_Tool.core.detection_manager import YOLODetectionManager
from AnomalyDetection_Tool.utils.font_utils import load_chinese_font
from AnomalyDetection_Tool.output.fabric_width_service import (
    init_fabric_detector as _init_fabric_service,
    get_fabric_detector,
    get_fabric_stats,
)
from AnomalyDetection_Tool.services.WebSocketImageServer import (
    init_websocket_server,
    get_websocket_server,
    shutdown_websocket_server
)
# ==================== 大恒图像 Galaxy SDK 导入 ====================
# gxipy 已随仓库放在 lib/gxipy。Camera_Tool.CameraOperation 在导入时会把
# lib/ 挂到 sys.path，所以必须先导入它，再 import gxipy，路径才是就绪的。
from Camera_Tool.CameraOperation import CameraOperation
from Camera_Tool import CameraRegistry
import gxipy as gx

# ==================== 全局变量 ====================
faulthandler.enable()

# 卡死诊断：由 config.yaml 的 logging.faulthandler_dump_after 控制（秒）。
# dump_traceback_later 的转储线程跑在 C 层、自带定时器、**不需要 GIL**，
# 所以即使所有 Python 线程都死锁在 GIL 上它照样能把栈打出来 —— 这正是
# 排查"整机卡死"最需要的手段。卡死发生时，crash.log 里最后一次转储
# 就是各线程当时的位置。
#
# 修掉两个问题：
#   1. 原来 open() 的结果是个临时对象，没有任何引用，也没有 try 保护；
#      文件打不开（磁盘满/权限/路径不存在）会直接掀掉整个进程启动。
#   2. 句柄一直不关。现在存到模块级变量，退出时由 _shutdown_common 关闭。
_dump_after = float(LOGGING_CONFIG.get("faulthandler_dump_after", 0) or 0)
_dump_file = None
if _dump_after > 0:
    try:
        os.makedirs("logs", exist_ok=True)
        _dump_file = open(os.path.join("logs", "crash.log"), "a", buffering=1)
        faulthandler.dump_traceback_later(
            _dump_after, repeat=True, file=_dump_file)
        info(f"[卡死诊断] 已开启：每 {_dump_after:.0f}s 转储一次各线程栈到 logs/crash.log")
    except Exception as e:
        error(f"[卡死诊断] 开启失败（不影响主流程）: {e}")
        _dump_file = None

# SAVE_PATH, SEAVEIMAGE, PUSH 已从 settings (yaml) 导入

obj_cam_operation = []
# 全局唯一的 gxipy DeviceManager：构造即 gx_init_lib()，回收即 gx_close_lib()
_device_manager = None
lock = threading.RLock()
java_client = None

# CAMERA_SN_MAP 已从 settings（yaml）导入
TARGET_SERIALS = set(CAMERA_SN_MAP.keys())   # ← 派生自 yaml 配置

_detection_manager: Optional[YOLODetectionManager] = None

# 布幅检测专用单线程池：保持 process_frame 串行语义，同时不阻塞相机回调线程
# 每台相机最多提交1帧（提交前检查上一帧是否 done），多余的帧直接丢弃
_fabric_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="FabricDetect")
_fabric_queue_lock = threading.Lock()
_fabric_pending: dict = {}  # camera_name -> Future，防止同一相机积压

# ── 辅助 I/O 线程池（调试存图 / 推帧给 Java）────────────────────────────
# 这两件事原先直接跑在相机 SDK 的 C 回调线程上：
#   cv2.imwrite() 落盘 + cv2.imencode() JPEG 编码 + Thrift 网络发送
# 一旦现场把 debug.save_image / debug.push_to_java 打开，回调线程就会被
# 磁盘和网络拖住，直接导致相机丢帧甚至 SDK 内部缓冲耗尽。
# 现在改为投递到本线程池，且**队列满即丢帧**——调试功能绝不允许拖累采集。
_aux_executor = ThreadPoolExecutor(
    max_workers=max(1, int(CONCURRENCY_CONFIG.get("aux_io_workers", 2))),
    thread_name_prefix="AuxIO")
_aux_lock = threading.Lock()
_aux_inflight = 0
_AUX_MAX_INFLIGHT = 8      # 在途辅助任务上限，超出直接丢弃
_aux_dropped = 0


def _submit_aux(fn, *args) -> bool:
    """提交辅助 I/O 任务；在途任务过多时丢弃并计数，保证不阻塞采集线程。"""
    global _aux_inflight, _aux_dropped

    with _aux_lock:
        if _aux_inflight >= _AUX_MAX_INFLIGHT:
            _aux_dropped += 1
            if _aux_dropped % 100 == 1:
                debug(f"[AuxIO] 队列积压，已丢弃 {_aux_dropped} 个调试任务")
            return False
        _aux_inflight += 1

    def _runner():
        global _aux_inflight
        try:
            fn(*args)
        except Exception as e:
            error(f"[AuxIO] 任务执行失败: {e}")
        finally:
            with _aux_lock:
                _aux_inflight -= 1

    try:
        _aux_executor.submit(_runner)
        return True
    except RuntimeError:
        # 线程池已关闭（程序退出中）
        with _aux_lock:
            _aux_inflight -= 1
        return False


_manual_stop_log_ts: dict = {}
_MANUAL_STOP_LOG_INTERVAL = 10.0   # 秒


def _log_manual_stop_skip(camera_name: str) -> None:
    """手动停止期间按相机节流打印，避免每帧刷屏但又能看出系统在保持停止"""
    now = time.time()
    last = _manual_stop_log_ts.get(camera_name, 0.0)
    if now - last >= _MANUAL_STOP_LOG_INTERVAL:
        _manual_stop_log_ts[camera_name] = now
        info(f"[停止] {camera_name} 前端已下发停止，跳过检测中"
             f"（等待前端重新点『开始』，织机速度回升也不会自动恢复）")


def _save_debug_image(image, camera_sn: str, sub_dir: str) -> None:
    """后台线程：调试原图落盘"""
    out_dir = os.path.join(SAVE_PATH, sub_dir)
    os.makedirs(out_dir, exist_ok=True)
    t = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    cv2.imwrite(os.path.join(out_dir, f"{camera_sn}_{t}.bmp"), image)


def _push_frame_to_java(image) -> None:
    """后台线程：JPEG 编码并推送给 Java 端"""
    if java_client is None:
        return
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if ok and java_client.connect():
        java_client.pushFrameToJava(buf.tobytes())


# ==================================================================
# 图像处理（接收已转换好的 BGR 图像）
# ==================================================================
def image_control(image, frame_info, camera_sn=""):
    """
    图像处理函数。

    ⚠ 本函数运行在大恒 SDK 的采集线程上，**任何阻塞都会造成相机丢帧**。
    因此这里只做分发：布幅检测、调试落盘、Java 推帧全部投递到线程池，
    YOLO 检测本身由 DetectionWorkerPool 内部排队消化。

    :param image:      BGR / uint8 / HxWx3 的独立副本（可以安全跨线程传递）
    :param frame_info: CameraOperation 给出的帧信息 dict
    :param camera_sn:  相机序列号
    """
    camera_name = CAMERA_SN_MAP.get(camera_sn)
    if camera_name is None:
        debug(f"[{camera_sn}] 未配置，跳过")
        return

    # ====== 织机停机预览模式判断 ======
    # 必须放在 getIsStop() 检查之前！
    # 原因：缺陷停机标志(getIsStop)一旦置位，在织机真实物理停机（速度长期为 0）
    # 的情况下永远无法通过自动恢复状态机（要求速度重新超过 5m/min）复位，会一直
    # 悬空为 True。若先检查 getIsStop()，此后所有软触发预览帧都会在这里被吞掉，
    # 前端 WebSocket 完全收不到画面。
    # 预览帧的推送应当独立于"是否有未处理的缺陷停机"。
    #
    # 模拟模式下 get_running_system() 恒为 None，不影响仿真流程。
    trigger_system = get_running_system()
    is_preview = trigger_system.is_preview_mode() if trigger_system else False

    if is_preview:
        # 停机预览帧：只推 WebSocket 给前端看，
        # 绝不进入布幅检测/YOLO检测/落盘/上传，避免污染生产数据统计
        ws_server = get_websocket_server()
        if ws_server is not None:
            ws_server.push_image(camera_name, image,
                                 metadata={"source": "idle_preview"})
        debug(f"[{camera_name}] 停机预览帧(软触发)，跳过检测/上传")
        return

    # 非预览模式（硬触发/Line0 正常检测路径）：
    # 遵守停机标志，避免在等待自动恢复期间重复检测、重复触发继电器。
    #
    # 手动停止（前端点"停止"）时这里同样返回：进程继续运行、相机继续出图，
    # 但一律不进检测链路，且**无论织机速度是否回升都不会自动恢复** ——
    # 只有前端重新点"开始"才恢复（见 ThriftControl.setDefectStop 的说明）。
    if Communication_Tool.ThriftControl.getIsStop():
        if Communication_Tool.ThriftControl.isManualPaused():
            _log_manual_stop_skip(camera_name)
        return

    frame_number = frame_info["nFrameNum"]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    # ====== 布幅检测异步化 ======
    # 只传引用不做 image.copy()：整帧拷贝约 1.2ms(15MB)，同样不该占用回调线程。
    # 该数组由 on_camera_frame 每帧新建、且已脱离 SDK 缓冲区，
    # 布幅检测只读不写，共享是安全的。
    fabric_detector = get_fabric_detector()
    if fabric_detector is not None:
        with _fabric_queue_lock:
            prev = _fabric_pending.get(camera_name)
            if prev is None or prev.done():
                fut = _fabric_executor.submit(
                    fabric_detector.process_frame, image, camera_name)
                _fabric_pending[camera_name] = fut

    # ====== YOLO 缺陷检测 ======
    if _detection_manager is not None:
        _detection_manager.detect(
            image=image, filename=f"{camera_sn}_{frame_number}",
            camera_name=camera_name, camera_sn=camera_sn,
            capture_timestamp=ts,
            metadata={"camera_sn": camera_sn, "frame": frame_number})

    # ====== 推送到 WebSocket ======
    ws_server = get_websocket_server()
    if ws_server is not None:
        ws_server.push_image(camera_name, image)

    # ====== 调试功能：一律异步，绝不阻塞采集线程 ======
    if SEAVEIMAGE:
        # 第 3/4 个参数分别是文件名前缀和子目录，旧实现两处都传 SN，行为保持不变
        _submit_aux(_save_debug_image, image.copy(), camera_sn, camera_sn)

    if PUSH and java_client is not None:
        _submit_aux(_push_frame_to_java, image.copy())


# ==================================================================
# 大恒相机图像回调
# ==================================================================
def on_camera_frame(image, frame_info):
    """
    相机帧回调（运行在大恒 SDK 的采集线程上）。

    image / frame_info 由 CameraOperation 准备好：
      - image 已经是 BGR / uint8 / HxWx3 的**独立副本**，可安全跨线程传递；
      - frame_info 含 nWidth / nHeight / nFrameNum / timestamp / camera_sn 等。

    与旧的海康实现相比，这里不再手工做
    ``ctypes.string_at(pData, w*h) -> frombuffer -> cvtColor(BAYER_GB2RGB)`` 那条链：
      1. 旧的 buff_size 只按 w*h 算，等于假设每像素恒为 1 字节且不带 chunk data，
         像素格式一变就欠读/越界读；
      2. COLOR_BAYER_GB2RGB 是硬编码的，换一台 Bayer 相位不同的相机红蓝就反了；
      3. 末尾那次 cv2.resize(..., (nWidth, nHeight)) 尺寸根本没变，纯属每帧白拷
         一份 ~45MB，8 路相机下是实打实的内存与 GC 压力。
    现在统一交给 CameraOperation._to_bgr()（大恒 DxImageProc 转换 + OpenCV 兜底）。

    注意：这里**不再**提前检查 getIsStop()。停机判断统一下沉到
    image_control()，因为停机预览帧必须绕过该标志才能推给前端，
    详见 image_control() 内的说明。
    """
    # 这里原来每帧一条 debug(f"[原始回调] ...")。8 台相机 x 10Hz = 80 条/秒，
    # 且跑在 SDK 采集线程上：即使级别是 INFO 被过滤掉，旧日志器也已经先付了
    # 一次全栈回溯的钱。现在日志器加了级别预检，但热路径上仍然不该留每帧日志
    # —— 需要看逐帧节拍时用 get_diag_info() 的计数器，不要开 DEBUG。
    try:
        image_control(image, frame_info, frame_info.get("camera_sn", ""))
    except Exception as e:
        error(f"回调处理图像失败: {e}")
        import traceback
        traceback.print_exc()


# ==================================================================
# 模拟模式
# ==================================================================
def run_folder_simulation(folder_path: str, skip_fabric: bool = False, loop: bool = False):
    """
    仿真模式：从文件夹加载图片并模拟 11 路并行输入。

    支持两种目录结构：
    1. 分相机子目录: folder_path/camera1/*.png, folder_path/camera2/*.png ...
    2. 扁平目录: folder_path/*.png（自动轮流分配到 11 路相机）

    Args:
        folder_path: 图片文件夹路径
        skip_fabric: 是否跳过布幅检测
        loop: 是否循环检测（检测完再从头开始）
    """
    name_to_sn = {v: k for k, v in CAMERA_SN_MAP.items()}
    sn_list = list(CAMERA_SN_MAP.keys())  # 所有8个 SN
    camera_names = list(CAMERA_SN_MAP.values())  # camera1 .. camera8
    exts = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif"}

    # ---------- 检测目录结构（只做一次）----------
    subdirs = [d for d in sorted(os.listdir(folder_path))
               if os.path.isdir(os.path.join(folder_path, d))]
    has_camera_dirs = any(d in camera_names for d in subdirs)

    round_idx = 0  # 循环轮次计数

    while True:
        round_idx += 1
        total = 0

        if loop:
            info(f"[模拟] ===== 第 {round_idx} 轮开始 =====")

        if has_camera_dirs:
            # ===== 分相机子目录模式 =====
            if round_idx == 1:
                info("[模拟] 检测到分相机子目录模式")

            for cam_name in sorted(subdirs):
                if cam_name not in camera_names:
                    continue
                folder = os.path.join(folder_path, cam_name)
                sn = name_to_sn.get(cam_name, cam_name)
                imgs = sorted(f for f in os.listdir(folder)
                              if os.path.splitext(f)[1].lower() in exts)
                if round_idx == 1:
                    info(f"[模拟] {cam_name} (SN:{sn}): {len(imgs)} 张")

                for idx, f in enumerate(imgs, 1):
                    img = cv2.imread(os.path.join(folder, f))
                    if img is None:
                        continue
                    st = {
                        "nWidth": img.shape[1],
                        "nHeight": img.shape[0],
                        # 加入轮次信息，避免文件名冲突
                        "nFrameNum": f"r{round_idx:03d}_{idx:06d}_{os.path.splitext(f)[0]}",
                        "enPixelType": "sim",
                    }
                    _submit_sim_frame(img, st, sn, skip_fabric)
                    total += 1

        else:
            # ===== 扁平目录模式：每 0.7 秒提交一组 8 张 =====
            GROUP_SIZE = len(sn_list)
            GROUP_INTERVAL = 0.7  # 秒

            all_imgs = sorted(f for f in os.listdir(folder_path)
                              if os.path.splitext(f)[1].lower() in exts)
            num_groups = (len(all_imgs) + GROUP_SIZE - 1) // GROUP_SIZE

            if round_idx == 1:
                info(f"[模拟] 扁平目录模式: {len(all_imgs)} 张图片, "
                     f"{num_groups} 组 x {GROUP_SIZE} 路, 间隔 {GROUP_INTERVAL}s")

            for g in range(num_groups):
                group_start = g * GROUP_SIZE
                group_end = min(group_start + GROUP_SIZE, len(all_imgs))
                group_files = all_imgs[group_start:group_end]

                t_group_start = time.perf_counter()

                for local_idx, f in enumerate(group_files):
                    global_idx = group_start + local_idx
                    img = cv2.imread(os.path.join(folder_path, f))
                    if img is None:
                        continue
                    sn = sn_list[local_idx % GROUP_SIZE]
                    cam_name = CAMERA_SN_MAP[sn]
                    st = {
                        "nWidth": img.shape[1],
                        "nHeight": img.shape[0],
                        "nFrameNum": (f"r{round_idx:03d}_{global_idx + 1:06d}_"
                                      f"{os.path.splitext(f)[0]}"),
                        "enPixelType": "sim",
                    }
                    _submit_sim_frame(img, st, sn, skip_fabric)
                    total += 1

                t_group_end = time.perf_counter()
                group_ms = (t_group_end - t_group_start) * 1000
                info(f"[模拟] 第 {g + 1}/{num_groups} 组完成 "
                     f"({len(group_files)} 张, 提交耗时={group_ms:.1f}ms)")

                # 节拍控制：等待到 0.7 秒
                elapsed = t_group_end - t_group_start
                if elapsed < GROUP_INTERVAL and g < num_groups - 1:
                    time.sleep(GROUP_INTERVAL - elapsed)

        info(f"[模拟] 第 {round_idx} 轮完成，共 {total} 张")

        # ---------- 是否继续循环 ----------
        if not loop:
            break

        # 等待本轮所有检测任务处理完再开始下一轮，避免积压
        if _detection_manager is not None:
            info(f"[模拟] 等待第 {round_idx} 轮检测任务完成 ...")
            _detection_manager.wait_until_done(timeout=30)

        # 轮次间隔（可按需调整）
        ROUND_INTERVAL = 1.0  # 秒
        info(f"[模拟] {ROUND_INTERVAL}s 后开始第 {round_idx + 1} 轮 ...")
        time.sleep(ROUND_INTERVAL)

def _submit_sim_frame(image, st_frame_info, sn, skip_fabric=False):
    """提交单帧到检测管道（仿真用）— 不阻塞，立刻返回"""
    # 仿真模式下：缺陷停机自动重置继续（不等待人工恢复）
    #
    # 原实现写的是 ThriftControl.is_stop（兼容用的模块变量），而 getIsStop()
    # 读的是 _is_stop —— 赋值压根不生效，于是一旦触发过缺陷停机，这条
    # "自动重置继续" 会每帧刷一次日志却从未真正复位。改为走状态机 API。
    #
    # 注意只重置缺陷停机：手动停止（前端点"停止"）在仿真下同样必须保持，
    # 否则就没法验证停止逻辑了。
    if Communication_Tool.ThriftControl.isManualPaused():
        return
    if Communication_Tool.ThriftControl.getIsStop():
        info("[模拟] 缺陷停机，自动重置继续")
        Communication_Tool.ThriftControl.setIsStop(False, "恢复", "Sim")

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    camera_name = CAMERA_SN_MAP.get(sn)
    frame_number = st_frame_info["nFrameNum"]

    # 布幅检测（可跳过）
    if not skip_fabric:
        fabric_detector = get_fabric_detector()
        if fabric_detector is not None:
            fabric_detector.process_frame(image, camera_name)

    # ====== YOLO 缺陷检测（移到外面，不管是否跳过布幅检测都执行）======

    if _detection_manager is not None:
        _detection_manager.detect(
            image=image, filename=f"{sn}_{frame_number}",
            camera_name=camera_name, camera_sn=sn,
            capture_timestamp=ts,
            metadata={"camera_sn": sn, "frame": frame_number})

    # ====== 推送到 WebSocket（原图兜底）======
    ws_server = get_websocket_server()
    if ws_server is not None:
        ws_server.push_image(camera_name, image)

def _wait_if_stopped():
    """等待停机恢复"""
    printed = False
    while Communication_Tool.ThriftControl.getIsStop():
        if not printed:
            info("[模拟] 已停机，等待手动恢复 ...")
            printed = True
        time.sleep(0.5)
    if printed:
        info("[模拟] 已恢复，继续检测")


# ==================================================================
# 设备管理（大恒 Galaxy SDK）
# ==================================================================
def _release_device_manager() -> None:
    """释放全局 DeviceManager。

    gxipy 把 gx_init_lib()/gx_close_lib() 挂在 DeviceManager 的
    __new__/__del__ 上，所以"反初始化"就是丢掉最后一个引用。
    """
    global _device_manager
    _device_manager = None
def Open_all_devices():
    global obj_cam_operation
    ok, fail = [], []
    for c in obj_cam_operation:
        ret = c.Open_device()
        (ok if ret == 0 else fail).append(c)
    if fail:
        error("以下设备打开失败:")
        for c in fail:
            error(f"  {c.st_mode_name}")
    obj_cam_operation = ok
    info(f"打开 {len(ok)} 成功, {len(fail)} 失败")


def Regist_callback_all_devices():
    global obj_cam_operation
    ok, fail = [], []
    for c in obj_cam_operation:
        ret = c.Registration_callback(on_camera_frame)
        (ok if ret == 0 else fail).append(c)
    if fail:
        error("以下设备回调注册失败:")
        for c in fail:
            error(f"  {c.st_mode_name}")
    obj_cam_operation = ok
    info(f"回调注册 {len(ok)} 成功, {len(fail)} 失败")


def Start_all_devices():
    global obj_cam_operation
    ok, fail = [], []
    for c in obj_cam_operation:
        ret = c.Start_grabbing()
        (ok if ret == 0 else fail).append(c)
    if fail:
        error("以下设备开启取图失败:")
        for c in fail:
            error(f"  {c.st_mode_name}")
    obj_cam_operation = ok
    info(f"取图启动 {len(ok)} 成功, {len(fail)} 失败")


def Stop_all_devices():
    global obj_cam_operation
    ok, fail = [], []
    for c in obj_cam_operation:
        ret = c.Stop_grabbing()
        (ok if ret == 0 else fail).append(c)
    if fail:
        error("以下设备关闭取图失败")
    obj_cam_operation = ok
    info(f"取图停止 {len(ok)} 成功, {len(fail)} 失败")


def Close_all_devices():
    ok, fail = [], []
    for c in obj_cam_operation:
        ret = c.Close_device()
        (ok if ret == 0 else fail).append(c)
    if fail:
        error("以下设备关闭失败")
    info(f"设备关闭 {len(ok)} 成功, {len(fail)} 失败")


# ==================================================================
# YOLO 检测初始化 / 关闭
# ==================================================================
def Init_yolo_detector() -> bool:
    global _detection_manager
    try:
        load_chinese_font()
        os.makedirs(DETECTION_SAVE_PATH, exist_ok=True)
        os.makedirs(MACHINE_STOP_PATH, exist_ok=True)

        _detection_manager = YOLODetectionManager(
            config=YOLO_CONFIG,
            save_path=DETECTION_SAVE_PATH,
            save_config=SAVE_CONFIG,
            class_thresholds=CLASS_CONFIDENCE_THRESHOLDS,
            default_class_threshold=DEFAULT_CLASS_CONFIDENCE,
            machine_stop_path=MACHINE_STOP_PATH,
        )

        if not _detection_manager.initialize():
            return False
        info(f"结果: {DETECTION_SAVE_PATH}")
        info(f"停机: {MACHINE_STOP_PATH}")

        # 注册「开始」按钮回调：每次前端点「开始」时，
        # 刷新批次标签 launch_tag，让本次疵点/停机图写入独立子目录
        Communication_Tool.ThriftControl.register_on_start(
            _detection_manager.reset_launch_tag)
        info("已注册『开始』回调：刷新批次目录标签 launch_tag")
        return True
    except Exception as e:
        error(f"YOLO初始化错误: {e}")
        import traceback
        traceback.print_exc()
        return False


def Shutdown_yolo_detector():
    global _detection_manager
    if _detection_manager:
        _detection_manager.shutdown()
        _detection_manager = None


# ==================================================================
# 布幅检测初始化
# ==================================================================
def Init_fabric_detector():
    _init_fabric_service(FABRIC_WIDTH_CONFIG)
    if FABRIC_WIDTH_CONFIG.get("enabled"):
        info("[布幅检测] 初始化完成")
    else:
        info("[布幅检测] 未启用")


def Init_relay():
    """启动阶段解析继电器串口，把"用的是哪个口"直接写进开机日志"""
    try:
        from Communication_Tool.DefectTrigger import init_relay
        init_relay()
    except ImportError as e:
        error(f"[继电器] 模块不可用: {e}")


def Init_thrift():
    global java_client
    java_client = PythonJavaClient()
    start_python_server()

def SetConfig() -> SystemConfig:
    """从 config.yaml 的 plc 段构造触发系统配置（此前这些参数硬编码在源码里）"""
    scaling_factor = float(PLC_CONFIG.get("scaling_factor", 0.78651))
    return SystemConfig(
        plc_host=PLC_CONFIG.get("host", "192.168.123.70"),
        plc_port=int(PLC_CONFIG.get("port", 502)),
        wheel_diameter=float(PLC_CONFIG.get("wheel_diameter", 60.0)),
        encoder_ppr=int(PLC_CONFIG.get("encoder_ppr", 2000)),
        trigger_distance=float(
            PLC_CONFIG.get("trigger_distance_mm", 102)) * scaling_factor,
        scaling_factor=scaling_factor,
        read_interval=float(PLC_CONFIG.get("read_interval_ms", 100)) / 1000.0,
        speed_threshold_ratio=float(
            PLC_CONFIG.get("speed_threshold_ratio", 0.0)),
        # 停机预览相关参数，可按现场织机启停特性调整
        idle_speed_threshold=float(
            PLC_CONFIG.get("idle_speed_threshold", 0.5)),
        idle_confirm_duration=float(
            PLC_CONFIG.get("idle_confirm_duration", 3.0)),
        preview_interval=float(PLC_CONFIG.get("preview_interval", 1.0)),
        fabric_upload_interval=float(
            PLC_CONFIG.get("fabric_upload_interval", 5.0)),
    )

# ==================================================================
# 主程序
# ==================================================================
# ==================================================================
# 启动流程
# ==================================================================
def _init_logging() -> None:
    """按 config.yaml 的 logging 段初始化日志"""
    level_name = str(LOGGING_CONFIG.get("level", "DEBUG")).upper()
    init_logger(
        log_file=LOGGING_CONFIG.get("file", "logs/app.log"),
        level=getattr(logging, level_name, logging.DEBUG),
        max_bytes=int(LOGGING_CONFIG.get("max_bytes", 5 * 1024 * 1024)),
        backup_count=int(LOGGING_CONFIG.get("backup_count", 3)),
    )


def _camera_diag_provider() -> dict:
    """把各相机的帧数/丢帧数并进健康快照"""
    out = {}
    for name, cam_op in CameraRegistry.get_all_cameras().items():
        try:
            d = cam_op.get_diag_info()
        except Exception:
            continue
        out[f"{name}.frames"] = d.get("frame_count", 0)
        out[f"{name}.bad"] = d.get("bad_frame_count", 0)
        lost = (d.get("stream") or {}).get("lost")
        if lost is not None:
            out[f"{name}.lost"] = lost
    return out


def _detection_diag_provider() -> dict:
    """把检测队列深度并进健康快照 —— 顶满说明下游吞吐跟不上采集"""
    if _detection_manager is None:
        return {}
    try:
        pool = getattr(_detection_manager, "worker_pool", None)
        if pool is None:
            return {}
        return {"infer_q": pool.qsize}
    except Exception:
        return {}


def _init_health_monitor() -> None:
    """启动健康心跳（卡死取证用，见 General_Tool/HealthMonitor.py）"""
    init_health_monitor(
        interval=float(LOGGING_CONFIG.get("health_interval", 30) or 0),
        rss_warn_mb=float(LOGGING_CONFIG.get("health_rss_warn_mb", 0) or 0),
        thread_warn=int(LOGGING_CONFIG.get("health_thread_warn", 0) or 0),
        providers=[_camera_diag_provider, _detection_diag_provider],
    )


def _print_stop_rules() -> None:
    """打印当前生效的停机规则"""
    info("=" * 60)
    info("运行中（大恒 Galaxy SDK）| 停机规则:")
    for cls, rule in MACHINE_STOP_RULES.get("rules", {}).items():
        # rule 为 None 表示该类别禁用
        if rule is None or not rule.get("enabled", True):
            info(f"  {cls}: 已禁用")
            continue

        trigger_mode = rule.get("trigger_mode", "area")  # 默认按面积
        conf = rule.get("min_confidence", 0.85)

        if trigger_mode == "dimension":
            min_w = rule.get("min_width_mm", 0)
            min_h = rule.get("min_height_mm", 0)
            parts = [f"conf>={conf}"]
            if min_w > 0:
                parts.append(f"宽>={min_w}mm")
            if min_h > 0:
                parts.append(f"高>={min_h}mm")
        else:
            parts = [
                f"conf>={conf}",
                f"area>={rule.get('min_area_mm2', 80)}mm²",
            ]
            gr = rule.get("min_gradient_ratio")
            if gr:
                parts.append(f"GR>={gr}")

        info(f"  {cls}: {' + '.join(parts)}")

    info("按 'q' 退出")
    info("=" * 60)


def _shutdown_common() -> None:
    """两种模式共用的收尾流程"""
    shutdown_health_monitor()
    shutdown_websocket_server()
    _fabric_executor.shutdown(wait=False)
    _aux_executor.shutdown(wait=False)
    Shutdown_yolo_detector()
    # 关掉卡死诊断的转储文件（原来这个句柄一直不关）
    if _dump_file is not None:
        try:
            faulthandler.cancel_dump_traceback_later()
            _dump_file.close()
        except Exception:
            pass
    info(f"退出: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


def run_simulation_mode() -> int:
    """
    仿真模式：从 simulation.folder 读图回放。

    此前这段逻辑被 `if False:` 硬关掉，要跑仿真必须改源码；
    现在由 config.yaml 的 simulation.mode 控制。
    """
    info(f"[模拟] 数据目录: {SIMULATION_FOLDER}")
    if not os.path.isdir(SIMULATION_FOLDER):
        error(f"[模拟] 目录不存在: {SIMULATION_FOLDER}")
        _shutdown_common()
        return 1

    try:
        # loop=True 开启无限循环回放
        run_folder_simulation(SIMULATION_FOLDER, loop=True)

        # loop=True 时不会执行到这里；loop=False 才走收尾统计
        if _detection_manager is not None:
            info("[模拟] 等待所有检测任务完成 ...")
            _detection_manager.wait_until_done(timeout=10)
            info("[模拟] 全部处理完毕")

        fs = get_fabric_stats()
        if fs.get("enabled"):
            fw = fs.get("last_fabric_width")
            if fw is not None:
                info("[模拟] 最终布幅: {:.2f} cm".format(fw))
            else:
                info("[模拟] 布幅数据不足，无法计算")

        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        info("[模拟] 用户手动退出")

    _shutdown_common()
    return 0


def run_production_mode() -> int:
    """生产模式：连接大恒相机，硬触发采图 + 检测。"""
    global _device_manager

    info("\n系统启动（大恒 Galaxy SDK + YOLO检测 + 布幅检测）")

    # ==================== 大恒 SDK 初始化 ====================
    # DeviceManager 的构造里就会调用 gx_init_lib()（等价于海康的
    # MV_CC_Initialize），所以不再需要单独的一次"初始化"调用；
    # 对应的 gx_close_lib() 在最后一个 DeviceManager 被回收时执行，
    # 见 _release_device_manager()。
    try:
        _device_manager = gx.DeviceManager()
    except Exception as e:
        error(f"大恒 GxIAPI 初始化失败: {e}\n"
              f"请确认已安装大恒 Galaxy 相机驱动，且环境变量 "
              f"GALAXY_GENICAM_ROOT 指向 SDK 安装目录。")
        Shutdown_yolo_detector()
        return 1

    try:
        enum_timeout = int(CAMERA_ACQ_CONFIG.get("enum_timeout_ms", 2000))
        dev_num, dev_info_list = _device_manager.update_all_device_list(enum_timeout)
    except Exception as e:
        error(f"枚举设备失败: {e}")
        _release_device_manager()
        Shutdown_yolo_detector()
        return 1

    if not dev_num:
        error("未找到任何相机设备!")
        _release_device_manager()
        Shutdown_yolo_detector()
        return 1

    info(f"找到 {dev_num} 个设备")

    # 创建目标相机操作对象
    for dev_info in (dev_info_list or []):
        strSN = str(dev_info.get("sn", "") or "")
        if strSN not in TARGET_SERIALS:
            continue
        # 传入 camera_name，使 CameraOperation.Open_device() 注册到
        # CameraRegistry 时用业务名(camera1/camera2...)而非退化用序列号
        obj_cam_operation.append(CameraOperation(
            _device_manager, dev_info,
            camera_name=CAMERA_SN_MAP[strSN],
            buffer_count=int(CAMERA_ACQ_CONFIG.get("buffer_count", 10)),
            packet_size=int(CAMERA_ACQ_CONFIG.get("packet_size", 0)),
            heartbeat_timeout_ms=int(CAMERA_ACQ_CONFIG.get("heartbeat_timeout_ms", 0)),
            trigger_source=str(CAMERA_ACQ_CONFIG.get("trigger_source", "Line0")),
        ))
        info(f"  添加相机 {strSN} -> {CAMERA_SN_MAP[strSN]} "
             f"({dev_info.get('model_name', '')} @ {dev_info.get('ip', '')})")

    if not obj_cam_operation:
        error("未找到目标相机设备")
        _release_device_manager()
        Shutdown_yolo_detector()
        return 1

    info(f"共添加 {len(obj_cam_operation)} 个目标相机")

    # 只创建一次 controller；BTM 不再自己构造
    controller = TriggerControlSystem(SetConfig())
    task_manager = BackgroundTaskManager(obj_cam_operation,
                                         controller=controller)

    # 打开 → 注册回调 → 启动取图
    # Open_all_devices() 内部（CameraOperation.Open_device）会自动把相机
    # 注册到 CameraRegistry，供 TriggerControlSystem 在织机停机时统一切换触发源
    Open_all_devices()
    Regist_callback_all_devices()
    Start_all_devices()
    task_manager.start_all_tasks()

    _print_stop_rules()

    press_any_key_exit()

    # ==================== 关闭流程 ====================
    info("=" * 60)
    info("系统关闭中...")
    info("=" * 60)

    task_manager.shutdown()
    Stop_all_devices()
    Close_all_devices()  # 内部会自动从 CameraRegistry 注销所有相机

    # 大恒 SDK 清理（最后一个 DeviceManager 被回收时执行 gx_close_lib）
    _release_device_manager()

    _shutdown_common()
    return 0


def main() -> int:
    Init_thrift()
    _init_logging()

    Communication_Tool.ThriftControl.setIsStop(False, "开始", "Main")

    Init_fabric_detector()
    Init_relay()

    if not Init_yolo_detector():
        error("YOLO检测模块初始化失败，退出程序")
        return 1

    init_websocket_server(
        host=WEBSOCKET_CONFIG.get("host", "0.0.0.0"),
        port=int(WEBSOCKET_CONFIG.get("port", 8765)),
        preview_max_fps=float(WEBSOCKET_CONFIG.get("preview_max_fps", 4.0)),
        preview_max_width=int(WEBSOCKET_CONFIG.get("preview_max_width", 960)),
        jpeg_quality=int(WEBSOCKET_CONFIG.get("jpeg_quality", 70)),
    )

    _init_health_monitor()

    # 运行模式由 config.yaml 的 simulation.mode 决定
    if SIMULATION_MODE:
        return run_simulation_mode()
    return run_production_mode()


if __name__ == "__main__":
    sys.exit(main())
