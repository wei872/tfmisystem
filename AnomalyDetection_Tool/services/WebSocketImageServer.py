"""
WebSocket 图片推送服务
前端发送相机名，服务端只推送该相机图片
"""
import asyncio
import base64
import json
import threading
import time
from typing import Optional

import cv2
import numpy as np

from General_Tool.EnhancedLogger import info, error

try:
    import websockets
except ImportError:
    raise ImportError("请安装 websockets: pip install websockets")


class WebSocketImageServer:
    """WebSocket 图片推送服务器 - 按相机名订阅"""

    def __init__(self, host='0.0.0.0', port=8765,
                 preview_max_fps: float = 4.0,
                 preview_max_width: int = 960,
                 jpeg_quality: int = 70):
        self.host = host
        self.port = port

        # ── 预览质量参数 ─────────────────────────────────────────────
        # 预览画面只给人眼看，没必要用原始分辨率和帧率：
        # 2440x2048 的 JPEG 编码约 36ms/帧，缩到 960 宽后约 5ms，差一个数量级。
        self.preview_max_fps = max(0.1, float(preview_max_fps))
        self.preview_min_interval = 1.0 / self.preview_max_fps
        self.preview_max_width = int(preview_max_width)
        self.jpeg_quality = int(jpeg_quality)

        # 只用于保护 latest_images 的短暂读写，绝不在持锁时做IO
        self.lock = threading.Lock()

        # {camera_name: {'timestamp': float, 'image': base64_str}}
        self.latest_images: dict = {}

        # ── 编码流水线 ───────────────────────────────────────────────
        # push_image() 由相机 SDK 回调线程调用，**绝不能在那里做编码**。
        # 这里只登记"最新待编码帧"，真正的编码由独立线程完成。
        self._pending: dict = {}          # camera_name -> (image, metadata)
        self._pending_lock = threading.Lock()
        self._pending_event = threading.Event()
        self._last_push_ts: dict = {}     # camera_name -> 上次接收时间（限帧用）
        self._encoder_thread: Optional[threading.Thread] = None
        self._dropped_frames = 0
        self._encoded_frames = 0

        self.connection_count = 0
        self._started = False
        self._stop_flag = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------
    def start(self):
        if self._started:
            info("[WebSocket] 服务已经启动，跳过")
            return

        thread = threading.Thread(
            target=self._run_server,
            daemon=True,
            name="WebSocketServer"
        )
        thread.start()

        self._encoder_thread = threading.Thread(
            target=self._encode_loop,
            daemon=True,
            name="WSPreviewEncoder"
        )
        self._encoder_thread.start()

        for _ in range(50):
            if self._started:
                break
            time.sleep(0.1)

        if self._started:
            info(f"[WebSocket] 服务启动成功: ws://{self.host}:{self.port}")
        else:
            error("[WebSocket] 服务启动超时")

    def _run_server(self):
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._async_main())
        except Exception as e:
            error(f"[WebSocket] 服务器错误: {e}")
            import traceback
            traceback.print_exc()

    async def _async_main(self):
        try:
            server = await websockets.serve(
                self._handle_client,
                self.host,
                self.port,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
            )
            self._started = True
            info(f"[WebSocket] 服务器已绑定到 {self.host}:{self.port}")

            while not self._stop_flag:
                await asyncio.sleep(0.5)

            server.close()
            await server.wait_closed()
            info("[WebSocket] 服务器已停止")

        except OSError as e:
            if "already in use" in str(e).lower() or getattr(e, 'errno', 0) == 10048:
                error(f"[WebSocket] 端口 {self.port} 已被占用")
            else:
                error(f"[WebSocket] 网络错误: {e}")
        except Exception as e:
            error(f"[WebSocket] 运行错误: {e}")
            import traceback
            traceback.print_exc()

    # ------------------------------------------------------------------
    # 消息解析
    # ------------------------------------------------------------------
    def _parse_camera_id(self, msg: str) -> str:
        msg = msg.strip()
        try:
            data = json.loads(msg)
            if isinstance(data, dict):
                cid = str(data.get('camera_id', data.get('cameraId', '')))
            elif isinstance(data, (int, float)):
                cid = str(int(data))
            else:
                cid = str(data)
        except (json.JSONDecodeError, TypeError):
            cid = msg

        cid = cid.strip()
        if cid.isdigit():
            cid = f"camera{cid}"
        return cid

    # ------------------------------------------------------------------
    # 快照读取（在锁外调用）
    # ------------------------------------------------------------------
    def _get_latest(self, camera_id: str):
        """
        只在锁内做极短暂的数据拷贝，立刻释放锁
        返回 (timestamp, image_base64) 或 (None, None)
        """
        with self.lock:
            data = self.latest_images.get(camera_id)
            if data is None:
                return None, None
            # 只复制引用（str 不可变，安全）
            return data['timestamp'], data['image']

    # ------------------------------------------------------------------
    # 客户端处理
    # ------------------------------------------------------------------
    async def _handle_client(self, websocket):
        conn_id = 0
        camera_id = None

        try:
            try:
                addr = websocket.remote_address
                addr_str = f"{addr[0]}:{addr[1]}"
            except Exception:
                addr_str = "unknown"

            with self.lock:
                self.connection_count += 1
                conn_id = self.connection_count

            info(f"[WebSocket] 客户端#{conn_id} 连接: {addr_str}")

            # 1. 等待前端发送相机名
            try:
                init_msg = await asyncio.wait_for(websocket.recv(), timeout=10.0)
            except asyncio.TimeoutError:
                await websocket.close(code=1000, reason="未发送相机名")
                return

            camera_id = self._parse_camera_id(init_msg)
            info(f"[WebSocket] 客户端#{conn_id} 订阅: {camera_id}")

            # 2. 发送确认
            await websocket.send(json.dumps({
                'type': 'subscribed',
                'camera': camera_id,
                'message': f'已订阅 {camera_id}'
            }))

            # 3. 推送循环
            last_timestamp = 0.0
            frame_count = 0

            while not self._stop_flag:
                # 先在锁外拿快照，再做IO，锁不跨越await
                ts, msg = self._get_latest(camera_id)

                if ts is not None and ts > last_timestamp:
                    # 完全在锁外执行 send
                    try:
                        await websocket.send(msg)
                        last_timestamp = ts
                        frame_count += 1

                        if frame_count == 1:
                            info(f"[WebSocket] ✓ 首帧 | 客户端#{conn_id} | {camera_id}")
                        elif frame_count % 100 == 0:
                            info(f"[WebSocket] ✓ 累计{frame_count}帧 | "
                                 f"客户端#{conn_id} | {camera_id}")

                    except websockets.exceptions.ConnectionClosed as e:
                        info(f"[WebSocket] ✗ 连接关闭 | 客户端#{conn_id} "
                             f"| {camera_id} | code={e.code}")
                        return
                    except Exception as e:
                        error(f"[WebSocket] ✗ 发送失败 | 客户端#{conn_id} "
                              f"| {camera_id} | {type(e).__name__}: {e}")
                        return

                # 用 asyncio.sleep 让出事件循环，不阻塞其他协程
                await asyncio.sleep(0.033)  # ~30fps 上限

        except websockets.exceptions.ConnectionClosedOK:
            info(f"[WebSocket] 客户端#{conn_id} {camera_id} 正常断开")
        except websockets.exceptions.ConnectionClosedError as e:
            info(f"[WebSocket] 客户端#{conn_id} {camera_id} 异常断开: code={e.code}")
        except Exception as e:
            error(f"[WebSocket] 客户端#{conn_id} {camera_id} "
                  f"错误: {type(e).__name__}: {e}")
        finally:
            with self.lock:
                self.connection_count -= 1
            info(f"[WebSocket] 当前连接数: {self.connection_count}")

    # ------------------------------------------------------------------
    # 图片推送（从相机回调线程调用）
    # ------------------------------------------------------------------
    def push_image(self, camera_name: str, image: np.ndarray, metadata: dict = None):
        """
        登记一帧待推送图像。**由相机 SDK 回调线程调用，必须立即返回。**

        原实现在这里同步做 cv2.imencode + base64：按现场 2440x2048 的分辨率
        实测约 43ms/帧，直接压在相机回调线程上 —— 单相机 10fps 就要占掉
        43% 的回调时间，多相机时 SDK 内部缓冲会被拖爆，表现为莫名其妙的丢帧。

        现在这里只做两件 O(1) 的事：限帧判断 + 记下最新帧引用，
        实际编码交给 _encode_loop 线程。预览天然只关心最新一帧，
        所以同一相机的旧帧被新帧覆盖是正确行为，不需要排队。
        """
        if not self._started or image is None:
            return

        now = time.time()

        # 限帧：预览给人看，4fps 足够，没必要按采集帧率编码
        last = self._last_push_ts.get(camera_name, 0.0)
        if now - last < self.preview_min_interval:
            self._dropped_frames += 1
            return
        self._last_push_ts[camera_name] = now

        # 只保存引用，不拷贝：整帧 copy 约 1.2ms，同样不该占用回调线程。
        # 该数组由 image_callback 每帧新建，后续不会被原地改写。
        with self._pending_lock:
            self._pending[camera_name] = (image, metadata)
        self._pending_event.set()

    # ------------------------------------------------------------------
    # 编码线程
    # ------------------------------------------------------------------
    def _encode_loop(self):
        """后台编码：把待推送帧缩放 + JPEG 编码 + base64，写入 latest_images"""
        info(f"[WebSocket] 预览编码线程启动 "
             f"(限帧 {self.preview_max_fps}fps, 最大宽度 {self.preview_max_width}px, "
             f"质量 {self.jpeg_quality})")

        while not self._stop_flag:
            if not self._pending_event.wait(timeout=0.5):
                continue
            self._pending_event.clear()

            with self._pending_lock:
                batch = self._pending
                self._pending = {}

            for camera_name, (image, metadata) in batch.items():
                try:
                    self._encode_one(camera_name, image, metadata)
                except Exception as e:
                    error(f"[WebSocket] 编码失败: {camera_name} | {e}")

    def _encode_one(self, camera_name: str, image: np.ndarray, metadata):
        # 缩放：预览不需要原始分辨率，这是最大的一笔节省
        h, w = image.shape[:2]
        if self.preview_max_width > 0 and w > self.preview_max_width:
            scale = self.preview_max_width / float(w)
            image = cv2.resize(
                image, (self.preview_max_width, max(1, int(h * scale))),
                interpolation=cv2.INTER_AREA)

        success, buffer = cv2.imencode(
            '.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
        if not success:
            return

        img_base64 = base64.b64encode(buffer).decode('utf-8')
        self._encoded_frames += 1

        # 锁内只做字典赋值，极短暂
        with self.lock:
            self.latest_images[camera_name] = {
                'timestamp': time.time(),
                'image': img_base64,
                'metadata': metadata or {},
            }

    def get_preview_stats(self) -> dict:
        """预览链路统计，用于确认限帧是否合理"""
        return {
            "encoded": self._encoded_frames,
            "dropped_by_rate_limit": self._dropped_frames,
            "max_fps": self.preview_max_fps,
            "max_width": self.preview_max_width,
        }

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------
    def get_connection_count(self) -> int:
        with self.lock:
            return self.connection_count

    def is_running(self) -> bool:
        return self._started

    def shutdown(self):
        info("[WebSocket] 正在关闭服务...")
        self._stop_flag = True
        self._started = False
        self._pending_event.set()   # 唤醒编码线程使其尽快退出
        info("[WebSocket] 服务已关闭")


# ── 全局单例 ──────────────────────────────────────────────────────────
_ws_server: Optional[WebSocketImageServer] = None


def get_websocket_server() -> Optional[WebSocketImageServer]:
    return _ws_server


def init_websocket_server(host='0.0.0.0', port=8765,
                          preview_max_fps: float = 4.0,
                          preview_max_width: int = 960,
                          jpeg_quality: int = 70) -> WebSocketImageServer:
    global _ws_server
    if _ws_server is None:
        _ws_server = WebSocketImageServer(
            host, port,
            preview_max_fps=preview_max_fps,
            preview_max_width=preview_max_width,
            jpeg_quality=jpeg_quality)
        _ws_server.start()
    return _ws_server


def shutdown_websocket_server():
    global _ws_server
    if _ws_server:
        _ws_server.shutdown()
        _ws_server = None