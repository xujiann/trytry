"""集中会话状态存储（M4 整改）：令牌黑名单与登录失败锁定。

默认使用进程内存实现（单实例部署可用，带 TTL 过期清理，进程重启即清空）；
设置 MEDPLAT_REDIS_URL 后自动切换为 Redis 共享存储——多实例/多 worker 部署
必须配置 Redis，否则登出黑名单与防爆破锁定不跨实例生效。
"""
import os
import threading
import time

#: 出网超时（秒）。**必须显式写死，不能吃 redis-py 的默认值**——
#: 那个默认值随版本变，而 `requirements.txt` 只钉了 `redis>=5.0` 且全仓库没有 lockfile，
#: 于是同一份代码装出来的行为完全不同（两个版本都实测过）：
#:
#: - **redis-py 5.0.0**：`socket_timeout` / `socket_connect_timeout` 默认都是 `None`。
#:   没有超时的 socket 不是"慢"，是"挂着"——`except Exception` 一行都救不了，
#:   因为根本没有异常抛出来。
#: - **redis-py 8.1.0**：两者默认 5 秒。把 MEDPLAT_REDIS_URL 指向一个丢包地址，
#:   实测一次调用阻塞 **5.01 秒**后抛 TimeoutError。
#:
#: 两种都不能要：一种永久挂起，另一种在**每个请求**的主路径上付 5 秒
#: （见 monitor._record_cluster）。所以超时由调用方按"这条路慢了要紧不要紧"显式选，
#: 谁都别去猜库的默认值。`scheduler.py` 早就给自己的客户端 setdefault 过 5 秒——
#: 超时这条口径仓库里本来就有，只是没铺到这里。
DEFAULT_REDIS_TIMEOUT = 5.0

#: **订阅循环必须传 `timeout=None`**，这是 `_redis_client` 唯一允许的例外。
#:
#: `pubsub.listen()` 是长阻塞读，频道安静多久就要阻塞多久。它扛不扛得住读超时
#: **也随 redis-py 版本变**，两版都实测过（假 Redis 起订阅、安静 9 秒后发一条）：
#:
#: - **8.1.0**：`PubSub.parse_response(block=True)` 显式传 `timeout=None` 关掉这次读的
#:   超时，`socket_timeout=0.3` 的客户端安静等 9 秒照样收到广播；
#: - **5.0.1**：`Connection.read_response` **根本没有 timeout 形参**，socket 自己的
#:   超时就生效——安静 5 秒后抛 TimeoutError，`ws._subscriber_loop` 的 `except` 记一条
#:   warning 就退出线程，**跨实例广播从此静默丢失**（危急值、缺药推送都走这条）。
#:
#: 也就是说"给客户端统一设读超时"这件事，在 5.x 上会打死订阅线程。所以订阅那一路
#: 显式要 `timeout=None`：不设读超时，但**连接超时始终有界**（见下面的实现）。
#: 这正是本模块开头那条口径的另一面——不吃库的默认值，也不假设库的行为跨版本一致。

#: 客户端缓存：**每次 from_url 都会新建一个连接池和一条 TCP 连接**。
#: 原实现每调用一次就建一次（实测两次调用 `client is client2` 为 False、
#: 连接池也不是同一个），于是每个请求都要走一遍三次握手——健康时是吞吐天花板，
#: 不健康时是每请求一次完整的连接超时。
#: 键是 (url, timeout)，**值里连 redis 模块一起存**，取用前比对模块身份。
#: 这一条是被自己的测试逼出来的：`test_config_matrix.py` 用同一个 URL 连着跑两个
#: 用例、每次换一个假 redis 模块，只按键做判断时第二个用例拿到的是第一个缓存的桩——
#: 单独跑绿、整模块跑红。与其要求每个测试记得清缓存（迟早有人忘），
#: 不如让"换了一套 redis 实现"**自动**算作缓存未命中。
#: 身份放在值里而不是键里：放键里的话每换一次模块就多一条永不回收的条目。
_CLIENTS: dict[tuple[str, float | None], tuple[object, object]] = {}
_CLIENTS_LOCK = threading.Lock()


def _redis_client(timeout: float | None = DEFAULT_REDIS_TIMEOUT):
    """按 MEDPLAT_REDIS_URL 取一个**复用的**客户端；未配置时返回 None。

    `timeout` 是**读写**超时。调用方按"这条路慢了要紧不要紧"选值：
    请求热路径上的监控计数用很短的值（丢一条计数无所谓，拖慢请求是事故），
    后台任务用默认值。

    `timeout=None` = 不设读写超时，**只给 pub/sub 的订阅循环用**（理由见上面的注释块）。
    即便如此**连接超时依然有界**——连不上和连上后不回包是两码事，
    前者没有任何理由无限等下去。
    """
    url = os.environ.get("MEDPLAT_REDIS_URL", "")
    if not url:
        return None
    import redis  # 延迟导入：未配置 Redis 时不要求安装

    key = (url, timeout)
    with _CLIENTS_LOCK:
        cached = _CLIENTS.get(key)
        if cached is not None and cached[0] is redis:
            return cached[1]
        client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=timeout,
            socket_connect_timeout=DEFAULT_REDIS_TIMEOUT if timeout is None else timeout,
            # 连接空闲久了对端可能已经悄悄断开，取用前先探活，
            # 免得把一条死连接的失败算到某个倒霉请求头上。
            health_check_interval=30,
        )
        # 换掉一条作废的缓存项时，把旧客户端的连接池关掉——否则它的 socket
        # 会一直挂到进程结束（缓存本身永不淘汰，见上）。
        if cached is not None:
            try:
                cached[1].close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - 关不掉就算了，不能因此拿不到新客户端
                pass
        _CLIENTS[key] = (redis, client)
        return client


class TokenBlacklist:
    """登出令牌黑名单：add() 拉黑、in 判断；条目按令牌剩余寿命过期清理。"""

    def __init__(self, default_ttl_seconds: int = 8 * 3600) -> None:
        self._ttl = default_ttl_seconds
        self._redis = _redis_client()
        self._lock = threading.Lock()
        self._memory: dict[str, float] = {}  # token -> 过期时刻

    def _prune(self) -> None:
        now = time.time()
        expired = [t for t, exp in self._memory.items() if exp <= now]
        for t in expired:
            self._memory.pop(t, None)

    def add(self, token: str, ttl_seconds: int | None = None) -> None:
        ttl = ttl_seconds or self._ttl
        if self._redis is not None:
            self._redis.setex(f"medplat:revoked:{token}", ttl, "1")
            return
        with self._lock:
            self._prune()
            self._memory[token] = time.time() + ttl

    def __contains__(self, token: str) -> bool:
        if self._redis is not None:
            return bool(self._redis.exists(f"medplat:revoked:{token}"))
        with self._lock:
            self._prune()
            return token in self._memory

    def clear(self) -> None:
        """测试辅助。"""
        if self._redis is not None:  # pragma: no cover
            for key in self._redis.scan_iter("medplat:revoked:*"):
                self._redis.delete(key)
            return
        with self._lock:
            self._memory.clear()


class LoginFailureTracker:
    """登录防爆破：连续失败达到阈值锁定一段时间。"""

    def __init__(self, fail_limit: int = 5, lock_seconds: int = 600) -> None:
        self.fail_limit = fail_limit
        self.lock_seconds = lock_seconds
        self._redis = _redis_client()
        self._lock = threading.Lock()
        self._memory: dict[str, dict] = {}  # username -> {count, locked_until}

    def locked_remaining(self, username: str) -> int:
        """返回剩余锁定秒数；未锁定返回 0。"""
        if self._redis is not None:
            ttl = self._redis.ttl(f"medplat:lock:{username}")
            return max(ttl, 0)
        with self._lock:
            record = self._memory.get(username)
            if not record:
                return 0
            remain = record.get("locked_until", 0) - time.time()
            return int(remain) if remain > 0 else 0

    def record_failure(self, username: str) -> bool:
        """记录一次失败；达到阈值则锁定并返回 True。"""
        if self._redis is not None:
            key = f"medplat:fail:{username}"
            count = self._redis.incr(key)
            self._redis.expire(key, self.lock_seconds)
            if count >= self.fail_limit:
                self._redis.setex(f"medplat:lock:{username}", self.lock_seconds, "1")
                self._redis.delete(key)
                return True
            return False
        with self._lock:
            now = time.time()
            record = self._memory.setdefault(
                username, {"count": 0, "locked_until": 0, "window_expires": 0.0}
            )
            # L-8 整改：与 Redis 路径口径一致的滑动窗口——
            # 距上次失败超过 lock_seconds 则计数清零，零散失败不再累计成锁定
            if now > record.get("window_expires", 0.0):
                record["count"] = 0
            record["count"] += 1
            record["window_expires"] = now + self.lock_seconds
            if record["count"] >= self.fail_limit:
                record["locked_until"] = now + self.lock_seconds
                record["count"] = 0
                return True
            return False

    def reset(self, username: str) -> None:
        if self._redis is not None:
            self._redis.delete(f"medplat:fail:{username}", f"medplat:lock:{username}")
            return
        with self._lock:
            self._memory.pop(username, None)

    def clear_all(self) -> None:
        """测试辅助。"""
        if self._redis is not None:  # pragma: no cover
            for pattern in ("medplat:fail:*", "medplat:lock:*"):
                for key in self._redis.scan_iter(pattern):
                    self._redis.delete(key)
            return
        with self._lock:
            self._memory.clear()


class SessionRegistry:
    """会话登记（等保 E1）：令牌活动时刻 + 每账号活跃令牌集合。

    与 TokenBlacklist / LoginFailureTracker 同一存储约定：默认进程内存
    （单实例可用、进程重启即清空），配置 MEDPLAT_REDIS_URL 自动切换 Redis——
    **多实例部署必须配 Redis**，否则空闲超时的活动记录与并发会话计数只在
    单个实例内生效。

    两组数据都以令牌自然寿命为 TTL，不用者自动过期：

    - `last_seen:{jti}`：该令牌最近一次通过校验的时刻，deps 的空闲超时用；
    - `sessions:{username}`：该账号活跃令牌集合（Redis 用 zset、score=过期时刻），
      登录时计数判并发上限，登出/闲置淘汰时移除释放名额。

    两项开关（session_idle_timeout_seconds / session_max_concurrent）为 0 时
    调用方直接旁路，不会触到本类——0 值零开销由调用方保证。
    """

    def __init__(self, default_ttl_seconds: int = 8 * 3600) -> None:
        self._ttl = default_ttl_seconds
        self._redis = _redis_client()
        self._lock = threading.Lock()
        self._last_seen: dict[str, tuple[float, float]] = {}  # jti -> (时刻, 过期时刻)
        self._sessions: dict[str, dict[str, float]] = {}  # username -> {jti: 过期时刻}

    # ---------- 空闲超时：令牌活动时刻 ----------

    def touch(self, jti: str, ttl_seconds: int | None = None) -> None:
        """记录一次活动（滑动续签的"续"就是这一步）。"""
        ttl = ttl_seconds or self._ttl
        now = time.time()
        if self._redis is not None:  # pragma: no cover - 需真实 Redis
            self._redis.setex(f"medplat:lastseen:{jti}", ttl, str(now))
            return
        with self._lock:
            self._prune(now)
            self._last_seen[jti] = (now, now + ttl)

    def last_seen(self, jti: str) -> float | None:
        """该令牌最近活动时刻；从未记录（如登录后首个请求）返回 None。"""
        if self._redis is not None:  # pragma: no cover - 需真实 Redis
            raw = self._redis.get(f"medplat:lastseen:{jti}")
            return float(raw) if raw else None
        with self._lock:
            self._prune(time.time())
            record = self._last_seen.get(jti)
            return record[0] if record else None

    # ---------- 并发上限：每账号活跃令牌集合 ----------

    def register(self, username: str, jti: str, ttl_seconds: int | None = None) -> None:
        """登录成功后登记新令牌。"""
        expires = time.time() + (ttl_seconds or self._ttl)
        if self._redis is not None:  # pragma: no cover - 需真实 Redis
            key = f"medplat:sessions:{username}"
            self._redis.zadd(key, {jti: expires})
            self._redis.expire(key, ttl_seconds or self._ttl)
            return
        with self._lock:
            self._prune(time.time())
            self._sessions.setdefault(username, {})[jti] = expires

    def active_count(self, username: str) -> int:
        """该账号当前活跃（未过期、未登出）的令牌数。"""
        now = time.time()
        if self._redis is not None:  # pragma: no cover - 需真实 Redis
            key = f"medplat:sessions:{username}"
            self._redis.zremrangebyscore(key, 0, now)
            return int(self._redis.zcard(key))
        with self._lock:
            self._prune(now)
            return len(self._sessions.get(username, {}))

    def remove(self, username: str, jti: str) -> None:
        """登出/闲置淘汰时移除令牌，立即释放并发名额。"""
        if self._redis is not None:  # pragma: no cover - 需真实 Redis
            self._redis.zrem(f"medplat:sessions:{username}", jti)
            self._redis.delete(f"medplat:lastseen:{jti}")
            return
        with self._lock:
            self._sessions.get(username, {}).pop(jti, None)
            self._last_seen.pop(jti, None)

    def clear_user(self, username: str) -> None:
        """清空该账号的全部会话登记，立即释放它占的所有并发名额。

        用在"推令牌基线"的地方（改密 / 管理员重置口令 / 停用账号）：基线一推，
        该账号既有令牌全部作废，可登记里的名额还挂着。并发上限为 1 时这会把人
        彻底锁死——改完密码登不进来（409 会话已达上限），旧令牌又过不了基线校验
        所以也登不出去（401），名额要占满令牌自然寿命（8 小时）才由空闲淘汰释放。
        `remove()` 只能按 jti 单个释放，而推基线时手上并没有那些 jti。
        """
        if self._redis is not None:  # pragma: no cover - 需真实 Redis
            key = f"medplat:sessions:{username}"
            jtis = self._redis.zrange(key, 0, -1)
            if jtis:
                self._redis.delete(*[f"medplat:lastseen:{j}" for j in jtis])
            self._redis.delete(key)
            return
        with self._lock:
            for jti in self._sessions.pop(username, {}):
                self._last_seen.pop(jti, None)

    def _prune(self, now: float) -> None:
        expired = [j for j, (_, exp) in self._last_seen.items() if exp <= now]
        for j in expired:
            self._last_seen.pop(j, None)
        for username in list(self._sessions):
            alive = {j: exp for j, exp in self._sessions[username].items() if exp > now}
            if alive:
                self._sessions[username] = alive
            else:
                self._sessions.pop(username, None)

    def clear_all(self) -> None:
        """测试辅助。"""
        if self._redis is not None:  # pragma: no cover - 需真实 Redis
            for pattern in ("medplat:lastseen:*", "medplat:sessions:*"):
                for key in self._redis.scan_iter(pattern):
                    self._redis.delete(key)
            return
        with self._lock:
            self._last_seen.clear()
            self._sessions.clear()


# 滑动窗口限速的 Redis 实现（T1.2）：必须原子，否则多实例并发下
# "先数后加"会各自读到未超限的旧计数，配额被击穿。用 Lua 在服务端一次做完
# 剔除过期成员 → 计数 → 未超限则写入，避免任何中间态被其他实例看到。
_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)
if count >= limit then
  return 0
end
redis.call('ZADD', key, now, member)
redis.call('EXPIRE', key, window)
return 1
"""


class SlidingWindowRateLimiter:
    """通用滑动窗口限速器（浙#80 资源控制）：按主体（IP/手机号等）限制单位时间内的请求数。

    T1.2 整改：补齐 Redis 后端。此前只有进程内存实现——多实例部署下 N 个实例
    各自计数，验证码下发这类配额实际被放大 N 倍，防轰炸形同虚设。现与
    TokenBlacklist / LoginFailureTracker 采用同一套 `_redis_client()` 约定：
    配置 MEDPLAT_REDIS_URL 即自动切换共享计数，未配置时保持原内存行为。
    """

    def __init__(self, max_events: int = 50, window_seconds: int = 60) -> None:
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._redis = _redis_client()
        self._script = None
        if self._redis is not None:  # pragma: no cover - 需真实 Redis
            self._script = self._redis.register_script(_SLIDING_WINDOW_LUA)
        self._lock = threading.Lock()
        self._events: dict[str, list] = {}
        self._seq = 0

    def allow(self, key: str) -> bool:
        """记录一次事件并判断是否仍在配额内；超限返回 False。"""
        now = time.time()
        if self._script is not None:  # pragma: no cover - 需真实 Redis
            with self._lock:
                self._seq += 1
                member = f"{now}:{self._seq}"  # 同毫秒多次请求需互不覆盖
            allowed = self._script(
                keys=[f"medplat:rate:{key}"],
                args=[now, self.window_seconds, self.max_events, member],
            )
            return bool(allowed)
        with self._lock:
            window = [t for t in self._events.get(key, []) if now - t < self.window_seconds]
            if len(window) >= self.max_events:
                self._events[key] = window
                return False
            window.append(now)
            self._events[key] = window
            return True

    def clear_all(self) -> None:
        """测试辅助。"""
        if self._redis is not None:  # pragma: no cover - 需真实 Redis
            for key in self._redis.scan_iter("medplat:rate:*"):
                self._redis.delete(key)
            return
        with self._lock:
            self._events.clear()
