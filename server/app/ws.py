"""实时消息通道：WebSocket 连接管理与预警广播（危急值、缺药）。

同步业务路由（线程池中执行）通过 manager.broadcast() 线程安全地
把消息投递到事件循环，向所有在线连接推送 JSON。
"""
import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .security import decode_token, revoked_tokens

router = APIRouter()


class ConnectionManager:
    """在线连接管理器：接入/断开/广播。"""

    def __init__(self) -> None:
        self.active: list[WebSocket] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active.append(websocket)
        self._loop = asyncio.get_running_loop()

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active:
            self.active.remove(websocket)

    async def _send_all(self, message: dict) -> None:
        for websocket in list(self.active):
            try:
                await websocket.send_json(message)
            except Exception:  # noqa: BLE001 - 单连接故障不影响其余广播
                self.disconnect(websocket)

    def broadcast(self, message: dict) -> None:
        """线程安全广播：供同步路由调用；无在线连接时为空操作。"""
        if not self.active or self._loop is None or self._loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._send_all(message), self._loop)


manager = ConnectionManager()


@router.websocket("/ws/notifications")
async def notifications_ws(websocket: WebSocket, token: str = ""):
    """实时通知通道：?token= 携带 JWT，校验通过后保持长连接接收广播。"""
    claims = decode_token(token)
    if claims is None or token in revoked_tokens:
        await websocket.close(code=1008)
        return
    await manager.connect(websocket)
    try:
        while True:
            # 客户端可发送心跳文本，服务端仅保持连接
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
