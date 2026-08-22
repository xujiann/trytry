"""集中会话状态存储（M4 整改）：令牌黑名单与登录失败锁定。

默认使用进程内存实现（单实例部署可用，带 TTL 过期清理，进程重启即清空）；
设置 MEDPLAT_REDIS_URL 后自动切换为 Redis 共享存储——多实例/多 worker 部署
必须配置 Redis，否则登出黑名单与防爆破锁定不跨实例生效。
"""
import os
import threading
import time


def _redis_client():
    url = os.environ.get("MEDPLAT_REDIS_URL", "")
    if not url:
        return None
    import redis  # 延迟导入：未配置 Redis 时不要求安装

    return redis.Redis.from_url(url, decode_responses=True)


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
