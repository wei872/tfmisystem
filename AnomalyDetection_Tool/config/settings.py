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
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import yaml

from General_Tool.EnhancedLogger import error, info


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

# 🔥 热更新：改 config.yaml 里的阈值后约 2 秒生效，无需重启
MACHINE_STOP_RULES = _LiveDict("machine_stop_rules", {})

# ================================================================
# 热力图 / 掩膜配置
# ================================================================

HEATMAP_CONFIG = _get("heatmap", {})

# ================================================================
# 类别尺寸过滤（不纳入检测）
# ================================================================

# 🔥 热更新
CLASS_SIZE_FILTER = _LiveDict("class_size_filter", {})

# ================================================================
# 类别边缘距离过滤
# ================================================================

# 🔥 热更新
CLASS_EDGE_FILTER = _LiveDict("class_edge_filter", {})

# ================================================================
# 竖线误检过滤
# ================================================================

# 🔥 阈值热更新；但 enabled 决定过滤器是否被创建，改它仍需重启
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

# 🔥 热更新
CLASS_CONFIDENCE_THRESHOLDS = _LiveDict("class_confidence_thresholds", {})

# 标量无法就地更新（Python 的数字是不可变对象，import 拿到的是值的副本），
# 因此对外提供访问器；模块级常量保留为启动时快照，仅供兼容旧调用点。
DEFAULT_CLASS_CONFIDENCE = _get("default_class_confidence", 0.1)


def get_default_class_confidence() -> float:
    """🔥 热更新：默认类别置信度阈值"""
    if _maybe_reload():
        _refresh_live_sections()
    val = _CFG.get("default_class_confidence", DEFAULT_CLASS_CONFIDENCE)
    return float(val if val is not None else DEFAULT_CLASS_CONFIDENCE)

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
    """🔥 热更新：经线异常是否触发停机"""
    return bool(_live_value("warp_density", "trigger", WARP_TRIGGER))


def get_warp_stop_trigger() -> float:
    """🔥 热更新：经线均匀度停机阈值（低于此值触发）"""
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
    """🔥 热更新：纬线异常是否触发停机"""
    return bool(_live_value("weft_density", "trigger", WEFT_TRIGGER))


def get_weft_stop_trigger() -> float:
    """🔥 热更新：纬线最大间距停机阈值（超过此值触发）"""
    return float(_live_value("weft_density", "stop_trigger",
                             WEFT_DENSITY_STOP_TRIGGER))


def get_lat_slope_threshold() -> float:
    """🔥 热更新：纬斜角度停机阈值（超过此值触发）"""
    return float(_live_value("weft_density", "lat_slope_angle_threshold",
                             LAT_SLOPE_ANGLE_THRESHOLD))
SAVE_WEFT = _weft.get("save_weft", False)
WEFT_OUTPUT_DIR = _weft.get("output_dir", r"D:\yolo_detection_results\test")

# 经纬密度触发停机保存地址
WARP_WEFT_OUTPUT_DIR = _get("warp_weft_output_dir", r"D:\yolo_detection_results\test")

# ================================================================
# 相机 SN -> 名称映射（从 yaml 读取）
# ================================================================

_cameras = _get("cameras", {})
CAMERA_SN_MAP: Dict[str, str] = _cameras.get("sn_map", {})


# ================================================================
# 调试与原图保存配置
# ================================================================

_debug = _get("debug", {})
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
