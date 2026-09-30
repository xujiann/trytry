"""运维监控页的审计统计：按日趋势在库里按 UTC 日分组（P2-1149，第三十三批扫描 A4-1 同形第二处）。

`GET /api/audit/stats`（缺省近 30 天，最多 365 天）的按日趋势原先把窗口内**每一条**审计的（时刻, 状态码）都取回来，
在 Python 里按 `created_at.strftime("%Y-%m-%d")` 分桶：30 天内审计 3 万 → 9 万行时，一次取回 30,028 → 90,028 行、
峰值 9 → 28 MB（扫描实测）。同一个接口里的 TOP 榜、失败码分布、总数早就在库里数。

修后按年、月、日 GROUP BY，失败数用 `SUM(CASE status_code >= 400)`。口径不变：按落库的 naive UTC 时间戳的日历日分桶
（页面按 UTC 日补零画折线，与之一致，P2-417）；只有下界、没有上界；没有写操作的日子照旧不出现。

- 特征化：窗口起点前后一微秒、UTC 日界、状态码 399 / 400 分界、「现在」之后的行，逐日与原算法相同（days=30、7）——
  改前改后都绿；
- 缺陷：窗口内的审计量翻几番，请求取回的行数不变——修前一条审计取回一行；
- 真 PG 档：`EXTRACT` 在 PG 上回 numeric、分组表达式须与选择列一致，SQLite 绿了不等于 PG 也对（CLAUDE.md §6）。
"""
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, insert

from app.database import SessionLocal, engine
from app.models import AuditLog
from app.routers import users as users_router

#: 统计窗口的「现在」（naive UTC）：days=30 的窗口起点是 2025-09-10 06:00，days=7 的是 2025-10-03 06:00
NOW = datetime(2025, 10, 10, 6, 0)

#: （时刻, 状态码）
ROWS = [
    (datetime(2025, 1, 1, 9, 0), 200),                       # 窗口以外的历史
    (datetime(2025, 9, 10, 5, 59, 59, 999999), 200),         # days=30 窗口起点前一微秒：不算
    (datetime(2025, 9, 10, 6, 0), 201),                      # 窗口起点本身：算
    # UTC 日界（也是月界）前一微秒：算 09-30。SQLite 的 `strftime` 会把它算进 10-01（`deps.utc_date_parts` 的 docstring）
    (datetime(2025, 9, 30, 23, 59, 59, 999999), 409),
    (datetime(2025, 10, 1, 0, 0), 400),                      # UTC 日界之后；400 起算失败
    (datetime(2025, 10, 1, 12, 0), 399),                     # 399 仍算成功
    (datetime(2025, 10, 2, 20, 0), 500),                     # UTC 晚上 8 点（东八区已是次日）：按落库的 UTC 日
    (datetime(2025, 10, 3, 5, 59), 200),                     # days=7 窗口起点之前
    (datetime(2025, 10, 3, 6, 0), 403),                      # days=7 窗口起点本身
    (datetime(2025, 10, 10, 5, 59), 200),
    (datetime(2025, 10, 10, 7, 0), 404),                     # 「现在」之后：原实现只有下界，照数
]


def _add(db, rows, username="审计甲", path="/api/audit-daily/a"):
    db.execute(insert(AuditLog), [
        {"username": username, "method": "POST", "path": path, "status_code": status, "created_at": at}
        for at, status in rows
    ])
    db.commit()


def _原算法_按日(db, since: datetime) -> list[dict]:
    """修前按日趋势的算法原样搬来当判据：窗口内每条审计都取回来，按 naive UTC 的日历日分桶。"""
    daily: dict[str, dict[str, int]] = {}
    for created_at, status_code in (
        db.query(AuditLog.created_at, AuditLog.status_code).filter(AuditLog.created_at >= since).all()
    ):
        day = created_at.strftime("%Y-%m-%d")
        bucket = daily.setdefault(day, {"date": day, "ok": 0, "failed": 0})
        bucket["failed" if status_code >= 400 else "ok"] += 1
    return [daily[d] for d in sorted(daily)]


@pytest.fixture(scope="module")
def world(client, admin):
    """直接落库造审计行：时刻要精确到微秒。本文件只发 GET（不落审计），库里的审计行就是这里造的这些。"""
    with SessionLocal() as db:
        _add(db, ROWS)
    return True


@pytest.fixture
def frozen_now(monkeypatch):
    monkeypatch.setattr(users_router, "utcnow", lambda: NOW)
    return NOW


def _rows_fetched(client, admin, url: str) -> int:
    """这一个请求里全部 SELECT 取回的行数：拦下每条语句与参数，请求结束后在同一份数据上重放一遍数行数。"""
    seen: list[tuple] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            seen.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        resp = client.get(url, headers=admin)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert resp.status_code == 200, resp.text
    assert seen, "没拦到任何 SELECT（用例失效，请检查拦截方式）"
    with engine.connect() as conn:
        return sum(len(conn.exec_driver_sql(statement, parameters).fetchall()) for statement, parameters in seen)


@pytest.mark.parametrize("days", [30, 7])
def test_特征化_按日趋势与原算法逐日相同(client, admin, world, frozen_now, days):
    resp = client.get(f"/api/audit/stats?days={days}", headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    with SessionLocal() as db:
        assert body["daily"] == _原算法_按日(db, NOW - timedelta(days=days))
    assert body["total"] == sum(d["ok"] + d["failed"] for d in body["daily"])


def test_特征化_窗口三十天的逐日明细(client, admin, world, frozen_now):
    body = client.get("/api/audit/stats?days=30", headers=admin).json()
    assert body["daily"] == [
        {"date": "2025-09-10", "ok": 1, "failed": 0},
        {"date": "2025-09-30", "ok": 0, "failed": 1},
        {"date": "2025-10-01", "ok": 1, "failed": 1},
        {"date": "2025-10-02", "ok": 0, "failed": 1},
        {"date": "2025-10-03", "ok": 1, "failed": 1},
        {"date": "2025-10-10", "ok": 1, "failed": 1},
    ]
    assert (body["total"], body["failed"]) == (9, 5)


def test_窗口内审计量涨了_取回行数不变(client, admin, world, frozen_now):
    url = "/api/audit/stats?days=30"
    before = _rows_fetched(client, admin, url)
    expected_days = [d["date"] for d in client.get(url, headers=admin).json()["daily"]]
    with SessionLocal() as db:   # 同一天、同一账号、同一路径、同一成功码：只加量，不加任何一个分组
        _add(db, [(datetime(2025, 10, 1, 12, 0) + timedelta(seconds=i), 399) for i in range(300)])
    after = _rows_fetched(client, admin, url)
    body = client.get(url, headers=admin).json()
    assert [d["date"] for d in body["daily"]] == expected_days
    assert body["daily"][expected_days.index("2025-10-01")] == {"date": "2025-10-01", "ok": 301, "failed": 1}
    assert after == before, f"窗口内多了 300 条审计，请求多取回了 {after - before} 行——按日趋势还在逐条取回来分桶"


# ------------------------------------------------------------------ 真 PG 档（默认跳过）

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")
SERVER_DIR = Path(__file__).resolve().parents[1]


@pytest.mark.integration
@pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL")
def test_真PG_按日分组与原算法逐日相同(monkeypatch):
    """**这套库是多人共用的**：只加带随机后缀账号的审计行（`entry_hash` 为空，不进哈希链），判据与被测在同一个会话上
    现算（库里别人的审计两边一样多），跑完按账号删掉自己的行，从不 DROP SCHEMA；空库就地补跑迁移（只做加法）。"""
    from sqlalchemy import create_engine
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.orm import sessionmaker

    pg = create_engine(PG_URL)
    if not sa_inspect(pg).has_table("audit_logs"):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_DIR, env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"迁移在 PG 上失败：\n{result.stderr[-2000:]}"
    Session = sessionmaker(bind=pg, autoflush=False)
    username = f"pg_audit_daily_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(users_router, "utcnow", lambda: NOW)
    with Session() as db:
        _add(db, ROWS, username=username)
    try:
        with Session() as db:
            for days in (30, 7):
                body = users_router.audit_stats(days=days, db=db)
                assert body["daily"] == _原算法_按日(db, NOW - timedelta(days=days)), days
            # 非空洞：自己造的日界前一微秒那条 409 落在 09-30 这一格
            body = users_router.audit_stats(days=30, db=db)
            assert any(d["date"] == "2025-09-30" and d["failed"] >= 1 for d in body["daily"])
    finally:
        with Session() as db:
            db.query(AuditLog).filter(AuditLog.username == username).delete(synchronize_session=False)
            db.commit()
        pg.dispose()
