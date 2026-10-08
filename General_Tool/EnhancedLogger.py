import logging
import os
import sys
import inspect
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from typing import Optional, Dict, Any, Union, List
from datetime import datetime
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
        更精确地获取调用者信息

        Returns:
            包含调用者信息的字典
        """
        try:
            # 获取完整的调用栈
            stack = inspect.stack()

            # 我们需要找到第一个不在日志类中的帧
            for frame_info in stack:
                filename = os.path.basename(frame_info.filename)

                # 跳过日志类相关的帧
                if (filename.endswith('EnhancedLogger.py') or
                        filename.endswith('logging/__init__.py') or
                        frame_info.function in ['debug', 'info', 'warning', 'error', 'critical',
                                                '_log_with_caller_info']):
                    continue

                # 找到实际调用者
                return {
                    'module': os.path.splitext(filename)[0],
                    'lineno': frame_info.lineno,
                    'funcName': frame_info.function
                }

        except (AttributeError, IndexError, TypeError):
            pass

        # 备用方案：使用倒数第4个帧（通常这是实际调用者）
        try:
            if len(stack) > 4:
                frame_info = stack[4]
                return {
                    'module': os.path.splitext(os.path.basename(frame_info.filename))[0],
                    'lineno': frame_info.lineno,
                    'funcName': frame_info.function
                }
        except (IndexError, AttributeError):
            # 栈帧不足或结构异常时退回 unknown，
            # 但不能用裸 except —— 那会连 KeyboardInterrupt 一起吞掉
            pass

        return {'module': 'unknown', 'lineno': 0, 'funcName': 'unknown'}

    def _log_with_caller_info(self, level: int, message: str, *args, **kwargs):
        """带调用者信息的日志记录"""
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


# 使用示例
if __name__ == "__main__":
    # 初始化日志
    logger = init_logger(
        log_file="logs/app.log",
        level=logging.DEBUG,
        max_bytes=5 * 1024 * 1024,
        backup_count=3
    )

    # 使用示例
    debug("这是一条调试信息", extra={'user': 'john', 'action': 'login'})
    info("程序启动成功", extra={'version': '1.0.0', 'timestamp': datetime.now()})
    warning("磁盘空间不足", extra={'free_space': '1.2GB', 'threshold': '2GB'})

    try:
        # 模拟异常
        raise ValueError("测试异常")
    except ValueError as e:
        error("处理数据时发生错误", extra={'data_id': 123})
        exception("详细的异常信息")

    critical("系统即将崩溃", extra={'reason': '内存溢出', 'pid': os.getpid()})
