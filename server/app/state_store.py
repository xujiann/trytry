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


class SlidingWindowRateLimiter:
    """通用滑动窗口限速器（浙#80 资源控制）：按主体（IP 等）限制单位时间内的请求数。

    进程内存实现；多实例部署时锁定/限速状态按实例独立（部署层可叠加网关限速）。
    """

    def __init__(self, max_events: int = 50, window_seconds: int = 60) -> None:
        self.max_events = max_events
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._events: dict[str, list] = {}

    def allow(self, key: str) -> bool:
        """记录一次事件并判断是否仍在配额内；超限返回 False。"""
        now = time.time()
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
        with self._lock:
            self._events.clear()
