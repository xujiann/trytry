"""项目清单的「只看逾期」在截断之后才筛：逾期最久的老项目恰恰看不见（P2-414）。

`GET /api/projects` 先取最新 200 条、再在内存里挑逾期的——逾期的多是早立项、早该完成的老项目，排在最新
200 条之外，勾了「只看逾期」反倒一条都看不见。修后逾期条件（未结项、有计划完成日、早于今天，与出参的
`overdue` 同一句）在库里筛、再截前 200 条。
"""
import pytest

from app.database import SessionLocal
from app.models import AdminProject


@pytest.fixture(scope="module")
def org(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2414 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    with SessionLocal() as db:
        # 先落的老项目逾期、没结项；后落的 200 个新项目都还没到期
        db.add(AdminProject(org_id=org, name="P2414 早该完成的老项目", status="ongoing", due_date="2026-01-31"))
        db.add(AdminProject(org_id=org, name="P2414 已结项的老项目", status="done", progress_pct=100,
                            due_date="2026-01-31"))
        db.add_all([AdminProject(org_id=org, name=f"P2414 新项目{i}", status="ongoing", due_date="2027-12-31")
                    for i in range(200)])
        db.commit()
    return org


def test_只看逾期_截断之前就在库里筛(client, admin, org):
    rows = client.get("/api/projects", headers=admin,
                      params={"org_id": org, "overdue_only": True, "today": "2026-09-26"}).json()
    assert [r["name"] for r in rows] == ["P2414 早该完成的老项目"], rows   # 修前 []：被最新 200 条截掉了
    assert all(r["overdue"] for r in rows)


def test_不勾只看逾期照旧取最新200条(client, admin, org):
    rows = client.get("/api/projects", headers=admin, params={"org_id": org, "today": "2026-09-26"}).json()
    assert len(rows) == 200 and not any(r["overdue"] for r in rows)
