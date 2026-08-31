"""敏感读留痕 `/api/access-logs` 三个端点的**特征化网 + 响应契约**。

套路同 test_rules_contract.py / test_admin_mgmt_contract.py：先钉住**当前**响应的
完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §11）。

本簇的建模判断：

- 监管清单与患者视角 `/mine` 是**同一个 `_row_out()` 形状**（11 键），一个模型
  两处复用；`viewer_org_id` 是**键恒在值可空**（居民端/无机构账号记 null）→
  `int | None`；`at` 是 isoformat **或空串**（created_at 缺省兜底）→ str。
- `stats` 的 `by_basis` 行固定三键，`total` 为 int；按 -count 排序。
- **查询本身也留痕**的语义一并钉住：按患者过滤的清单/统计会追加一条
  `access_log_view` 记录——这是行为语义不是契约噪声，逐值断言。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import SmsCode

ROW_KEYS = [
    "id", "viewer", "viewer_org_id", "viewer_org_name", "patient_id", "patient_name",
    "resource", "resource_name", "basis", "basis_name", "at",
]
STATS_KEYS = ["total", "by_basis"]
BASIS_KEYS = ["basis", "basis_name", "count"]


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def _login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def world(client):
    """医师经就诊依据、管理员经全域依据各调阅一次档案——两条真实留痕。"""
    admin = _login(client, "admin", "admin123")
    org = client.post(
        "/api/organizations",
        json={"name": "留痕契约医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    client.post(
        "/api/users",
        json={"username": "alc_doc", "password": "pass123456", "role": "doctor",
              "org_id": org["id"]},
        headers=admin,
    )
    doc = _login(client, "alc_doc", "pass123456")
    patient = client.post(
        "/api/patients",
        json={"name": "留痕契约患者", "id_card": "330424199002021234", "phone": "13700110001"},
        headers=admin,
    ).json()
    client.post(
        "/api/encounters",
        json={"patient_id": patient["id"], "org_id": org["id"], "encounter_type": "outpatient"},
        headers=doc,
    )
    assert client.get(f"/api/archive/{patient['ehc_no']}", headers=doc).status_code == 200
    assert client.get(f"/api/archive/{patient['ehc_no']}", headers=admin).status_code == 200
    return {"admin": admin, "doc": doc, "org": org, "patient": patient}


def test_监管清单精确形状与键序(client, world):
    resp = client.get("/api/access-logs", headers=world["admin"])
    rows = resp.json()
    assert resp.headers["x-total-count"] == "2"
    assert [list(r.keys()) for r in rows] == [ROW_KEYS] * 2
    assert all(isinstance(r["at"], str) and "T" in r["at"] for r in rows)
    assert rows == [
        {   # id 倒序：admin 的全域调阅在前
            "id": rows[0]["id"],
            "viewer": "admin",
            "viewer_org_id": None,   # 键恒在值可空：平台账号不挂机构
            "viewer_org_name": "",
            "patient_id": world["patient"]["id"],
            "patient_name": "留痕契约患者",
            "resource": "archive_360",
            "resource_name": "患者360全景",
            "basis": "global",
            "basis_name": "全域角色",
            "at": rows[0]["at"],
        },
        {
            "id": rows[1]["id"],
            "viewer": "alc_doc",
            "viewer_org_id": world["org"]["id"],
            "viewer_org_name": "留痕契约医院",
            "patient_id": world["patient"]["id"],
            "patient_name": "留痕契约患者",
            "resource": "archive_360",
            "resource_name": "患者360全景",
            "basis": "encounter",
            "basis_name": "本机构就诊",
            "at": rows[1]["at"],
        },
    ]
    assert client.get("/api/access-logs?username=nobody", headers=world["admin"]).json() == []


def test_按患者过滤本身也留痕(client, world):
    pid = world["patient"]["id"]
    filtered = client.get(f"/api/access-logs?patient_id={pid}", headers=world["admin"]).json()
    assert len(filtered) == 2  # 本次查询的留痕写在响应之后，不含自身
    after = client.get("/api/access-logs", headers=world["admin"]).json()
    assert len(after) == 3
    assert after[0] == {
        "id": after[0]["id"],
        "viewer": "admin",
        "viewer_org_id": None,
        "viewer_org_name": "",
        "patient_id": pid,
        "patient_name": "留痕契约患者",
        "resource": "access_log_view",
        "resource_name": "调阅记录查询",
        "basis": "global",
        "basis_name": "全域角色",
        "at": after[0]["at"],
    }


def test_患者视角mine精确_与监管行同形(client, world):
    with SessionLocal() as db:
        db.query(SmsCode).delete()
        db.commit()
    code = client.post(
        "/api/portal/auth/sms/code", json={"phone": "13700110001", "purpose": "login"}
    ).json()["debug_code"]
    token = client.post(
        "/api/portal/auth/sms/login", json={"phone": "13700110001", "code": code}
    ).json()["access_token"]
    me = {"Authorization": f"Bearer {token}"}
    resp = client.get("/api/access-logs/mine", headers=me)
    rows = resp.json()
    assert resp.headers["x-total-count"] == "3"
    assert [list(r.keys()) for r in rows] == [ROW_KEYS] * 3
    # 与监管清单同形同值（本人过滤后正是这三条）
    assert rows == client.get("/api/access-logs", headers=world["admin"]).json()
    # 业务令牌不得读 /mine（语义未动）
    assert client.get("/api/access-logs/mine", headers=world["admin"]).status_code in (401, 403)


def test_统计精确_聚焦患者时自我留痕(client, world):
    body = client.get("/api/access-logs/stats", headers=world["admin"]).json()
    assert list(body.keys()) == STATS_KEYS
    assert [list(b.keys()) for b in body["by_basis"]] == [BASIS_KEYS] * 2
    assert body == {
        "total": 3,
        "by_basis": [
            {"basis": "global", "basis_name": "全域角色", "count": 2},
            {"basis": "encounter", "basis_name": "本机构就诊", "count": 1},
        ],
    }
    pid = world["patient"]["id"]
    focused = client.get(f"/api/access-logs/stats?patient_id={pid}", headers=world["admin"]).json()
    # 聚焦即调阅：先自我留痕再聚合，本次查询自身计入 global
    assert focused == {
        "total": 4,
        "by_basis": [
            {"basis": "global", "basis_name": "全域角色", "count": 3},
            {"basis": "encounter", "basis_name": "本机构就诊", "count": 1},
        ],
    }
    assert client.get(
        "/api/access-logs/stats?patient_id=999999", headers=world["admin"]
    ).status_code == 404
