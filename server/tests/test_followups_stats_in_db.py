"""随访中心统计在库里按类别、状态分组数（P2-1151，第三十三批扫描 A4-4）。

`GET /api/followups/stats`（每次进随访中心都调）原先 `db.query(FollowupTask).all()` 把随访任务整表载入 ORM，在内存里按类别、
状态分组、逐条比超期：任务 1 万 → 3 万时载入 10,001 → 30,001 个对象、368 → 823 ms、峰值 16 → 49 MB（扫描实测；数十万条
外推约 8 s、0.5 GB 一次）。同功能的慢专病统计（`spd/routers/followup.py`）早就在库里 GROUP BY。

修后一条 `GROUP BY category, status`，超期用 `SUM(CASE WHEN status = 'pending' AND due_date < 截止日 THEN 1 ELSE 0 END)`，
结果逐项不变：完成率分母仍排除已取消，类别名照旧查 `CATEGORY_TITLES`（查不到回原码），按类别排序。

- 特征化：四类随访加一个表外类别、三种状态、到期日正好是截止日与前一天，逐项与原算法相同（三个截止日）——改前改后都绿；
- 缺陷：请求里载入的随访任务 ORM 对象为 0（修前一条任务一个），任务翻几番取回的行数也不变。
"""
from datetime import date, timedelta

import pytest
from sqlalchemy import event, insert

from app.database import SessionLocal, engine
from app.models import FollowupTask, Organization, Patient
from app.routers.followups import CATEGORY_TITLES

#: （类别, 状态, 到期日）
TASKS = [
    ("chronic", "pending", "2026-03-09"),     # 截止日 2026-03-10 时超期
    ("chronic", "pending", "2026-03-10"),     # 到期日就是截止日：不算超期（严格小于）
    ("chronic", "done", "2026-01-01"),        # 已完成的过期任务不算超期
    ("chronic", "cancelled", "2026-01-01"),   # 已取消的不进完成率分母
    ("discharge", "pending", "2026-03-11"),
    ("discharge", "done", "2026-03-01"),
    ("discharge", "done", "2026-03-02"),
    ("surgery", "cancelled", "2026-02-01"),   # 只有取消的：完成率分母为 0，回 0.0
    ("maternal", "pending", "2025-12-31"),
    ("other", "done", "2026-03-05"),          # 表外类别：类别名回原码
]


def _add(db, world, tasks):
    db.execute(insert(FollowupTask), [
        {"patient_id": world["patient"], "org_id": world["org"], "category": category, "status": status,
         "due_date": due, "title": "随访"} for category, status, due in tasks
    ])
    db.commit()


@pytest.fixture(scope="module")
def world(client, admin):
    with SessionLocal() as db:
        org = Organization(name="随访统计卫生院", org_type="township", level="township")
        patient = Patient(ehc_no="EHC-FUSTAT-0001", name="随访统计患者", id_card="330382197007071234")
        db.add_all([org, patient])
        db.flush()
        ids = {"org": org.id, "patient": patient.id}
        _add(db, ids, TASKS)
    return ids


def _原算法(db, cutoff: str) -> list[dict]:
    """修前的算法原样搬来当判据：任务整表载入，逐条分组、比超期。"""
    stats: dict[str, dict] = {}
    for t in db.query(FollowupTask).all():
        entry = stats.setdefault(t.category, {
            "category": t.category, "category_name": CATEGORY_TITLES.get(t.category, t.category),
            "pending": 0, "done": 0, "cancelled": 0, "overdue": 0,
        })
        entry[t.status] += 1
        if t.status == "pending" and t.due_date < cutoff:
            entry["overdue"] += 1
    for entry in stats.values():
        denominator = entry["pending"] + entry["done"]
        entry["completion_rate_pct"] = round(entry["done"] * 100 / denominator, 2) if denominator else 0.0
    return sorted(stats.values(), key=lambda x: x["category"])


def _stats(client, admin, cutoff: str) -> tuple[list[dict], int]:
    """（响应, 这一请求里载入的随访任务 ORM 对象个数）"""
    loaded = [0]

    def on_load(target, context):
        loaded[0] += 1

    event.listen(FollowupTask, "load", on_load)
    try:
        resp = client.get(f"/api/followups/stats?today={cutoff}", headers=admin)
    finally:
        event.remove(FollowupTask, "load", on_load)
    assert resp.status_code == 200, resp.text
    return resp.json(), loaded[0]


@pytest.mark.parametrize("cutoff", ["2026-03-10", "2026-03-11", "2025-01-01"])
def test_特征化_逐项与原算法相同(client, admin, world, cutoff):
    body, _ = _stats(client, admin, cutoff)
    with SessionLocal() as db:
        assert body == _原算法(db, cutoff)


def test_特征化_截止日当天的逐项明细(client, admin, world):
    body, _ = _stats(client, admin, "2026-03-10")
    assert body == [
        {"category": "chronic", "category_name": "慢病随访", "pending": 2, "done": 1, "cancelled": 1, "overdue": 1,
         "completion_rate_pct": 33.33},
        {"category": "discharge", "category_name": "出院随访", "pending": 1, "done": 2, "cancelled": 0, "overdue": 0,
         "completion_rate_pct": 66.67},
        {"category": "maternal", "category_name": "妇幼访视", "pending": 1, "done": 0, "cancelled": 0, "overdue": 1,
         "completion_rate_pct": 0.0},
        {"category": "other", "category_name": "other", "pending": 0, "done": 1, "cancelled": 0, "overdue": 0,
         "completion_rate_pct": 100.0},
        {"category": "surgery", "category_name": "术后随访", "pending": 0, "done": 0, "cancelled": 1, "overdue": 0,
         "completion_rate_pct": 0.0},
    ]


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


def test_不再把随访任务整表载入_ORM(client, admin, world):
    _, loaded = _stats(client, admin, "2026-03-10")
    assert loaded == 0, f"统计一次载入了 {loaded} 个随访任务 ORM 对象——还在整表读进内存分组"


def test_任务翻几番_取回行数不变(client, admin, world):
    url = "/api/followups/stats?today=2026-03-10"
    before = _rows_fetched(client, admin, url)
    start = date(2026, 1, 1)
    with SessionLocal() as db:   # 已有的类别 × 状态组合里加量，不加任何一个分组
        _add(db, world, [("chronic", ("pending", "done", "cancelled")[i % 3], (start + timedelta(days=i)).isoformat())
                         for i in range(150)])
    after = _rows_fetched(client, admin, url)
    assert after == before, f"多了 150 条随访任务，请求多取回了 {after - before} 行——还在整表读出来分组"
