#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
集中配置管理 — 从 config.yaml 加载
模块加载时一次性读取，所有常量保持原 API 不变。
对 dict 类默认值与 yaml 内容做 **深合并**，支持嵌套段的"增量覆盖"。

配置文件定位（按优先级）:
  1. 环境变量 TFMI_CONFIG 指定的绝对/相对路径
  2. 程序所在目录（PyInstaller 打包后为 exe 同级目录）
  3. 仓库根目录（源码运行时为本文件上溯三级）
  4. 当前工作目录

找不到配置文件时**直接抛异常**（fail-fast），而不是静默使用内置默认值 —
后者在打包成服务自启动、CWD 不确定的场景下会导致"参数全默认却毫无提示"的事故。
确需容忍缺失（如单元测试）时设置环境变量 TFMI_ALLOW_MISSING_CONFIG=1。
"""

import os
import sys
import copy
import time
import inspect
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import yaml

from General_Tool.EnhancedLogger import error, info, warning


# ================================================================
# 配置文件定位
# ================================================================

CONFIG_FILENAME = "config.yaml"


def _candidate_config_paths() -> List[str]:
    """按优先级返回可能的配置文件绝对路径。"""
    paths: List[str] = []

    env_path = os.environ.get("TFMI_CONFIG")
    if env_path:
        paths.append(os.path.abspath(os.path.expanduser(env_path)))

    if getattr(sys, "frozen", False):
        # PyInstaller 打包：exe 同级目录
        paths.append(os.path.join(os.path.dirname(sys.executable), CONFIG_FILENAME))
        # onefile 模式解包目录
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            paths.append(os.path.join(meipass, CONFIG_FILENAME))

    # 源码运行：settings.py -> config/ -> AnomalyDetection_Tool/ -> 仓库根
    repo_root = os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
    paths.append(os.path.join(repo_root, CONFIG_FILENAME))

    # 最后兜底：当前工作目录
    paths.append(os.path.abspath(CONFIG_FILENAME))

    # 去重且保序
    seen, uniq = set(), []
    for p in paths:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def resolve_config_path() -> Optional[str]:
    """返回第一个真实存在的配置文件路径，找不到返回 None。"""
    for p in _candidate_config_paths():
        if os.path.isfile(p):
            return p
    return None


CONFIG_PATH: Optional[str] = resolve_config_path()

if CONFIG_PATH is None:
    _msg = ("[配置] 未找到 config.yaml，已搜索以下位置:\n  "
            + "\n  ".join(_candidate_config_paths())
            + "\n可通过环境变量 TFMI_CONFIG 显式指定配置文件路径。")
    if os.environ.get("TFMI_ALLOW_MISSING_CONFIG") == "1":
        error(_msg + "\n(TFMI_ALLOW_MISSING_CONFIG=1，回退到内置默认值)")
    else:
        raise FileNotFoundError(_msg)


def _load_yaml(path: Optional[str]) -> dict:
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        # 配置文件存在却解析失败属于明确的人为错误，不应静默吞掉
        raise ValueError(f"[配置] 解析 {path} 失败: {e}") from e


_CFG = _load_yaml(CONFIG_PATH)

if CONFIG_PATH:
    info(f"[配置] 已加载: {CONFIG_PATH}")


# ================================================================
# 深合并
# ================================================================

def _deep_merge(base: dict, override: dict) -> dict:
    """
    递归合并两个 dict：override 覆盖 base，嵌套 dict 逐层合并而非整段替换。

    这修复了原浅合并的问题：yaml 里只要写了 machine_stop_rules，
    内置默认值中的 rules 子树就会被整段丢弃。
    """
    result = copy.deepcopy(base)
    for key, val in override.items():
        if (key in result and isinstance(result[key], dict)
                and isinstance(val, dict)):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = copy.deepcopy(val)
    return result


def _get(section: str, default=None):
    """
    读取 yaml 段。
    - 段不存在 -> 返回 default
    - 段存在且 default 与段值均为 dict -> 深合并
    - 否则直接返回段值
    """
    val = _CFG.get(section, default)
    if val is None:
        return default if default is not None else {}
    if isinstance(default, dict) and isinstance(val, dict):
        return _deep_merge(default, val)
    return val


# ================================================================
# 配置热更新
# ================================================================
#
# 背景：现场调停机阈值原先必须重启服务，一次调参就要中断生产。
#
# 策略是把配置分成两类：
#   ① 判定类参数（每帧被读取，改了立刻能用）—— 支持热更新
#      停机规则、类别置信度、尺寸/边缘过滤、经纬停机阈值
#   ② 结构类参数（决定对象怎么创建）—— 仍需重启
#      模型路径、线程池规模、端口、相机映射、保存路径、各 enable 开关
#      这类参数热改是不安全的：线程池已经建好、模型已经载入显存、
#      端口已经绑定，改内存里的数值只会造成"配置显示已改、实际没生效"
#      的错觉，比明确要求重启更危险。
#
# 实现方式：热更新段用 _LiveDict（dict 子类）承载，读取时按文件 mtime
# 惰性重载并就地更新自身内容。这样所有 `from settings import XXX` 的
# 调用点都无需修改，拿到的引用始终指向最新值。

_reload_cfg = _get("config_reload", {
    "enabled": True,
    "check_interval": 2.0,
    "log_changes": True,
    "watch_in_background": True,
})
HOT_RELOAD_ENABLED: bool = bool(_reload_cfg.get("enabled", True))
HOT_RELOAD_INTERVAL: float = float(_reload_cfg.get("check_interval", 2.0))
HOT_RELOAD_LOG_CHANGES: bool = bool(_reload_cfg.get("log_changes", True))
HOT_RELOAD_WATCH_BACKGROUND: bool = bool(
    _reload_cfg.get("watch_in_background", True))

_reload_lock = threading.Lock()
_last_check_ts: float = 0.0
_last_mtime: float = -1.0
_last_bad_mtime: float = -1.0   # 上一次被拒绝的文件版本，用于日志去重
_reload_count: int = 0
# 已创建的热更新段，重载后统一刷新
_live_sections: List["_LiveDict"] = []

try:
    _last_mtime = os.path.getmtime(CONFIG_PATH) if CONFIG_PATH else -1.0
except OSError:
    _last_mtime = -1.0


def _validate_reload(new_cfg: dict) -> Tuple[bool, str]:
    """
    重载前的完整性校验。

    真实场景里最危险的不是"YAML 语法错"（那个能被解析器抓到），
    而是**文件写到一半**：内容仍是合法 YAML，只是后面的段还没写完。
    如果照单全收，就会出现"保存文件的瞬间停机规则被清空"这种事故。

    因此这里采用保守策略：**只接受内容完整的配置**。
      1. 顶层键不允许凭空消失（截断文件的典型特征）
      2. 热更新段必须仍是非空 dict
      3. 停机规则必须仍带有非空的 rules 子树

    真要删掉某个配置段，重启一次即可 —— 删段是极低频操作，
    为它牺牲"防截断"的保护不划算。
    """
    if not isinstance(new_cfg, dict) or not new_cfg:
        return False, "配置为空"

    missing = [k for k in _CFG if k not in new_cfg]
    if missing:
        return False, f"顶层配置段缺失 {missing}（疑似文件尚未写入完成）"

    for sec in _live_sections:
        name = sec._section
        if name not in new_cfg:
            continue
        val = new_cfg.get(name)
        if len(sec) > 0 and (not isinstance(val, dict) or not val):
            return False, f"热更新段 {name} 变为空值（疑似文件尚未写入完成）"

    rules = (new_cfg.get("machine_stop_rules") or {})
    if isinstance(rules, dict) and "rules" in _CFG.get(
            "machine_stop_rules", {}):
        if not rules.get("rules"):
            return False, "machine_stop_rules.rules 为空（疑似文件尚未写入完成）"

    return True, ""


def _maybe_reload() -> bool:
    """
    按需重载 config.yaml。返回本次是否真的发生了重载。

    做了两层节流，保证可以被放在每帧路径上调用：
      1. 距上次检查不足 check_interval 秒 -> 直接返回（无系统调用）
      2. 文件 mtime 未变 -> 不读盘
    """
    global _last_check_ts, _last_mtime, _CFG, _reload_count, _last_bad_mtime

    if not HOT_RELOAD_ENABLED or not CONFIG_PATH:
        return False

    now = time.monotonic()
    # 快路径：不加锁读，允许极小概率的重复检查，避免每帧抢锁
    if now - _last_check_ts < HOT_RELOAD_INTERVAL:
        return False

    with _reload_lock:
        if now - _last_check_ts < HOT_RELOAD_INTERVAL:
            return False
        _last_check_ts = now

        try:
            mtime = os.path.getmtime(CONFIG_PATH)
        except OSError:
            return False

        if mtime == _last_mtime:
            return False

        def _reject(reason: str) -> bool:
            # 注意：这里**不更新** _last_mtime，
            # 这样等文件写完（mtime 再变）时还会再试一次；
            # 同时按 mtime 去重日志，避免每 2s 刷同样的错误
            global _last_bad_mtime
            if mtime != _last_bad_mtime:
                _last_bad_mtime = mtime
                error(f"[配置热更新] {reason}，本次重载已跳过，"
                      f"继续使用上一份有效配置")
            return False

        try:
            new_cfg = _load_yaml(CONFIG_PATH)
        except ValueError as e:
            return _reject(str(e))

        ok, reason = _validate_reload(new_cfg)
        if not ok:
            return _reject(reason)

        _CFG = new_cfg
        _last_mtime = mtime
        _last_bad_mtime = -1.0
        _reload_count += 1
        info(f"[配置热更新] 检测到 config.yaml 变更，正在应用"
             f"（第 {_reload_count} 次）")
        return True


def _refresh_live_sections() -> None:
    """重载后刷新所有热更新段"""
    for section in _live_sections:
        section._apply_reload()


class _LiveDict(dict):
    """
    支持热更新的配置段。

    本身就是 dict，因此 `CFG["k"]` / `CFG.get(...)` / `CFG.items()` /
    解包 / 传参等所有既有用法都不受影响；差别只在于读取时会按需
    从 config.yaml 重新加载并就地更新内容。
    """

    def __init__(self, section: str, default=None):
        self._section = section
        self._default = copy.deepcopy(default) if default else default
        super().__init__(_get(section, default))
        _live_sections.append(self)

    # ---- 重载 ----
    def _apply_reload(self) -> None:
        new_val = _get(self._section, self._default)
        if not isinstance(new_val, dict) or new_val == dict(self):
            return
        if HOT_RELOAD_LOG_CHANGES:
            for line in _diff_dict(dict(self), new_val, self._section):
                info(f"[配置热更新] {line}")
        dict.clear(self)
        dict.update(self, new_val)

    def _sync(self) -> None:
        if _maybe_reload():
            _refresh_live_sections()

    # ---- 读取入口（读之前先按需同步）----
    def __getitem__(self, key):
        self._sync()
        return dict.__getitem__(self, key)

    def __contains__(self, key):
        self._sync()
        return dict.__contains__(self, key)

    def __iter__(self):
        self._sync()
        return dict.__iter__(self)

    def __len__(self):
        self._sync()
        return dict.__len__(self)

    def get(self, key, default=None):
        self._sync()
        return dict.get(self, key, default)

    def items(self):
        self._sync()
        return dict.items(self)

    def keys(self):
        self._sync()
        return dict.keys(self)

    def values(self):
        self._sync()
        return dict.values(self)


def _diff_dict(old: dict, new: dict, prefix: str) -> List[str]:
    """生成配置变更的可读描述，便于现场确认"改的参数到底生效没有" """
    lines: List[str] = []
    for key in sorted(set(old) | set(new)):
        o, n = old.get(key), new.get(key)
        if o == n:
            continue
        path = f"{prefix}.{key}"
        if isinstance(o, dict) and isinstance(n, dict):
            lines.extend(_diff_dict(o, n, path))
        elif key not in old:
            lines.append(f"{path}: 新增 = {n}")
        elif key not in new:
            lines.append(f"{path}: 已删除（原 {o}）")
        else:
            lines.append(f"{path}: {o} -> {n}")
    return lines


def _live_value(section: str, key: str, default):
    """读取热更新段里的单个标量值"""
    if _maybe_reload():
        _refresh_live_sections()
    sec = _CFG.get(section) or {}
    if not isinstance(sec, dict):
        return default
    val = sec.get(key, default)
    return default if val is None else val


def reload_config(force: bool = True) -> bool:
    """
    立即重载配置（供外部主动触发，如前端"重载配置"按钮）。

    Returns:
        True 表示确实读取到了新内容
    """
    global _last_check_ts, _last_mtime
    if force:
        with _reload_lock:
            _last_check_ts = 0.0
            _last_mtime = -1.0
    changed = _maybe_reload()
    if changed:
        _refresh_live_sections()
    return changed


def hot_reload_status() -> dict:
    """热更新运行状态，便于排查"改了没生效"的问题"""
    return {
        "enabled": HOT_RELOAD_ENABLED,
        "watcher_running": _watcher_thread is not None
                           and _watcher_thread.is_alive(),
        "config_path": CONFIG_PATH,
        "check_interval": HOT_RELOAD_INTERVAL,
        "reload_count": _reload_count,
        "last_mtime": _last_mtime,
        "live_sections": [s._section for s in _live_sections],
    }


# ================================================================
# 后台监听线程
# ================================================================
#
# 为什么需要它：热更新本身是"拉取式"的 —— 只有代码真的去读某个配置项时
# 才会触发文件检查。这带来一个很反直觉的现象：改完 config.yaml 后，
# 如果当前没有疵点被检出（那些阈值没人读），日志里就什么都不会打印，
# 让人以为热更新坏了；等到几分钟后偶然检出一个疵点，配置才"突然"生效。
#
# 这个线程负责主动轮询，保证：
#   1. 改完约 check_interval 秒内必定生效，与是否有检测流量无关
#   2. 变更日志立刻可见，现场能马上确认改对没有

_watcher_thread: Optional[threading.Thread] = None
_watcher_stop = threading.Event()


def _watch_loop() -> None:
    while not _watcher_stop.is_set():
        # 轮询间隔取 check_interval，_maybe_reload 内部还有 mtime 判断，
        # 文件没变时只是一次 getmtime 系统调用，开销可忽略
        if _watcher_stop.wait(HOT_RELOAD_INTERVAL):
            break
        try:
            if _maybe_reload():
                _refresh_live_sections()
        except Exception as e:
            error(f"[配置热更新] 监听线程异常: {e}")


def start_config_watcher() -> bool:
    """启动配置监听线程（幂等）。返回是否处于运行状态。"""
    global _watcher_thread

    if not HOT_RELOAD_ENABLED or not CONFIG_PATH:
        return False
    if _watcher_thread is not None and _watcher_thread.is_alive():
        return True

    _watcher_stop.clear()
    _watcher_thread = threading.Thread(
        target=_watch_loop, name="ConfigWatcher", daemon=True)
    _watcher_thread.start()
    info(f"[配置热更新] 监听已启动: {CONFIG_PATH}"
         f"（每 {HOT_RELOAD_INTERVAL}s 检查一次，改完即可在此看到变更日志）")
    return True


def stop_config_watcher() -> None:
    """停止配置监听线程"""
    _watcher_stop.set()






# ================================================================
# 基础数据类
# ================================================================

@dataclass
class SaveConfig:
    save_original: bool = True


# ================================================================
# 路径配置
# ================================================================

_paths = _get("paths", {})
DETECTION_BASE_PATH = _paths.get("detection_base_path", r"D:\yolo_detection_results")
DETECTION_SAVE_PATH = _paths.get("detection_save_path", DETECTION_BASE_PATH)
MACHINE_STOP_PATH = _paths.get("machine_stop_path", DETECTION_SAVE_PATH)


# ================================================================
# 上传 / 后端接口配置
# ================================================================

_upload_cfg_cache: Optional[dict] = None
_upload_cfg_mtime: float = -1.0
_upload_cfg_lock = threading.Lock()


def get_upload_config() -> dict:
    """
    获取上传配置。

    支持 yaml 热更新：按文件 mtime 判断，变更时才重新读盘，
    避免定时上报线程每 5s 都做一次完整的 IO + YAML 解析。
    """
    global _upload_cfg_cache, _upload_cfg_mtime

    try:
        mtime = os.path.getmtime(CONFIG_PATH) if CONFIG_PATH else 0.0
    except OSError:
        mtime = 0.0

    with _upload_cfg_lock:
        if _upload_cfg_cache is not None and mtime == _upload_cfg_mtime:
            return dict(_upload_cfg_cache)

        try:
            data = _load_yaml(CONFIG_PATH)
        except ValueError as e:
            # 热更新期间文件被写坏，沿用上一次的有效配置而不是崩掉上报线程
            error(f"{e}，沿用上一次的有效上传配置")
            if _upload_cfg_cache is not None:
                return dict(_upload_cfg_cache)
            data = {}

        up = data.get("upload", {}) or {}
        cfg = {
            "api_host": up.get("api_host", "localhost"),
            "api_port": up.get("api_port", 8890),
            "api_base_path": up.get("api_base_path", "/api"),
            "normal_upload_workers": up.get("normal_upload_workers", 3),
            "normal_upload_queue_size": up.get("normal_upload_queue_size", 256),
            "normal_upload_timeout": up.get("normal_upload_timeout", 10),
            "stop_upload_timeout": up.get("stop_upload_timeout", 3),
            "max_retry": up.get("max_retry", 5),
            "retry_interval": up.get("retry_interval", 1.0),
        }
        _upload_cfg_cache = cfg
        _upload_cfg_mtime = mtime
        return dict(cfg)


def get_api_base_url() -> str:
    """
    后端 API 基础地址，形如 http://localhost:8890/api

    **全系统唯一来源**。此前 periodic_report_service / UploadImage /
    ThriftControl / Modbus 各自硬编码了一份 http://localhost:8890/api，
    导致改 yaml 不生效、换服务器要改 4 处源码。
    """
    cfg = get_upload_config()
    base = cfg["api_base_path"]
    if base and not base.startswith("/"):
        base = "/" + base
    return f"http://{cfg['api_host']}:{cfg['api_port']}{base.rstrip('/')}"


# 兼容旧代码的模块级快照（新代码请调用 get_upload_config()，以支持热更新）
UPLOAD_CONFIG = get_upload_config()


# ================================================================
# 尺寸转换常量
# ================================================================

_size = _get("size_conversion", {})
REAL_WIDTH_CM = _size.get("real_width_cm", 23)
IMAGE_WIDTH_PX = _size.get("image_width_px", 2440)
PIXEL_TO_CM = REAL_WIDTH_CM / IMAGE_WIDTH_PX
PIXEL_TO_MM = PIXEL_TO_CM * 10
PIXEL_AREA_TO_MM2 = PIXEL_TO_MM ** 2

# ================================================================
# 分析模式（互斥开关）
# ================================================================

_analysis = _get("analysis", {})
_VALID_ANALYSIS_MODES = set(_analysis.get("valid_modes", ["coarse", "dinomaly", "none"]))
STOP_ANALYSIS_MODE = _analysis.get("stop_analysis_mode", "coarse")


def validate_analysis_mode(mode: str) -> str:
    mode = mode.lower().strip()
    if mode not in _VALID_ANALYSIS_MODES:
        error(f"[配置错误] STOP_ANALYSIS_MODE='{mode}' 不合法，"
              f"可选值: {_VALID_ANALYSIS_MODES}，已回退到 'none'")
        return "none"
    return mode


# ================================================================
# 粗粒度缺陷分析配置
# ================================================================

COARSE_DETECT_CONFIG = _get("coarse_detect", {
    "blur_kernel_size": 9, "blur_sigma": 2.5,
    "bright_percentile": 85, "dark_percentile": 15,
    "morph_kernel_size": 11, "close_iterations": 2, "open_iterations": 1,
    "min_area_pixels": 20,
})

CLASS_DETECT_CONFIG = _get("class_detect", {})

DEFAULT_DETECT_CONFIG = _get("default_detect", {
    'detect_dark': False, 'percentile': 85, 'min_area': 20,
})

# ================================================================
# 停机规则
# ================================================================

# 热更新：改 config.yaml 里的阈值后约 2 秒生效，无需重启
MACHINE_STOP_RULES = _LiveDict("machine_stop_rules", {})

# ----------------------------------------------------------------
# 按相机覆盖的停机规则
# ----------------------------------------------------------------
# 结构（见 config.yaml 注释）：
#   camera_stop_rule_overrides:
#     camera1:
#       rules:
#         多股线:
#           min_confidence: 0.7
#           min_width_mm: 30.0
#
# 语义是**逐键覆盖**：只写出来的键生效，没写的键沿用全局
# machine_stop_rules.rules.<类>。所以配了 min_confidence 不会把
# min_width_mm 弄丢。
#
# 同样走 _LiveDict，改完 config.yaml 不用重启就生效。
CAMERA_STOP_RULE_OVERRIDES = _LiveDict("camera_stop_rule_overrides", {})

# ================================================================
# 热力图 / 掩膜配置
# ================================================================

HEATMAP_CONFIG = _get("heatmap", {})

# ================================================================
# 类别尺寸过滤（不纳入检测）
# ================================================================

# 热更新
CLASS_SIZE_FILTER = _LiveDict("class_size_filter", {})

# ================================================================
# 类别边缘距离过滤
# ================================================================

# 热更新
CLASS_EDGE_FILTER = _LiveDict("class_edge_filter", {})

# ================================================================
# 竖线误检过滤
# ================================================================

# 阈值热更新；但 enabled 决定过滤器是否被创建，改它仍需重启
VERTICAL_LINE_FILTER = _LiveDict("vertical_line_filter", {})

# ================================================================
# YOLO 检测配置
# ================================================================

YOLO_CONFIG = _get("yolo", {
    "model_path": r"./Modelfile/yolo_20260608.engine",
    # 多模型切换（按布料是否含边缘选模型）。留空表示只用 default 模型。
    "with_edge_model_path": None,
    "without_edge_model_path": None,
    "confidence": 0.1,
    "image_size": 1280,
    "iou_threshold": 0.5,
    "patch_size": 640,
    "num_workers": 5,
    "queue_size": 512,
    "dinomaly_use_fp16": True,
    "use_cuda_stream": True,
    "cuda_benchmark": True,
    "pth_checkpoint": r"./Modelfile/YoloDinomaly/best_acc.pth",
    "pth_device": "cuda:0",
    "pth_model_source_dir": r"./Modelfile/YoloDinomaly",
    "mask_threshold": 0.7,
    "max_inflight_frames": 128,
    "memory_high_watermark": 90,
    "memory_critical_watermark": 95,
    "post_workers": 6,
    "upload_timeout": 10,
})

# ================================================================
# 类别置信度阈值
# ================================================================

# 热更新
CLASS_CONFIDENCE_THRESHOLDS = _LiveDict("class_confidence_thresholds", {})


# ================================================================
#  【四】正常图片定时保存
# ================================================================
# 前端下发「开始时刻 + 持续时长」后，在这段时间内定时保存**无缺陷**帧，
# 每台相机每个窗口最多 max_per_camera 张。没下发窗口就一张都不存。
NORMAL_IMAGE_SAVE = _LiveDict("normal_image_save", {})

# 标量无法就地更新（Python 的数字是不可变对象，import 拿到的是值的副本），
# 因此对外提供访问器；模块级常量保留为启动时快照，仅供兼容旧调用点。
DEFAULT_CLASS_CONFIDENCE = _get("default_class_confidence", 0.1)


def get_default_class_confidence() -> float:
    """热更新：默认类别置信度阈值"""
    if _maybe_reload():
        _refresh_live_sections()
    val = _CFG.get("default_class_confidence", DEFAULT_CLASS_CONFIDENCE)
    return float(val if val is not None else DEFAULT_CLASS_CONFIDENCE)


# ----------------------------------------------------------------
# 按相机覆盖的置信度阈值
# ----------------------------------------------------------------
# 现场 8 台相机的安装位置/光照条件不同，同一套阈值不一定都合适。
# 这里允许只写"需要跟全局不一样"的那几台、那几个类，没写的沿用全局值。
#
# 同样走 _LiveDict，所以改完 config.yaml 不用重启就生效。
CAMERA_THRESHOLD_OVERRIDES = _LiveDict("camera_threshold_overrides", {})


def get_camera_threshold_override(camera_name) -> Optional[Dict]:
    """热更新：取某台相机的阈值覆盖块；没有则返回 None。

    返回的是 config.yaml 里 ``camera_threshold_overrides.<camera_name>``
    那一段原样内容（dict），结构见 config.yaml 里的注释。

    **返回 None 表示"这台相机没有任何覆盖，完全沿用全局"** —— 调用方必须
    区分 None 和空 dict：前者是"没配"，后者是"配了但为空"。
    """
    if not camera_name:
        return None
    try:
        block = CAMERA_THRESHOLD_OVERRIDES.get(str(camera_name))
    except Exception:
        return None
    return block if isinstance(block, dict) else None


def get_camera_default_confidence(camera_name):
    """热更新：某台相机的默认置信度阈值；没配则返回 None（不是 0！）

    返回 None 让调用方能区分"这台相机没配默认值"和"配成了 0"。
    """
    block = get_camera_threshold_override(camera_name)
    if not block:
        return None
    val = block.get("default_class_confidence")
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        warning(f"[配置] camera_threshold_overrides.{camera_name}."
                f"default_class_confidence = {val!r} 不是数字，已忽略")
        return None


def effective_stop_gate(det_threshold, stop_min_conf):
    """有效停机门槛 = max(检测阈值, 停机 min_confidence)。

    为什么是 max：停机判定吃的是**过滤后**的框
    （detection_manager 里 ``for box in result.boxes``，而 result.boxes 是
    ``_filter_boxes`` 的输出），一个框要停机必须同时满足
    ``conf >= 检测阈值`` 和 ``conf >= 停机阈值``，所以真正把关的是两者中较高的那个。

    推论（很重要）：
      * 检测阈值 **高于** 停机阈值时，检测阈值才是实际把关的那道 ——
        降它会直接让停机变频繁；
      * 检测阈值 **低于** 停机阈值时，由停机阈值把关 —— 再怎么降都不影响停机。

    例：多股线 检测 0.65 / 停机 0.70 → 门槛 0.70，检测降到 0.2 门槛仍是 0.70。
        油点线 检测 0.85 / 停机 0.50 → 门槛 0.85，检测降到 0.3 门槛掉到 0.50。
    """
    d = _safe_float(det_threshold)
    s = _safe_float(stop_min_conf)
    if d is None and s is None:
        return None
    if d is None:
        return s
    if s is None:
        return d
    return max(d, s)


def _safe_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------
# 按相机停机规则的解析
# ----------------------------------------------------------------

# 允许按相机覆盖的键。**白名单**：不在名单里的一律忽略并告警。
# 停机规则写错是会真停机的，所以笔误必须被挡下来，不能悄悄生效。
_STOP_RULE_NUMERIC_KEYS = (
    "min_confidence", "min_area_mm2", "min_width_mm", "min_height_mm",
    "min_gradient_ratio",
)
_STOP_RULE_BOOL_KEYS = ("enabled",)
_STOP_RULE_STR_KEYS = ("trigger_mode",)


def get_camera_stop_rule_override(camera_name, class_name) -> Optional[Dict]:
    """热更新：取某台相机对某个类的停机规则覆盖块；没有则返回 None。

    返回 None 的三种情况都当作"这台相机对该类没有覆盖"，一律回落全局：
    没配这台相机 / 配了但 ``rules`` 不是 dict / 配了但该类那一项不是 dict。
    （T16 的教训：YAML 少写一个冒号就会让整块变成字符串，且不报错。）
    """
    if not camera_name or not class_name:
        return None
    try:
        cam = CAMERA_STOP_RULE_OVERRIDES.get(str(camera_name))
    except Exception:
        return None
    if not isinstance(cam, dict):
        return None
    rules = cam.get("rules")
    if not isinstance(rules, dict):
        return None
    block = rules.get(str(class_name))
    return block if isinstance(block, dict) else None


def _sanitise_stop_rule_block(block, where: str) -> Dict:
    """剔除覆盖块里的非法值，并逐项告警。

    非法项被**丢弃**（该项回落全局值），而不是让整个覆盖块失效——
    否则一个笔误会让这台相机所有按相机停机配置静默消失，更难查。
    """
    out = {}
    for k, v in (block or {}).items():
        if k in _STOP_RULE_BOOL_KEYS:
            out[k] = bool(v)
        elif k in _STOP_RULE_NUMERIC_KEYS:
            f = _safe_float(v)
            if f is None:
                warning(f"[配置] {where}.{k} = {v!r} 不是数字，已忽略该项")
            else:
                out[k] = f
        elif k in _STOP_RULE_STR_KEYS:
            out[k] = str(v)
        else:
            warning(f"[配置] {where}.{k} 不是可覆盖的停机规则字段，已忽略")
    return out


def effective_stop_rule(class_name, camera_name=None, stop_rules=None) -> Optional[Dict]:
    """算出**某台相机上某个类实际生效**的停机规则。

    合并顺序：全局 ``machine_stop_rules.rules.<类>`` → 按相机覆盖（逐键覆盖）。
    返回的是**副本**，调用方改它不会污染配置本体。

    返回 None 表示"这台相机上这个类不停机"，可能是：
      * 总开关 ``machine_stop_rules.enabled`` 为 false（最高优先，
        任何按相机覆盖都不能绕过它）；
      * 该类既没有全局规则，也没有按相机规则；
      * 全局或按相机把 ``enabled`` 置为 false。

    :param class_name:  类别名
    :param camera_name: 相机名。**传 None 时行为与改动前完全一致**
                        （纯全局规则），这是向后兼容的关键。
    :param stop_rules:  停机规则 dict，默认取 ``MACHINE_STOP_RULES``。
                        测试时注入，免得依赖全局配置状态。
    """
    rules = MACHINE_STOP_RULES if stop_rules is None else stop_rules
    try:
        if not rules.get("enabled", False):
            return None          # 总开关关着，谁也不能停
        global_rules = rules.get("rules", {}) or {}
        base_rule = global_rules.get(class_name)
    except Exception:
        warning(f"[停机规则] 读取 {class_name} 的全局规则失败，按不停机处理")
        return None

    if isinstance(base_rule, dict):
        base = dict(base_rule)
    elif base_rule is None:
        base = {}
    else:
        # config.yaml 里写了 "某类:" 但下面没内容 → None 之外的怪值
        warning(f"[停机规则] {class_name} 的全局规则不是字典"
                f"（{type(base_rule).__name__}），已忽略")
        base = {}

    ovr = get_camera_stop_rule_override(camera_name, class_name)
    if ovr:
        base.update(_sanitise_stop_rule_block(
            ovr, f"camera_stop_rule_overrides.{camera_name}.rules.{class_name}"))

    if not base:
        return None
    if not base.get("enabled", True):
        return None
    return base


# ================================================================
#  【三】停机 · 重复触发（同一台相机上同类疵点在时间窗口内反复出现）
# ================================================================
# 与单次停机规则**并存**：两条任一条满足就停机。
# 需求原话：「在一台相机上，连续几秒检测到相同类型的疵点就停机」。
#
# 这里的 min_confidence 是**计数门槛**，与单次停机规则的 min_confidence
#    是两回事，不要混。单次规则问"这一个疵点够不够严重"，重复规则问
#    "这类疵点是不是一直在出现"。若复用单次门槛，那些每次都差一点的
#    疵点永远计不进来，本功能等于没开。

REPEAT_STOP_RULES = _LiveDict("repeat_stop_rules", {})
CAMERA_REPEAT_STOP_OVERRIDES = _LiveDict("camera_repeat_stop_overrides", {})

_REPEAT_RULE_BOOL_KEYS = ("enabled",)
_REPEAT_RULE_NUMERIC_KEYS = ("min_confidence", "window_sec")
# count 必须是整数：3.7 次没有意义，而且 int(3.7)=3 会静默吞掉笔误。
_REPEAT_RULE_INT_KEYS = ("count",)


def _sanitise_repeat_rule_block(block, where: str) -> Dict:
    """剔除重复停机覆盖块里的非法值，逐项告警。

    非法项**丢弃**（回落全局值），而不是让整块失效——否则一个笔误会让
    这台相机所有重复停机配置静默消失，更难查。
    """
    out = {}
    for k, v in (block or {}).items():
        if k in _REPEAT_RULE_BOOL_KEYS:
            out[k] = bool(v)
        elif k in _REPEAT_RULE_INT_KEYS:
            f = _safe_float(v)
            if f is None:
                warning(f"[配置] {where}.{k} = {v!r} 不是数字，已忽略该项")
            else:
                out[k] = int(f)
        elif k in _REPEAT_RULE_NUMERIC_KEYS:
            f = _safe_float(v)
            if f is None:
                warning(f"[配置] {where}.{k} = {v!r} 不是数字，已忽略该项")
            else:
                out[k] = f
        else:
            warning(f"[配置] {where}.{k} 不是可覆盖的重复停机字段，已忽略")
    return out


def get_camera_repeat_rule_override(camera_name, class_name) -> Optional[Dict]:
    """热更新：取某台相机对某个类的重复停机覆盖块；没有则 None。

    与 get_camera_stop_rule_override 同样的三种"当作没有"情况：
    没配这台相机 / rules 不是 dict / 该类那一项不是 dict。
    （YAML 少写一个冒号就会让整块变成字符串且不报错，必须防。）
    """
    if not camera_name or not class_name:
        return None
    try:
        cam = CAMERA_REPEAT_STOP_OVERRIDES.get(str(camera_name))
    except Exception:
        return None
    if not isinstance(cam, dict):
        return None
    rules = cam.get("rules")
    if not isinstance(rules, dict):
        return None
    block = rules.get(str(class_name))
    return block if isinstance(block, dict) else None


def repeat_stop_allowed(camera_name, repeat_rules=None) -> bool:
    """这台相机**参不参与**【三】重复触发。

    需求：只有指定的几台（现场是 camera1 / camera8）参与。其余相机
    **连计数都不做** —— 攒够了也不能停，计数本身没有意义，
    白占一份滑动窗口、白刷日志。所以调用方在计数**之前**就先问这里。

    读的是 ``repeat_stop_rules.stop_allowed_cameras``：

    * **键不存在** -> 全部允许（向后兼容，老配置不受影响）
    * **空列表 ``[]``** -> 谁都不许停（只计数、只打日志）
    * **非空列表**     -> 只有列出的相机许停

    "键不存在"和"空列表"必须区分开。用 ``in`` 判断而不是
    ``rules.get(k) or 默认``——后者会把 ``[]`` 当成"没配"，
    于是"谁都不许停"被静默变成"全都许停"，方向正好反了。

    这个名单**只管【三】重复触发**，不影响【二】单次停机规则。
    单次停机在 8 台上的既有行为一个字都没改。

    :param camera_name: 相机名
    :param repeat_rules: 规则 dict，默认取 REPEAT_STOP_RULES。测试时注入。
    """
    rules = REPEAT_STOP_RULES if repeat_rules is None else repeat_rules
    try:
        if "stop_allowed_cameras" not in rules:
            return True                     # 没配 = 不限制
        allowed = rules.get("stop_allowed_cameras")
    except Exception:
        warning("[重复停机] 读取 stop_allowed_cameras 失败，按不允许停机处理")
        return False                        # 读坏了就**不许停**，宁可漏停不误停
    if allowed is None:                     # 写了键但没给值
        return True
    if not isinstance(allowed, (list, tuple, set)):
        warning(f"[重复停机] stop_allowed_cameras = {allowed!r} 不是列表，"
                f"按不允许停机处理")
        return False
    return str(camera_name) in {str(c) for c in allowed}


def effective_repeat_rule(class_name, camera_name=None, repeat_rules=None):
    """算出**某台相机上某个类实际生效**的重复停机规则。

    合并顺序：全局 ``repeat_stop_rules.rules.<类>`` → 按相机覆盖（逐键覆盖）。
    返回**副本**，调用方改它不会污染配置本体。

    返回 None 表示"这台相机上这个类不做重复判定"，可能是：
      * 总开关 ``repeat_stop_rules.enabled`` 为 false（最高优先）；
      * 该类既没有全局规则也没有按相机规则；
      * 全局或按相机把 ``enabled`` 置为 false；
      * count < 2 或 window_sec <= 0（退化成单次判定，不归这里管）。

    :param camera_name: 相机名。传 None 时只看全局规则。
    :param repeat_rules: 规则 dict，默认取 REPEAT_STOP_RULES。测试时注入。
    """
    rules = REPEAT_STOP_RULES if repeat_rules is None else repeat_rules
    try:
        if not rules.get("enabled", False):
            return None
        global_rules = rules.get("rules", {}) or {}
        base_rule = global_rules.get(class_name)
    except Exception:
        warning(f"[重复停机] 读取 {class_name} 的全局规则失败，按不判定处理")
        return None

    if isinstance(base_rule, dict):
        base = dict(base_rule)
    elif base_rule is None:
        base = {}
    else:
        warning(f"[重复停机] {class_name} 的全局规则不是字典"
                f"（{type(base_rule).__name__}），已忽略")
        base = {}

    ovr = get_camera_repeat_rule_override(camera_name, class_name)
    if ovr:
        base.update(_sanitise_repeat_rule_block(
            ovr, f"camera_repeat_stop_overrides.{camera_name}.rules.{class_name}"))

    if not base or not base.get("enabled", True):
        return None

    # 退化保护：count<2 就是"出现即停"，那是单次停机规则的职责。
    # 在这里放行会导致同一个疵点被两条路径各停一次，日志也会打架。
    try:
        if int(base.get("count", 0)) < 2 or float(base.get("window_sec", 0)) <= 0:
            return None
    except (TypeError, ValueError):
        warning(f"[重复停机] {class_name} 的 count/window_sec 不是数字，已忽略")
        return None
    return base



def stop_rule_min_confidence(rule):
    """取一条停机规则的置信度门槛，**含隐式默认值**。

    必须与 StopMachineService 里的兜底保持一致：
    ``rule.get("min_confidence", 0.85)``（area 模式）和
    ``rule.get("min_confidence", 0.5)``（dimension 模式）。

    为什么需要它：规则里没写 min_confidence 时，直接 ``.get()`` 会拿到 None，
    于是被当成"没有停机规则"，算出来的有效门槛就是错的。当前 config.yaml
    里 15 个类都显式写了，所以现在不会踩到；但这是隐患，不能靠"现在没写错"。
    """
    if not isinstance(rule, dict) or not rule:
        # 空 dict 表示"根本没有规则"（例如该相机把这个类禁用了），
        # 不能套用默认值——那会凭空算出一个 0.85 的门槛。
        return None
    v = _safe_float(rule.get("min_confidence"))
    if v is not None:
        return v
    return 0.5 if rule.get("trigger_mode", "area") == "dimension" else 0.85


def describe_camera_stop_rules(camera_name, stop_rules=None) -> Dict:
    """算出某台相机**实际生效**的停机规则表，供启动时打印。

    返回 ``{类名: 生效规则 dict 或 None}``；None 表示该类在这台相机上不停机。
    覆盖过的类会带一个 ``"<camera_override>"`` 标记键，方便一眼看出哪台被改过。
    """
    rules = MACHINE_STOP_RULES if stop_rules is None else stop_rules
    out = {}
    classes = []
    try:
        classes = list((rules.get("rules", {}) or {}).keys())
    except Exception:
        classes = []
    cam = None
    try:
        cam = CAMERA_STOP_RULE_OVERRIDES.get(str(camera_name)) if camera_name else None
    except Exception:
        cam = None
    if isinstance(cam, dict) and isinstance(cam.get("rules"), dict):
        for k in cam["rules"]:
            if k not in classes:
                classes.append(k)

    for cls in classes:
        eff = effective_stop_rule(cls, camera_name, stop_rules=rules)
        if eff is None:
            out[cls] = None
            continue
        if get_camera_stop_rule_override(camera_name, cls):
            eff = dict(eff)
            eff["<camera_override>"] = True
        out[cls] = eff
    return out


def check_camera_stop_rule_impact(camera_names, stop_rules=None):
    """列出**所有"按相机停机规则与全局不一致"的项**。

    与 :func:`check_camera_threshold_stop_impact` 不同：那个检查的是
    "改检测阈值会不会连带影响停机"，这个检查的是"停机规则本身被改了没有"。

    比较的是**生效值**（``effective_stop_rule`` 的输出）而不是配置里的原始键。
    这一点很关键：``trigger_mode`` 缺省时隐式为 "area"、``min_confidence``
    缺省时 area 模式取 0.85 / dimension 模式取 0.5。直接比原始键会漏掉
    "全局没写、按相机写了" 这种情况——而那恰恰是行为真的变了。

    :return: list of dict，字段 camera / class_name / field /
             global_value / camera_value / effect；空列表 = 与全局完全一致。
    """
    rules = MACHINE_STOP_RULES if stop_rules is None else stop_rules
    out = []
    for name in camera_names or []:
        try:
            cam = CAMERA_STOP_RULE_OVERRIDES.get(str(name))
        except Exception:
            cam = None
        if not isinstance(cam, dict) or not isinstance(cam.get("rules"), dict):
            continue
        for cls, raw_block in cam["rules"].items():
            if not isinstance(raw_block, dict):
                continue
            before = effective_stop_rule(cls, None, stop_rules=rules) or {}
            after = effective_stop_rule(cls, name, stop_rules=rules) or {}

            # 有效门槛 = max(检测阈值, 停机 min_confidence)。光看停机字段的变化
            # 会误导：把停机 0.7 降到 0.5，若该相机检测阈值是 0.65，真实门槛
            # 只从 0.7 掉到 0.65，不是掉到 0.5。所以两处都要报。
            #
            # 规则整体不存在时门槛记为 None——不能退化成"只剩检测阈值"，
            # 那会报出一个根本不存在的停机门槛。
            det = _camera_det_threshold(name, cls)
            gate_b = (effective_stop_gate(det, stop_rule_min_confidence(before))
                      if before else None)
            gate_a = (effective_stop_gate(det, stop_rule_min_confidence(after))
                      if after else None)

            # 规则整体"有 → 无"或"无 → 有"，这是最要紧的变化，单独报一条
            if bool(before) != bool(after):
                gb, ga = bool(before), bool(after)
                out.append({
                    "camera": name, "class_name": cls, "field": "enabled",
                    "global_value": gb, "camera_value": ga,
                    "effect": _stop_change_effect("enabled", gb, ga),
                    "det_threshold": det,
                    "gate_before": gate_b, "gate_after": gate_a,
                    "gate_effect": ("该类在这台相机上不再停机，无有效停机门槛"
                                    if not after else
                                    f"该类在这台相机上开始停机，有效门槛 {gate_a}"),
                })
                continue

            gate_effect = _gate_effect(gate_b, gate_a)
            for field in sorted(set(before) | set(after)):
                bv, av = before.get(field), after.get(field)
                if bv is None and av is None:
                    continue
                if _stop_values_equal(bv, av):
                    continue
                out.append({
                    "camera": name,
                    "class_name": cls,
                    "field": field,
                    "global_value": bv,
                    "camera_value": av,
                    "effect": _stop_change_effect(field, bv, av),
                    "det_threshold": det,
                    "gate_before": gate_b,
                    "gate_after": gate_a,
                    "gate_effect": gate_effect,
                })
    return out


def _camera_det_threshold(camera_name, class_name):
    """该相机上该类的**生效检测阈值**；取不到返回 None。

    用于把停机规则的变化换算成有效停机门槛的变化。
    """
    try:
        eff = describe_camera_thresholds(camera_name, CLASS_CONFIDENCE_THRESHOLDS)
        return _safe_float(eff.get(class_name, get_default_class_confidence()))
    except Exception:
        return None


def _gate_effect(gate_before, gate_after):
    """把有效门槛的前后变化说成一句人话。"""
    if gate_before is None or gate_after is None:
        return "无法计算有效门槛（缺检测阈值或停机阈值）"
    if abs(gate_after - gate_before) < 1e-9:
        return f"有效门槛不变（{gate_before}）"
    return (f"有效门槛 {gate_before}→{gate_after}，"
            + ("停机会变频繁" if gate_after < gate_before else "停机会变少"))


def _stop_values_equal(a, b):
    fa, fb = _safe_float(a), _safe_float(b)
    if fa is not None and fb is not None:
        return abs(fa - fb) < 1e-9
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    return a == b


def _stop_change_effect(field, g_val, cam_val):
    """用一句话说明这处改动的后果。方向搞反会让人误判风险，所以单独测。"""
    if field == "enabled":
        return "该类会停机" if cam_val else "该类不再停机"
    if field == "trigger_mode":
        # 缺省时隐式为 area，显示成 area 比显示 None 有用
        g_show = g_val if g_val is not None else "area"
        c_show = cam_val if cam_val is not None else "area"
        return f"停机判定方式 {g_show}→{c_show}"
    if field == "min_confidence":
        gf, cf = _safe_float(g_val), _safe_float(cam_val)
        if gf is None or cf is None:
            return "停机门槛变化"
        if abs(cf - gf) < 1e-9:
            return "无变化"
        return "停机会变少" if cf > gf else "停机会变频繁"
    if field in ("min_area_mm2", "min_width_mm", "min_height_mm"):
        gf, cf = _safe_float(g_val), _safe_float(cam_val)
        if gf is None or cf is None:
            return "尺寸条件变化"
        if abs(cf - gf) < 1e-9:
            return "无变化"
        return "停机会变少" if cf > gf else "停机会变频繁"
    return "停机条件变化"


def _stop_conf_lookup_takes_camera(fn) -> bool:
    """判断注入的 ``stop_conf_lookup`` 能不能接受 camera_name。

    T15 的签名是 ``(class_name)``；T17 起停机规则也能按相机覆盖，所以支持
    ``(class_name, camera_name)``。**两种都必须能用** —— 否则升级 settings
    会让所有还按老签名注入的调用点直接炸掉。
    """
    try:
        sig = inspect.signature(fn)
    except Exception:
        # 不只是 (TypeError, ValueError)：任何探测失败都按老签名调。
        # 这个函数在启动打印路径上，探测失败不能把启动流程带崩。
        return False
    n_pos = 0
    for p in sig.parameters.values():
        if p.kind == inspect.Parameter.VAR_POSITIONAL:
            return True     # *args，怎么传都行
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                      inspect.Parameter.POSITIONAL_OR_KEYWORD):
            n_pos += 1
    return n_pos >= 2


def check_camera_threshold_stop_impact(camera_names,
                                       global_thresholds,
                                       global_default,
                                       stop_conf_lookup):
    """检查按相机覆盖是否**改变了停机行为**。

    :param camera_names:     要检查的相机名列表
    :param global_thresholds: 全局按类阈值 dict
    :param global_default:   全局默认阈值
    :param stop_conf_lookup: 取停机 ``min_confidence`` 的可调用对象，取不到
                             返回 None。可以是 ``(class_name)``，也可以是
                             ``(class_name, camera_name)`` —— 后者用于停机
                             规则本身也按相机覆盖的情况（T17）。
                             用注入而不是直接 import StopMachineService，
                             是为了避免 settings ↔ services 的循环导入。
    :return: list of dict，每项描述一处停机行为变化；空列表表示没有影响。

    返回项字段：camera / class_name / det_before / det_after /
                stop_conf / gate_before / gate_after / effect
    """
    global_thresholds = dict(global_thresholds or {})
    takes_camera = _stop_conf_lookup_takes_camera(stop_conf_lookup)
    out = []
    for name in camera_names or []:
        block = get_camera_threshold_override(name)
        if not block:
            continue
        eff = describe_camera_thresholds(name, global_thresholds)
        for cls, det_after in eff.items():
            if cls == "<default>":
                continue
            try:
                stop_conf = (stop_conf_lookup(cls, name) if takes_camera
                             else stop_conf_lookup(cls))
            except Exception:
                stop_conf = None
            if stop_conf is None:
                continue            # 该类没有停机规则，不影响停机
            det_before = _safe_float(global_thresholds.get(cls, global_default))
            gb = effective_stop_gate(det_before, stop_conf)
            ga = effective_stop_gate(det_after, stop_conf)
            if gb is None or ga is None or abs(ga - gb) < 1e-9:
                continue
            out.append({
                "camera": name,
                "class_name": cls,
                "det_before": det_before,
                "det_after": _safe_float(det_after),
                "stop_conf": _safe_float(stop_conf),
                "gate_before": gb,
                "gate_after": ga,
                "effect": "停机会变频繁" if ga < gb else "停机会变少",
            })
    return out


def describe_camera_thresholds(camera_name, global_thresholds) -> Dict:
    """算出某台相机**实际生效**的阈值表，供启动时打印。

    按相机覆盖很容易配出"我以为只改了一个类，结果整机灵敏度都变了"的情况
    （相机级 default 会盖掉全局的按类阈值）。所以启动时把每台的生效值
    打出来，让配置意图和实际行为对得上。
    """
    eff = dict(global_thresholds or {})
    block = get_camera_threshold_override(camera_name)
    if not block:
        return eff
    per_class = block.get("class_confidence_thresholds")
    if isinstance(per_class, dict):
        for k, v in per_class.items():
            try:
                eff[k] = float(v)
            except (TypeError, ValueError):
                warning(f"[配置] camera_threshold_overrides.{camera_name}."
                        f"class_confidence_thresholds.{k} = {v!r} 不是数字，已忽略")
    cam_default = get_camera_default_confidence(camera_name)
    if cam_default is not None:
        eff["<default>"] = cam_default
    return eff

# ================================================================
# 实例化配置
# ================================================================

_save_cfg = _get("save", {})
SAVE_CONFIG = SaveConfig(save_original=_save_cfg.get("save_original", True))

# 落盘图像格式。PNG 无损但极慢：2440x2048 实测约 179ms/张、体积 8MB；
# JPEG(q95) 约 16ms/张、1.1MB —— 编码快 11 倍、体积小 7 倍。
# 缺陷取证用 q95 的 JPEG 完全够用；若下游硬性要求无损，改回 "png"。
SAVE_IMAGE_EXT = str(_save_cfg.get("image_format", "jpg")).strip().lower()
if not SAVE_IMAGE_EXT.startswith("."):
    SAVE_IMAGE_EXT = "." + SAVE_IMAGE_EXT
SAVE_JPEG_QUALITY = int(_save_cfg.get("jpeg_quality", 95))

# 启动时读取一次，改完需重启（不参与热更新）。
# 串口号在启动阶段解析并固定下来，运行期间不再变动，便于现场排查。
RELAY_CONFIG = _get("relay", {
    "port": "",
    "baudrate": 9600,
    "channel": 1,
    "pulse_ms": 50,
    "cooldown_seconds": 0,
})


# ================================================================
# 模拟模式
# ================================================================

_sim = _get("simulation", {})
SIMULATION_MODE = _sim.get("mode", False)
SIMULATION_FOLDER = _sim.get("folder", r"D:\DataFile\dataset\20260703")

# ================================================================
# 布幅检测配置
# ================================================================

FABRIC_WIDTH_CONFIG = _get("fabric_width", {})

# ================================================================
# 经线密度
# ================================================================

_warp = _get("warp_density", {})
# enable 决定检测器是否被创建与预热，改它必须重启
WARP_DENSITY_ENABLE = _warp.get("enable", True)
# 以下两项为启动快照，仅供兼容；新代码请用下面的访问器（支持热更新）
WARP_TRIGGER = _warp.get("trigger", False)
WARP_DENSITY_STOP_TRIGGER = _warp.get("stop_trigger", 0.45)


def get_warp_trigger() -> bool:
    """热更新：经线异常是否触发停机"""
    return bool(_live_value("warp_density", "trigger", WARP_TRIGGER))


def get_warp_stop_trigger() -> float:
    """热更新：经线均匀度停机阈值（低于此值触发）"""
    return float(_live_value("warp_density", "stop_trigger",
                             WARP_DENSITY_STOP_TRIGGER))

# ================================================================
# 纬线密度
# ================================================================

_weft = _get("weft_density", {})
# enable 决定检测器是否被创建与预热，改它必须重启
WEFT_DENSITY_ENABLE = _weft.get("enable", True)
# 以下为启动快照，仅供兼容；新代码请用下面的访问器（支持热更新）
WEFT_TRIGGER = _weft.get("trigger", False)
WEFT_DENSITY_STOP_TRIGGER = _weft.get("stop_trigger", 260)
LAT_SLOPE_ANGLE_THRESHOLD = _weft.get("lat_slope_angle_threshold", 5.0)


def get_weft_trigger() -> bool:
    """热更新：纬线异常是否触发停机"""
    return bool(_live_value("weft_density", "trigger", WEFT_TRIGGER))


def get_weft_stop_trigger() -> float:
    """热更新：纬线最大间距停机阈值（超过此值触发）"""
    return float(_live_value("weft_density", "stop_trigger",
                             WEFT_DENSITY_STOP_TRIGGER))


def get_lat_slope_threshold() -> float:
    """热更新：纬斜角度停机阈值（超过此值触发）"""
    return float(_live_value("weft_density", "lat_slope_angle_threshold",
                             LAT_SLOPE_ANGLE_THRESHOLD))
SAVE_WEFT = _weft.get("save_weft", False)
WEFT_OUTPUT_DIR = _weft.get("output_dir", r"D:\yolo_detection_results\test")

# 经纬密度触发停机保存地址
WARP_WEFT_OUTPUT_DIR = _get("warp_weft_output_dir", r"D:\yolo_detection_results\test")

# ================================================================
# 相机 SN -> 名称映射 / 采集参数（从 yaml 读取）
# ================================================================

_cameras = _get("cameras", {})
CAMERA_SN_MAP: Dict[str, str] = _cameras.get("sn_map", {})

# 大恒 Galaxy SDK 采集参数，含义见 config.yaml 的 cameras.acquisition 注释
CAMERA_ACQ_CONFIG: Dict[str, Any] = _cameras.get("acquisition", {}) or {}


# ================================================================
# 调试与原图保存配置
# ================================================================

_debug = _get("debug", {})
# 整个 debug 段也导出去：触发突发探针的开关在里面（burst_probe 等）。
# 只导 SAVE_PATH / SEAVEIMAGE / PUSH 三个的话，新加的诊断项每次都要改这里。
DEBUG_CONFIG: Dict[str, Any] = _debug
SAVE_PATH = _debug.get("save_path", r"G:/fabric_ex_data")
SEAVEIMAGE = _debug.get("save_image", False)
PUSH = _debug.get("push_to_java", False)


# ================================================================
# PLC / 编码器触发（Modbus）
# ================================================================

PLC_CONFIG = _get("plc", {
    "host": "192.168.123.70",
    "port": 502,
    "wheel_diameter": 60.0,
    "encoder_ppr": 2000,
    "scaling_factor": 0.78651,
    "trigger_distance_mm": 102,
    "read_interval_ms": 100,
    "speed_threshold_ratio": 0.0,
    "idle_speed_threshold": 0.5,
    "idle_confirm_duration": 3.0,
    "preview_interval": 1.0,
})


# ================================================================
# WebSocket 预览服务
# ================================================================

WEBSOCKET_CONFIG = _get("websocket", {"host": "0.0.0.0", "port": 8765})


# ================================================================
# 并发与采样
# ================================================================

CONCURRENCY_CONFIG = _get("concurrency", {
    "warp_sample_interval": 1,
    "weft_sample_interval": 1,
    "warp_workers": 2,
    "weft_workers": 2,
    "save_workers": 2,
    "async_save_workers": 4,
    "infer_pool_workers": 2,
    "io_pool_workers": 4,
    "aux_io_workers": 2,
})


# ================================================================
# 日志
# ================================================================

LOGGING_CONFIG = _get("logging", {
    "file": "logs/app.log",
    "level": "DEBUG",
    "max_bytes": 5 * 1024 * 1024,
    "backup_count": 3,
    "faulthandler_dump_after": 0,
})


# ================================================================
# 模块加载完成后启动配置监听
# ================================================================
# 必须放在文件末尾：此时所有 _LiveDict 段都已创建完毕，
# 监听线程一旦触发重载就能刷新到全部热更新段。
if HOT_RELOAD_WATCH_BACKGROUND:
    start_config_watcher()
