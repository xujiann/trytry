"""监控按模块计数只认路由表里的模块：原先客户端送什么路径就开什么格，不登录也能无限往里灌（P2-341）。

`monitor._module_of` 直接切原始路径：`/api` 下取第二段、`/api` 外整条路径原样当模块。没匹配到任何路由的请求
（含未登录的 404）照样各开一格——进程内计数器只增不减；配了 Redis 还落进跨重启的 hash，每次打开监控台
HGETALL 全量读回；top_modules 被 `zz0`、`/nope-0` 这类垃圾路径占满。

修法：main.py 挂完全部路由后把路由表里的模块登记给监控（`monitor.register_routes`）；计数只认登记过的模块——
模块对、路径错的 404 照旧归那个模块，静态资源并成一格 `/static`，其余并成一格 `UNMATCHED`。
"""
from app import monitor
from app.monitor import metrics


def _buckets():
    """两个并格的键（放进函数里取：修前的 monitor 没有这两个名字，回归用例照样能跑出行为上的红）。"""
    return monitor.UNMATCHED, monitor.STATIC_PREFIX


class _Pipe:
    def __init__(self, fields):
        self.fields = fields

    def hincrby(self, key, field, amount):
        self.fields.setdefault(key, set()).add(field)
        return self

    hincrbyfloat = hincrby

    def execute(self):
        return []


class _Redis:
    """只记集群计数写进了哪些 hash 字段。"""

    def __init__(self):
        self.fields: dict[str, set] = {}

    def pipeline(self):
        return _Pipe(self.fields)


def _junk(client, n):
    for i in range(n):
        client.get(f"/api/zz{i}")
        client.get(f"/nope-{i}")
        client.get(f"/static/nope-{i}.js")


def test_未匹配的路径不再各开一格(client):
    metrics.reset()
    _junk(client, 30)
    UNMATCHED, STATIC_PREFIX = _buckets()
    assert dict(metrics.by_module) == {UNMATCHED: 60, STATIC_PREFIX: 30}   # 修前 90 个键，一条路径一格
    assert set(metrics.module_duration) == {UNMATCHED, STATIC_PREFIX}


def test_集群计数的hash字段同样有界(client, monkeypatch):
    fake = _Redis()
    monkeypatch.setattr(monitor, "_redis_client", lambda *_a, **_kw: fake)
    _junk(client, 20)
    UNMATCHED, STATIC_PREFIX = _buckets()
    assert fake.fields[f"{monitor._METRICS_PREFIX}module_count"] == {UNMATCHED, STATIC_PREFIX}   # 修前 60 个字段
    assert fake.fields[f"{monitor._METRICS_PREFIX}module_duration"] == {UNMATCHED, STATIC_PREFIX}


def test_路由表里的模块都登记了(client):
    """FastAPI 的 include_router 不摊平子路由；登记要钻进去，否则模块全落进 UNMATCHED。"""
    assert {"exams", "organizations", "mgmt/departments", "health", "/", "/openapi.json"} <= monitor._known_modules
    assert len(monitor._known_modules) > 90


def test_路由表里的模块照旧按模块归(client, admin):
    metrics.reset()
    client.get("/api/organizations/999999", headers=admin)   # 模块对、路径错的 404 仍归 organizations
    client.get("/api/mgmt/departments", headers=admin)       # mgmt 下取第三段
    client.get("/static/core.js")                            # 静态资源并成一格（原先每个文件一格）
    client.get("/openapi.json")                              # 其余固定路由照旧按路径
    _, STATIC_PREFIX = _buckets()
    assert dict(metrics.by_module) == {
        "organizations": 1, "mgmt/departments": 1, STATIC_PREFIX: 1, "/openapi.json": 1}
