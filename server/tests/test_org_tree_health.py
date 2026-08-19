"""机构树体检接口（GET /api/organizations/tree-health）。

配套 ADR-0004：转诊分级审核按机构树 parent_id 逐级上收，非顶层机构缺 parent_id
会把非全域账号 403。本接口把这类缺陷一次列清，供运维在越权校验"咬人"前修好机构树。
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


def _admin(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _mkorg(client, admin, name, level, org_type, parent_id=None):
    body = {"name": name, "org_type": org_type, "level": level}
    if parent_id is not None:
        body["parent_id"] = parent_id
    r = client.post("/api/organizations", json=body, headers=admin)
    assert r.status_code == 201, r.text
    return r.json()


def test_tree_health_flags_orphans(client):
    admin = _admin(client)

    # 健康三级树：county(根) → township → village
    county = _mkorg(client, admin, "体检县医院", "county", "lead_hospital")
    town = _mkorg(client, admin, "体检卫生院", "township", "township", county["id"])
    _mkorg(client, admin, "体检村卫生室", "village", "village", town["id"])

    health = client.get("/api/organizations/tree-health", headers=admin).json()
    assert health["total"] == 3
    assert health["roots"] == 1                 # 只有顶层 county 是合法树根
    assert health["orphans"] == []
    assert health["referral_ready"] is True

    # 造一个孤儿：village 级却无 parent_id → 会卡转诊分级审核
    orphan = _mkorg(client, admin, "孤儿村卫生室", "village", "village")
    health = client.get("/api/organizations/tree-health", headers=admin).json()
    assert orphan["id"] in {o["id"] for o in health["orphans"]}
    assert health["referral_ready"] is False
    # roots 不因孤儿增加（顶层无父才算根，孤儿不双记）
    assert health["roots"] == 1
    # county 仍是根、不算孤儿（顶层 parent 为空是正常的）
    assert county["id"] not in {o["id"] for o in health["orphans"]}


def test_tree_health_requires_admin(client):
    admin = _admin(client)
    org = _mkorg(client, admin, "体检权限院", "township", "township")
    client.post(
        "/api/users",
        json={"username": "tree_doc", "password": "pass123456", "role": "doctor",
              "org_id": org["id"]},
        headers=admin,
    )
    doc = client.post(
        "/api/auth/login", json={"username": "tree_doc", "password": "pass123456"}
    ).json()
    doc = {"Authorization": f"Bearer {doc['access_token']}"}
    resp = client.get("/api/organizations/tree-health", headers=doc)
    assert resp.status_code == 403, resp.text
