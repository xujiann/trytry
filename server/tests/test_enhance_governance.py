"""S16 医共体治理架构：章程 / 理事会·管委会 / 编制周转池（县管乡用）。

治理是联合体级的管理职能，几条用例守的是：章程同版本不得重录、现行章程全局
唯一（激活新的自动熄灭旧的）、治理成员与编制调配的引用完整性（机构/机构不存在
即 404，同机构调配 422），以及写入只对全域管理角色开放（非全域 operator 403）。
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
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def orgs(client, admin):
    return [
        client.post(
            "/api/organizations",
            json={"name": name, "org_type": org_type, "level": level},
            headers=admin,
        ).json()
        for name, org_type, level in [
            ("治理县医院", "lead_hospital", "county"),
            ("治理东镇卫生院", "township", "township"),
        ]
    ]


@pytest.fixture(scope="module")
def operator(client, admin, orgs):
    client.post(
        "/api/users",
        json={"username": "gov_op", "password": "passw0rd1", "role": "operator",
              "org_id": orgs[0]["id"]},
        headers=admin,
    )
    resp = client.post("/api/auth/login", json={"username": "gov_op", "password": "passw0rd1"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# ---------------- 章程 ----------------


def test_章程创建与同版本查重(client, admin):
    resp = client.post(
        "/api/governance/charters",
        json={"title": "医共体章程", "version": "v1", "content": "第一条…",
              "effective_date": "2026-01-01"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["active"] is False
    dup = client.post(
        "/api/governance/charters",
        json={"title": "医共体章程", "version": "v1"},
        headers=admin,
    )
    assert dup.status_code == 409


def test_现行章程全局唯一_激活新版熄灭旧版(client, admin):
    v2 = client.post(
        "/api/governance/charters",
        json={"title": "医共体章程", "version": "v2"},
        headers=admin,
    ).json()
    v3 = client.post(
        "/api/governance/charters",
        json={"title": "医共体章程", "version": "v3"},
        headers=admin,
    ).json()
    # 先激活 v2
    r2 = client.patch(f"/api/governance/charters/{v2['id']}/activate", headers=admin)
    assert r2.status_code == 200 and r2.json()["active"] is True
    active = client.get("/api/governance/charters?active=true", headers=admin).json()
    assert [c["id"] for c in active] == [v2["id"]]
    # 再激活 v3——v2 应自动熄灭，现行有且仅一份
    r3 = client.patch(f"/api/governance/charters/{v3['id']}/activate", headers=admin)
    assert r3.status_code == 200 and r3.json()["active"] is True
    active = client.get("/api/governance/charters?active=true", headers=admin).json()
    assert len(active) == 1 and active[0]["id"] == v3["id"]


def test_激活不存在的章程_404(client, admin):
    resp = client.patch("/api/governance/charters/999999/activate", headers=admin)
    assert resp.status_code == 404


# ---------------- 治理机构与成员 ----------------


def test_治理机构与成员_机构缺失404(client, admin, orgs):
    body = client.post(
        "/api/governance/bodies",
        json={"name": "医共体理事会", "body_type": "council"},
        headers=admin,
    )
    assert body.status_code == 201, body.text
    body_id = body.json()["id"]
    # 正常加入一名代表 orgs[0] 的理事长
    m = client.post(
        f"/api/governance/bodies/{body_id}/members",
        json={"member_name": "张三", "title": "理事长", "org_id": orgs[0]["id"]},
        headers=admin,
    )
    assert m.status_code == 201, m.text
    members = client.get(f"/api/governance/bodies/{body_id}/members", headers=admin).json()
    assert [x["member_name"] for x in members] == ["张三"]
    # 机构不存在 → 404
    bad_org = client.post(
        f"/api/governance/bodies/{body_id}/members",
        json={"member_name": "李四", "org_id": 999999},
        headers=admin,
    )
    assert bad_org.status_code == 404
    # 治理机构不存在 → 404
    bad_body = client.post(
        "/api/governance/bodies/999999/members",
        json={"member_name": "王五", "org_id": orgs[0]["id"]},
        headers=admin,
    )
    assert bad_body.status_code == 404


# ---------------- 编制周转池·县管乡用 ----------------


def test_编制周转_创建列表与结束(client, admin, orgs):
    resp = client.post(
        "/api/governance/staff-pool",
        json={"member_name": "赵医生", "from_org_id": orgs[0]["id"],
              "to_org_id": orgs[1]["id"], "assign_type": "county_manage_town_use",
              "start_date": "2026-03-01"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    pool_id = resp.json()["id"]
    assert resp.json()["status"] == "active"
    # 列表按调入机构过滤
    rows = client.get(
        f"/api/governance/staff-pool?to_org_id={orgs[1]['id']}", headers=admin
    ).json()
    assert pool_id in [r["id"] for r in rows]
    # 结束调配
    ended = client.patch(f"/api/governance/staff-pool/{pool_id}/end", headers=admin)
    assert ended.status_code == 200 and ended.json()["status"] == "ended"
    # 按状态过滤
    active_rows = client.get("/api/governance/staff-pool?status=active", headers=admin).json()
    assert pool_id not in [r["id"] for r in active_rows]


def test_同机构调配被拒_422(client, admin, orgs):
    resp = client.post(
        "/api/governance/staff-pool",
        json={"from_org_id": orgs[0]["id"], "to_org_id": orgs[0]["id"],
              "assign_type": "rotation"},
        headers=admin,
    )
    assert resp.status_code == 422


def test_调配机构缺失_404(client, admin, orgs):
    resp = client.post(
        "/api/governance/staff-pool",
        json={"from_org_id": orgs[0]["id"], "to_org_id": 999999,
              "assign_type": "support"},
        headers=admin,
    )
    assert resp.status_code == 404


# ---------------- 隔离：非全域角色不得写治理数据 ----------------


def test_非全域角色写治理数据_403(client, operator, orgs):
    # 章程（admin 档）
    r1 = client.post(
        "/api/governance/charters",
        json={"title": "越权章程", "version": "v1"},
        headers=operator,
    )
    assert r1.status_code == 403
    # 治理机构（director 档）
    r2 = client.post(
        "/api/governance/bodies",
        json={"name": "越权理事会", "body_type": "council"},
        headers=operator,
    )
    assert r2.status_code == 403
    # 编制周转（director 档）
    r3 = client.post(
        "/api/governance/staff-pool",
        json={"from_org_id": orgs[0]["id"], "to_org_id": orgs[1]["id"],
              "assign_type": "rotation"},
        headers=operator,
    )
    assert r3.status_code == 403
