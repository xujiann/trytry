"""P1-54 回归：**截断在筛选之前**导致的静默漏报。

五个带筛选的清单原先都是同一个形状——

    rows = query.order_by(...).limit(N).all()      # 先取前 N 条
    return [r for r in rows if 条件(r)]            # 再在 Python 里筛

于是 `.limit(N)` 限的是**扫描范围**而不是输出条数：第 N 条之后的匹配项
**根本没被看过**。后果不是"看不到全部"（那是 P2-8 的分页问题），而是
**筛出来的结果就是错的**，而且少报的恰恰是最该看见的那几条：
近效期批次、可用疫苗、短缺应急物资、逾期项目、在用的知识条目。

修法是把判定下推到 SQL，让上限从扫描上限变成**输出上限**，这样才谈得上分页。

**每条用例都必须把匹配项放在原硬上限之外**——否则它证明不了任何事：
把匹配项放在前 N 条里，改之前的代码也一样能返回它。所以下面统一的做法是
先插 1 条匹配项，再插满 N 条**排在它前面的**不匹配项，然后断言匹配项仍在
响应里。删掉对应的 `query.filter(...)` 下推，这些用例会当场变红。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import (
    AdminProject,
    DrugBatch,
    EmergencyResource,
    KnowledgeEntry,
    Organization,
    VaccineBatch,
)


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


@pytest.fixture(scope="module")
def org_id(client, admin):
    db = SessionLocal()
    try:
        org = Organization(name="P1-54 回归院", org_type="township", level="township")
        db.add(org)
        db.commit()
        return org.id
    finally:
        db.close()


def _seed(rows):
    db = SessionLocal()
    try:
        db.add_all(rows)
        db.commit()
    finally:
        db.close()


TODAY = "2026-06-01"


def test_近效期批次_第500条之后仍有余量的批次不会被漏掉(client, admin, org_id):
    # 排序是 (expire_date, id) 升序：效期同天时按 id 先后，所以匹配项先插。
    _seed([DrugBatch(org_id=org_id, drug_code="P154", batch_no="hit",
                     expire_date="2026-06-10", quantity=100, used_quantity=0)])
    _seed([DrugBatch(org_id=org_id, drug_code="P154", batch_no=f"used-{i}",
                     expire_date="2026-06-10", quantity=10, used_quantity=10)
           for i in range(500)])

    resp = client.get(f"/api/pharmacy/batches/expiring?days=30&org_id={org_id}&today={TODAY}",
                      headers=admin)
    assert resp.status_code == 200, resp.text
    hits = [b for b in resp.json() if b["batch_no"] == "hit"]
    assert hits, "第 500 条之后的近效期批次被漏掉了——余量判定还没下推到 SQL"
    # 用完的批次一条都不该出现：下推不能顺手放宽条件
    assert all(b["batch_no"] == "hit" for b in resp.json())
    assert resp.headers["X-Total-Count"] == "1", "总数要报筛完的条数，不是扫描池大小"


def test_可用疫苗批次_第500条之后的可用批次不会被漏掉(client, admin, org_id):
    # 排序是 id 降序：匹配项先插（id 最小），500 条不可用的排在它前面。
    _seed([VaccineBatch(org_id=org_id, vaccine_code="P154", vaccine_name="回归苗",
                        batch_no="hit", expire_date="2027-01-01",
                        quantity=100, used_quantity=0, status="normal")])
    _seed([VaccineBatch(org_id=org_id, vaccine_code="P154", vaccine_name="回归苗",
                        batch_no=f"expired-{i}", expire_date="2020-01-01",
                        quantity=100, used_quantity=0, status="normal")
           for i in range(500)])

    resp = client.get(
        f"/api/vaccine-supply/batches?usable_only=true&org_id={org_id}&today={TODAY}",
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    assert [b["batch_no"] for b in resp.json()] == ["hit"], (
        "第 500 条之后的可用批次被漏掉了——接种点会以为没苗了"
    )


def test_应急物资短缺_第500条之后的短缺物资不会被漏掉(client, admin, org_id):
    _seed([EmergencyResource(org_id=org_id, name="P154-短缺", resource_type="drug",
                             quantity=1, min_quantity=100)])
    _seed([EmergencyResource(org_id=org_id, name=f"P154-充足-{i}", resource_type="drug",
                             quantity=999, min_quantity=1)
           for i in range(500)])

    resp = client.get(
        f"/api/surveillance/resources?shortage_only=true&org_id={org_id}&today={TODAY}",
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    assert [r["name"] for r in resp.json()] == ["P154-短缺"], (
        "第 500 条之后的短缺物资被漏掉了——预警页少报的正是最该看见的那条"
    )


def test_逾期项目_第200条之后的逾期项目不会被漏掉(client, admin, org_id):
    _seed([AdminProject(org_id=org_id, name="P154-逾期", status="ongoing",
                        due_date="2020-01-01")])
    _seed([AdminProject(org_id=org_id, name=f"P154-在办-{i}", status="ongoing",
                        due_date="2099-01-01")
           for i in range(200)])

    resp = client.get(f"/api/projects?overdue_only=true&org_id={org_id}&today={TODAY}",
                      headers=admin)
    assert resp.status_code == 200, resp.text
    assert [p["name"] for p in resp.json()] == ["P154-逾期"]


def test_知识检索_第200条之后的在用条目不会被漏掉(client, admin, org_id):
    _seed([KnowledgeEntry(category="regulation", title="P154-在用", body="",
                          expire_date="2099-01-01", created_by=1)])
    _seed([KnowledgeEntry(category="regulation", title=f"P154-过期-{i}", body="",
                          expire_date="2020-01-01", created_by=1)
           for i in range(200)])

    resp = client.get(f"/api/knowledge?category=regulation&today={TODAY}", headers=admin)
    assert resp.status_code == 200, resp.text
    titles = [e["title"] for e in resp.json()]
    assert "P154-在用" in titles, "第 200 条之后的在用条目被漏掉了——检索结果静默少条"
    assert not any(t.startswith("P154-过期") for t in titles), (
        "过期条目默认不该返回：下推不能顺手放宽条件"
    )

    # include_expired=true 时不筛，过期的要回来（下推只发生在默认分支）
    resp = client.get(
        f"/api/knowledge?category=regulation&include_expired=true&today={TODAY}",
        headers=admin,
    )
    assert any(e["expired"] for e in resp.json())
