import inspect
import os
import sys
import threading

# # 获取当前脚本的绝对路径
# current_file_path = os.path.abspath(__file__)
# # 获取脚本所在目录的绝对路径
# current_dir = os.path.dirname(current_file_path)
# # 构建目标库的绝对路径
# lib_path = os.path.join(current_dir, "..", "lib", "MvImport")
# # 添加路径到 sys.path
# sys.path.append(lib_path)
sys.path.append("./lib/MvImport")

from lib.MvImport.MvCameraControl_class import *
from General_Tool.EnhancedLogger import info, error, warning
from Camera_Tool import CameraRegistry


# 强制关闭线程
def Async_raise(tid, exctype):
    """
    强制关闭线程
    :param tid: 线程id
    :param exctype: 异常类型
    :return:
    """
    tid = ctypes.c_long(tid)
    if not inspect.isclass(exctype):
        exctype = type(exctype)
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, ctypes.py_object(exctype))
    if res == 0:
        raise ValueError("invalid thread id")
    elif res != 1:
        ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, None)
        raise SystemError("PyThreadState_SetAsyncExc failed")


# 停止线程
def Stop_thread(thread):
    """
    停止线程
    :param thread:
    :return:
    """
    Async_raise(thread.ident, SystemExit)


def To_hex_str(num):
    """
    转为16进制字符串
    :param num: 数值
    :return:
    """
    chaDic = {10: 'a', 11: 'b', 12: 'c', 13: 'd', 14: 'e', 15: 'f'}
    hexStr = ""
    if num < 0:
        num = num + 2 ** 32
    while num >= 16:
        digit = num % 16
        hexStr = chaDic.get(digit, str(digit)) + hexStr
        num //= 16
    hexStr = chaDic.get(num, str(num)) + hexStr
    return hexStr


def Creat_IntPtr(str_value):
    # user_string = str_value
    # user_buffer = ctypes.create_string_buffer(user_string.encode('utf-8'))
    # user_ptr = ctypes.cast(user_buffer, ctypes.c_void_p)
    user_ptr = ctypes.py_object(str_value)
    return user_ptr


def Read_IntPtr_Value(int_part_value):
    # 将 void* 指针转换为字符串指针
    # user_ptr = ctypes.cast(int_part_value, ctypes.POINTER(ctypes.c_char))
    user_value = ctypes.cast(int_part_value, ctypes.py_object).value
    # # 读取原始字符串数据
    # # 注意：这里假设字符串以 null 结尾
    # user_data = ctypes.string_at(user_ptr)
    # # 转换为 Python 字符串 (解码字节串)
    # user_str = user_data.decode('utf-8')
    return user_value


class CameraOperation:
    def __init__(self, cam_object, st_device_list, n_connect_num=0, b_open_device=False,
                 b_regist_callback=False, b_start_grabbing=False, camera_name: str = None):

        self.cam_object = cam_object
        self.b_start_grabbing = b_start_grabbing
        self.b_regist_callback = b_regist_callback
        self.b_open_device = b_open_device
        self.n_connect_num = n_connect_num
        self.st_device_list = st_device_list
        self.st_mode_name = ""
        self.st_ip_address = ""
        self.st_serial_number = ""

        self.camera_name = camera_name
        self.trigger_source = "Line0"

        self._trigger_switch_lock = threading.Lock()

        #  新增：软触发调用计数器，用于诊断 trigger_all_software() 是否真的被周期性调用
        # 可通过 get_diag_info() 查看，排查完问题后可以不再关注
        self._diag_soft_trigger_call_count = 0
        self._diag_soft_trigger_success_count = 0
        self._diag_soft_trigger_last_ret = None

    def Open_device(self):
        # try:
        if self.b_open_device is False:
            # 选择设备并创建句柄
            nConnectionNum = int(self.n_connect_num)
            stDeviceList = cast(self.st_device_list.pDeviceInfo[int(nConnectionNum)],
                                POINTER(MV_CC_DEVICE_INFO)).contents
            if stDeviceList.nTLayerType == MV_GIGE_DEVICE or stDeviceList.nTLayerType == MV_GENTL_GIGE_DEVICE:
                # 构建GigE设备的设备名称和IP
                for per in stDeviceList.SpecialInfo.stGigEInfo.chModelName:
                    if per == 0:
                        break
                    self.st_mode_name = self.st_mode_name + chr(per)

                nip1 = ((stDeviceList.SpecialInfo.stGigEInfo.nCurrentIp & 0xff000000) >> 24)
                nip2 = ((stDeviceList.SpecialInfo.stGigEInfo.nCurrentIp & 0x00ff0000) >> 16)
                nip3 = ((stDeviceList.SpecialInfo.stGigEInfo.nCurrentIp & 0x0000ff00) >> 8)
                nip4 = (stDeviceList.SpecialInfo.stGigEInfo.nCurrentIp & 0x000000ff)

                sn = stDeviceList.SpecialInfo.stGigEInfo.chSerialNumber
                # 将数组转换为 ASCII 字符串
                self.st_serial_number = ''.join(chr(byte) for byte in sn if byte != 0)
                # 合并为一个 IP 地址字符串
                self.st_ip_address = f"{nip1}.{nip2}.{nip3}.{nip4}"
                # print(f"设备模块名称: {self.st_mode_name} ,SN:{self.st_serial_number},IP: {self.st_ip_address}\n")

            # 创建设备实例
            self.cam_object = MvCamera()
            ret = self.cam_object.MV_CC_CreateHandle(stDeviceList)
            if ret != 0:
                self.cam_object.MV_CC_DestroyHandle()
                raise Exception(f"设备 {self.st_serial_number} 创建句柄失败! ret:0x{ret:x}")
            # 打开设备
            ret = self.cam_object.MV_CC_OpenDevice(MV_ACCESS_Exclusive, 0)
            if ret != 0:
                self.b_open_device = False
                raise Exception(f"打开设备 {self.st_serial_number} 失败! ret:0x{ret:x}")
            self.b_open_device = True

            # 探测网络最佳包大小(只对GigE相机有效)
            if stDeviceList.nTLayerType == MV_GIGE_DEVICE or stDeviceList.nTLayerType == MV_GENTL_GIGE_DEVICE:
                nPacketSize = self.cam_object.MV_CC_GetOptimalPacketSize()
                if int(nPacketSize) > 0:
                    ret = self.cam_object.MV_CC_SetIntValue("GevSCPSPacketSize", nPacketSize)
                    if ret != 0:
                        raise Exception(f"警告：设备 {self.st_serial_number} 设置数据包大小失败! ret:0x{ret:x}")
                else:
                    raise Exception(f"警告：设备 {self.st_serial_number} 获取数据包大小失败!  ret:0x{ret:x}")

            # 关键修复：显式设置 TriggerSelector = FrameStart 
            # 海康相机的触发体系分三层：TriggerSelector（选择触发哪种动作，
            # 常见取值 FrameStart/FrameBurstStart/AcquisitionStart 等）
            # → TriggerMode（该选择器对应的触发开关 On/Off）
            # → TriggerSource（该选择器的触发信号来源，Line0/Software等）。
            # 如果 TriggerSelector 没有被显式设置成 FrameStart，
            # 那么后续对 TriggerMode / TriggerSource 的设置可能作用在了
            # 相机默认选中的其他触发通道上（取决于具体型号的出厂默认值），
            # 表现为：SetEnumValueByString 调用全部返回0(成功)，
            # 但实际拍照行为完全不受影响 —— 这跟"切换触发源后不出图，
            # 但也不报任何错误"的现象高度吻合，是最容易被忽略的一个参数。
            ret = self.cam_object.MV_CC_SetEnumValueByString("TriggerSelector", "FrameStart")
            if ret != 0:
                raise Exception(f"设备 {self.st_serial_number} 设置TriggerSelector失败! ret:0x{ret:x}")

            # 显式开启触发模式(TriggerMode=On)
            ret = self.cam_object.MV_CC_SetEnumValueByString("TriggerMode", "On")
            if ret != 0:
                raise Exception(f"设备 {self.st_serial_number} 设置触发模式(TriggerMode=On)失败! ret:0x{ret:x}")

            # NOTE:设置触发源为软触发,后期使用时改为 Line0
            ret = self.cam_object.MV_CC_SetEnumValueByString("TriggerSource", "Line0")  # Software Line0
            if ret != 0:
                raise Exception(f"设备 {self.st_serial_number} 设置触发源失败! ret:0x{ret:x}")
            self.trigger_source = "Line0"

            ret = self.cam_object.MV_CC_SetImageNodeNum(1)
            if ret != 0:
                raise Exception(f"设备 {self.st_serial_number} 设置缓存数量失败! ret:0x{ret:x}")

            info(f"设备 [{self.st_serial_number}] 启动成功！"
                 f"(TriggerSelector=FrameStart, TriggerMode=On, TriggerSource=Line0)")

            if self.camera_name is None:
                self.camera_name = self.st_serial_number or f"camera_{self.n_connect_num}"
            CameraRegistry.register_camera(self.camera_name, self)

            return 0
        else:
            return -1

    # except Exception as e:
    #     print(e)
    #     self.cam_object.MV_CC_CloseDevice()
    #     self.cam_object.MV_CC_DestroyHandle()

    def Registration_callback(self, CALL_BACK_FUN):
        if self.b_open_device:
            pUser = Creat_IntPtr(self.st_serial_number)
            ret = self.cam_object.MV_CC_RegisterImageCallBackEx(CALL_BACK_FUN, pUser)
            if ret != 0:
                raise Exception(f"设备 {self.st_serial_number} 注册图像回调失败! ret:0x{ret:x}")
            self.b_regist_callback = True
            return ret
        else:
            print(f"设备 {self.st_serial_number} 注册图像回调失败! 设备未开启。")
            return -1

    def Start_grabbing(self):
        """
        开始取图
        :return:
        """
        if self.b_open_device and self.b_regist_callback:
            ret = self.cam_object.MV_CC_StartGrabbing()
            if ret != 0:
                raise Exception(f"设备 {self.st_serial_number} 开始抓取失败! ret:0x{ret:x}")
            self.b_start_grabbing = True
            return ret
        else:
            return -1
            # raise Exception(f"设备 {self.st_serial_number} 开始抓取失败! 设备未开启或回调未成功注册。")

    def Stop_grabbing(self):
        """
        开始取图
        """
        if self.b_start_grabbing and self.b_open_device:
            ret = self.cam_object.MV_CC_StopGrabbing()
            if ret != 0:
                raise Exception(f"设备 {self.st_serial_number} 停止抓取失败! ret:0x{ret:x}")
            self.b_start_grabbing = False
            return ret
        else:
            raise Exception(f"设备 {self.st_serial_number} 停止抓取失败! ")

    def Close_device(self):
        """
        # 停止取图
        """
        if self.b_open_device:
            ret = self.cam_object.MV_CC_CloseDevice()
            if ret != 0:
                raise Exception(f"设备 {self.st_serial_number} 关闭设备失败! ret:0x{ret:x}")
            # ch:销毁句柄 | Destroy handle
            self.cam_object.MV_CC_DestroyHandle()
            self.b_open_device = False
            self.b_start_grabbing = False

            if self.camera_name:
                CameraRegistry.unregister_camera(self.camera_name)

            return ret
        else:
            raise Exception(f"设备 {self.st_serial_number} 关闭设备失败!")

        # 软触发一次

    def Trigger_once(self):
        """
        软触发一次

         诊断增强：无论成功失败都打印 INFO 级别日志（不再依赖DEBUG，
        避免因为日志系统 handler 级别过滤导致关键诊断信息完全看不到）。
        同时维护调用计数器，方便确认这个函数到底有没有被周期性调用到。
        排查问题结束后，如果觉得日志太吵，可以把下面的 info() 改回 debug()。
        """
        if self.b_open_device:
            self._diag_soft_trigger_call_count += 1
            ret = self.cam_object.MV_CC_SetCommandValue("TriggerSoftware")
            self._diag_soft_trigger_last_ret = ret
            if ret != 0:
                error(f"设备 {self.st_serial_number} 软触发失败! ret:0x{ret:x} "
                      f"(第{self._diag_soft_trigger_call_count}次调用)")
            else:
                self._diag_soft_trigger_success_count += 1
                info(f"设备 {self.st_serial_number} 软触发命令已发送成功 "
                     f"(第{self._diag_soft_trigger_call_count}次调用, "
                     f"累计成功{self._diag_soft_trigger_success_count}次)")
            return ret
        raise Exception(f"设备 {self.st_serial_number} 取图失败设备未开启!")

    def get_diag_info(self) -> dict:
        """诊断信息：软触发调用次数、成功次数、最近一次返回码"""
        return {
            "camera_name": self.camera_name,
            "serial": self.st_serial_number,
            "trigger_source": self.trigger_source,
            "b_open_device": self.b_open_device,
            "b_start_grabbing": self.b_start_grabbing,
            "soft_trigger_call_count": self._diag_soft_trigger_call_count,
            "soft_trigger_success_count": self._diag_soft_trigger_success_count,
            "soft_trigger_last_ret": self._diag_soft_trigger_last_ret,
        }

    def Set_trigger_source(self, source: str) -> int:
        """
        动态切换触发源。

        修复历史：
        1. 取流过程中直接热切换 TriggerSource 在部分机型上"返回成功但不生效"，
           改为 StopGrabbing → 改参数 → StartGrabbing 的方式，保证一定生效。
        2. 补充在切换后重新确认 TriggerSelector（防御性，理论上 Open_device 时
           已经设置好且开流期间不会自动被重置，但网络异常重连等场景下加一层保险）。
        """
        if not self.b_open_device:
            warning(f"设备 {self.st_serial_number} 未开启，无法切换触发源")
            return -1

        if self.trigger_source == source:
            return 0

        with self._trigger_switch_lock:
            if self.trigger_source == source:
                return 0

            was_grabbing = self.b_start_grabbing

            if was_grabbing:
                stop_ret = self.cam_object.MV_CC_StopGrabbing()
                if stop_ret != 0:
                    error(f"设备 {self.st_serial_number} 切换触发源前停止取流失败! "
                          f"ret:0x{stop_ret:x}，本次切换中止")
                    return stop_ret
                self.b_start_grabbing = False
                info(f"设备 {self.st_serial_number} 已停止取流，准备切换触发源为 {source}")

            # 防御性：确保 TriggerSelector 仍是 FrameStart（正常不会变，多此一举但成本很低）
            sel_ret = self.cam_object.MV_CC_SetEnumValueByString("TriggerSelector", "FrameStart")
            if sel_ret != 0:
                error(f"设备 {self.st_serial_number} 切换前重设TriggerSelector失败! ret:0x{sel_ret:x}")

            ret = self.cam_object.MV_CC_SetEnumValueByString("TriggerSource", source)
            if ret != 0:
                error(f"设备 {self.st_serial_number} 切换触发源为 {source} 失败! ret:0x{ret:x}")
            else:
                self.trigger_source = source
                info(f"设备 {self.st_serial_number} 触发源已切换为 {source}")

            if was_grabbing:
                start_ret = self.cam_object.MV_CC_StartGrabbing()
                if start_ret != 0:
                    error(f"设备 {self.st_serial_number} 切换触发源后重新开流失败! "
                          f"ret:0x{start_ret:x}，相机将无法出图，需要人工介入排查")
                    return start_ret
                self.b_start_grabbing = True
                info(f"设备 {self.st_serial_number} 已重新开始取流 (触发源={self.trigger_source})")

            return ret