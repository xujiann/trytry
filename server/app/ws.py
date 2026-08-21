"""实时消息通道：WebSocket 连接管理与预警广播（危急值、缺药）。

同步业务路由（线程池中执行）通过 manager.broadcast() 线程安全地
把消息投递到事件循环，向在线连接推送 JSON。

M3 整改：
- 支持首帧鉴权：不在 URL query 携带令牌（避免入日志/浏览器历史），
  连接后以首条文本帧发送令牌完成鉴权；query 方式仅为兼容保留；
- 连接期周期复核：每收到一条客户端消息（心跳）即复核令牌过期与登出
  黑名单，登出/过期的长连接即时断开（1008）；
- 集群部署注意（工程包 P2 收口）：manager.active 为进程内字典；
  配置 `MEDPLAT_REDIS_URL` 后广播自动走 Redis Pub/Sub 跨进程/跨实例分发
  （发布端 publish，各 worker 的订阅线程转发给本进程在线连接）。
  **未配置 Redis 时保持进程内语义**：多 worker/多实例部署下，广播只送达
  与写请求同一进程的在线连接——这是已知边界（见运维手册"多实例部署"节），
  多 worker 上线必须配 Redis，单实例不受影响。

M-2 整改（定向通知）：
- 连接建立时登记用户 org_id 与角色；
- broadcast(target_org_id=...) 时仅推送给该机构在线用户与监管角色
  （admin/director），无关机构不再收到含患者临床信息的危急值/缺药消息。
"""
import asyncio
import json
import logging
import threading

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .database import SessionLocal
from .models import User
from .security import decode_token, revoked_tokens
from .state_store import _redis_client

router = APIRouter()

logger = logging.getLogger("medplat.ws")

#: 跨进程广播的 Redis 频道
_BROADCAST_CHANNEL = "medplat:ws:broadcast"


class ConnectionManager:
    """在线连接管理器：接入/断开/（定向）广播。"""

    # 定向广播时始终可见的监管角色
    SUPERVISOR_ROLES = ("admin", "director")

    def __init__(self) -> None:
        # websocket -> {"org_id": int|None, "role": str}
        self.active: dict[WebSocket, dict] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        # Redis Pub/Sub 订阅线程（每进程至多一条，惰性启动）
        self._subscriber_started = False
        self._subscriber_lock = threading.Lock()

    async def connect(self, websocket: WebSocket, org_id: int | None = None, role: str = "") -> None:
        self.active[websocket] = {"org_id": org_id, "role": role}
        self._loop = asyncio.get_running_loop()
        # 持有连接的进程必须订阅总线：广播可能由**别的 worker** 发布
        self._ensure_subscriber()

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

    def broadcast(self, message: dict, target_org_id: int | None = None) -> bool:
        """线程安全广播：供同步路由调用；返回是否已投递/发布出去。

        target_org_id 给定时定向推送：仅该机构在线用户与 admin/director 收到。

        存在 `MEDPLAT_REDIS_URL` 时经 Redis Pub/Sub 跨进程分发：本进程只
        publish，各 worker（含发布进程自己）的订阅线程收到后投递给**本进程**
        的在线连接——发布路径不再本地直投，避免发布进程收到双份。
        Redis publish 失败时降级为本地直投，单实例语义不受影响。

        返回 False 表示**确定无人收到**（无 Redis 且本进程无在线连接）；
        返回 True 只保证"已投递本进程连接或已发布上总线"。调用方可据此做
        "无人在线"的兜底（见 jobs._alert 的 webhook 转发）。
        """
        redis = _redis_client()
        if redis is not None:
            self._ensure_subscriber()
            try:
                redis.publish(
                    _BROADCAST_CHANNEL,
                    json.dumps(
                        {"message": message, "target_org_id": target_org_id},
                        ensure_ascii=False,
                    ),
                )
                return True
            except Exception:  # noqa: BLE001 - Redis 抖动降级为进程内直投
                logger.warning("WS 广播 publish 失败，降级为进程内直投", exc_info=True)
        return self._broadcast_local(message, target_org_id)

    def _broadcast_local(self, message: dict, target_org_id: int | None = None) -> bool:
        """把消息投递给**本进程**的在线连接；无在线连接时为空操作（返回 False）。"""
        if not self.active or self._loop is None or self._loop.is_closed():
            return False
        asyncio.run_coroutine_threadsafe(self._send_all(message, target_org_id), self._loop)
        return True

    # ---- Redis Pub/Sub 订阅端（跨进程广播的接收侧） ----

    def _ensure_subscriber(self) -> None:
        """惰性启动本进程的订阅线程（幂等；未配置 Redis 时什么都不做）。"""
        if self._subscriber_started:
            return
        with self._subscriber_lock:
            if self._subscriber_started:
                return
            redis = _redis_client()
            if redis is None:
                return
            try:
                pubsub = redis.pubsub()
                pubsub.subscribe(_BROADCAST_CHANNEL)
            except Exception:  # noqa: BLE001 - 订阅失败退回进程内语义，不拦启动
                logger.warning("WS 订阅 Redis 总线失败，广播保持进程内语义", exc_info=True)
                return
            threading.Thread(
                target=self._subscriber_loop, args=(pubsub,),
                name="ws-broadcast-subscriber", daemon=True,
            ).start()
            self._subscriber_started = True

    def _subscriber_loop(self, pubsub) -> None:
        """订阅循环：把总线上的广播转发给本进程在线连接。"""
        try:
            for item in pubsub.listen():
                if item.get("type") != "message":
                    continue
                try:
                    payload = json.loads(item["data"])
                    self._broadcast_local(
                        payload.get("message") or {}, payload.get("target_org_id")
                    )
                except Exception:  # noqa: BLE001 - 单条坏消息不终止订阅
                    logger.warning("WS 总线消息处理失败，已跳过", exc_info=True)
        except Exception:  # noqa: BLE001 - Redis 连接中断：进程内广播继续可用
            logger.warning("WS 订阅线程退出（Redis 连接中断）", exc_info=True)
            with self._subscriber_lock:
                self._subscriber_started = False


manager = ConnectionManager()


def _token_valid(token: str) -> bool:
    if not token:
        return False
    claims = decode_token(token)
    if claims is None:
        return False
    # L-9 整改：黑名单按 jti 判定，与 HTTP 侧口径一致
    return (claims.get("jti") or token) not in revoked_tokens


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
