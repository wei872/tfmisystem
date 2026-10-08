import logging
import os
import sys
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from typing import Optional, Dict, Any, Union
from pathlib import Path


class SafeEnhancedLogger:
    """
    安全增强的日志记录器类，自动记录当前模块的文件名、方法名、行号
    避免与 LogRecord 保留字段冲突，支持多种日志处理器
    """

    # LogRecord 的保留字段集合
    RESERVED_FIELDS = {
        'args', 'asctime', 'created', 'exc_info', 'exc_text', 'filename',
        'funcName', 'levelname', 'levelno', 'lineno', 'module', 'msecs',
        'message', 'msg', 'name', 'pathname', 'process', 'processName',
        'relativeCreated', 'stack_info', 'thread', 'threadName', 'taskName'
    }

    def __init__(self,
                 name: str = "SafeEnhancedLogger",
                 log_file: Optional[Union[str, Path]] = None,
                 level: int = logging.INFO,
                 max_bytes: int = 10 * 1024 * 1024,  # 10MB
                 backup_count: int = 5,
                 when: str = 'midnight',  # 时间轮转间隔
                 interval: int = 1,  # 时间轮转间隔数
                 encoding: str = 'utf-8',
                 formatter: Optional[logging.Formatter] = None):
        """
        初始化安全的增强日志记录器

        Args:
            name: 日志记录器名称
            log_file: 日志文件路径，None则只输出到控制台
            level: 日志级别
            max_bytes: 单个日志文件最大字节数（用于大小轮转）
            backup_count: 备份文件数量
            when: 时间轮转间隔 ('S', 'M', 'H', 'D', 'W0'-'W6', 'midnight')
            interval: 时间轮转间隔数
            encoding: 文件编码
            formatter: 自定义格式化器
        """
        self.logger = logging.getLogger(name)
        self.logger.setLevel(level)
        self.logger.propagate = False  # 防止重复记录

        # 清除现有的处理器，避免重复添加
        self._clear_handlers()

        # 创建默认格式化器
        if formatter is None:
            formatter = self._create_default_formatter()

        # 添加控制台处理器
        self._add_console_handler(formatter)

        # 添加文件处理器（如果指定了日志文件）
        if log_file:
            self._add_file_handler(log_file, max_bytes, backup_count,
                                   when, interval, encoding, formatter)

        # 保存原始的 makeRecord 方法并重写
        self._override_make_record()

    def _clear_handlers(self):
        """清除现有的处理器"""
        for handler in self.logger.handlers[:]:
            self.logger.removeHandler(handler)

    def _create_default_formatter(self) -> logging.Formatter:
        """创建默认的日志格式化器"""
        return logging.Formatter(
            '%(asctime)s,%(msecs)03d - %(name)s - %(levelname)s - '
            '[%(caller_module)s:%(caller_lineno)d] - %(caller_func)s() - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

    def _add_console_handler(self, formatter: logging.Formatter):
        """添加控制台处理器"""
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(logging.DEBUG)
        self.logger.addHandler(console_handler)

    def _add_file_handler(self, log_file: Union[str, Path], max_bytes: int,
                          backup_count: int, when: str, interval: int,
                          encoding: str, formatter: logging.Formatter):
        """添加文件处理器"""
        log_path = Path(log_file)

        # 确保日志目录存在
        log_path.parent.mkdir(parents=True, exist_ok=True)

        if when:  # 时间轮转
            file_handler = TimedRotatingFileHandler(
                filename=str(log_path),
                when=when,
                interval=interval,
                backupCount=backup_count,
                encoding=encoding
            )
        else:  # 大小轮转
            file_handler = RotatingFileHandler(
                filename=str(log_path),
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding=encoding
            )

        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)
        self.logger.addHandler(file_handler)

    def _override_make_record(self):
        """重写 makeRecord 方法以避免字段冲突"""
        original_make_record = self.logger.makeRecord

        def safe_make_record(name, level, fn, lno, msg, args, exc_info,
                             func=None, extra=None, sinfo=None):
            # 过滤掉保留字段
            safe_extra = {}
            if extra:
                for key, value in extra.items():
                    if key not in self.RESERVED_FIELDS:
                        safe_extra[key] = value

            # 调用原始方法
            return original_make_record(
                name, level, fn, lno, msg, args, exc_info,
                func, safe_extra, sinfo
            )

        self.logger.makeRecord = safe_make_record

    def _get_caller_info(self) -> Dict[str, Any]:
        """
        获取调用者信息（module / lineno / funcName）。

        ⚠ 性能：本函数在**每一条**日志上都会被调用，包括相机 SDK 的采集线程。
        原实现用的是 inspect.stack()，实测单次 0.19~0.62ms（随栈深线性增长，
        本项目 detect_image→pipeline→tasks 嵌套 25 帧以上时约 0.4ms），
        而 logging 自带的 findCaller 只要 0.0008ms —— 相差 250~750 倍。
        inspect.stack() 慢的根本原因是它会对**每一帧**调用 getframeinfo()，
        后者要经 linecache 把源文件对应行读出来。

        改成 sys._getframe() 手工回溯：只取 co_filename / f_lineno / co_name，
        不读任何源文件，单次开销降到微秒级。

        另外修掉一个潜在崩溃：原实现的"备用方案"分支引用了 `stack`，
        但若 inspect.stack() 自身抛错，`stack` 根本没被绑定，会抛 NameError；
        而那个 except 只捕获 IndexError/AttributeError，NameError 会直接漏出去。
        """
        try:
            frame = sys._getframe(1)
            while frame is not None:
                filename = os.path.basename(frame.f_code.co_filename)
                func_name = frame.f_code.co_name

                # 跳过日志模块自身的帧
                if not (filename.endswith('EnhancedLogger.py') or
                        filename.endswith('logging/__init__.py') or
                        func_name in ('debug', 'info', 'warning', 'error',
                                      'critical', '_log_with_caller_info')):
                    return {
                        'module': os.path.splitext(filename)[0],
                        'lineno': frame.f_lineno,
                        'funcName': func_name,
                    }
                frame = frame.f_back
        except (AttributeError, ValueError):
            # ValueError: 栈已到底；AttributeError: 帧对象异常
            pass

        return {'module': 'unknown', 'lineno': 0, 'funcName': 'unknown'}

    def _log_with_caller_info(self, level: int, message: str, *args, **kwargs):
        """带调用者信息的日志记录"""
        # 级别预检必须放在最前面。
        # 原来无论级别是否放行，都要先付一次 _get_caller_info() 的全额开销 ——
        # 现场 config.yaml 把 level 设成 INFO 时，热路径上那些 debug() 调用
        # 依然要白跑一遍栈回溯 + 字符串格式化。
        if not self.logger.isEnabledFor(level):
            return

        caller_info = self._get_caller_info()

        # 创建安全的额外信息（避免使用保留字段名）
        safe_extra = {
            'caller_module': caller_info['module'],
            'caller_lineno': caller_info['lineno'],
            'caller_func': caller_info['funcName']
        }

        # 合并用户提供的额外信息
        user_extra = kwargs.pop('extra', {})
        if user_extra:
            for key, value in user_extra.items():
                if key not in self.RESERVED_FIELDS:
                    safe_extra[key] = value

        kwargs['extra'] = safe_extra
        self.logger.log(level, message, *args, **kwargs)

    def debug(self, message: str, *args, **kwargs):
        """记录DEBUG级别日志"""
        self._log_with_caller_info(logging.DEBUG, message, *args, **kwargs)

    def info(self, message: str, *args, **kwargs):
        """记录INFO级别日志"""
        self._log_with_caller_info(logging.INFO, message, *args, **kwargs)

    def warning(self, message: str, *args, **kwargs):
        """记录WARNING级别日志"""
        self._log_with_caller_info(logging.WARNING, message, *args, **kwargs)

    def error(self, message: str, *args, **kwargs):
        """记录ERROR级别日志"""
        self._log_with_caller_info(logging.ERROR, message, *args, **kwargs)

    def critical(self, message: str, *args, **kwargs):
        """记录CRITICAL级别日志"""
        self._log_with_caller_info(logging.CRITICAL, message, *args, **kwargs)

    def exception(self, message: str, *args, **kwargs):
        """记录异常信息（包含堆栈跟踪）"""
        self._log_with_caller_info(logging.ERROR, message, *args, **kwargs)
        # 手动记录异常信息
        self.logger.exception(message, *args, **kwargs)

    def add_handler(self, handler: logging.Handler):
        """添加自定义处理器"""
        self.logger.addHandler(handler)

    def set_level(self, level: int):
        """设置日志级别"""
        self.logger.setLevel(level)
        for handler in self.logger.handlers:
            handler.setLevel(level)


# 单例模式管理
class LoggerManager:
    """日志记录器管理器"""

    _instances = {}

    @classmethod
    def get_logger(cls, name: str = "AppLogger", **kwargs) -> SafeEnhancedLogger:
        """
        获取日志记录器实例

        Args:
            name: 日志记录器名称
            **kwargs: 传递给 SafeEnhancedLogger 的初始化参数

        Returns:
            SafeEnhancedLogger: 日志记录器实例
        """
        if name not in cls._instances or kwargs:
            cls._instances[name] = SafeEnhancedLogger(name, **kwargs)
        return cls._instances[name]

    @classmethod
    def shutdown_all(cls):
        """关闭所有日志记录器"""
        for logger in cls._instances.values():
            logging.getLogger(logger.logger.name).handlers.clear()
        cls._instances.clear()


# 全局便捷函数
def get_logger(name: str = "AppLogger", **kwargs) -> SafeEnhancedLogger:
    """获取全局日志记录器"""
    return LoggerManager.get_logger(name, **kwargs)


def debug(msg: str, *args, **kwargs):
    """全局DEBUG日志"""
    logger = get_logger()
    logger.debug(msg, *args, **kwargs)


def info(msg: str, *args, **kwargs):
    """全局INFO日志"""
    logger = get_logger()
    logger.info(msg, *args, **kwargs)


def warning(msg: str, *args, **kwargs):
    """全局WARNING日志"""
    logger = get_logger()
    logger.warning(msg, *args, **kwargs)


def error(msg: str, *args, **kwargs):
    """全局ERROR日志"""
    logger = get_logger()
    logger.error(msg, *args, **kwargs)


def critical(msg: str, *args, **kwargs):
    """全局CRITICAL日志"""
    logger = get_logger()
    logger.critical(msg, *args, **kwargs)


def exception(msg: str, *args, **kwargs):
    """全局异常日志"""
    logger = get_logger()
    logger.exception(msg, *args, **kwargs)


# 初始化函数
def init_logger(log_file: Optional[Union[str, Path]] = None, level: int = logging.INFO, **kwargs):
    """
    初始化默认日志记录器

    Args:
        log_file: 日志文件路径
        level: 日志级别
        **kwargs: 其他参数

    Returns:
        SafeEnhancedLogger: 初始化的日志记录器
    """
    return get_logger(log_file=log_file, level=level, **kwargs)

