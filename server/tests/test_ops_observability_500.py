"""500 与健康探针的**可观测性**：错误率要看得见，探针要真的能摘节点。

两个缺口都属于"平时看不出来、出事时正好失灵"的那一类：

- **未捕获异常整个跳过了记账路径**。它从 `await call_next` 一路抛到 Starlette 最外层，
  于是 `monitor_metrics.record` 没跑、访问日志没写、`X-Request-ID` 没回。
  结果是**全站 500 的时候监控台错误率显示 0%**——看板越红的时候越干净。
- **`/api/health` 库不通也回 200**。探针看状态码不看响应体，而 `Dockerfile` 的
  HEALTHCHECK 写的就是 `.raise_for_status()`——那条 HEALTHCHECK 从设计之初就没生效过，
  一个连不上库的实例会一直留在轮询里。

两处都要求**响应体逐字节不变**（CLAUDE.md §11 治理不得改响应字节）：500 体照抄
Starlette 默认的 `Internal Server Error`，health 体的字段一个不增不减。
"""
import json
import logging

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app import main
from app.main import app
from app.monitor import metrics

BOOM_PATH = "/api/__boom_for_test__"


class Boom(RuntimeError):
    """只在本模块里抛，好让断言能认出"就是这一次"。"""


@pytest.fixture(scope="module")
def client():
    reset_database()
    # 临时挂一条必炸的路由。用完摘掉并还原 openapi 缓存，免得污染别的用例。
    @app.get(BOOM_PATH)
    def _boom():  # pragma: no cover - 只为抛异常而存在
        raise Boom("故意炸的")

    cached_schema = app.openapi_schema
    try:
        # raise_server_exceptions=False：让 TestClient 把 500 当成响应交回来，
        # 好断言状态码/响应头/响应体；否则异常会直接被重新抛出。
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c
    finally:
        # 按 path 摘，不按下标——`routes[-1]` 假设了「最后一个就是我加的那个」，
        # 一旦别处也往 app 上挂了东西就会摘错人。摘不干净的后果不在本模块：
        # `test_refactor_drift_guards.py` 逐条快照了全部端点 URL，
        # 漏一个临时路由会让它报「凭空多出一个端点」。
        app.router.routes[:] = [
            r for r in app.router.routes if getattr(r, "path", None) != BOOM_PATH
        ]
        app.openapi_schema = cached_schema
    assert all(getattr(r, "path", None) != BOOM_PATH for r in app.router.routes)


@pytest.fixture
def access_log(monkeypatch):
    """截获访问日志：直接挂一个 handler 到 `medplat.access` 上收原始 JSON 行。

    不用 caplog：那要经 root，中间还要挑出是哪个 logger 的；这里要的就是这条 logger
    写出去的**那一行字节**，自己挂 handler 最直接，也不受日志级别配置影响。
    """
    rows: list[dict] = []

    class Collector(logging.Handler):
        def emit(self, record):
            rows.append(json.loads(record.getMessage()))

    handler = Collector()
    main._access_logger.addHandler(handler)
    monkeypatch.setattr(main.settings, "log_json", True)
    yield rows
    main._access_logger.removeHandler(handler)


# ---------------------------------------------------------------- 未捕获异常

def test_未捕获异常计入错误率(client):
    """核心缺口：500 不进 by_status_class，监控台在全站崩溃时显示 0% 错误率。"""
    metrics.reset()
    resp = client.get(BOOM_PATH)
    assert resp.status_code == 500
    snap = metrics.snapshot()
    assert snap["by_status_class"].get("5xx") == 1, (
        f"未捕获异常没有计入错误率：by_status_class={snap['by_status_class']}"
    )
    assert snap["by_status_code"].get(500) == 1


def test_未捕获异常进错误样本(client):
    """错误样本是定位入口——只记 4xx 不记 500，等于把最严重的一类漏掉。"""
    metrics.reset()
    client.get(BOOM_PATH)
    samples = metrics.snapshot()["error_samples"]
    assert any(s["path"] == BOOM_PATH and s["status"] == 500 for s in samples), samples


def test_未捕获异常也计入耗时(client):
    """500 常常是**超时**引起的，把它排除在耗时统计外会让平均耗时看着很健康。"""
    metrics.reset()
    client.get(BOOM_PATH)
    snap = metrics.snapshot()
    assert snap["total_requests"] == 1
    assert snap["avg_duration_ms"] > 0


def test_500响应带追踪ID(client):
    resp = client.get(BOOM_PATH)
    assert resp.headers.get("X-Request-ID"), "500 没有 X-Request-ID，用户报错时无从定位"


def test_500透传调用方的追踪ID(client):
    """网关已经给了 ID 就沿用，别在最需要串联的那一跳上换号。"""
    resp = client.get(BOOM_PATH, headers={"X-Request-ID": "trace-from-gateway"})
    assert resp.headers["X-Request-ID"] == "trace-from-gateway"


@pytest.mark.parametrize(
    "hostile",
    [
        "a\r\nSet-Cookie: evil=1",       # 换行注入：想在响应里多塞一个头
        "b" * 4096,                       # 超长
        "<script>alert(1)</script>",      # 反射到别处时的老套路
        "id with spaces; and=semis",
    ],
)
def test_恶意追踪ID既注入不了也打不断500(client, hostile):
    """`X-Request-ID` 是**调用方给什么就回什么**（原中间件就是这样，本次把它延伸到了
    500 那条路）。既然反射，就得确认反射不出事：不能多出一个头，也不能让异常处理器
    自己先炸——那会把一个干净的 500 变成一个断掉的响应。"""
    try:
        resp = client.get(BOOM_PATH, headers={"X-Request-ID": hostile})
    except Exception as exc:  # noqa: BLE001 - 客户端侧就拒了同样算安全，但要说清是哪种
        assert "\r" in hostile or "\n" in hostile, f"合法头不该被拒：{type(exc).__name__}"
        return
    assert resp.status_code == 500
    assert "evil" not in resp.headers, f"响应里多出了一个头：{dict(resp.headers)}"
    assert "set-cookie" not in {k.lower() for k in resp.headers}
    assert resp.content == b"Internal Server Error"


def test_500响应体逐字节不变(client):
    """只加头，不改字节——500 的响应体也是对外行为。"""
    resp = client.get(BOOM_PATH)
    assert resp.content == b"Internal Server Error"
    assert resp.headers["content-type"].startswith("text/plain")


def test_500写访问日志且带异常类名(client, access_log):
    metrics.reset()
    resp = client.get(BOOM_PATH)
    rows = [r for r in access_log if r["path"] == BOOM_PATH]
    assert rows, "500 没有写访问日志"
    row = rows[-1]
    assert row["status"] == 500
    assert row["error"] == "Boom", f"日志没记异常类型，排障只能靠猜：{row}"
    assert row["request_id"] == resp.headers["X-Request-ID"], "日志与响应头的 ID 对不上，串不起来"
    assert row["duration_ms"] >= 0


def test_正常请求日志形状不变(client, access_log):
    """回归：成功路径的日志字段不增不减（`error` 只在异常时出现）。"""
    client.get("/api/health")
    rows = [r for r in access_log if r["path"] == "/api/health"]
    assert rows
    assert set(rows[-1]) == {"ts", "request_id", "method", "path", "status", "duration_ms"}


# ---------------------------------------------------------------- 健康探针

def test_健康检查正常时回200(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert resp.json()["database"] == "ok"


def test_库不通时健康检查回503(client, monkeypatch):
    """探针看的是状态码。回 200 等于告诉负载均衡"我很好"，然后把流量全部吸进来报错。"""
    def _dead(*_a, **_kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(main.engine, "connect", _dead)
    resp = client.get("/api/health")
    assert resp.status_code == 503, (
        f"库不通仍回 {resp.status_code}，Dockerfile 里那条 raise_for_status 的 "
        "HEALTHCHECK 永远不会触发，坏节点摘不掉"
    )


def test_库不通时健康检查响应体不变(client, monkeypatch):
    """改的只有状态码：字段集合与取值口径都照旧，免得下游解析响应体的探针跟着坏。"""
    def _dead(*_a, **_kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(main.engine, "connect", _dead)
    body = client.get("/api/health").json()
    assert set(body) == {"status", "service", "version", "database"}
    assert body["status"] == "degraded"
    assert body["database"] == "error"
    assert body["service"] == "medplat"


def test_健康检查不因库不通而抛异常(client, monkeypatch):
    """探针本身不能是新的故障源——库炸了它要回 503，不是回连接异常的 traceback。"""
    def _dead(*_a, **_kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(main.engine, "connect", _dead)
    assert client.get("/api/health").status_code == 503
    monkeypatch.undo()
    assert client.get("/api/health").status_code == 200  # 恢复后自己回来，不用重启
