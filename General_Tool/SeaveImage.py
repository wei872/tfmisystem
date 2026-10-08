import datetime

import cv2
import numpy

from General_Tool.EnhancedLogger import info

BASE_SAVE_PATH = ""


def image_seave(custom_path, data):
    """
    保存图片到本地
    :param data: 要保存的图片数据
    :param custom_path: 包含文件后缀（.png .jpg .bmp）
    :return:
    """
    filename = f"{BASE_SAVE_PATH}/{custom_path}"
    imwrite_info = cv2.imwrite(filename, data)
    if imwrite_info:
        info(f"成功保存到路径：{filename}")
    else:
        info(f"保存文件失败{imwrite_info}")


def salt_treatment(image) -> numpy:
    """
    对原图进行无视觉干扰的加盐处理（暂不实现）
    :param image: 需要保存的分类结果切片图
    :return: salt_iamge
    """
    pass
