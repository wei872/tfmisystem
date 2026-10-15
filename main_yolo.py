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
from General_Tool.EnhancedLogger import init_logger, info, debug, warning, error
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
    NORMAL_IMAGE_SAVE,
    SPEED_GUARD,
    MULTILINE_EDGE_LEARNING,
    PLC_CONFIG, WEBSOCKET_CONFIG, LOGGING_CONFIG, CONCURRENCY_CONFIG,
    DEBUG_CONFIG,
)
from AnomalyDetection_Tool.core.detection_manager import YOLODetectionManager
from AnomalyDetection_Tool.utils.font_utils import load_chinese_font
from AnomalyDetection_Tool.output.fabric_width_service import (
    init_fabric_detector as _init_fabric_service,
    get_fabric_detector,
    get_fabric_stats,
)
from AnomalyDetection_Tool.services.normal_image_saver import init_normal_saver
from General_Tool.SpeedGuard import init_speed_guard, get_speed_guard
from AnomalyDetection_Tool.filter.edge_region_learner import (
    init_edge_learner)
from AnomalyDetection_Tool.output.encoder_state_service import (
    get_encoder_state)
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

    本函数运行在大恒 SDK 的采集线程上，**任何阻塞都会造成相机丢帧**。
    因此这里只做分发：布幅检测、调试落盘、Java 推帧全部投递到线程池，
    YOLO 检测本身由 DetectionWorkerPool 内部排队消化。

    :param image:      BGR / uint8 / HxWx3，只读（可以安全跨线程传递，
                       但要原地修改必须先 copy）
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
      - image 已经是 BGR / uint8 / HxWx3，生命周期不依赖 SDK 环形缓冲
        （gxipy 构造时已 string_at 拷成 Python bytes），可安全跨线程传递；
        但它是**只读**的，要在上面画框/改像素必须先 copy；
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
        # 距上一次出图多少秒（-1 = 这台相机从未出过图）。
        # **这是判断"相机还在不在出图"最直接的指标**：frames 是累计值，
        # 要跨两条心跳相减才知道涨没涨；frame_age_s 是瞬时值，一条心跳
        # 就能看出这台相机是不是已经断了。
        #
        # 为什么必须加：现场排查过一次"突然没有图片进来"，Modbus 侧
        # [触发] 日志一路正常、速度正常、触发源仍是 Line0，但 8 台相机的
        # frames 同时冻结在 712。断点在 PLC→Line0→相机 这一段，Python 侧
        # 完全健康（submitted/processed 同步增长、零丢帧）。
        # 而 [触发] 第 N 次 那条日志**只代表 send_trigger() 这个 Modbus 写
        # 成功了**（写 VW4=1），不代表 PLC 真的脉冲了 Line0、更不代表相机
        # 真的曝光出图 —— 只看它会误判成"相机在出图"。
        _lft = d.get("last_frame_time", 0.0) or 0.0
        out[f"{name}.frame_age_s"] = (round(time.time() - _lft, 1)
                                      if _lft > 0 else -1.0)
        # 超速守卫钳掉的帧数。**非零就意味着这台相机已经确定性漏检了**，
        # 现场必须能一眼看到 —— 现场定的是"宁可漏检也要保护机器"，
        # 但"漏了多少"不能是笔糊涂账。
        spd = d.get("speed_dropped", 0)
        if spd:
            out[f"{name}.speed_capped"] = spd
        stream = d.get("stream") or {}
        lost = stream.get("lost")
        if lost is not None:
            out[f"{name}.lost"] = lost
        # incomplete 是"包长设得比链路能承载的大"的直接特征：包被丢弃后
        # 帧收不全。它和 lost 一起看能区分是带宽不够还是包长不对。
        incomplete = stream.get("incomplete")
        if incomplete is not None:
            out[f"{name}.incomplete"] = incomplete
        # 包长实测结果。applied 明显小于 mtu、或 note 非空，
        # 说明包长被降级过，需要按 note 里的提示处理。
        out[f"{name}.mtu"] = d.get("link_mtu", 0)
        out[f"{name}.pkt"] = d.get("packet_size_applied", 0)
        note = d.get("packet_size_note") or ""
        if note:
            out[f"{name}.pkt_note"] = note
    return out


def _speed_guard_diag_provider() -> dict:
    """把超速守卫的状态并进健康快照。

    钳帧是**确定性漏检**，而现场选择的是"宁可漏检也要保护机器"。
    选择可以做，但必须看得见：这里报出当前是否钳帧、判定用的车速、
    钳到多少帧率、以及钳帧后布面有没有盲区、盲区多宽。
    """
    try:
        sg = get_speed_guard()
        if sg is None:
            return {}
        st = sg.snapshot()
        out = {
            "speed_guard.capping": int(bool(st["capping"])),
            "speed_guard.speed_mpm": st["filtered_mpm"],
            "speed_guard.cap_fps": st["cap_fps"],
            "speed_guard.capped_frames": st["stats"]["frames_capped"],
        }
        if st["capping"]:
            # 只在钳帧时报覆盖情况，正常时这些数没有意义
            covered = st["accept_gap_mm"] <= st["frame_height_mm"]
            out["speed_guard.coverage_ok"] = int(covered)
            out["speed_guard.accept_gap_mm"] = st["accept_gap_mm"]
            if not covered:
                out["speed_guard.blind_strip_mm"] = round(
                    st["accept_gap_mm"] - st["frame_height_mm"], 1)
        return out
    except Exception:
        return {}


def _detection_diag_provider() -> dict:
    """
    把检测侧的吞吐/丢帧指标并进健康快照。

    只报 infer_q 是不够的：队列深度是"正在积压"的瞬时值，采样错开就看不到；
    而 dropped / skipped 是累计计数，一旦非零就说明**已经真的漏检了**。
    这两类必须一起报，否则现场无法回答"到底有没有漏检"。

    各计数含义（见 DetectionWorkerPool._counters）：
      dropped           总丢帧数 = memory_dropped + semaphore_dropped + 队列溢出
      skipped           skip_old_frames 去重丢掉的同相机旧帧数
      semaphore_dropped 在途帧数顶到 max_inflight_frames 而被拒 —— 吞吐不足的主因
      memory_dropped    内存水位过高主动丢帧
      submitted/processed 提交与完成数，两者长期背离即为积压
    """
    if _detection_manager is None:
        return {}
    try:
        pool = getattr(_detection_manager, "worker_pool", None)
        if pool is None:
            return {}
        snap = {"infer_q": pool.qsize}
        try:
            st = pool.get_stats()
            for k in ("submitted", "processed", "dropped", "skipped",
                      "semaphore_dropped", "memory_dropped"):
                if k in st:
                    snap[k] = st[k]
        except Exception:
            # 取不到计数不影响队列深度这一项
            pass
        return snap
    except Exception:
        return {}


def _init_health_monitor() -> None:
    """启动健康心跳（卡死取证用，见 General_Tool/HealthMonitor.py）"""
    init_health_monitor(
        interval=float(LOGGING_CONFIG.get("health_interval", 30) or 0),
        rss_warn_mb=float(LOGGING_CONFIG.get("health_rss_warn_mb", 0) or 0),
        thread_warn=int(LOGGING_CONFIG.get("health_thread_warn", 0) or 0),
        pool_growth_warn_mb=float(
            LOGGING_CONFIG.get("health_pool_growth_warn_mb", 512) or 0),
        providers=[_camera_diag_provider, _detection_diag_provider,
                   _speed_guard_diag_provider],
    )


def _format_stop_rule_parts(rule: dict) -> str:
    """把一条停机规则渲染成 ``conf>=0.7 + 宽>=30mm + 高>=30mm`` 这样的串。

    全局和按相机两段打印共用同一个渲染，保证"看到的"和"生效的"是同一套逻辑。
    """
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
    return " + ".join(parts)


def _print_stop_rules() -> None:
    """打印当前生效的停机规则，并把**与全局不同的那几台相机**单独列出来。

    停机规则现在可以按相机覆盖（camera_stop_rule_overrides）。覆盖之后
    "改全局"对已覆盖的相机不再生效，这个后果必须在启动时就看得见，
    不能等到现场发现某台相机行为对不上。
    """
    from AnomalyDetection_Tool.config.settings import (
        describe_camera_stop_rules, check_camera_stop_rule_impact,
        REPEAT_STOP_RULES, repeat_stop_allowed,
    )

    info("=" * 60)
    info("运行中（大恒 Galaxy SDK）| 停机规则:")

    global_rules = MACHINE_STOP_RULES.get("rules", {}) or {}
    for cls, rule in global_rules.items():
        # rule 为 None 表示该类别禁用
        if not isinstance(rule, dict) or not rule.get("enabled", True):
            info(f"  {cls}: 已禁用")
            continue
        info(f"  {cls}: {_format_stop_rule_parts(rule)}")

    names = sorted(set(CAMERA_SN_MAP.values()))
    changed = check_camera_stop_rule_impact(names)

    info("-" * 60)
    if not changed:
        info("按相机停机规则：8 台与全局完全一致"
             "（未配置或与 machine_stop_rules 逐项相同）")
    else:
        info(f"以下相机的停机规则**与全局不同**（共 {len(changed)} 处）：")
        for c in changed:
            info(f"  {c['camera']} / {c['class_name']} / {c['field']}: "
                 f"{c['global_value']} → {c['camera_value']}，{c['effect']}")
            # 必须同时报有效门槛：光看停机字段会误导。把停机 0.7 降到 0.5，
            # 若该相机检测阈值是 0.65，真实门槛只从 0.7 掉到 0.65。
            info(f"      ↳ {c['gate_effect']}"
                 f"（该相机 {c['class_name']} 检测阈值 {c['det_threshold']}）")
        # 只把真正被改过的相机整段打出来，避免 8 台全量刷屏
        for name in sorted({c["camera"] for c in changed}):
            info(f"  [{name}] 实际生效：")
            for cls, eff in describe_camera_stop_rules(name).items():
                if eff is None:
                    info(f"    {cls}: 已禁用")
                else:
                    mark = "  ← 已覆盖" if eff.get("<camera_override>") else ""
                    info(f"    {cls}: {_format_stop_rule_parts(eff)}{mark}")
        info("  注意：已覆盖的项**不再跟随全局 machine_stop_rules 的修改**，"
             "要改请改对应相机那一段。")

    # ---- 【三】重复触发：哪几台参与 ----
    # 必须打出来：这个功能是"攒够 N 次才停"，现场看不出它有没有在跑。
    # 尤其"只有部分相机参与"这件事，不打日志根本无从确认。
    info("-" * 60)
    if not REPEAT_STOP_RULES.get("enabled", False):
        info("【三】重复触发停机：已关闭（repeat_stop_rules.enabled=false）")
    else:
        parts = []
        for cls, r in (REPEAT_STOP_RULES.get("rules", {}) or {}).items():
            if not isinstance(r, dict) or not r.get("enabled", True):
                continue
            # min_confidence 没写时不是"不限"——能走到这里的框都已经过了
            # 检测阈值，所以实际含义是"跟随检测阈值"。写成"不限"会误导。
            mc = r.get("min_confidence")
            mc_txt = f"conf≥{mc}" if mc is not None else "conf 跟随检测阈值"
            parts.append(f"{cls}（{int(r.get('count', 0))} 次 / "
                         f"{float(r.get('window_sec', 0)):.1f}s / {mc_txt}）")
        joined = [n for n in names if repeat_stop_allowed(n)]
        skipped = [n for n in names if not repeat_stop_allowed(n)]
        if not parts:
            info("【三】重复触发停机：已开启但没配任何类别规则，实际不生效")
        elif not joined:
            info("【三】重复触发停机：已配规则，但 stop_allowed_cameras "
                 "里没有任何在用的相机，实际不生效")
        else:
            info(f"【三】重复触发停机：{'；'.join(parts)}")
            info(f"      参与相机：{', '.join(joined)}")
            if skipped:
                info(f"      不参与（连计数都不做）：{', '.join(skipped)}")
            info("      与【二】单次停机并存，任一条满足即停机")

    info("按 'q' 退出")
    info("=" * 60)


def _print_camera_thresholds() -> None:
    """打印每台相机**实际生效**的检测阈值，并交叉校验停机行为是否被改变。

    两件事必须在启动时就看清楚：

    1. **相机级 default 会盖掉全局的按类阈值**。给 camera3 配 0.15，全局的
       「油点线: 0.85」在 camera3 上也变成 0.15。
    2. **降检测阈值可能连带改变停机行为**。停机判定吃的是过滤后的框
       （detection_manager 里 `for box in result.boxes`，而 result.boxes 是
       _filter_boxes 的输出），所以：

           有效停机门槛 = max(该相机检测阈值, 该类停机 min_confidence)

       检测阈值高于停机阈值时，它是实际把关的那道，降它就会让停机变频繁；
       检测阈值低于停机阈值时，停机阈值把关，怎么降都不影响停机。
       例：多股线 检测 0.65 / 停机 0.70 → 有效门槛 0.70，把检测降到 0.2
       有效门槛仍是 0.70，停机行为不变。而油点线 检测 0.85 / 停机 0.50 →
       有效门槛 0.85，把检测降到 0.3 会让有效门槛掉到 0.50，停机会变频繁。
    """
    from AnomalyDetection_Tool.config.settings import (
        CLASS_CONFIDENCE_THRESHOLDS, CAMERA_THRESHOLD_OVERRIDES,
        get_default_class_confidence, describe_camera_thresholds,
        check_camera_threshold_stop_impact, stop_rule_min_confidence,
    )
    from AnomalyDetection_Tool.services.stop_machine_service import (
        StopMachineService,
    )

    overrides = dict(CAMERA_THRESHOLD_OVERRIDES or {})
    global_det = dict(CLASS_CONFIDENCE_THRESHOLDS or {})
    g_default = get_default_class_confidence()

    def _stop_conf(cls, cam=None):
        # 停机规则现在也能按相机覆盖，所以这里必须带相机名。
        # 不带的话交叉校验会拿全局停机值去比，算出的有效门槛是错的。
        # settings 里用 inspect 探测签名，两个参数才会被传进来。
        rule = StopMachineService.get_stop_rule(cls, cam)
        if not rule:
            return None
        # 用 stop_rule_min_confidence 而不是 .get("min_confidence")：
        # 规则里没写 min_confidence 时后者返回 None，会被当成"没有停机规则"，
        # 于是漏报。默认值 area 0.85 / dimension 0.5 由那个函数按 trigger_mode 补。
        return stop_rule_min_confidence(rule)

    names = sorted(set(CAMERA_SN_MAP.values()))

    info("=" * 60)
    if not overrides:
        info("[阈值] 未配置 camera_threshold_overrides，"
             f"8 台相机全部使用全局检测阈值 (default={g_default})")
    else:
        info("[阈值] 按相机覆盖已启用，以下为各相机**实际生效**的检测阈值：")
        shadowed = []
        for name in names:
            eff = describe_camera_thresholds(name, CLASS_CONFIDENCE_THRESHOLDS)
            if name in overrides:
                keys = ", ".join(f"{k}={v}" for k, v in
                                 sorted(eff.items(), key=lambda x: str(x[0])))
                info(f"  {name:<9} [已覆盖] {keys}")
                if "<default>" in eff:
                    shadowed.append(name)
            else:
                info(f"  {name:<9} [全局]   default={g_default}")
        # 只在真的有相机配了 default 时才警告。原来这条是无条件打印的，
        # 于是"一切正常"时也顶着 ，既让人以为出了事，又会淹没下面
        # 真正需要看的交叉校验结论。
        if shadowed:
            info(f"  {', '.join(shadowed)} 配了相机级 default_class_confidence："
                 "该相机上所有**未逐类列出**的类都会改用这个默认值，"
                 "全局的按类阈值被盖掉，请确认这是你的意图。")
        else:
            info("  无相机配置 default_class_confidence，"
                 "全局的按类阈值在各台均完整生效")

    # ---- 交叉校验：有效停机门槛是否被改变 ----
    # 有效停机门槛 = max(检测阈值, 停机 min_confidence)，因为停机判定吃的是
    # 过滤后的框。只有这个 max 值变了，停机行为才会变。
    changed = check_camera_threshold_stop_impact(
        names, global_det, g_default, _stop_conf)

    info("-" * 60)
    if changed:
        info("以下覆盖**改变了停机行为**"
             "（有效停机门槛 = max(检测阈值, 停机阈值)）：")
        for c in changed:
            info(f"  {c['camera']} / {c['class_name']}: "
                 f"检测阈值 {c['det_before']}→{c['det_after']}, "
                 f"停机阈值 {c['stop_conf']} → "
                 f"有效门槛 {c['gate_before']}→{c['gate_after']}, {c['effect']}")
        info("  若不希望影响停机，请把该类检测阈值保持在停机阈值之下"
             "（此时由停机阈值把关），或同步调整 machine_stop_rules。")
    else:
        info("交叉校验：以上覆盖均未改变停机行为"
             "（各类的有效停机门槛 max(检测阈值, 停机阈值) 保持不变）")
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

    # ==================== 网卡过滤驱动检查 ====================
    # 必须在开相机**之前**做：两个 GigE 过滤驱动挂同一张网卡是整机网络栈
    # 卡死的高频原因，而这种卡死从 Python 侧完全看不出来（进程还活着，
    # 整台机器不动了）。趁还能打日志的时候先说清楚。
    # 内部保证不抛异常、不阻塞——查不到就只是日志里说没查成。
    # ==================== 触发突发探针 ====================
    # 必须在开相机之前启动：探针要在第一帧到达之前就绪，否则漏掉最初的突发。
    # 内部保证不抛异常、不阻塞；关掉时 record_arrival 直接 return。
    if DEBUG_CONFIG.get("burst_probe", True):
        try:
            from General_Tool.TriggerBurstProbe import init_probe
            init_probe(
                enabled=True,
                report_interval=float(DEBUG_CONFIG.get("burst_report_interval", 30)),
                min_burst_cameras=int(DEBUG_CONFIG.get("burst_min_cameras", 2)))
        except Exception as _e:
            warning(f"[触发突发] 探针初始化失败（已忽略，不影响启动）: {_e!r}")

    if LOGGING_CONFIG.get("nic_filter_check", True):
        from General_Tool.NicFilterCheck import check_nic_filter_drivers
        _nic_rep = check_nic_filter_drivers(
            gige_patterns=LOGGING_CONFIG.get("nic_filter_patterns"))

        # 硬性拦截：确认有冲突就**拒绝启动**。
        #
        # 判定门槛很关键 —— 只在**确实查到了、且确认有冲突**时才拦：
        #   conflicts 非空  ⇒  一定是 checked=True（analyse_nic_filters 在
        #                       没查到数据时返回的 conflicts 恒为空）
        #   读不到注册表    ⇒  conflicts 为空 ⇒ **不拦**，只有一条"没查成"的日志
        # 后者必须放过：否则非 Windows 开发机、以及权限不足读不到注册表的机器
        # 会永远起不来，而这跟"装了两个过滤驱动"是两回事。
        _conflicts = _nic_rep.get("conflicts") or []
        if _conflicts:
            _lines = ["拒绝启动：检测到同一张网卡上挂了多个 GigE Vision "
                      "过滤驱动。"]
            for _c in _conflicts:
                _lines.append(f"    网卡 {_c.get('nic')} "
                              f"({_c.get('driver_desc')}):")
                # 只列**启用中**的 —— 已禁用的不算冲突（见 analyse_nic_filters）
                for _f in (_c.get("gige_active") or _c.get("gige_filters") or []):
                    _lines.append(f"      - {_f.get('service')}  "
                                  f"({_f.get('display_name')})")
                _dis = _c.get("gige_disabled") or []
                if _dis:
                    _lines.append(f"      （另有 {len(_dis)} 个同类驱动**已禁用**，"
                                  f"不算冲突）：")
                    for _f in _dis:
                        _lines.append(f"      - {_f.get('service')}  "
                                      f"({_f.get('display_name')})  [已禁用]")
            _lines += [
                "",
                "  这是整机网络栈卡死 / 电脑死机的高频原因。**取消勾选即可，"
                "不必卸载**：",
                "    Win+R → ncpa.cpl → 对上面每张网卡右键 → 属性 → "
                "只留 'GigE Vision LightWeight Filter'（大恒 Galaxy），",
                "    取消勾选其余所有 GigE Vision 过滤驱动 → 确定。",
                "    取消勾选会把 Enabled 置为 False，驱动就不再在收包路径上；"
                "绑定项仍会出现在清单里，但会标成 [已禁用]，不再算冲突。",
                "",
                "  也可以用命令行代劳（停产时做，网卡会短暂 reset、相机断线重连）：",
                "    python -m General_Tool.NicFilterCheck          # 先预演看计划",
                "    python -m General_Tool.NicFilterCheck --yes    # 以管理员身份执行",
                "  详见 docs/SDK迁移_海康转大恒.md。",
            ]
            error("\n".join(_lines))
            Shutdown_yolo_detector()
            return 1


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
            # 触发错峰步长（微秒）。0 = 不错峰。见 config.yaml 里的代价说明。
            trigger_stagger_us=int(CAMERA_ACQ_CONFIG.get("trigger_stagger_us", 0)),
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

    _print_camera_thresholds()
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

    # —— 【五】超速守卫 ——
    try:
        _sg_cfg = SPEED_GUARD
        if bool(_sg_cfg.get("enabled", False)):
            _sg = init_speed_guard(
                warn_mpm=float(_sg_cfg.get("warn_mpm", 15.0)),
                cap_speed_mpm=float(_sg_cfg.get("cap_speed_mpm", 10.0)),
                trigger_distance_mm=float(
                    _sg_cfg.get("trigger_distance_mm", 80.224)),
                frame_height_mm=float(_sg_cfg.get("frame_height_mm", 193.05)),
                window=int(_sg_cfg.get("window", 5)),
                hysteresis=float(_sg_cfg.get("hysteresis", 0.9)),
            )
            # 触发距离必须和 plc 段一致，否则钳出来的帧率是错的。
            # 这里只做校验和告警，不自动改 —— 两个数分属不同模块，
            # 静默覆盖会让现场排查时看到的配置和实际生效的不一致。
            _plc_td = float(PLC_CONFIG.get("trigger_distance_mm", 102)) * \
                float(PLC_CONFIG.get("scaling_factor", 0.78651))
            if abs(_plc_td - _sg.trigger_distance_mm) > 0.5:
                warning(f"[超速守卫] trigger_distance_mm="
                        f"{_sg.trigger_distance_mm:.3f} 与 plc 段算出的 "
                        f"{_plc_td:.3f} 不一致，钳帧间隔会偏。请对齐 config.yaml。")
        else:
            info("[超速守卫] 已在 config.yaml 关闭（speed_guard.enabled=false）")
    except Exception as e:
        error(f"[超速守卫] 初始化失败，功能不可用（不影响检测）: {e}")

    # —— 【六】边缘区自学习 ——
    # 窗口起点有两个：前端「开始」和程序启动。这里先把程序启动这一次
    # 开出来 —— 现场可能不点「开始」就直接开机跑，那种情况下第一卷
    # 也得能学。之后每次「开始」会重新开窗并清掉上一卷的生效区。
    try:
        _er_cfg = MULTILINE_EDGE_LEARNING
        if bool(_er_cfg.get("enabled", False)):
            # 三态：键不存在=用默认 camera2/camera7；配成空的 {}=显式禁用。
            # 不能写 `_er_cfg.get("targets") or None` —— 那会把"显式禁用"
            # 变成"回落到默认"，现场想关掉这个功能反而关不掉。
            _targets = _er_cfg.get("targets", None)
            if _targets is not None and not isinstance(_targets, dict):
                warning(f"[边缘区学习] targets={_targets!r} 不是字典，"
                        f"已按未配置处理（用默认）")
                _targets = None
            # 类别也走三态；配置里是列表，以后加类别只改配置不改代码。
            _classes = _er_cfg.get("target_classes", None)
            if _classes is not None and not isinstance(_classes, (list, tuple)):
                warning(f"[边缘区学习] target_classes={_classes!r} 不是列表，"
                        f"已按未配置处理（用默认）")
                _classes = None
            _el = init_edge_learner(
                targets=_targets,
                target_classes=_classes,
                learning_threshold=float(
                    _er_cfg.get("learning_threshold", 0.1)),
                window_length_mm=float(
                    _er_cfg.get("window_length_mm", 10000)),
                count_threshold=int(_er_cfg.get("count_threshold", 5)),
                region_padding_px=int(
                    _er_cfg.get("region_padding_px", 50)),
                image_width_px=int(_er_cfg.get("image_width_px", 2440)),
                distance_provider=lambda: get_encoder_state().cumulative_distance,
            )
            if not _el.targets or not _el.target_classes:
                warning("[边缘区学习] targets / target_classes 配置无效，"
                        "功能不生效")
            else:
                # 用限定名：main_yolo 只 from ThriftControl 导入了
                # PythonJavaClient / start_python_server，其余一律走
                # Communication_Tool.ThriftControl.xxx（与 :613 一致）。
                #
                # 「开始」= 新一卷，开窗重学；
                # 「停止」= 这批布结束，**只清场不结算** —— 学习期是否结束
                # 只由 10 m 米长判断，不能拿半截数据凑生效区。
                Communication_Tool.ThriftControl.register_on_start(
                    lambda: _el.begin_window("前端开始"))
                Communication_Tool.ThriftControl.register_on_stop(
                    lambda: _el.end_roll("前端停止"))
                _el.begin_window("程序启动")
                info(f"[边缘区学习] 已就绪：类别 {list(_el.target_classes)}，"
                     f"学习阈值 {_el.learning_threshold}，学习长度 "
                     f"{_el.window_length_mm / 1000:.1f} m，"
                     f"采样数 > {_el.count_threshold} 生成生效区，"
                     f"学习区 {_el.targets}")
        else:
            info("[边缘区学习] 已在 config.yaml 关闭"
                 "（multiline_edge_learning.enabled=false）")
    except Exception as e:
        error(f"[边缘区学习] 初始化失败，功能不可用（不影响检测）: {e}")
    # —— 【四】正常图片定时保存：只待命，等前端下发窗口 ——
    try:
        _nis = NORMAL_IMAGE_SAVE
        if bool(_nis.get("enabled", False)):
            init_normal_saver(
                base_path=DETECTION_SAVE_PATH,
                launch_tag=_detection_manager._launch_tag
                           if _detection_manager is not None else "",
                interval_sec=float(_nis.get("interval_sec", 5.0)),
                max_per_camera=int(_nis.get("max_per_camera", 10)),
                png_compression=int(_nis.get("png_compression", 1)),
                save_subdir=str(_nis.get("save_subdir", "NormalSave")),
            )
        else:
            info("[正常图保存] 已在 config.yaml 关闭（normal_image_save.enabled=false）")
    except Exception as e:
        error(f"[正常图保存] 初始化失败，功能不可用（不影响检测）: {e}")

    _init_health_monitor()

    # 运行模式由 config.yaml 的 simulation.mode 决定
    if SIMULATION_MODE:
        return run_simulation_mode()
    return run_production_mode()


if __name__ == "__main__":
    sys.exit(main())
