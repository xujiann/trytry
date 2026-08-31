"""审计中间件并发与故障隔离（工程包 P2）。

两件事各一张网：

1. **链尾串行化**：`_write_audit` 的"读链尾→算哈希→插入"三步不是原子的。
   SQLite 路径由进程内全局锁串行化——用 8 路 Barrier 线程**直接并发调用**
   `_write_audit` 验证（HTTP 请求经异步中间件在事件循环上天然串行，测不出
   这个洞；直接多线程调用才是"多写者"的诚实模拟，也正是去掉锁会分叉的路径）。
   PG 路径用 `pg_advisory_xact_lock`，无真 PG 时锁 SQL 的方言渲染与代码路径
   由静态断言钉住。
2. **故障隔离**：审计写失败（会话建不出来/commit 抛错）不得拖垮业务响应——
   业务写照常 2xx，丢失的审计记入错误日志。

非空洞性：去掉 `_AUDIT_SQLITE_LOCK` → 并发用例链分叉变红；去掉 try/except
兜底 → 故障注入用例业务响应变 500 变红。
"""
import inspect
import threading

from sqlalchemy.dialects import postgresql

import app.main as main_mod
from app.audit_chain import verify_chain
from app.database import SessionLocal
from app.models import AuditLog

WORKERS = 8


class _FakeURL:
    def __init__(self, path: str) -> None:
        self.path = path


class _FakeRequest:
    method = "POST"

    def __init__(self, path: str) -> None:
        self.headers: dict = {}
        self.url = _FakeURL(path)


def _chain_entries():
    db = SessionLocal()
    try:
        return db.query(AuditLog).order_by(AuditLog.id).all()
    finally:
        db.close()


def test_concurrent_direct_writes_keep_chain_intact(client):
    """8 路 Barrier 并发直调 _write_audit：链不得分叉（验证 SQLite 锁路径）。"""
    barrier = threading.Barrier(WORKERS)
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=10)
            main_mod._write_audit(_FakeRequest(f"/api/audit-race-probe/{i}"), 201)
        except BaseException as exc:  # noqa: BLE001 - 收集断言用
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, errors

    entries = _chain_entries()
    probe = [e for e in entries if e.path.startswith("/api/audit-race-probe/")]
    assert len(probe) == WORKERS, "并发写有丢失（兜底吞掉了失败？先查错误日志）"
    result = verify_chain(entries)
    assert result["valid"] is True, f"审计链在并发写后分叉/断链：{result}"


def test_concurrent_http_writes_keep_chain_intact(client, admin):
    """端到端回归：8 路并发 HTTP 写请求后 verify 链完整。"""
    barrier = threading.Barrier(WORKERS)
    statuses: list[int] = []

    def worker(i: int) -> None:
        barrier.wait(timeout=10)
        resp = client.post(f"/api/audit-http-probe/{i}", headers=admin)
        statuses.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(WORKERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert statuses == [404] * WORKERS  # 无此路由，但写操作尝试必须留痕

    entries = _chain_entries()
    probe = [e for e in entries if e.path.startswith("/api/audit-http-probe/")]
    assert len(probe) == WORKERS
    assert verify_chain(entries)["valid"] is True


def test_pg_advisory_lock_sql_renders_and_is_wired():
    """PG 路径的 SQL 渲染断言：无真 PG 环境时钉住方言语句与接线。"""
    rendered = str(main_mod._AUDIT_PG_LOCK_SQL.compile(dialect=postgresql.dialect()))
    assert "pg_advisory_xact_lock" in rendered
    assert isinstance(main_mod._AUDIT_PG_LOCK_KEY, int)
    # 接线断言：_write_audit 必须按方言分流，PG 分支执行咨询锁语句
    src = inspect.getsource(main_mod._write_audit)
    assert "_AUDIT_PG_LOCK_SQL" in src, "PG 咨询锁未接入 _write_audit"
    assert "postgresql" in src, "_write_audit 未按方言分流串行化机制"
    assert "_AUDIT_SQLITE_LOCK" in src, "SQLite 进程内锁未接入 _write_audit"


def test_audit_session_failure_does_not_break_business(client, admin, monkeypatch):
    """故障注入①：审计会话建不出来（如连接池耗尽），业务写仍 2xx。"""
    before = len(_chain_entries())

    def boom():
        raise RuntimeError("audit db down")

    # 只打 app.main 的名字：业务路由走 app.database.get_db，不受影响
    monkeypatch.setattr(main_mod, "SessionLocal", boom)
    resp = client.post(
        "/api/organizations",
        json={"name": "审计故障注入甲机构", "org_type": "township", "level": "township"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    monkeypatch.undo()
    assert len(_chain_entries()) == before  # 本条审计确实丢了（已记错误日志）


def test_audit_commit_failure_rolls_back_and_business_ok(client, admin, monkeypatch):
    """故障注入②：审计 commit 抛错，rollback 后业务写仍 2xx、链无残缺行。"""

    class _CommitBoomSession:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, item):
            return getattr(self._real, item)

        def commit(self):
            raise RuntimeError("audit commit boom")

    real_factory = main_mod.SessionLocal
    monkeypatch.setattr(main_mod, "SessionLocal", lambda: _CommitBoomSession(real_factory()))
    before = len(_chain_entries())
    resp = client.post(
        "/api/organizations",
        json={"name": "审计故障注入乙机构", "org_type": "township", "level": "township"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    monkeypatch.undo()
    entries = _chain_entries()
    assert len(entries) == before
    assert verify_chain(entries)["valid"] is True


def test_审计落库必须经线程池且必须await(client):
    """ADR-0016（P2-30）：`audit_middleware` 是 async def、跑在事件循环上，而
    `_write_audit` 是同步阻塞 I/O（会话/查链尾/commit，生产 PG 还要等跨实例
    advisory lock）——在循环上直调会让**全部在途请求**陪着一条审计等库。

    钉两头：
    - 不得直调 `_write_audit`（挡"顺手改回去"）；
    - 两条路径（正常返回 + 异常 500）都必须 `await run_in_threadpool(...)`——
      丢掉 await 变 fire-and-forget，"响应返回时审计已尝试落库"的保证会
      **静默**消失（协程根本没跑，审计一条都不落，还不报错）。
    """
    import ast
    import textwrap

    src = textwrap.dedent(inspect.getsource(main_mod.audit_middleware))
    tree = ast.parse(src)
    direct = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "_write_audit"
    ]
    assert not direct, "审计落库回到了事件循环直调——P2-30 复发"
    hops = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "run_in_threadpool"
        and any(isinstance(a, ast.Name) and a.id == "_write_audit" for a in node.args)
    ]
    assert len(hops) == 2, "正常与异常两条路径都必须经线程池落审计"
    awaited = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Await)}
    assert all(id(h) in awaited for h in hops), (
        "run_in_threadpool 必须 await——不 await 协程根本不执行，审计静默全丢"
    )
