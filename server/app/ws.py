"""实时消息通道：WebSocket 连接管理与预警广播（危急值、缺药）。

同步业务路由（线程池中执行）通过 manager.broadcast() 线程安全地
把消息投递到事件循环，向在线连接推送 JSON。

M3 整改：
- 支持首帧鉴权：不在 URL query 携带令牌（避免入日志/浏览器历史），
  连接后以首条文本帧发送令牌完成鉴权；query 方式仅为兼容保留；
- 连接期周期复核：每收到一条客户端消息（心跳）即复核令牌过期与登出
  黑名单，登出/过期的长连接即时断开（1008）；
- 集群部署注意：manager.active 为进程内字典，多 worker/多实例部署时
  广播需迁移 Redis Pub/Sub 等集中消息总线（见部署文档），单实例不受影响。

M-2 整改（定向通知）：
- 连接建立时登记用户 org_id 与角色；
- broadcast(target_org_id=...) 时仅推送给该机构在线用户与监管角色
  （admin/director），无关机构不再收到含患者临床信息的危急值/缺药消息。
"""
import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .database import SessionLocal
from .models import User
from .security import decode_token, revoked_tokens

router = APIRouter()


class ConnectionManager:
    """在线连接管理器：接入/断开/（定向）广播。"""

    # 定向广播时始终可见的监管角色
    SUPERVISOR_ROLES = ("admin", "director")

    def __init__(self) -> None:
        # websocket -> {"org_id": int|None, "role": str}
        self.active: dict[WebSocket, dict] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    async def connect(self, websocket: WebSocket, org_id: int | None = None, role: str = "") -> None:
        self.active[websocket] = {"org_id": org_id, "role": role}
        self._loop = asyncio.get_running_loop()

    def disconnect(self, websocket: WebSocket) -> None:
        self.active.pop(websocket, None)

    async def _send_all(self, message: dict, target_org_id: int | None) -> None:
        for websocket, meta in list(self.active.items()):
            if (
                target_org_id is not None
                and meta.get("org_id") != target_org_id
                and meta.get("role") not in self.SUPERVISOR_ROLES
            ):
                continue
            try:
                await websocket.send_json(message)
            except Exception:  # noqa: BLE001 - 单连接故障不影响其余广播
                self.disconnect(websocket)

    def broadcast(self, message: dict, target_org_id: int | None = None) -> None:
        """线程安全广播：供同步路由调用；无在线连接时为空操作。

        target_org_id 给定时定向推送：仅该机构在线用户与 admin/director 收到。
        """
        if not self.active or self._loop is None or self._loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._send_all(message, target_org_id), self._loop)


manager = ConnectionManager()


def _token_valid(token: str) -> bool:
    return bool(token) and token not in revoked_tokens and decode_token(token) is not None


def _lookup_user_meta(token: str) -> tuple[int | None, str]:
    """按令牌主体查询用户 org_id 与角色（定向广播登记用）。"""
    claims = decode_token(token) or {}
    username = claims.get("sub", "")
    if not username:
        return None, ""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if user is None:
            return None, ""
        return user.org_id, user.role
    finally:
        db.close()


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
    org_id, role = _lookup_user_meta(token)
    await manager.connect(websocket, org_id=org_id, role=role)
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
