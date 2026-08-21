"""特征化网 + 契约：ADR-0006 搬家后补上的 8 个端点。

`gapfill.py` 被拆掉后，`cssd` 的 3 个 `/cost-*` 与 `performance` 的 5 个
`/improvements*` 落进了两个**已治理**模块，而它们本来就没有 `response_model`——
契约棘轮立刻报"回退"。这里把契约补齐，两个模块回归 `FULLY_GOVERNED`，
基线 740 → 732。

按 `docs/接口标准与治理.md` 的配方：先钉住现状、再加契约，加完网照样绿
即证明响应字节没变（CLAUDE.md §11）。

**这一批最容易踩的坑是 `total_cost` 的类型**：它是 `round(totals.get(id, 0), 2)`，
没有成本项时 `round(0, 2)` 返回的是 **int `0`** 而不是 `0.0`。声明成 `float`
会把 `0` 变成 `0.0`——那是改响应字节，不是治理。故契约写 `int | float`。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "契约消毒院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def batch(client, admin, org):
    r = client.post(
        "/api/cssd/batches",
        json={"org_id": org["id"], "center_org_id": org["id"],
              "batch_no": "CT-1", "item_name": "器械包", "quantity": 10},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- cssd 成本


def test_无成本项时total_cost是int零而不是浮点零(client, admin, batch):
    """这条是本文件的核心：`round(0, 2)` 返回 int。

    契约若写 `float`，Pydantic 会把 `0` 序列化成 `0.0`——响应字节就变了。
    写 `int | float` 让它原样透传。
    """
    stats = client.get("/api/cssd/cost-stats", headers=admin).json()
    assert stats["total_cost"] == 0
    assert isinstance(stats["total_cost"], int) and not isinstance(stats["total_cost"], bool)
    row = next(b for b in stats["batches"] if b["batch_id"] == batch["id"])
    assert isinstance(row["total_cost"], int), "逐批次的 total_cost 同理"
    # 单件成本是除法（或字面量 0.0），两条分支都是 float
    assert isinstance(row["unit_cost"], float)
    assert isinstance(stats["overall_unit_cost"], float)


def test_成本项字段集合(client, admin, batch):
    r = client.post(
        "/api/cssd/cost-items",
        json={"batch_id": batch["id"], "cost_type": "labor", "amount": 123.45},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    item = r.json()
    assert set(item) == {"id", "batch_id", "cost_type", "cost_type_name", "amount", "note"}
    assert item["cost_type_name"] == "人工", "出参要带中文名，不是只回显编码"
    assert isinstance(item["amount"], float)

    listed = client.get("/api/cssd/cost-items", headers=admin).json()
    assert set(listed[0]) == set(item)


def test_有成本项后total_cost转为float且构成明细出现(client, admin, batch):
    stats = client.get("/api/cssd/cost-stats", headers=admin).json()
    assert set(stats) == {
        "batches", "total_cost", "total_quantity", "overall_unit_cost", "by_cost_type",
    }
    assert stats["total_cost"] == 123.45
    assert isinstance(stats["total_cost"], float), "有数据时是 float——与空集时的 int 并存"
    assert stats["total_quantity"] == 10
    assert stats["overall_unit_cost"] == 12.35, "123.45 / 10 保留两位"
    assert stats["by_cost_type"] == {"labor": {"amount": 123.45, "name": "人工"}}
    assert set(stats["batches"][0]) == {
        "batch_id", "batch_no", "item_name", "quantity", "total_cost", "unit_cost",
    }


# ---------------------------------------------------------------- 整改任务


TASK_FIELDS = {
    "id", "org_id", "indicator_key", "problem", "measures", "owner_name", "due_date",
    "status", "status_name", "overdue", "completion_note", "completed_at",
    "verify_comment", "verified_by",
}


@pytest.fixture(scope="module")
def task(client, admin, org):
    r = client.post(
        "/api/performance/improvements",
        json={"org_id": org["id"], "problem": "随访率偏低",
              "owner_name": "张三", "due_date": "2030-01-01"},
        headers=admin,
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_建任务字段集合与派生字段(client, admin, task):
    assert set(task) == TASK_FIELDS
    assert task["status_name"] == "待整改"
    assert task["overdue"] is False, "due_date 在未来，overdue 是现算的派生字段"
    assert task["completed_at"] is None, "未提交完成时为 null，不是空串"


def test_列表与详情同一形状(client, admin, task):
    listed = client.get("/api/performance/improvements", headers=admin).json()
    assert set(listed[0]) == TASK_FIELDS


def test_推进与确认三段流转都回同一形状(client, admin, task):
    tid = task["id"]
    prog = client.post(
        f"/api/performance/improvements/{tid}/progress",
        json={"measures": "加派人手"}, headers=admin,
    ).json()
    assert set(prog) == TASK_FIELDS and prog["status_name"] == "整改中"

    done = client.post(
        f"/api/performance/improvements/{tid}/progress",
        json={"complete": True, "completion_note": "已完成"}, headers=admin,
    ).json()
    assert done["status_name"] == "已完成待确认"
    assert isinstance(done["completed_at"], str), "提交完成后才有时间戳"

    ok = client.post(
        f"/api/performance/improvements/{tid}/verify",
        json={"approve": True, "comment": "通过"}, headers=admin,
    ).json()
    assert set(ok) == TASK_FIELDS
    assert ok["status_name"] == "已确认关闭" and ok["verified_by"]


def test_汇总字段集合与闭环率类型(client, admin, task):
    stats = client.get("/api/performance/improvement-stats", headers=admin).json()
    assert set(stats) == {"total", "by_status", "overdue", "closed_rate_pct"}
    assert isinstance(stats["closed_rate_pct"], float)
    # by_status 只列出现过的状态——没有 open 的机构不该凭空多一个 open: 0
    assert set(stats["by_status"]) == {"verified"}
    assert stats["by_status"]["verified"] == {"count": 1, "name": "已确认关闭"}
