# General_Tool/BackgroundTaskManager.py

import asyncio
import concurrent.futures
import time
import threading
from threading import Event, Lock
from typing import List, Any, Optional
import signal
import os

from General_Tool.EnhancedLogger import info, error, warning

# ==================== 全局触发计数器 ====================
_trigger_counter_lock = Lock()
_trigger_counter: int = 0


def get_trigger_counter() -> int:
    with _trigger_counter_lock:
        return _trigger_counter


def set_trigger_counter(value: int) -> None:
    global _trigger_counter
    with _trigger_counter_lock:
        _trigger_counter = value
        info(f"[全局] trigger_counter 已设置为: {value}")


def increment_trigger_counter() -> int:
    global _trigger_counter
    with _trigger_counter_lock:
        _trigger_counter += 1
        return _trigger_counter


def reset_trigger_counter() -> None:
    set_trigger_counter(0)


class BackgroundTaskManager:
    def __init__(self, obj_cam_operation: List[Any], controller=None):
        """
        后台任务管理器

        Args:
            obj_cam_operation: 相机操作对象列表
            controller: TriggerControlSystem 实例（由外部传入）
        """
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=10,
            thread_name_prefix="BGTask"
        )
        self.shutdown_event = Event()
        self.obj_cam_operation = obj_cam_operation
        self.controller = controller  # ← 外部传入，不在此处构造
        self._futures = []
        self._setup_signal_handlers()

    def _setup_signal_handlers(self):
        def signal_handler(signum, frame):
            info(f"收到信号 {signum}, 正在关闭...")
            self.shutdown()

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    def simulation_software_trigger_task(self):
        """可控制退出的相机触发任务"""
        info("相机触发任务启动")
        while not self.shutdown_event.is_set():
            try:
                for obj in self.obj_cam_operation:
                    if self.shutdown_event.is_set():
                        break
                    ret = obj.Trigger_once()
                    info(f"设备{obj.st_serial_number}被触发, code: {ret}")
                    time.sleep(0.01)
                time.sleep(0.4)
            except Exception as e:
                error(f"相机触发任务执行出错: {e}")
                time.sleep(5)
        info("相机触发任务已停止")

    def _run_modbus_server(self):
        """
        运行 Modbus 服务器（同步版本）
        controller.run() 是普通同步函数，直接在线程中调用即可
        """
        if self.controller is None:
            error("[BGTask] controller 未设置，跳过 Modbus 服务")
            return

        info("Modbus 服务器线程启动")
        try:
            # 直接调用同步函数，不需要 asyncio
            self.controller.run()
        except Exception as e:
            error(f"Modbus 服务器错误: {e}")
            import traceback
            traceback.print_exc()
        finally:
            info("Modbus 服务器线程已停止")

    def start_all_tasks(self):
        """启动所有后台任务"""
        try:

            # future1 = self.executor.submit(self.simulation_software_trigger_task)
            # self._futures.append(future1)

            # 启动 Modbus 服务器
            if self.controller is not None:
                future = self.executor.submit(self._run_modbus_server)
                self._futures.append(future)
                info("[BGTask] Modbus 服务器任务已提交")

            info("所有后台任务已启动")

        except Exception as e:
            error(f"启动后台任务失败: {e}")
            raise

    def shutdown(self, timeout: float = 3.0):
        if self.shutdown_event.is_set():
            return

        info("正在强制停止所有任务...")
        self.shutdown_event.set()

        # 停止 controller 的运行循环
        if self.controller is not None:
            self.controller.running = False

        for future in self._futures:
            if not future.done():
                try:
                    future.cancel()
                except Exception as e:
                    error(f"取消任务时出错: {e}")

        try:
            self.executor.shutdown(wait=False)
        except Exception as e:
            error(f"关闭线程池时出错: {e}")

        info("所有任务已强制停止")

    def wait_for_shutdown(self, timeout: float = 5.0):
        info("等待任务正常完成...")
        self.shutdown_event.set()

        if self.controller is not None:
            self.controller.running = False

        start_time = time.time()
        while time.time() - start_time < timeout:
            all_done = all(future.done() for future in self._futures)
            if all_done:
                info("所有任务已完成")
                return True
            time.sleep(0.1)

        warning(f"等待超时（{timeout}s），强制关闭")
        self.shutdown()
        return False

    def __del__(self):
        if not self.shutdown_event.is_set():
            self.shutdown()


# 主程序示例
def main():
    """主程序入口"""

    # 模拟相机操作对象
    class MockCamOperation:
        def __init__(self, serial_number):
            self.st_serial_number = serial_number

        def Trigger_once(self):
            return 0

    # 创建任务管理器
    cam_objects = [MockCamOperation(f"Cam{i}") for i in range(3)]
    task_manager = BackgroundTaskManager(cam_objects)

    try:
        # 启动任务
        task_manager.start_all_tasks()

        info("主程序运行中，按 Ctrl+C 停止...")

        # 主循环
        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        info("收到键盘中断")
    except Exception as e:
        error(f"主程序错误: {e}")
    finally:
        # 首先尝试优雅关闭
        if not task_manager.wait_for_shutdown():
            warning("优雅关闭失败，使用强制关闭")

        info("程序退出")


if __name__ == "__main__":
    # 设置全局异常处理
    def global_exception_handler(exc_type, exc_value, exc_traceback):
        error("未处理的异常:", exc_info=(exc_type, exc_value, exc_traceback))
        os._exit(1)


    import sys

    sys.excepthook = global_exception_handler

    # 运行主程序
    main()
