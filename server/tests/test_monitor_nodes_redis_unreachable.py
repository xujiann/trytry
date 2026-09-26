"""节点状态把「配了 Redis 但连不上」说成「未配置 Redis，单实例部署可忽略」（P2-355）。

`known_instances()` 在没配 Redis 与读心跳失败时都返回 None，`/api/monitor/nodes` 一律回「未配置 Redis……可忽略此项」；
同一页的概览却报着「已配置、未连通」。多实例部署恰恰在 Redis 连不上时最需要知道集群里有几台——一句「可忽略」把人带偏。
"""
from app import monitor as monitor_mod
from app.routers import monitor as monitor_router


class DownRedis:
    """配了、但每一次调用都连不上。"""

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise ConnectionError("redis down")
        return fail


def test_配了redis但连不上_不说未配置(client, admin, monkeypatch):
    down = DownRedis()
    monkeypatch.setattr(monitor_mod, "_redis_client", lambda *a, **kw: down)
    monkeypatch.setattr(monitor_router, "_redis_client", lambda *a, **kw: down)
    body = client.get("/api/monitor/nodes", headers=admin).json()
    assert body["scope"] == "unknown" and body["instances"] is None
    assert body["note"] == "已配置 Redis 但读取实例心跳失败，无法发现同集群其他实例：请检查 Redis 连接"   # 修前「未配置……可忽略」


def test_没配redis照旧说可忽略(client, admin):
    body = client.get("/api/monitor/nodes", headers=admin).json()
    assert body["note"] == "未配置 Redis，无法发现同集群其他实例；单实例部署可忽略此项"
