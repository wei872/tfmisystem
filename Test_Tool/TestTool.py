import os
import sys
import time

import cv2
import numpy as np



def press_any_key_exit(prompt="wite..."):
    while True:
        char = sys.stdin.read(1)
        if char.lower() == "q":
            print()
            return "q"
def load_image(image_path):
    """
    支持中文路径的图像加载函数
    解决OpenCV cv2.imread不支持中文路径的问题

    参数:
        image_path (str): 图像路径（可包含中文）

    返回:
        numpy.ndarray: 加载的图像，如果失败返回None
    """
    try:
        # 以二进制模式读取文件
        with open(image_path, 'rb') as f:
            img_data = np.frombuffer(f.read(), dtype=np.uint8)

        # 使用imdecode解码图像
        image = cv2.imdecode(img_data, cv2.IMREAD_COLOR)

        # 检查是否成功加载
        if image is None:
            print(f"无法解码图像: {image_path}")
            return None

        return image
    except Exception as e:
        print(f"加载图像错误: {image_path}, 错误: {str(e)}")
        return None
