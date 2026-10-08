import json
import time

import cv2
import numpy as np
import aiohttp
import asyncio
from typing import Dict, Any, Optional, Union, List

BASE_URL = "http://localhost:8890/api"


async def upload_cv2_image(
        image: np.ndarray,
        api_url: str = "/image/upload",
        file_param_name: str = "file",
        extra_data: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: int = 30
) -> Optional[Dict[str, Any]]:
    """
    将 OpenCV 图像直接异步上传到 Spring Boot API

    Args:
        image: OpenCV 图像 (NumPy 数组)
        api_url: 接口地址
        file_param_name: Spring 接收文件的参数名 (@RequestParam)
        headers: 请求头
        timeout: 超时时间
        extra_data: 数据描述信息
    """

    # 1. 将内存中的图像编码为二进制流
    # 根据扩展名自动选择编码格式 (.jpg, .png 等)
    extension = "." + extra_data.get("imageType").lower()
    success, buffer = cv2.imencode(extension, image)

    if not success:
        info("错误：图像编码失败")
        return None

    # 转换为字节流
    image_bytes = buffer.tobytes()

    # 2. 构造请求头
    default_headers = {
        "User-Agent": "Python-Async-CV2-Client",
    }
    if headers:
        default_headers.update(headers)

    # 3. 构造 Multipart 表单数据
    data = aiohttp.FormData()
    try:
        cam_id = extra_data.get("cameraId", "unknown")

        # 处理列表类型的尺寸和坐标
        d_size_list = extra_data.get("defectSize", [0, 0])
        d_size = f"{d_size_list[0]}x{d_size_list[1]}"

        d_center_list = extra_data.get("defectCenter", [0, 0])
        d_center = f"[{d_center_list[0]}_{d_center_list[1]}]"

        ts = extra_data.get("timestamp", 0)

        # 过滤掉类型名称中的空格
        d_type = str(extra_data.get("defectType", "none")).replace(" ", "_")
        img_ext = extra_data.get("imageType", "jpg")

        filename = f"{cam_id}_{d_size}_{d_center}_{ts}_{d_type}.{img_ext}"
        info(f"文件名：{filename}")
    except (KeyError, IndexError, TypeError) as e:
        info(f"文件名拼接失败，请检查 extra_data 结构: {e}")
        filename = f"upload_{extra_data.get('timestamp', 'now')}.jpg"
    data.add_field(
        file_param_name,
        image_bytes,
        filename=filename,
        content_type=f"image/{extension.lstrip('.')}"
    )
    ex_data = json.dumps(extra_data, ensure_ascii=False, indent=2)
    data.add_field("qoJson", ex_data, content_type='application/json')

    try:
        async with aiohttp.ClientSession(headers=default_headers) as session:
            full_url = BASE_URL + api_url


            async with session.post(
                    full_url,
                    data=data,
                    timeout=aiohttp.ClientTimeout(total=timeout)
            ) as response:
                # 处理响应
                if response.status != 200:
                    text = await response.text()
                    info(f"错误：HTTP {response.status}，内容：{text}")
                    return None

                result = await response.json()
                info(f"上传成功！响应：{result}")
                return result

    except asyncio.TimeoutError:
        info("错误：请求超时")
        return None
    except aiohttp.ClientError as e:
        info(f"错误：网络连接异常 → {str(e)}")
        return None
    except Exception as e:
        info(f"错误：未知异常 → {str(e)}")
        return None


async def batch_upload_cv2_images(
        images: List[np.ndarray],
        api_url: str,
        file_param_name: str = "file",
        extra_data: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    """
    批量并发上传多张 OpenCV 图像
    """
    tasks = []
    for i, img in enumerate(images):
        # 为每张图生成一个虚拟文件名
        task = upload_cv2_image(
            img,
            api_url,
            file_param_name,
            filename=f"frame_{i}.jpg",
            extra_data=extra_data
        )
        tasks.append(task)

    # 并发执行
    results = await asyncio.gather(*tasks)
    return [r for r in results if r is not None]


async def batch_upload_cv2_images_async(sample_img, qoJson):
    # qoJson = {
    #     "machineId": "094",  # 机台号
    #     "cameraId": "CAM_001",  # 相机编号
    #     "imageSize": [sample_img.shape[0], sample_img.shape[1]],  # 图片大小
    #     "imageType": "jpg",  # 图片编码格式
    #     "colorSpace": "RGB",  # 图片的色彩空间
    #     "defectType": "毛羽",  # 疵点类型
    #     "confidence": 0.9,  # yolo模型推理的置信度
    #     "timestamp": 1770298394 + i,  # 时间戳
    #     "isStop": 1,  # 0：不停机疵点；1：停机疵点
    #     # 列表会自动转为 JSON 字符串
    #     "defectCenter": [120, 130],  # 疵点中心坐标
    #     "defectSize": [10, 20],  # 疵点大小
    #     "fabricCoords": [1230, 32],  # 疵点的布料空间坐标
    #     "additionalInfo": ["person", "mask"],  # 附加信息 字符串列表
    # }

    # 单张上传
    await upload_cv2_image(sample_img, extra_data=qoJson)


# --- 使用示例 ---
async def main():
    API_URL = "http://192.168.123.98:8890/api/image/upload"  # Spring 接口地址
    # API_URL = "http://192.168.123.209:8890/api/image/upload"
    # 1. 模拟一个 OpenCV 图像 (RGB/BGR)
    # 实际场景中可能是：img = cv2.imread("test.jpg") 或来自摄像头
    sample_img = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.putText(sample_img, "Test Image", (100, 200),
                cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
    for i in range(3):
        qoJson = {
            "machineId": "094",  # 机台号
            "cameraId": "CAM_001",  # 相机编号
            "imageSize": [sample_img.shape[0], sample_img.shape[1]],  # 图片大小
            "imageType": "jpg",  # 图片编码格式
            "colorSpace": "RGB",  # 图片的色彩空间
            "defectType": "毛羽",  # 疵点类型
            "confidence": 0.9,  # yolo模型推理的置信度
            "timestamp": 1770298394 + i,  # 时间戳
            "isStop": 1,  # 0：不停机疵点；1：停机疵点
            # 列表会自动转为 JSON 字符串
            "defectCenter": [120, 130],  # 疵点中心坐标
            "defectSize": [10, 20],  # 疵点大小
            "fabricCoords": [1230, 32],  # 疵点的布料空间坐标
            "additionalInfo": ["person", "mask"],  # 附加信息 字符串列表
        }

        # 单张上传
        await upload_cv2_image(sample_img, extra_data=qoJson)
        time.sleep(5)

    # # 批量上传示例
    # img_list = [sample_img, sample_img]
    # await batch_upload_cv2_images(img_list, API_URL, extra_data=payload)


if __name__ == "__main__":
    asyncio.run(main())
    # asyncio.run(upload_cv2_image(sample_img, extra_data=qoJson))


