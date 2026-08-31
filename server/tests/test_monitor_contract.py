"""运行监控 `/api/monitor` 三个端点的**特征化网 + 响应契约**。

套路同 test_rules_contract.py / test_admin_mgmt_contract.py：先钉住**当前**响应的
完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §11）。

本簇的建模判断（条件键最密集的模块，三个端点全要 exclude_unset）：

- `/overview` 的探活块是**分支异形**：database 成功带 `latency_ms`、失败带
  `error`（`dialect` 两分支恒在）；redis 未配置带 `note`、连通带 `latency_ms`、
  失败带 `error`（`configured/connected` 恒在）→ 条件键按出键序声明 +
  `response_model_exclude_unset=True`。故障分支（数据库探活失败/redis ping 失败）
  依赖外部故障注入，链路上 pragma no cover，本网只钉可达的三种形态。
- `/api-stats` 的 `counter_scope` 是**真条件键**：仅集群口径出现，进程口径
  **整键不在**（P1-24c 明确"不另加字段"）——双向钉。计数值/耗时是 walltime，
  按既有先例回绑后钉类型（`avg_duration_ms`/`slow_threshold_ms` 恒 float，
  `total_requests` int）；`by_status_code` 键是 int，JSON 化后仍是字符串键。
- `/nodes` 两形状：无 Redis 四键（`instances` 是**值为 null 的恒在键**）、
  有 Redis 两键（`instance_id`/`note` 整键不在）→ 同模型 + exclude_unset 双向钉。
- 走 FakeRedis 桩（与 test_monitor_cluster_metrics 同款）钉集群分支。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app import monitor as monitor_mod
from app.main import app
from app.monitor import INSTANCE_ID, metrics
from app.routers import monitor as monitor_router

OVERVIEW_KEYS = ["scope", "instance_id", "uptime_seconds", "environment", "database", "redis", "scheduler"]
SCHEDULER_KEYS = ["jobs_total", "jobs_enabled", "overdue_jobs", "recent_failures"]
API_STATS_KEYS = [
    "total_requests", "avg_duration_ms", "by_status_class", "by_status_code",
    "top_modules", "slow_samples", "error_samples", "scope", "instance_id", "slow_threshold_ms",
]
TOP_MODULE_KEYS = ["module", "count", "avg_duration_ms"]
ERROR_SAMPLE_KEYS = ["method", "path", "status", "at"]
NODES_UNKNOWN_KEYS = ["scope", "instance_id", "instances", "note"]


class FakeRedis:
    """最小桩：hash 计数 + 心跳 KV + ping（口径同 test_monitor_cluster_metrics）。"""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}
        self.kv: dict[str, str] = {}
        self._queued: list = []

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

    def ping(self):
        return True

    def setex(self, key, ttl, value):
        self.kv[key] = str(value)

    def keys(self, pattern):
        return [k for k in self.kv if k.startswith("medplat:instance:")]

    def get(self, key):
        return self.kv.get(key)


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# ---------------------------------------------------------------- /overview


def test_概览精确形状与键序_无redis(client, admin):
    body = client.get("/api/monitor/overview", headers=admin).json()
    assert list(body.keys()) == OVERVIEW_KEYS
    assert list(body["scheduler"].keys()) == SCHEDULER_KEYS
    jobs = client.get("/api/jobs", headers=admin).json()
    # uptime/latency 是 walltime（回绑钉类型）；其余逐值精确
    assert type(body["uptime_seconds"]) is int and body["uptime_seconds"] >= 0
    assert isinstance(body["database"]["latency_ms"], float)
    assert body == {
        "scope": "本实例（调用统计与启动时长为进程内数据）",
        "instance_id": INSTANCE_ID,
        "uptime_seconds": body["uptime_seconds"],
        "environment": "dev",
        # database 成功分支：connected/latency_ms/dialect，无 error 键
        "database": {"connected": True, "latency_ms": body["database"]["latency_ms"],
                     "dialect": "sqlite"},
        # redis 未配置分支：configured/connected/note，无 latency_ms/error 键
        "redis": {
            "configured": False,
            "connected": False,
            "note": "未配置 Redis：登出黑名单、防爆破锁定、限流与任务抢锁均为进程内生效，"
                    "多实例部署必须配置",
        },
        "scheduler": {
            "jobs_total": len(jobs),
            "jobs_enabled": sum(1 for j in jobs if j["enabled"]),
            "overdue_jobs": [],
            "recent_failures": [],
        },
    }


def test_概览_redis连通分支精确(client, admin, monkeypatch):
    monkeypatch.setattr(monitor_router, "_redis_client", lambda *a, **kw: FakeRedis())
    body = client.get("/api/monitor/overview", headers=admin).json()
    assert list(body["redis"].keys()) == ["configured", "connected", "latency_ms"]
    assert body["redis"]["configured"] is True and body["redis"]["connected"] is True
    assert isinstance(body["redis"]["latency_ms"], float)
    assert "note" not in body["redis"] and "error" not in body["redis"]


def test_概览_近期失败行精确(client, admin):
    """recent_failures 行是 name/at/status/message 四键（键序即 handler 出键序）。"""
    from app.database import SessionLocal
    from app.models import JobRun

    with SessionLocal() as db:
        db.add(JobRun(job_name="mc_job", trigger="manual", status="failed", message="演练失败"))
        db.commit()
    try:
        rows = client.get("/api/monitor/overview", headers=admin).json()["scheduler"]["recent_failures"]
        assert [list(r.keys()) for r in rows] == [["name", "at", "status", "message"]]
        assert rows == [{"name": "mc_job", "at": rows[0]["at"],
                         "status": "failed", "message": "演练失败"}]
        assert isinstance(rows[0]["at"], str) and "T" in rows[0]["at"]
    finally:
        with SessionLocal() as db:
            db.query(JobRun).delete()
            db.commit()


# ---------------------------------------------------------------- /api-stats


def test_调用统计精确_进程口径无counter_scope(client, admin):
    # 先各预热一次再清零：首次调用要付一次性成本（导入/查询编译），实测冷调用
    # 能超过 1000ms 慢阈值而混进 slow_samples——那是 walltime 噪声，不是契约。
    client.get("/api/users/roles", headers=admin)
    client.get("/api/organizations/999999", headers=admin)
    metrics.reset()
    assert client.get("/api/users/roles", headers=admin).status_code == 200
    assert client.get("/api/organizations/999999", headers=admin).status_code == 404
    body = client.get("/api/monitor/api-stats", headers=admin).json()
    assert list(body.keys()) == API_STATS_KEYS       # counter_scope 整键不在
    assert [list(m.keys()) for m in body["top_modules"]] == [TOP_MODULE_KEYS] * 2
    assert [list(e.keys()) for e in body["error_samples"]] == [ERROR_SAMPLE_KEYS]
    # 耗时均值是 walltime（回绑钉 float）；计数、状态分布、样本逐值精确
    assert isinstance(body["avg_duration_ms"], float)
    for m in body["top_modules"]:
        assert isinstance(m["avg_duration_ms"], float)
    assert body == {
        "total_requests": 2,
        "avg_duration_ms": body["avg_duration_ms"],
        "by_status_class": {"2xx": 1, "4xx": 1},
        "by_status_code": {"200": 1, "404": 1},
        "top_modules": [
            {"module": "users", "count": 1, "avg_duration_ms": body["top_modules"][0]["avg_duration_ms"]},
            {"module": "organizations", "count": 1,
             "avg_duration_ms": body["top_modules"][1]["avg_duration_ms"]},
        ],
        "slow_samples": [],
        "error_samples": [{"method": "GET", "path": "/api/organizations/999999",
                           "status": 404, "at": body["error_samples"][0]["at"]}],
        "scope": "本实例自启动以来（进程重启即清零）",
        "instance_id": INSTANCE_ID,
        "slow_threshold_ms": 1000.0,
    }
    assert isinstance(body["slow_threshold_ms"], float)


def test_调用统计_集群口径带counter_scope(client, admin, monkeypatch):
    """顺带用一条 ≥1000ms 的合成记录钉住 slow_samples 的行形：真实慢请求的
    成员资格是 walltime（冷启动才超阈值，两次同码运行都不同），只有合成记录
    能把这四键行确定性地摆进响应。"""
    fake = FakeRedis()
    monkeypatch.setattr(monitor_mod, "_redis_client", lambda *a, **kw: fake)
    metrics.reset()
    metrics.record("GET", "/api/exams/1", 200, 10.0)
    metrics.record("POST", "/api/patients", 409, 30.5)
    metrics.record("GET", "/api/exams/2", 200, 1500.0)
    body = client.get("/api/monitor/api-stats", headers=admin).json()
    # counter_scope 声明于 scope 之后、instance_id 之前——集群分支的插入位
    assert list(body.keys()) == API_STATS_KEYS[:8] + ["counter_scope"] + API_STATS_KEYS[8:]
    slow = body["slow_samples"][0]
    assert list(slow.keys()) == ["method", "path", "duration_ms", "at"]
    # duration_ms 是中间件 round(x, 2) 的 float——声明 int 会把 1500.0 变 1500
    assert type(slow["duration_ms"]) is float
    assert body == {
        "total_requests": 3,
        "avg_duration_ms": 513.5,
        "by_status_class": {"2xx": 2, "4xx": 1},
        "by_status_code": {"200": 2, "409": 1},
        "top_modules": [
            {"module": "exams", "count": 2, "avg_duration_ms": 755.0},
            {"module": "patients", "count": 1, "avg_duration_ms": 30.5},
        ],
        "slow_samples": [{"method": "GET", "path": "/api/exams/2",
                          "duration_ms": 1500.0, "at": slow["at"]}],
        "error_samples": [{"method": "POST", "path": "/api/patients", "status": 409,
                           "at": body["error_samples"][0]["at"]}],
        "scope": "集群（Redis 计数汇总，跨实例跨重启；慢请求与错误样本仍为本实例）",
        "counter_scope": "cluster",
        "instance_id": INSTANCE_ID,
        "slow_threshold_ms": 1000.0,
    }


# ---------------------------------------------------------------- /nodes


def test_节点_无redis四键_instances是null值恒在键(client, admin):
    body = client.get("/api/monitor/nodes", headers=admin).json()
    assert list(body.keys()) == NODES_UNKNOWN_KEYS
    assert body == {
        "scope": "unknown",
        "instance_id": INSTANCE_ID,
        "instances": None,
        "note": "未配置 Redis，无法发现同集群其他实例；单实例部署可忽略此项",
    }


def test_节点_有redis两键_instance_id与note整键不在(client, admin, monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(monitor_mod, "_redis_client", lambda *a, **kw: fake)
    body = client.get("/api/monitor/nodes", headers=admin).json()
    assert list(body.keys()) == ["scope", "instances"]
    assert [list(i.keys()) for i in body["instances"]] == [["instance_id", "self", "uptime_seconds"]]
    row = body["instances"][0]
    assert type(row["uptime_seconds"]) is int
    assert body == {
        "scope": "集群（Redis 心跳，90 秒内有心跳视为存活）",
        "instances": [{"instance_id": INSTANCE_ID, "self": True,
                       "uptime_seconds": row["uptime_seconds"]}],
    }
