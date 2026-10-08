import serial
import time
from typing import Optional


class GPDController:
    """GPD光源控制器通讯协议类"""

    def __init__(self, port: str = 'COM1', baudrate: int = 9600):
        """
        初始化串口连接
        :param port: 串口号，如 'COM1' (Windows) 或 '/dev/ttyUSB0' (Linux)
        :param baudrate: 波特率，默认9600
        """
        self.ser = None
        try:
            self.ser = serial.Serial(
                port=port,
                baudrate=baudrate,
                bytesize=serial.EIGHTBITS,
                stopbits=serial.STOPBITS_ONE,
                parity=serial.PARITY_NONE,
                timeout=1
            )
            print(f"✓ 串口 {port} 已成功打开")
        except Exception as e:
            print(f"✗ 串口打开失败: {e}")

    def is_connected(self) -> bool:
        """检查串口是否已连接"""
        return self.ser is not None and self.ser.is_open

    def calculate_xor_checksum(self, data: str) -> str:
        """计算异或校验和"""
        xor_result = 0
        for char in data:
            xor_result ^= ord(char)
        high_nibble = (xor_result >> 4) & 0x0F
        low_nibble = xor_result & 0x0F
        return f"{high_nibble:X}{low_nibble:X}"

    def build_command(self, func_cmd: str, op_cmd: str, channel: int, param: int) -> str:
        """构建完整的指令"""
        feature = '$'
        channel_char = str(channel)
        param_str = f"{param:03X}"
        data_without_checksum = f"{feature}{func_cmd}{op_cmd}{channel_char}{param_str}"
        checksum = self.calculate_xor_checksum(data_without_checksum)
        return data_without_checksum + checksum

    def send_command(self, command: str) -> Optional[str]:
        """发送指令并接收响应"""
        if not self.is_connected():
            print("✗ 串口未连接")
            return None

        try:
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            self.ser.write(command.encode('ascii'))
            print(f"→ 发送: {command}")

            time.sleep(0.1)
            if self.ser.in_waiting > 0:
                response = self.ser.read(self.ser.in_waiting).decode('ascii')
                print(f"← 接收: {response}")
                return response
            else:
                print("  (未收到响应)")
                return None
        except Exception as e:
            print(f"✗ 通讯错误: {e}")
            return None

    def turn_on(self, channel: int) -> Optional[str]:
        """打开指定通道"""
        cmd = self.build_command('1', '1', channel, 0)
        return self.send_command(cmd)

    def turn_off(self, channel: int) -> Optional[str]:
        """关闭指定通道"""
        cmd = self.build_command('1', '2', channel, 0)
        return self.send_command(cmd)

    def set_brightness(self, channel: int, brightness: int) -> Optional[str]:
        """设置亮度 (0-4095)"""
        if not 0 <= brightness <= 4095:
            print(f"✗ 亮度值超出范围: {brightness}")
            return None
        cmd = self.build_command('1', '3', channel, brightness)
        return self.send_command(cmd)

    def read_brightness(self, channel: int) -> Optional[str]:
        """读取亮度"""
        cmd = self.build_command('1', '4', channel, 0)
        return self.send_command(cmd)

    def save_settings(self, channel: int) -> Optional[str]:
        """保存设置"""
        cmd = self.build_command('1', 'S', channel, 0)
        return self.send_command(cmd)

    def close(self):
        """关闭串口"""
        if self.ser and self.ser.is_open:
            self.ser.close()
            print("串口已关闭")


def test_protocol():
    """测试协议功能"""
    print("=" * 50)
    print("GPD光源控制器通讯协议测试")
    print("=" * 50)

    # ========== 创建控制器（只创建一次！）==========
    controller_face = GPDController(port='COM4')  # ← 修改为您的实际端口
    controller_back = GPDController(port='COM3')  # ← 修改为您的实际端口

    if not controller_face.is_connected():
        print("\n串口连接失败，程序退出")
        return
    if not controller_back.is_connected():
        print("\n串口连接失败，程序退出")
        return

    # ========== 校验和测试 ==========
    print("\n" + "-" * 30)
    print("校验和测试:")
    test_data = "$132032"
    checksum_0 = controller_face.calculate_xor_checksum(test_data)
    checksum_1 = controller_back.calculate_xor_checksum(test_data)

    print(f"数据: {test_data}")
    print(f"面光计算的校验和: {checksum_0}")
    print(f"背光计算的校验和: {checksum_1}")

    print(f"期望的校验和: 25")
    print(f"面光校验{'通过!' if checksum_0 == '25' else '失败!'}")
    print(f"背光校验{'通过!' if checksum_1 == '25' else '失败!'}")

    # ========== 指令构建测试 ==========
    print("\n" + "-" * 30)
    print("指令构建测试:")
    cmd = controller_face.build_command('1', '3', 2, 50)
    print(f"设置通道2亮度为50的指令: {cmd}")
    print(f"期望指令: $13203225")
    print(f"指令{'正确!' if cmd == '$13203225' else '错误!'}")

    # ========== 实际串口通讯测试 ==========
    print("\n" + "=" * 50)
    print("开始实际串口通讯测试...")
    print("=" * 50)

    try:
        # 1. 设置通道2亮度为50

        controller_face.set_brightness(1, 125)
        controller_face.set_brightness(2, 125)
        controller_face.set_brightness(3, 125)
        controller_face.set_brightness(4, 125)
        controller_back.set_brightness(1, 44)
        controller_back.set_brightness(2, 44)
        controller_back.set_brightness(3, 44)
        controller_back.set_brightness(4, 44)
        time.sleep(1)
        # for i in range(4):
        #     controller_face.turn_off(i)
        # controller_back.turn_off(1)
        # # 2. 打开通道2
        # print("\n【测试2】打开通道2")
        # controller.turn_on(2)
        # time.sleep(0.5)
        #
        # # 3. 读取通道2亮度
        # print("\n【测试3】读取通道2亮度")
        # controller.read_brightness(2)
        # time.sleep(0.5)
        #
        # # 4. 设置通道1亮度为100
        # print("\n【测试4】设置通道1亮度为100")
        # controller.set_brightness(1, 100)
        # time.sleep(0.5)
        #
        # 5. 关闭通道2
        print("\n【测试5】关闭通道2")
        # controller_face.turn_off(1)
        # controller_face.turn_off(2)
        # controller_face.turn_off(3)
        # controller_face.turn_off(4)
        # controller_back.turn_off(1)

        # time.sleep(0.5)
        #
        # 6. 保存设置
        # print("\n【测试6】保存通道2设置")
        # controller.save_settings(2)

    except Exception as e:
        print(f"测试过程出错: {e}")

    finally:
        # 关闭串口
        controller_face.close()
        controller_back.close()

    print("\n" + "=" * 50)
    print("测试完成!")
    print("=" * 50)


if __name__ == "__main__":
    test_protocol()

'''
┌────┬────┬────┬────┬──────────┬────────┐
│ $  │ 1  │ 3  │ 2  │   032    │   25   │
├────┼────┼────┼────┼──────────┼────────┤
│特征 │功能 │操作 │ 通道│  参数值   │ 校验和  │
│字符 │命令 │命令 │ 号  │(3位十六进制)│        │
└────┴────┴────┴────┴──────────┴────────┘
  固定  ↓    ↓    ↓      ↓         ↓
       │    │    │      │         │
       │    │    │      │         └─ 通过计算得出
       │    │    │      │
       │    │    │      └─ 50 转成十六进制 = 032
       │    │    │
       │    │    └─ 通道2，就写 2
       │    │
       │    └─ 3 = 设置亮度（查表得知）
       │
       └─ 1 = 光源控制（查表得知）

       '''

