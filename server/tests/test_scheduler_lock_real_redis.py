"""执行锁在**真 Redis** 上的行为（`test_scheduler_lock.py` 的补充）。

那份用例拿假 redis 验的是**逻辑**：token 对不上就不删、不续期。但假 redis 的
`eval` 是我自己用 Python 写的 if——它证明不了 Lua 脚本本身写对了，也证明不了
`SET NX EX` 与 `EXPIRE` 在真服务上的语义。这两件事恰恰是这把锁的全部依据。

默认跳过；CI 起了 Redis service 后自动生效：

    export MEDPLAT_REDIS_TEST_URL=redis://127.0.0.1:6379/1
    python -m pytest tests/test_scheduler_lock_real_redis.py -q
"""
import os
import time

import pytest

import app.scheduler as scheduler

REDIS_URL = os.environ.get("MEDPLAT_REDIS_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not REDIS_URL, reason="需要 MEDPLAT_REDIS_TEST_URL 指向可用的 Redis"),
]

KEY = "medplat:joblock:realjob"


@pytest.fixture()
def redis_client(monkeypatch):
    import redis

    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    client.delete(KEY)
    monkeypatch.setattr(scheduler, "_redis_client", lambda: client)
    # 进程内那层锁在同一进程里会先挡住第二次获取，本文件要验的是 Redis 那层，
    # 故清掉它——两层锁各自的职责在 test_scheduler_lock.py 里分别验过。
    scheduler._LOCAL_LOCKS.pop("realjob", None)
    yield client
    client.delete(KEY)


def test_真redis上_SET_NX_确实互斥(redis_client):
    first = scheduler._acquire_lock("realjob")
    assert first
    assert scheduler._acquire_lock("realjob") is None, "SET NX 应让第二个拿不到"
    assert redis_client.get(KEY) == first


def test_真redis上_TTL确实被设上(redis_client):
    scheduler._acquire_lock("realjob")
    ttl = redis_client.ttl(KEY)
    assert 0 < ttl <= scheduler.LOCK_TTL_SECONDS, f"锁应带 TTL，实际 {ttl}"


def test_真Lua_错误token不得删掉他人的锁(redis_client):
    """这条是假 redis 证明不了的：比对与删除必须在**一条** Lua 里原子完成。"""
    holder = scheduler._acquire_lock("realjob")
    scheduler._release_lock("realjob", "stale-token-of-another-instance")
    assert redis_client.get(KEY) == holder, "别人的锁被误删了"
    scheduler._release_lock("realjob", holder)
    assert redis_client.get(KEY) is None, "持有者用正确 token 应能释放"


def test_真Lua_错误token不得替他人续期(redis_client):
    scheduler._acquire_lock("realjob")
    # 把 TTL 压到一个明显小于 LOCK_TTL 的值，续期成功与否一眼可辨
    redis_client.expire(KEY, 5)
    assert scheduler._renew_lock("realjob", "stale-token") is False
    assert redis_client.ttl(KEY) <= 5, "易主后不该被续期"


def test_真Lua_持有者续期把TTL顶回去(redis_client):
    holder = scheduler._acquire_lock("realjob")
    redis_client.expire(KEY, 5)
    assert scheduler._renew_lock("realjob", holder) is True
    assert redis_client.ttl(KEY) > 5, "持有者续期应把 TTL 顶回 LOCK_TTL"


def test_真redis上_锁到期后可被接管(redis_client):
    """自愈语义：实例崩溃→心跳停→锁自然过期→别的实例接手。"""
    scheduler._acquire_lock("realjob")
    redis_client.expire(KEY, 1)
    deadline = time.monotonic() + 5
    while redis_client.get(KEY) is not None and time.monotonic() < deadline:
        time.sleep(0.1)
    assert redis_client.get(KEY) is None, "锁应在 TTL 后自行消失"
    assert scheduler._acquire_lock("realjob"), "过期后别的实例应能拿到"


def test_job_lock在真redis上起心跳并如期释放(redis_client):
    """`job_lock` 的完整路径：拿锁 → 起 keeper 续期 → 退出时释放。"""
    with scheduler.job_lock("realjob") as token:
        assert token
        assert redis_client.get(KEY) == token
        redis_client.expire(KEY, 2)
        # keeper 的心跳间隔是 TTL/4（75s），这里等不到；直接验它确实在跑
        keeper = scheduler._LockKeeper("realjob", token, interval=0.05)
        keeper.start()
        deadline = time.monotonic() + 5
        while redis_client.ttl(KEY) <= 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        keeper.stop()
        assert redis_client.ttl(KEY) > 2, "心跳应把 TTL 顶回去"
    assert redis_client.get(KEY) is None, "退出 job_lock 应释放锁"
