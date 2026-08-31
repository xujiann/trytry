"""监控计数多实例化（P1-24c）：无 Redis 输出与现状逐字节同构，有 Redis 走 hash 汇总。

单进程无 Redis 是默认部署形态，其响应必须与引入集群计数前一致（scope 文案即
process 口径的标注，不另加字段）；配了 Redis 时计数字段取集群 hash（跨实例、
跨重启），附 counter_scope="cluster"，样本仍为本实例。
"""


from app import monitor
from app.monitor import cluster_snapshot, metrics


class FakeRedis:
    """最小 Redis 桩：hash incr + hgetall + pipeline（与 redis-py 返回口径一致，
    decode_responses=True → hgetall 返回 str→str）。"""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self._queued: list = []

    # -- hash 命令（pipeline 复用同一批方法，排队后 execute 统一跑）
    def hincrby(self, key, field, amount):
        self._queued.append(("int", key, field, int(amount)))
        return self

    def hincrbyfloat(self, key, field, amount):
        self._queued.append(("float", key, field, float(amount)))
        return self

    def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    def pipeline(self):
        return self

    def execute(self):
        for kind, key, field, amount in self._queued:
            bucket = self.hashes.setdefault(key, {})
            if kind == "int":
                bucket[field] = str(int(bucket.get(field, "0")) + amount)
            else:
                bucket[field] = str(float(bucket.get(field, "0")) + amount)
        self._queued = []


def _stats(client, admin) -> dict:
    resp = client.get("/api/monitor/api-stats", headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_无redis_进程内口径与现状一致(client, admin, monkeypatch):
    """默认部署（无 Redis）回归：键集合与 scope 文案不变，无 counter_scope。"""
    monkeypatch.setattr(monitor, "_redis_client", lambda *_a, **_kw: None)
    body = _stats(client, admin)
    assert body["scope"] == "本实例自启动以来（进程重启即清零）"
    assert "counter_scope" not in body
    assert set(body) == {
        "total_requests", "avg_duration_ms", "by_status_class", "by_status_code",
        "top_modules", "slow_samples", "error_samples",
        "scope", "instance_id", "slow_threshold_ms",
    }


def test_无redis_record不触碰集群计数(monkeypatch):
    monkeypatch.setattr(monitor, "_redis_client", lambda *_a, **_kw: None)
    metrics.record("GET", "/api/exams/1", 200, 12.0)  # 不该抛，也无处可写
    assert cluster_snapshot() is None, "未配置 Redis 时集群快照必须是 None（无从得知，不冒充）"


def test_有redis_计数走hash且响应标注集群口径(client, admin, monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(monitor, "_redis_client", lambda *_a, **_kw: fake)
    # 两个"实例"各自 record（进程内计数器只吃得到本实例，Redis hash 吃得到全部）
    metrics.record("GET", "/api/exams/1", 200, 10.0)
    metrics.record("GET", "/api/exams/2", 200, 30.0)
    metrics.record("POST", "/api/patients", 409, 20.0)
    snap = cluster_snapshot()
    assert snap is not None
    assert snap["total_requests"] == 3
    assert snap["avg_duration_ms"] == 20.0
    assert snap["by_status_class"] == {"2xx": 2, "4xx": 1}
    assert snap["by_status_code"] == {200: 2, 409: 1}
    assert {m["module"]: m["count"] for m in snap["top_modules"]} == {"exams": 2, "patients": 1}

    body = _stats(client, admin)
    assert body["counter_scope"] == "cluster"
    assert body["scope"].startswith("集群")
    # 计数字段来自 Redis hash（api-stats 请求本身经中间件也会再计一次，>= 播种数即可）
    assert body["total_requests"] >= 3
    assert body["by_status_code"]["409"] == 1
    # 样本仍是进程内的键，形状不变
    assert "slow_samples" in body and "error_samples" in body


def test_有redis_计数跨清零仍在(monkeypatch):
    """进程重启（这里用 metrics.reset 模拟）后集群 hash 不清零——这正是集群口径的意义。"""
    fake = FakeRedis()
    monkeypatch.setattr(monitor, "_redis_client", lambda *_a, **_kw: fake)
    metrics.record("GET", "/api/exams/1", 200, 10.0)
    metrics.reset()
    snap = cluster_snapshot()
    assert snap is not None and snap["total_requests"] == 1
