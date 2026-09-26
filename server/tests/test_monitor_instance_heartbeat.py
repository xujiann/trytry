"""实例心跳只在有人打开监控页时续：多实例部署下「集群节点」里只剩刚好接了监控请求的那一个（P2-256）。

`monitor.heartbeat()` 往 Redis 写 `medplat:instance:{id}`（TTL 90 秒），节点页按这些键列存活实例，说明写着「90 秒内有心跳
视为存活」。可全仓库只有 `/api/monitor/overview` 与 `/nodes` 两个接口会调它——负载均衡后面，节点页那个请求只落在一个实例
上，其余实例的心跳 90 秒后过期，好好在跑的实例显示成已下线。修法：`heartbeat_loop` 每 TTL/3 续一次，由 lifespan 与调度
循环一起启动（不挂在调度 tick 上：一轮任务跑上几分钟，心跳会跟着断）。
"""
import asyncio

import pytest


class FakeRedis:
    def __init__(self):
        self.beats: list[tuple[str, int]] = []

    def setex(self, key, ttl, value):
        self.beats.append((key, ttl))


@pytest.fixture()
def fake_redis(monkeypatch):
    from app import monitor

    fake = FakeRedis()
    monkeypatch.setattr(monitor, "_redis_client", lambda: fake)
    return fake


def test_心跳按周期续_不等人打开监控页(fake_redis, monkeypatch):
    from app import monitor

    monkeypatch.setattr(monitor, "HEARTBEAT_INTERVAL", 0.01)

    async def run_briefly():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(monitor.heartbeat_loop(), timeout=0.2)

    asyncio.run(run_briefly())
    assert len(fake_redis.beats) >= 3
    assert set(fake_redis.beats) == {(f"medplat:instance:{monitor.INSTANCE_ID}", monitor.HEARTBEAT_TTL)}


def test_应用一启动就开始续心跳(fake_redis):
    """lifespan 起得来心跳循环：修前应用跑着、没人打开监控页，Redis 里一条心跳都没有。"""
    import time

    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app):
        deadline = time.monotonic() + 5
        while not fake_redis.beats and time.monotonic() < deadline:
            time.sleep(0.05)
    assert fake_redis.beats, "应用启动后没有续过一次心跳"


def test_间隔是TTL的三分之一():
    from app import monitor

    assert monitor.HEARTBEAT_INTERVAL == monitor.HEARTBEAT_TTL // 3 == 30
