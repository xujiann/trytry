"""块4：PG/Redis 就绪验证（无真实 PG/Redis，可不实连验证的部分全覆盖）。

- DATABASE_URL 为 postgresql:// 时 engine 参数正确（create_engine 惰性建连，不实连）
- REDIS_URL 配置时 state_store 选择 Redis 后端（mock redis 客户端验证读写调用）
"""
import sys
import types

import pytest
from sqlalchemy import create_engine

from app.database import build_engine, engine_kwargs

PG_URL = "postgresql+psycopg2://medplat:secret@db.example:5432/medplat"


# ---------- PostgreSQL：engine 参数矩阵（不实连） ----------


def test_engine_kwargs_matrix():
    # PG：不得携带 SQLite 专属参数
    assert engine_kwargs(PG_URL) == {"connect_args": {}}
    assert engine_kwargs("postgresql://u:p@h/db") == {"connect_args": {}}
    # SQLite：必须关闭同线程校验（TestClient 多线程）
    assert engine_kwargs("sqlite:///./medplat.db") == {
        "connect_args": {"check_same_thread": False}
    }


def test_pg_engine_builds_without_connecting():
    """create_engine 惰性建连：仅解析 URL/方言，不触达网络。"""
    eng = build_engine(PG_URL)
    try:
        assert eng.dialect.name == "postgresql"
        assert eng.dialect.driver == "psycopg2"
        assert eng.url.host == "db.example"
        assert eng.url.port == 5432
        assert eng.url.database == "medplat"
        assert eng.url.username == "medplat"
        # QueuePool 默认连接池（生产可扩展 pool_size/max_overflow）
        assert type(eng.pool).__name__ == "QueuePool"
    finally:
        eng.dispose()


def test_pg_driver_installed():
    """requirements 携带 psycopg2-binary：切 PG 无需新增依赖。"""
    import psycopg2  # noqa: F401

    eng = create_engine("postgresql+psycopg2://u:p@h/db")
    eng.dispose()


# ---------- Redis：state_store 后端选择（mock 客户端验证读写调用） ----------


class FakeRedisClient:
    """记录调用并模拟最小 Redis 语义（setex/exists/incr/expire/delete/ttl）。"""

    def __init__(self):
        self.url = ""
        self.decode_responses = None
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.calls: list[tuple] = []

    def setex(self, key, ttl, value):
        self.calls.append(("setex", key, ttl))
        self.store[key] = value
        self.ttls[key] = ttl

    def exists(self, key):
        self.calls.append(("exists", key))
        return 1 if key in self.store else 0

    def incr(self, key):
        self.calls.append(("incr", key))
        self.store[key] = str(int(self.store.get(key, "0")) + 1)
        return int(self.store[key])

    def expire(self, key, ttl):
        self.calls.append(("expire", key, ttl))
        self.ttls[key] = ttl

    def delete(self, *keys):
        self.calls.append(("delete",) + keys)
        for key in keys:
            self.store.pop(key, None)
            self.ttls.pop(key, None)

    def ttl(self, key):
        self.calls.append(("ttl", key))
        return self.ttls.get(key, -2) if key in self.store else -2


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedisClient()
    module = types.ModuleType("redis")

    class Redis:  # noqa: D401 - mock
        @staticmethod
        def from_url(url, decode_responses=False, **kwargs):
            # 生产侧现在会传 socket_timeout / socket_connect_timeout /
            # health_check_interval（见 state_store._redis_client 的说明）。
            # 桩照收并记下来，好让下面的用例能断言"超时确实被设上了"。
            fake.url = url
            fake.decode_responses = decode_responses
            fake.connect_kwargs = kwargs
            return fake

    module.Redis = Redis
    monkeypatch.setitem(sys.modules, "redis", module)
    monkeypatch.setenv("MEDPLAT_REDIS_URL", "redis://cache.example:6379/0")
    return fake


def test_memory_backend_without_redis_url(monkeypatch):
    monkeypatch.delenv("MEDPLAT_REDIS_URL", raising=False)
    from app.state_store import TokenBlacklist

    blacklist = TokenBlacklist()
    assert blacklist._redis is None  # 未配置 → 进程内存实现
    blacklist.add("tok-mem")
    assert "tok-mem" in blacklist


def test_token_blacklist_selects_redis_backend(fake_redis):
    from app.state_store import TokenBlacklist

    blacklist = TokenBlacklist(default_ttl_seconds=120)
    # 后端选择：命中 mock 客户端，URL/decode 参数传递正确
    assert blacklist._redis is fake_redis
    assert fake_redis.url == "redis://cache.example:6379/0"
    assert fake_redis.decode_responses is True
    # 写路径：setex 带 TTL
    blacklist.add("tok-1")
    assert ("setex", "medplat:revoked:tok-1", 120) in fake_redis.calls
    # 读路径：exists
    assert "tok-1" in blacklist
    assert ("exists", "medplat:revoked:tok-1") in fake_redis.calls
    assert "tok-other" not in blacklist
    # 自定义 TTL 透传
    blacklist.add("tok-2", ttl_seconds=30)
    assert ("setex", "medplat:revoked:tok-2", 30) in fake_redis.calls


def test_login_failure_tracker_selects_redis_backend(fake_redis):
    from app.state_store import LoginFailureTracker

    tracker = LoginFailureTracker(fail_limit=3, lock_seconds=60)
    assert tracker._redis is fake_redis
    # 未达阈值：incr + expire（滑动窗口）
    assert tracker.record_failure("alice") is False
    assert tracker.record_failure("alice") is False
    assert ("incr", "medplat:fail:alice") in fake_redis.calls
    assert ("expire", "medplat:fail:alice", 60) in fake_redis.calls
    assert tracker.locked_remaining("alice") == 0
    # 达阈值：落锁 + 清计数
    assert tracker.record_failure("alice") is True
    assert ("setex", "medplat:lock:alice", 60) in fake_redis.calls
    assert ("delete", "medplat:fail:alice") in fake_redis.calls
    # 锁定剩余：走 Redis TTL
    assert tracker.locked_remaining("alice") == 60
    assert ("ttl", "medplat:lock:alice") in fake_redis.calls
    # 重置：删除失败计数与锁
    tracker.reset("alice")
    assert ("delete", "medplat:fail:alice", "medplat:lock:alice") in fake_redis.calls
    assert tracker.locked_remaining("alice") == 0
