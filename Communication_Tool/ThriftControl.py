#!/usr/bin/env python3
import sys
import threading
import time
from thrift.transport import TSocket
from thrift.transport import TTransport
from thrift.protocol import TBinaryProtocol
from thrift.server import TServer
from thrift.transport.TTransport import TTransportException
from General_Tool.RunningSystemRegistry import get_running_system
from General_Tool.EnhancedLogger import info, error
sys.path.append('./gen-py')
from lib.camera.thrift import CameraService, SimpleService
import aiohttp
import asyncio

# ==================== 停机原因枚举 ====================
class StopReason:
    NONE = "none"           # 未停机（运行中）
    MANUAL_PAUSE = "manual" # 手动暂停（必须手动恢复）
    DEFECT_STOP = "defect"  # 缺陷触发停机（速度恢复后自动恢复）

# ==================== 线程安全的状态管理 ====================
_state_lock = threading.Lock()
_is_stop: bool = False
_stop_reason: str = StopReason.NONE  # ← 新增：停机原因
_defect_stop_time: float = 0.0   # ← 新增：记录缺陷停机发生的时间戳

# ==================== "开始"事件回调（供外部注册新批次逻辑） ====================
_on_start_callbacks: list = []
_on_start_lock = threading.Lock()


def register_on_start(cb) -> None:
    """注册一个在收到前端"开始"指令时执行的回调（无参，可阻塞）。

    典型用途：让检测管理器在每次点"开始"时刷新 launch_tag，
    把本次批次的疵点/停机图写入一个全新的批次子目录。
    """
    with _on_start_lock:
        _on_start_callbacks.append(cb)


def _fire_start_callbacks() -> None:
    with _on_start_lock:
        callbacks = list(_on_start_callbacks)
    for cb in callbacks:
        try:
            cb()
        except Exception as e:
            error(f"[Thrift] \"开始\"事件回调异常: {e}")

# 兼容旧代码的直接访问变量
is_stop = False
# local_IP_ADDRESS = '192.168.123.69'
# server_IP_ADDRESS = '192.168.123.98'
local_IP_ADDRESS = '127.0.0.1'
server_IP_ADDRESS = '127.0.0.1'


def getIsStop(user="Python") -> bool:
    """获取停止状态（线程安全）"""
    with _state_lock:
        return _is_stop


def getStopReason() -> str:
    """获取停机原因（线程安全）"""
    with _state_lock:
        return _stop_reason


def isManualPaused() -> bool:
    """是否为手动暂停状态（必须手动恢复）"""
    with _state_lock:
        return _is_stop and (_stop_reason == StopReason.MANUAL_PAUSE)


def isDefectStopped() -> bool:
    """是否为缺陷触发停机（可自动恢复）"""
    with _state_lock:
        return _is_stop and (_stop_reason == StopReason.DEFECT_STOP)

# ===== 新增：通知前端恢复上传的异步请求 =====
async def _send_recover_request(base_url: str = None):
    # base_url=None 表示跟随 config.yaml（唯一来源），不再硬编码 localhost:8890
    if base_url is None:
        from AnomalyDetection_Tool.config.settings import get_api_base_url
        base_url = get_api_base_url()
    url = f"{base_url}/v1/defect/log/recover"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url) as response:
                response.raise_for_status()
                info("[自动恢复] 已成功通知前端恢复上传")
                return await response.json() if response.content_type == 'application/json' else {"status": "ok"}
    except Exception as e:
        error(f"[自动恢复] 通知前端失败: {e}")
        return None

def _notify_frontend_recover():
    """起一个子线程跑异步事件循环，防止阻塞 Thrift 服务"""
    def _send():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(_send_recover_request())
            loop.close()
        except Exception as e:
            error(f"[自动恢复] 发送恢复请求线程异常: {e}")
    threading.Thread(target=_send, daemon=True).start()

def getDefectStopTime() -> float:
    """获取缺陷停机发生的时间戳（线程安全）"""
    with _state_lock:
        return _defect_stop_time

def setIsStop(value: bool, status: str, user: str = "Python") -> bool:
    """
    设置停止状态（线程安全）
    """
    # ★ 修复1：加上 _defect_stop_time 的 global 声明
    global _is_stop, _stop_reason, is_stop, _defect_stop_time

    with _state_lock:
        old_value = _is_stop
        old_reason = _stop_reason
        _is_stop = value
        is_stop = value  # 同步兼容变量

        # 根据 status 设置停机原因
        if not value:
            _stop_reason = StopReason.NONE
        elif status == "暂停":
            _stop_reason = StopReason.MANUAL_PAUSE
        elif status == "缺陷停机":
            _stop_reason = StopReason.DEFECT_STOP
        elif status in ("停止",):
            _stop_reason = StopReason.MANUAL_PAUSE

    # ===== 关键修改：在锁外记录是否是从“缺陷停机”恢复到运行 =====
    is_recovering_from_defect = (old_value == True and value == False and old_reason == StopReason.DEFECT_STOP)

    # 锁外执行副作用（避免死锁）
    if status in ("开始", "停止"):
        PCL_reset()  # 完全重置(距离+触发次数归零)
        if status == "开始":
            _fire_start_callbacks()   # 通知外部开启新批次（例如刷新 launch_tag 重建批次目录）
    elif status == "Auto恢复":
        PCL_reset(reset_distance=False)  # ★ 仅复位PLC触发状态，保留距离
        info(f"[系统] ▶ 速度恢复，自动继续检测")
    elif status == "暂停":
        info(f"[系统] ⏸ 手动暂停，停止图片检测，必须手动点击'恢复'才能继续")
    elif status == "缺陷停机":
        # ★ 修复1配套：现在能正确写入全局变量
        _defect_stop_time = time.time()
        info(f"[系统] 缺陷触发停机，等待速度恢复后自动恢复 (停机时刻: {_defect_stop_time})")
    elif status == "恢复":
        info(f"[系统] ▶ 手动恢复运行")

    info(f"[Thrift服务] {user} | 状态: {status} | "
         f"is_stop: {old_value}({old_reason}) -> {_is_stop}({_stop_reason})")

    # 只要是从缺陷停机恢复运行，统一通知前端
    if is_recovering_from_defect:
        info(f"[系统] 检测到从【缺陷停机】恢复运行，通知前端恢复上传")
        _notify_frontend_recover()

    return True

def isDetectionPaused() -> bool:
    """
    是否处于"人为要求停止检测"的状态。

    此状态下应当彻底停下检测链路（含已在队列中的帧、后台经纬分析），
    但进程本身继续运行，等待前端重新点"开始"。
    """
    return isManualPaused()


def setDefectStop() -> bool:
    """
    缺陷检测触发停机（供检测模块调用）
    与手动暂停区分，速度恢复后可自动继续

    ★ 手动停止优先：若当前已是手动停止/暂停，则忽略本次缺陷停机。

    原实现无条件调用 setIsStop(True, "缺陷停机")，会把 _stop_reason
    从 manual 覆盖成 defect。后果很严重：Modbus 的自动恢复状态机只认
    isDefectStopped()，一旦原因被改成 defect，织机速度回升后它就会
    执行 setIsStop(False, "Auto恢复") —— 于是操作工明明点了"停止"，
    检测却自己恢复了。日志里表现为"停止"之后过一会儿又出现
    "True(defect) -> False(none)"。
    """
    if isManualPaused():
        info("[系统] 当前为手动停止状态，忽略缺陷停机请求（保持手动停止）")
        return False
    return setIsStop(True, "缺陷停机", "DefectDetector")


# ==================== Python服务端（供Java调用） ====================
class PythonServiceHandler:
    def setIsStop(self, value, status):
        """Java调用此方法修改Python变量"""
        setIsStop(value, status, "Java")
        return True

    def getIsStop(self):
        """Java获取Python变量"""
        return getIsStop("Java")

    def ping(self):
        return "Python服务正常"


def PCL_reset(reset_distance=True):
    """重置PLC状态 - 操作正在运行的实例

    Args:
        reset_distance: True=完全重置(距离+触发次数归零), False=仅复位PLC触发状态
    """
    running_system = get_running_system()
    if running_system is None:
        error("[PCL_reset] 未找到运行中的 TriggerControlSystem，跳过")
        return

    try:
        if running_system.modbus.is_connected():
            running_system.modbus.send_reset()
            info("[PCL_reset] PLC复位信号已发送")
        else:
            error("[PCL_reset] Modbus未连接")
            return

        if reset_distance:
            # 完全重置：生产开始/停止时使用
            #  调用 TriggerControlSystem 上的统一方法，
            #   同步把内存累计、PLC 持久化寄存器、写回节流状态一并清零，
            #   保证停电重启后累计米长也仍是 0。
            running_system.reset_total_distance()
            info("[PCL_reset] 累计距离、触发次数已全部归零（本会话+PLC持久化）")
        else:
            # 仅复位PLC触发状态：自动恢复时使用，保留距离
            # PLC复位后HC0可能被清零，需重新同步基准值避免脉冲跳变
            time.sleep(0.05)
            new_hc0 = running_system.modbus.read_hc0()
            if new_hc0 is not None:
                running_system.motion.resync_hc0(new_hc0)
            info(f"[PCL_reset] 仅复位PLC触发状态，累计距离保持: "
                 f"{running_system.motion.cumulative_distance:.2f}mm")
    except Exception as e:
        error(f"[PCL_reset] 重置时出错: {e}")

def start_python_server():
    """启动Python服务端（端口9091）"""
    handler = PythonServiceHandler()
    processor = SimpleService.Processor(handler)

    transport = TSocket.TServerSocket(host=local_IP_ADDRESS, port=9091)
    tfactory = TTransport.TBufferedTransportFactory()
    pfactory = TBinaryProtocol.TBinaryProtocolFactory()

    server = TServer.TSimpleServer(processor, transport, tfactory, pfactory)

    info(f"[Python] 服务端启动在 {local_IP_ADDRESS}:9091")

    # 在新线程中启动服务器
    server_thread = threading.Thread(target=server.serve, daemon=True)
    server_thread.start()
    return server_thread


# ==================== Python客户端（调用Java） ====================
class PythonJavaClient:
    def __init__(self, host=server_IP_ADDRESS, port=9090):
        self.host = host
        self.port = port
        self.client = None
        self.transport = None

    def connect(self):
        """连接到Java服务端"""
        try:
            self.transport = TSocket.TSocket(self.host, self.port)
            self.transport = TTransport.TBufferedTransport(self.transport)
            protocol = TBinaryProtocol.TBinaryProtocol(self.transport)
            self.client = CameraService.Client(protocol)
            self.transport.open()
            print(f"[Python客户端] 连接到Java服务端 {self.host}:{self.port}")
            return True
        except Exception as e:
            print(f"[Python客户端] 连接失败: {e}")
            return False

    def pushFrameToJava(self, jpeg_bytes):
        try:
            print("发送图片")
            self.client.pushFrame(jpeg_bytes)
        except TTransportException:
            self.transport.close()
            self.transport.open()
            protocol = TBinaryProtocol.TBinaryProtocol(self.transport)
            client = CameraService.Client(protocol)
            client.pushFrame(jpeg_bytes)

    def close(self):
        """关闭连接"""
        if self.transport:
            self.transport.close()
            print("[Python客户端] 断开与Java的连接")


# ==================== 主程序 ====================
def main():
    # 1. 启动Python服务端（供Java调用）
    print("=== 启动Python服务 ===")
    server_thread = start_python_server()

    # 等待服务端启动
    time.sleep(1)

    # 2. 创建Python客户端（用于调用Java）
    java_client = PythonJavaClient()

    # 3. 连接到Java服务端
    if java_client.connect():

        # 5. 模拟推送图片（这里用文本模拟）
        print("\n=== Python调用Java推送图片 ===")

        # 创建一个模拟的图片数据
        mock_image_data = b'PNG\x89PNG\x1a\n' + b'X' * 100  # 模拟PNG头部
        mock_image_name = "test_image.png"

        # 直接调用（不使用文件）
        try:
            success = java_client.client.pushFrame(mock_image_data, mock_image_name)
            print(f"推送模拟图片结果: {'成功' if success else '失败'}")
        except Exception as e:
            print(f"推送失败: {e}")

        # 保持运行，等待Java调用
        print("\n=== Python等待Java调用 ===")
        print("Python服务运行中...")
        print("Java可以调用 setIsStop(true/false) 修改Python变量")
        print("按 Ctrl+C 退出")

        try:
            while True:
                # 定期检查Java状态
                time.sleep(10)
                print("运行中。。。。。")
        except KeyboardInterrupt:
            print("\n收到中断信号，正在退出...")
        finally:
            java_client.close()

    # 等待服务器线程结束
    server_thread.join(timeout=1)


if __name__ == '__main__':
    main()
