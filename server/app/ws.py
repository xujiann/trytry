"""实时消息通道：WebSocket 连接管理与预警广播（危急值、缺药）。

同步业务路由（线程池中执行）通过 manager.broadcast() 线程安全地
把消息投递到事件循环，向所有在线连接推送 JSON。

M3 整改：
- 支持首帧鉴权：不在 URL query 携带令牌（避免入日志/浏览器历史），
  连接后以首条文本帧发送令牌完成鉴权；query 方式仅为兼容保留；
- 连接期周期复核：每收到一条客户端消息（心跳）即复核令牌过期与登出
  黑名单，登出/过期的长连接即时断开（1008）；
- 集群部署注意：manager.active 为进程内列表，多 worker/多实例部署时
  广播需迁移 Redis Pub/Sub 等集中消息总线（见部署文档），单实例不受影响。
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


def _token_valid(token: str) -> bool:
    return bool(token) and token not in revoked_tokens and decode_token(token) is not None


@router.websocket("/ws/notifications")
async def notifications_ws(websocket: WebSocket, token: str = ""):
    """实时通知通道。

    鉴权方式（二选一）：
    1. 首帧鉴权（推荐）：连接后第一条文本帧发送 JWT 令牌；
    2. ?token= query 携带（兼容保留，注意令牌可能进入访问日志）。
    连接期每收到一条心跳消息即复核令牌有效性与黑名单，失效即断开。
    """
    await websocket.accept()
    if not token:
        # 首帧鉴权：第一条文本帧即令牌
        try:
            token = (await websocket.receive_text()).strip()
        except WebSocketDisconnect:
            return
    if not _token_valid(token):
        await websocket.close(code=1008)
        return
    await manager.connect(websocket)
    try:
        while True:
            # 客户端可发送心跳文本；每次心跳复核令牌（登出/过期即断开）
            await websocket.receive_text()
            if not _token_valid(token):
                manager.disconnect(websocket)
                await websocket.close(code=1008)
                return
    except WebSocketDisconnect:
        manager.disconnect(websocket)
