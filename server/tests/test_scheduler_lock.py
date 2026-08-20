"""回归测试：定时任务分布式锁必须校验持有者，释放不得误删他人的锁。

修的 bug：`_acquire_lock` 原本存固定值 "1"、`_release_lock` 无条件 delete。任务执行
超过 LOCK_TTL_SECONDS 时——实例 A 的锁过期 → 实例 B 抢到并重设 → A 收尾时 `finally`
把 **B 的锁**删掉 → 同一任务多实例并发。修法：acquire 存唯一 token 并返回，release 用
Lua 比对后删（仅当值等于自己的 token 才删）。

用假 redis 验证所有权语义（真 Redis 的 Lua 原子性需集成环境，CI 现无 Redis，
故此处只验逻辑；见 ROADMAP 关于集成门的待办）。
"""
from __future__ import annotations

import app.scheduler as scheduler

KEY = "medplat:joblock:job"


class FakeRedis:
    """最小实现，模拟 SET NX EX 与释放锁用的 Lua 比对删。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, key, val, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = val
        return True

    def get(self, key):
        return self.store.get(key)

    def eval(self, script, numkeys, key, arg):
        # 模拟 _RELEASE_LUA：get(key)==arg 才 del
        if self.store.get(key) == arg:
            self.store.pop(key, None)
            return 1
        return 0


def test_acquire_返回token且已持有时拒绝第二个(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(scheduler, "_redis_client", lambda: fake)
    token = scheduler._acquire_lock("job")
    assert token and fake.get(KEY) == token
    assert scheduler._acquire_lock("job") is None  # 已被持有，第二个拿不到


def test_错误token释放不得删掉他人的锁(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(scheduler, "_redis_client", lambda: fake)
    token_b = scheduler._acquire_lock("job")  # 实例 B 持有
    # 实例 A 的锁早已过期、B 已接管；A 迟到的释放带的是 A 自己的旧 token
    scheduler._release_lock("job", "stale-token-of-A")
    assert fake.get(KEY) == token_b, "B 的锁被 A 的释放误删了（正是要修的 bug）"
    # 只有持有者用正确 token 才能释放
    scheduler._release_lock("job", token_b)
    assert fake.get(KEY) is None


def test_无redis时acquire返回token且release不报错(monkeypatch):
    monkeypatch.setattr(scheduler, "_redis_client", lambda: None)
    token = scheduler._acquire_lock("job")
    assert token  # 单实例语义：恒持有
    scheduler._release_lock("job", token)  # no-op，不抛异常
