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

本轮整改（WS 鉴权口径对齐 HTTP）：
- 握手鉴权原先只验「签名 + 过期 + 登出黑名单」，不查 `users.status`、不判改密
  基线、不拒居民端令牌——停用账号与居民端令牌在 HTTP 侧 403/401，在本通道却能
  建连并收到定向广播。现三条握手路径（query token / 首帧 / Cookie 兜底）一律
  走 `deps.check_token_admission`，与 `get_current_user` 同一份判定；
- 长连接存活期同样复核：心跳时现查（保住 M3 的"登出即断开"），每次广播投递前
  按令牌带短 TTL 缓存复核（挡住一条心跳都不发的沉默连接），取舍见 `_authorize`。
"""
import asyncio
import json
import logging
import threading
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .database import SessionLocal
from .deps import check_token_admission
from .security import AUTH_COOKIE, decode_token
from .state_store import _redis_client

router = APIRouter()

logger = logging.getLogger("medplat.ws")

#: 跨进程广播的 Redis 频道
_BROADCAST_CHANNEL = "medplat:ws:broadcast"

#: 长连接存活期复核的缓存窗口（秒）；握手不吃缓存。取舍见 `_authorize`。
_REVALIDATE_TTL_SECONDS = 30.0

#: token -> (判定时刻, (是否放行, org_id, 角色))
_admission_cache: dict[str, tuple[float, tuple[bool, int | None, str]]] = {}
_admission_lock = threading.Lock()


def _authorize_uncached(token: str) -> tuple[bool, int | None, str]:
    """现查一次：令牌是否放行，以及定向广播要登记的 org_id / 角色。"""
    claims = decode_token(token)
    if claims is None:
        return False, None, ""
    db = SessionLocal()
    try:
        user, denial = check_token_admission(db, claims, token)
        if denial or user is None:
            return False, None, ""
        return True, user.org_id, user.role
    finally:
        db.close()


def _authorize(token: str, *, cached: bool = False) -> tuple[bool, int | None, str]:
    """本通道的准入判定。判定本身不在这里实现，调 `deps.check_token_admission`
    ——与 HTTP 侧 `get_current_user` 同一份代码，不再各留一套口径。

    **`cached` 的取舍**——缓存只开在一条路径上：

    - `cached=False`（**握手** + **心跳复核**）一律现查。握手是鉴权本身，不能吃
      缓存的滞后；心跳是客户端驱动的低频事件，现查才保得住 M3 那条"登出后下一次
      心跳即断开"的既有语义（`test_p0_fixes.test_ws_heartbeat_revalidates_revoked_token`
      钉着它）。
    - `cached=True` 只用于**广播投递前**的复核。这条路径按在线连接数放大：一次
      危急值广播若逐连接现查，就是"连接数"次查库，且发生在事件循环里。所以按
      令牌缓存 `_REVALIDATE_TTL_SECONDS` 秒，每枚令牌每 30 秒至多一次查询，
      代价是停用/改密之后最多再多收 30 秒消息——而在此之前这条路径**一次都不查**。
      选它而不选"推送前按 user_id 批量查一次"，是因为后者要把连接按用户聚合、
      还要处理首帧鉴权尚未登记用户的中间态，复杂度换来的只是同一量级的窗口。
    """
    if not token:
        return False, None, ""
    now = time.time()
    if cached:
        with _admission_lock:
            hit = _admission_cache.get(token)
        if hit is not None and now - hit[0] < _REVALIDATE_TTL_SECONDS:
            return hit[1]
    result = _authorize_uncached(token)
    with _admission_lock:
        for stale in [t for t, (at, _) in _admission_cache.items()
                      if now - at >= _REVALIDATE_TTL_SECONDS]:
            _admission_cache.pop(stale, None)
        _admission_cache[token] = (now, result)
    return result


class ConnectionManager:
    """在线连接管理器：接入/断开/（定向）广播。"""

    # 定向广播时始终可见的监管角色
    SUPERVISOR_ROLES = ("admin", "director")

    def __init__(self) -> None:
        # websocket -> {"org_id": int|None, "role": str, "token": str}
        # token 留着是为了投递前复核准入（见 _send_all）；它本就是本连接的凭据，
        # 不写日志、不进广播消息。
        self.active: dict[WebSocket, dict] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        # Redis Pub/Sub 订阅线程（每进程至多一条，惰性启动）
        self._subscriber_started = False
        self._subscriber_lock = threading.Lock()

    async def connect(
        self, websocket: WebSocket, org_id: int | None = None, role: str = "", token: str = ""
    ) -> None:
        self.active[websocket] = {"org_id": org_id, "role": role, "token": token}
        self._loop = asyncio.get_running_loop()
        # 持有连接的进程必须订阅总线：广播可能由**别的 worker** 发布
        self._ensure_subscriber()

    def disconnect(self, websocket: WebSocket) -> None:
        self.active.pop(websocket, None)

    async def _send_all(self, message: dict, target_org_id: int | None) -> None:
        for websocket, meta in list(self.active.items()):
            # 投递前复核准入（缓存 + 短 TTL，见 _authorize）：账号被停用、令牌被
            # 登出或被改密基线吊销的连接不再收消息，不必等它下一次心跳——
            # 沉默的客户端可以一条心跳都不发，握手时的一次校验挡不住这种连接。
            if not _authorize(meta.get("token", ""), cached=True)[0]:
                self.disconnect(websocket)
                try:
                    await websocket.close(code=1008)
                except Exception:  # noqa: BLE001 - 已断开的连接无需再关
                    pass
                continue
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


@router.websocket("/ws/notifications")
async def notifications_ws(websocket: WebSocket, token: str = ""):
    """实时通知通道。

    鉴权方式（三选一）：
    1. 首帧鉴权（推荐）：连接后第一条文本帧发送 JWT 令牌；
    2. ?token= query 携带（兼容保留，注意令牌可能进入访问日志）；
    3. 会话 Cookie 兜底（G3）：Cookie 模式的前端不再持有裸令牌，握手时浏览器
       自动携带的 HttpOnly Cookie 即凭据。仅当 Cookie 里的令牌**当前有效**时
       采用；无效/缺失仍回退首帧鉴权，两种既有方式不受影响。WS 握手是 GET、
       无副作用，SameSite=Lax 下跨站页面也无法用 Cookie 建立本通道的写能力，
       故此处不做 CSRF 双提交。
    三条路径鉴权口径**完全一致**（`_authorize` → `deps.check_token_admission`）：
    令牌有效且未登出、账号存在且未停用、令牌未被改密/停用推的基线吊销、
    不是居民端令牌（scope=portal）。连接期每收到一条心跳消息复核一次，
    每次广播投递前也复核一次（见 `ConnectionManager._send_all`）。
    """
    await websocket.accept()
    ok, org_id, role = False, None, ""
    if not token:
        cookie_token = websocket.cookies.get(AUTH_COOKIE, "")
        if cookie_token:
            ok, org_id, role = _authorize(cookie_token)
            if ok:
                token = cookie_token
    if not token:
        # 首帧鉴权：第一条文本帧即令牌
        try:
            token = (await websocket.receive_text()).strip()
        except WebSocketDisconnect:
            return
    if not ok:
        ok, org_id, role = _authorize(token)
    if not ok:
        await websocket.close(code=1008)
        return
    await manager.connect(websocket, org_id=org_id, role=role, token=token)
    try:
        while True:
            # 客户端可发送心跳文本；每次心跳**现查**复核准入（登出/过期/停用/改密
            # 即断开）。心跳是客户端驱动的低频事件，一次查库换"登出即时生效"这条
            # M3 既有语义，值；缓存只用在广播扇出那条按连接数放大的路径上。
            await websocket.receive_text()
            if not _authorize(token)[0]:
                manager.disconnect(websocket)
                await websocket.close(code=1008)
                return
    except WebSocketDisconnect:
        manager.disconnect(websocket)
