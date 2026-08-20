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
    """最小实现，模拟 SET NX EX、比对删（_RELEASE_LUA）与比对续期（_RENEW_LUA）。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.expire_calls: list[tuple[str, int]] = []  # 续期留痕，供断言

    def set(self, key, val, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = val
        return True

    def get(self, key):
        return self.store.get(key)

    def eval(self, script, numkeys, key, *args):
        token = args[0]
        if self.store.get(key) != token:
            return 0
        if "expire" in script:  # _RENEW_LUA：仍持有才顶 TTL
            self.expire_calls.append((key, int(args[1])))
            return 1
        # _RELEASE_LUA：仍持有才删
        self.store.pop(key, None)
        return 1


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


# ---------------------------------------------------------------- 锁续期（防双跑）


def test_renew_仍持有才续期_易主返回False(monkeypatch):
    """token 所有权修的是'误删'，续期修的是'双跑'：仍持有把 TTL 顶回去，易主拒绝。"""
    fake = FakeRedis()
    monkeypatch.setattr(scheduler, "_redis_client", lambda: fake)
    token = scheduler._acquire_lock("job")
    assert scheduler._renew_lock("job", token) is True
    assert fake.expire_calls == [(KEY, scheduler.LOCK_TTL_SECONDS)]
    # 锁易主（TTL 过期后被实例 B 接管）：A 的续期必须失败且不得动 B 的 TTL
    fake.store[KEY] = "token-of-B"
    assert scheduler._renew_lock("job", "stale-token-of-A") is False
    assert len(fake.expire_calls) == 1, "易主后不得替他人续期"


def test_keeper_周期续期且stop后停止(monkeypatch):
    """任务执行期间心跳持续把 TTL 顶回去——任务跑超 TTL 不再丢锁（双跑的根因）。"""
    import time

    fake = FakeRedis()
    monkeypatch.setattr(scheduler, "_redis_client", lambda: fake)
    token = scheduler._acquire_lock("job")
    keeper = scheduler._LockKeeper("job", token, interval=0.02)
    keeper.start()
    deadline = time.monotonic() + 5
    while len(fake.expire_calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    keeper.stop()
    renewed = len(fake.expire_calls)
    assert renewed >= 2, f"心跳应多次续期，实际 {renewed} 次"
    time.sleep(0.08)
    assert len(fake.expire_calls) == renewed, "stop 后不得再续期"


def test_keeper_锁易主后自行停止(monkeypatch):
    """易主后心跳应停止（记日志退出），不得反复替他人续期。"""
    import time

    fake = FakeRedis()
    monkeypatch.setattr(scheduler, "_redis_client", lambda: fake)
    token = scheduler._acquire_lock("job")
    fake.store[KEY] = "token-of-B"  # 模拟 TTL 过期后被 B 接管
    keeper = scheduler._LockKeeper("job", token, interval=0.02)
    keeper.start()
    keeper._thread.join(timeout=5)  # 等它自行退出，不靠固定 sleep（CI 卡顿会假红）
    assert fake.expire_calls == [], "易主后不得续期"
    assert not keeper._thread.is_alive(), "易主后心跳线程应自行退出"
    keeper.stop()  # 幂等，不抛异常


# ---------------------------------------------------------------- 陈旧到期清单（防重复）


def test_tick_拿锁后重新确认到期_不跑已被他实例跑过的任务(monkeypatch):
    """锁只保证"不重叠"，保证不了"不重复"。

    多实例场景：本轮开头两个任务都到期；别的实例先跑完了其中一个并把
    next_run_at 推到未来。本实例遍历到它时锁已释放、能拿到——若不重新确认到期，
    就会把同一任务再跑一遍（长任务持锁期间尤其容易发生）。
    """
    monkeypatch.setattr(scheduler, "_redis_client", lambda: None)  # 单实例语义，锁恒得
    calls: list[str] = []
    # 第一次 due_jobs 是本轮快照（两个都到期）；之后 slow 仍到期、fast 已被他实例跑掉
    due_returns = iter([["slow", "fast"], ["slow"], ["slow"]])

    def fake_due(db):
        try:
            return next(due_returns)
        except StopIteration:  # pragma: no cover - 调用次数超出预期即测试自身有问题
            return []

    monkeypatch.setattr(scheduler, "due_jobs", fake_due)
    monkeypatch.setattr(scheduler, "run_job", lambda db, name: calls.append(name))

    executed = scheduler.tick()
    assert calls == ["slow"], f"fast 已被他实例跑过，不应重复执行；实际 {calls}"
    assert executed == 1


# ------------------------------------------------ 手工触发也要走锁（防与调度并发）


def test_job_lock_被占用时yield_None(monkeypatch):
    """`job_lock` 是调度循环与手工触发共用的那把锁——占用中必须让第二个进不来。"""
    fake = FakeRedis()
    monkeypatch.setattr(scheduler, "_redis_client", lambda: fake)
    with scheduler.job_lock("job") as first:
        assert first, "第一个应拿到锁"
        with scheduler.job_lock("job") as second:
            assert second is None, "锁被占用时必须拿不到"
    # 退出上下文后锁已释放，下一个又能拿到
    with scheduler.job_lock("job") as third:
        assert third


def test_job_lock_退出时释放且异常也释放(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(scheduler, "_redis_client", lambda: fake)
    try:
        with scheduler.job_lock("job"):
            raise RuntimeError("任务炸了")
    except RuntimeError:
        pass
    assert fake.get(KEY) is None, "任务抛异常也必须把锁还回去，否则任务永久卡死"


def test_手工触发在任务执行中返回409(monkeypatch):
    """`POST /api/jobs/{name}/run` 此前直接调 run_job，会和调度中的同一任务并发。"""
    from fastapi.testclient import TestClient

    from app.main import app

    fake = FakeRedis()
    monkeypatch.setattr(scheduler, "_redis_client", lambda: fake)
    name = next(iter(scheduler.REGISTRY))

    with TestClient(app) as client:
        token = client.post(
            "/api/auth/login", json={"username": "admin", "password": "admin123"}
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        # 锁空闲：正常跑
        ok = client.post(f"/api/jobs/{name}/run", headers=headers)
        assert ok.status_code == 201, ok.text

        # 模拟调度实例正持有该任务的锁
        assert scheduler._acquire_lock(name)
        busy = client.post(f"/api/jobs/{name}/run", headers=headers)
        assert busy.status_code == 409, busy.text
        assert "正在执行" in busy.json()["detail"]


def test_无redis时job_lock仍然互斥(monkeypatch):
    """Redis 锁挡跨实例，挡不住同进程内两条路径重叠。

    不配 `MEDPLAT_REDIS_URL` 是默认部署形态，那时 `_acquire_lock` 恒返回 token。
    若 `job_lock` 只靠它，这把锁在默认配置下等于不存在——调度线程与请求线程
    照样能同时跑同一个任务。故进程内还要有一层 threading.Lock。
    """
    monkeypatch.setattr(scheduler, "_redis_client", lambda: None)
    with scheduler.job_lock("job") as first:
        assert first, "无 Redis 时第一个仍应拿到（单实例语义）"
        with scheduler.job_lock("job") as second:
            assert second is None, "同进程内第二个必须被挡住，否则默认配置下锁形同虚设"
    with scheduler.job_lock("job") as third:
        assert third, "退出后必须能重新获取，否则任务永久卡死"
