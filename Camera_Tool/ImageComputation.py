from typing import List, Any
from enum import Enum

import cv2

# 示意图以及计算说明文档连接 https://www.yuque.com/zhanghan-yeuuc/mgx8w7/gsyvfe4w2we3i2f3
# 以C1R1为坐标原点建立一个坐标轴，以各点距离C1R1为坐标。
# 标尺度数
RULER_DATA = [
    (50, 110),  # C1(R1,R2)
    (100, 210),  # C2(R1,R2)
    (500, 610),  # C3(R1,R2)
    (600, 710)]  # C4(R1,R2)
image_width, image_height = 3072, 0
pixel_scale = 0  # 像素到实际的转换比例系数
cam_to_bg = 0  # OF
fabric_to_bg = 0  # EF
triangle_scale = (cam_to_bg - fabric_to_bg) / cam_to_bg  # 三角相似比例系数
R1_reading = 0  # 浸胶机边缘（左）的真实度数（计算偏移的基准）
R2_reading = 0  # 浸胶机边缘（右）的真实度数（计算偏移的基准）


class CameraID(Enum):
    DA4474416 = 1
    DA4474385 = 2
    DA4474398 = 3
    DA4474414 = 4


def get_pixel_scale():
    """计算并返回当前的像素比例尺"""
    global pixel_scale
    # 在实际代码中，这里应该检查 image_width 是否为0，以避免除以零错误
    if image_width == 0:
        return pixel_scale  # 或者抛出一个异常，或者返回一个默认值
    else:
        pixel_scale = abs(RULER_DATA[0][0] - RULER_DATA[1][0]) / image_width


def computation_Px(edge_pixel):
    """
    计算帘子布边缘到图像边缘的真实距离
    :return:
    """
    # 边缘到图像边缘的像素距离
    pixel_distance_to_edge = image_width - edge_pixel

    # 转换为真实距离（布料平面）
    real_distance = pixel_distance_to_edge * pixel_scale

    return real_distance


def edge_detection(iamge):
    """
    查找像素边缘
    :param iamge: 图像数据
    :return: 返回查找到的像素值，如果图片中没有边缘则返回 -1
    """
    global image_width, image_height
    image_width, image_height = iamge.shape
    edge_pixel = 0

    return edge_pixel


def overlapping_areas_handle(cam_id, edge_pixel):
    """
    处理重叠区域的计算
    :param cam_id: 相机的ID序号
    :param edge_pixel: 像素坐标
    :return: 处理结果
    """
    new_pixel = 0

    return new_pixel


def compute_fabric_parameters(P1_R, P2_R):
    """
    计算布幅和偏心距的主要函数
    :param P1_R: 布料边缘的到图像边缘的真实距离（布料平面）
    :param P2_R: 布料边缘的到图像边缘的真实距离（布料平面）
    :return: 计算结果字典
    """
    # 整个画面最左到最右的距离（布料平面）
    C1R1_C4R2_width = abs(RULER_DATA[-1][1] - RULER_DATA[0][0]) * triangle_scale
    edge_P = abs(P1_R + P2_R)
    # 布幅
    fabric_with = C1R1_C4R2_width - edge_P  # 布幅

    # 计算偏心距
    dist_R1_C1 = abs(RULER_DATA[0][0] - R1_reading) * triangle_scale
    dist_R2_C4 = abs(RULER_DATA[-1][1] - R2_reading) * triangle_scale

    eccentricity = 0.5 * ((P1_R - dist_R1_C1) + (P2_R - dist_R2_C4))
    if P1_R > P2_R:  # 后面改为南北
        offset_direction = "right"
    else:
        offset_direction = "left"
    return {
        "P1_width": P1_R,
        "P2_width": P2_R,
        "fabric": fabric_with,
        "eccentricity": eccentricity,
        "offset_direction": offset_direction,
        "units": "mm"
    }


def Detection(images: List[Any]):
    p1, p2 = 0, 0
    for i, image in images:
        # 寻找边缘像素
        edge_pixel = edge_detection(image["iamge"])
        if edge_pixel != -1:
            # 处理重叠区域数据
            pixel_value = overlapping_areas_handle(image["cam_id"], edge_pixel)
        else:
            pixel_value = edge_pixel
        # 判断边缘方位
        if image["cam_id"] == CameraID.DA4474385 or image["cam_id"] == CameraID.DA4474414:
            p1 = pixel_value
        else:
            p2 = pixel_value

    # 将边缘布料边缘到图像边缘的像素距离转为真实距离（布料平面）
    p1_real = computation_Px(p1)
    p2_real = computation_Px(p2)
    # 计算布幅和偏心距的主要函数
    compute_fabric_parameters(p1_real, p2_real)


if __name__ == '__main__':
    image = []
    # edge_detection(image)
