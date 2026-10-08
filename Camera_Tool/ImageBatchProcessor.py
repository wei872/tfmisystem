import asyncio
import threading
from collections import defaultdict
from typing import Dict, List, Callable, Any, Optional
import numpy as np
from concurrent.futures import ThreadPoolExecutor


class ImageBatchProcessor:
    def __init__(self,
                 process_callback: Callable[[int, List[Any]], Any],
                 max_workers: int = 4):
        """
        图像批处理器 - 每个编号满4个图像时触发处理

        Args:
            process_callback: 图像处理回调函数 (编号, 图像列表) -> 处理结果
            max_workers: 处理线程池大小
        """
        # 每个编号的图像缓冲区
        self.image_buffers: Dict[int, List[Any]] = defaultdict(list)
        # 每个编号的锁，确保线程安全
        self.buffer_locks: Dict[int, threading.Lock] = defaultdict(threading.Lock)
        self.process_callback = process_callback
        # 使用线程池处理计算密集型的图像处理任务
        self.process_executor = ThreadPoolExecutor(max_workers=max_workers)

    async def add_image(self, identifier: int, image_data: Any):
        """
        添加图像到指定编号的缓冲区

        Args:
            identifier: 图像编号
            image_data: 图像数据
        """
        # 异步执行添加操作
        await asyncio.to_thread(self._add_image_sync, identifier, image_data)

    def _add_image_sync(self, identifier: int, image_data: Any):
        """
        同步添加图像（线程安全）
        """
        with self.buffer_locks[identifier]:
            self.image_buffers[identifier].append(image_data)
            print(f"编号 {identifier} 添加图像，当前数量: {len(self.image_buffers[identifier])}/4")

            # 检查是否达到4个图像
            if len(self.image_buffers[identifier]) == 4:
                print(f"编号 {identifier} 已收集满4个图像，开始处理...")

                # 获取完整的图像批次
                image_batch = self.image_buffers[identifier].copy()
                # 清空缓冲区
                self.image_buffers[identifier].clear()

                # 在线程池中异步处理图像批次
                self.process_executor.submit(
                    self._process_image_batch, identifier, image_batch
                )

    def _process_image_batch(self, identifier: int, image_batch: List[Any]):
        """
        处理图像批次（在线程池中执行）
        """
        try:
            print(f"正在处理编号 {identifier} 的图像批次...")
            # 执行用户定义的处理逻辑
            result = self.process_callback(identifier, image_batch)
            print(f"编号 {identifier} 处理完成，结果: {result}")

        except Exception as e:
            print(f"处理编号 {identifier} 的图像时发生错误: {e}")
            # 可以选择将处理失败的图像重新放回缓冲区
            with self.buffer_locks[identifier]:
                self.image_buffers[identifier].extend(image_batch)

    def get_buffer_status(self, identifier: int) -> int:
        """获取指定编号的缓冲区当前图像数量"""
        with self.buffer_locks[identifier]:
            return len(self.image_buffers[identifier])

    async def flush_all(self):
        """强制处理所有缓冲区中的图像（即使不满4个）"""
        print("强制处理所有剩余图像...")
        for identifier in list(self.image_buffers.keys()):
            with self.buffer_locks[identifier]:
                if self.image_buffers[identifier]:
                    batch = self.image_buffers[identifier].copy()
                    self.image_buffers[identifier].clear()

                    if batch:
                        self.process_executor.submit(
                            self._process_image_batch, identifier, batch
                        )

    async def close(self):
        """关闭处理器，等待所有任务完成"""
        await self.flush_all()
        self.process_executor.shutdown(wait=True)


# 示例图像处理函数
def image_processor(identifier: int, images: List[Any]) -> dict:
    """
    示例图像处理函数 - 这里替换为您的实际算法

    Args:
        identifier: 图像编号
        images: 4个图像的列表

    Returns:
        处理结果
    """
    print("image_processor 图像处理测试程序")


# 使用示例
async def main():
    # 创建处理器
    processor = ImageBatchProcessor(image_processor, max_workers=2)

    # 模拟图像数据生成
    def generate_test_image(identifier: int, index: int) -> np.ndarray:
        """生成测试图像数据"""
        return np.random.rand(64, 64, 3).astype(np.float32) * 255

    # 模拟异步添加图像
    async def simulate_async_adding():
        tasks = []

        # 为多个编号添加图像
        for identifier in [1, 2, 3]:
            for image_index in range(5):  # 每个编号添加5个图像（会触发一次处理）
                image_data = generate_test_image(identifier, image_index)
                tasks.append(processor.add_image(identifier, image_data))

                # 添加随机延迟模拟真实场景
                await asyncio.sleep(0.1)

        await asyncio.gather(*tasks)

    print("开始添加图像数据...")
    await simulate_async_adding()

    # 检查缓冲区状态
    print(f"编号1缓冲区状态: {processor.get_buffer_status(1)} 图像")
    print(f"编号2缓冲区状态: {processor.get_buffer_status(2)} 图像")
    print(f"编号3缓冲区状态: {processor.get_buffer_status(3)} 图像")

    # 等待处理完成
    await asyncio.sleep(2)

    # 清理资源
    await processor.close()
    print("所有处理完成")


if __name__ == "__main__":
    asyncio.run(main())
