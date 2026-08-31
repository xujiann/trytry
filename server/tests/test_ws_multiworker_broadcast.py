"""WebSocket 多进程广播（工程包 P2）。

依赖里没有 fakeredis，Redis 路径用一个进程内的假客户端单测：publish 把消息
压进各订阅者的队列，订阅线程从 listen() 取出并转发给本进程在线连接——
验证"发布端不直投、订阅端转发"的完整链路与消息编解码。
无 Redis 时的进程内路径做回归（broadcast 直投本进程连接 + 返回值语义）。
"""
import json
import queue

import pytest

import app.ws as ws_mod


@pytest.fixture(scope="module")
def admin_token(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return resp.json()["access_token"]


class _FakePubSub:
    def __init__(self, bus):
        self._q: queue.Queue = queue.Queue()
        self._bus = bus

    def subscribe(self, channel):
        self._bus.subscribers.append(self)

    def listen(self):
        while True:
            yield self._q.get()


class _FakeRedis:
    """最小 Pub/Sub 桩：publish 同步投递到所有订阅者队列（decode_responses 语义）。"""

    def __init__(self):
        self.subscribers: list[_FakePubSub] = []
        self.published: list[tuple[str, str]] = []

    def pubsub(self):
        return _FakePubSub(self)

    def publish(self, channel, data):
        self.published.append((channel, data))
        for sub in self.subscribers:
            sub._q.put({"type": "message", "channel": channel, "data": data})


@pytest.fixture()
def _fresh_manager(monkeypatch):
    """每例独立的连接管理器，避免订阅线程状态在测试间串扰。"""
    fresh = ws_mod.ConnectionManager()
    monkeypatch.setattr(ws_mod, "manager", fresh)
    return fresh


def test_inprocess_broadcast_regression(client, admin_token, _fresh_manager, monkeypatch):
    """无 Redis：广播直投本进程连接；无在线连接时返回 False（供 jobs 兜底）。"""
    monkeypatch.setattr(ws_mod, "_redis_client", lambda *a, **kw: None)
    assert _fresh_manager.broadcast({"type": "probe"}) is False  # 无人在线
    with client.websocket_connect(f"/ws/notifications?token={admin_token}") as sock:
        assert _fresh_manager.broadcast({"type": "probe", "n": 1}) is True
        assert sock.receive_json() == {"type": "probe", "n": 1}


def test_redis_broadcast_publishes_and_forwards(client, admin_token, _fresh_manager, monkeypatch):
    """有 Redis：广播走 publish，订阅线程转发到本进程在线连接（不双份）。"""
    fake = _FakeRedis()
    monkeypatch.setattr(ws_mod, "_redis_client", lambda *a, **kw: fake)
    with client.websocket_connect(f"/ws/notifications?token={admin_token}") as sock:
        assert _fresh_manager._subscriber_started is True  # 连接建立即订阅总线
        assert _fresh_manager.broadcast({"type": "critical", "n": 2}) is True
        # 消息确实经总线走了一遭
        assert len(fake.published) == 1
        channel, data = fake.published[0]
        assert channel == ws_mod._BROADCAST_CHANNEL
        assert json.loads(data) == {"message": {"type": "critical", "n": 2}, "target_org_id": None}
        # 订阅线程把总线消息转投给了本进程连接，且只收到一份
        assert sock.receive_json() == {"type": "critical", "n": 2}


def test_redis_broadcast_carries_target_org(client, admin_token, _fresh_manager, monkeypatch):
    """定向广播的 target_org_id 必须过总线不丢（admin 是监管角色仍收到）。"""
    fake = _FakeRedis()
    monkeypatch.setattr(ws_mod, "_redis_client", lambda *a, **kw: fake)
    with client.websocket_connect(f"/ws/notifications?token={admin_token}") as sock:
        _fresh_manager.broadcast({"type": "critical"}, target_org_id=42)
        assert json.loads(fake.published[0][1])["target_org_id"] == 42
        assert sock.receive_json() == {"type": "critical"}


def test_redis_publish_failure_degrades_to_local(client, admin_token, _fresh_manager, monkeypatch):
    """publish 抛错时降级为进程内直投，广播不丢。"""

    class _BrokenRedis(_FakeRedis):
        def publish(self, channel, data):
            raise RuntimeError("redis down")

    monkeypatch.setattr(ws_mod, "_redis_client", lambda *a, **kw: _BrokenRedis())
    with client.websocket_connect(f"/ws/notifications?token={admin_token}") as sock:
        assert _fresh_manager.broadcast({"type": "degraded"}) is True
        assert sock.receive_json() == {"type": "degraded"}
