"""E4 公共卫生短板补全：精神障碍/结核/卫生监督/公卫项目看板 + 横向隔离。

守四条：
- 高敏患者关联数据（精神障碍/结核）跨机构写被拦（cross-org 403）；
- 缺失患者/项目返回 404；
- 公卫项目目录建项去重（409）；
- 完成度看板按机构可见范围聚合，覆盖率防除零。
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
            ("公卫甲县医院", "lead_hospital", "county"),
            ("公卫乙镇卫生院", "township", "township"),
        ]
    ]


def _mkuser(client, admin, username, role, org_id):
    client.post(
        "/api/users",
        json={"username": username, "password": "passw0rd1", "full_name": username,
              "role": role, "org_id": org_id},
        headers=admin,
    )
    resp = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def doctor_a(client, admin, orgs):
    return _mkuser(client, admin, "ph_doc_a", "doctor", orgs[0]["id"])


@pytest.fixture(scope="module")
def doctor_b(client, admin, orgs):
    return _mkuser(client, admin, "ph_doc_b", "doctor", orgs[1]["id"])


@pytest.fixture(scope="module")
def ph_a(client, admin, orgs):
    return _mkuser(client, admin, "ph_ph_a", "public_health", orgs[0]["id"])


@pytest.fixture(scope="module")
def operator_b(client, admin, orgs):
    return _mkuser(client, admin, "ph_op_b", "operator", orgs[1]["id"])


@pytest.fixture(scope="module")
def patient(client, admin):
    return client.post(
        "/api/patients", json={"name": "公卫患者", "id_card": "331782199003031234"},
        headers=admin,
    ).json()


# ---------------- 严重精神障碍 ----------------


@pytest.fixture(scope="module")
def psych(client, doctor_a, orgs, patient):
    resp = client.post(
        "/api/psych/patients",
        json={"patient_id": patient["id"], "org_id": orgs[0]["id"],
              "diagnosis": "精神分裂症", "danger_level": "2", "guardian": "家属"},
        headers=doctor_a,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_精神障碍建档与随访(client, doctor_a, psych):
    assert psych["danger_level"] == "2" and psych["status"] == "in_manage"
    fu = client.post(
        f"/api/psych/patients/{psych['id']}/followups",
        json={"followup_date": "2026-08-01", "medication_adherence": "good",
              "stability": "stable"},
        headers=doctor_a,
    )
    assert fu.status_code == 201, fu.text
    # 随访 org_id 由父档案带出
    assert fu.json()["org_id"] == psych["org_id"]
    rows = client.get(f"/api/psych/patients/{psych['id']}/followups", headers=doctor_a).json()
    assert len(rows) == 1 and rows[0]["medication_adherence"] == "good"


def test_精神障碍按患者可见性列出(client, doctor_a, psych, patient):
    rows = client.get(f"/api/psych/patients?patient_id={patient['id']}", headers=doctor_a).json()
    assert any(r["id"] == psych["id"] for r in rows)


def test_精神障碍缺患者404(client, doctor_a, orgs):
    resp = client.post(
        "/api/psych/patients",
        json={"patient_id": 999999, "org_id": orgs[0]["id"], "diagnosis": "x"},
        headers=doctor_a,
    )
    assert resp.status_code == 404


def test_跨机构写他院精神障碍随访被拒(client, doctor_b, psych):
    """乙镇医师（非全域）不得给甲县医院管理的精神障碍患者写随访。"""
    resp = client.post(
        f"/api/psych/patients/{psych['id']}/followups",
        json={"followup_date": "2026-08-02", "medication_adherence": "none"},
        headers=doctor_b,
    )
    assert resp.status_code == 403


def test_跨机构读他院精神障碍随访被拒(client, doctor_b, psych):
    resp = client.get(f"/api/psych/patients/{psych['id']}/followups", headers=doctor_b)
    assert resp.status_code == 403


# ---------------- 结核病 ----------------


@pytest.fixture(scope="module")
def tb(client, doctor_a, orgs, patient):
    resp = client.post(
        "/api/tb/patients",
        json={"patient_id": patient["id"], "org_id": orgs[0]["id"],
              "tb_type": "pulmonary", "regimen": "2HRZE/4HR"},
        headers=doctor_a,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_结核督导服药与密切接触者(client, doctor_a, tb):
    fu = client.post(
        f"/api/tb/patients/{tb['id']}/followups",
        json={"followup_date": "2026-08-05", "taken": True, "sputum_result": "negative"},
        headers=doctor_a,
    )
    assert fu.status_code == 201 and fu.json()["taken"] is True
    ct = client.post(
        f"/api/tb/patients/{tb['id']}/contacts",
        json={"name": "同住家属", "relation": "spouse", "screened": True, "result": "阴性"},
        headers=doctor_a,
    )
    assert ct.status_code == 201
    assert len(client.get(f"/api/tb/patients/{tb['id']}/followups", headers=doctor_a).json()) == 1
    assert len(client.get(f"/api/tb/patients/{tb['id']}/contacts", headers=doctor_a).json()) == 1


def test_跨机构写他院结核督导被拒(client, operator_b, doctor_b, tb):
    resp = client.post(
        f"/api/tb/patients/{tb['id']}/followups",
        json={"followup_date": "2026-08-06", "taken": False},
        headers=doctor_b,
    )
    assert resp.status_code == 403


# ---------------- 卫生监督协管 ----------------


def test_卫生监督巡查(client, ph_a, orgs):
    resp = client.post(
        "/api/health-supervision",
        json={"org_id": orgs[0]["id"], "domain": "food", "target": "某小学食堂",
              "inspect_date": "2026-08-10", "findings": "留样合规", "passed": True},
        headers=ph_a,
    )
    assert resp.status_code == 201, resp.text
    rows = client.get(f"/api/health-supervision?org_id={orgs[0]['id']}&domain=food", headers=ph_a).json()
    assert any(r["target"] == "某小学食堂" for r in rows)


def test_卫生监督未知领域422(client, ph_a, orgs):
    resp = client.post(
        "/api/health-supervision",
        json={"org_id": orgs[0]["id"], "domain": "unknown", "target": "x"},
        headers=ph_a,
    )
    assert resp.status_code == 422


def test_卫生监督跨机构写被拒(client, operator_b, orgs):
    resp = client.post(
        "/api/health-supervision",
        json={"org_id": orgs[0]["id"], "domain": "water", "target": "甲县水厂"},
        headers=operator_b,
    )
    assert resp.status_code == 403


# ---------------- 公卫 12 类项目 ----------------


@pytest.fixture(scope="module")
def project(client, admin):
    resp = client.post(
        "/api/pubhealth-projects",
        json={"code": "PH01", "name": "居民健康档案管理", "category": "档案"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_项目目录建项去重(client, admin, project):
    dup = client.post(
        "/api/pubhealth-projects",
        json={"code": "PH01", "name": "重复项"},
        headers=admin,
    )
    assert dup.status_code == 409


def test_项目建项限管理员(client, ph_a):
    resp = client.post(
        "/api/pubhealth-projects",
        json={"code": "PH99", "name": "越权建项"},
        headers=ph_a,
    )
    assert resp.status_code == 403


def test_完成度登记与看板(client, ph_a, admin, project, orgs):
    up = client.post(
        "/api/pubhealth-projects/stats",
        json={"project_id": project["id"], "org_id": orgs[0]["id"], "year": "2026",
              "target_count": 100, "done_count": 40},
        headers=ph_a,
    )
    assert up.status_code == 201, up.text
    # 幂等 upsert：再登记覆盖而非累加
    up2 = client.post(
        "/api/pubhealth-projects/stats",
        json={"project_id": project["id"], "org_id": orgs[0]["id"], "year": "2026",
              "target_count": 100, "done_count": 55},
        headers=ph_a,
    )
    assert up2.status_code == 201 and up2.json()["done_count"] == 55

    dash = client.get("/api/pubhealth-projects/dashboard?year=2026", headers=admin).json()
    row = next(r for r in dash["projects"] if r["project_id"] == project["id"])
    assert row["target_count"] == 100 and row["done_count"] == 55
    assert row["coverage_pct"] == 55.0


def test_完成度登记项目缺失404(client, ph_a, orgs):
    resp = client.post(
        "/api/pubhealth-projects/stats",
        json={"project_id": 999999, "org_id": orgs[0]["id"], "year": "2026",
              "target_count": 1, "done_count": 0},
        headers=ph_a,
    )
    assert resp.status_code == 404


def test_看板覆盖率防除零(client, admin):
    """新建一个无任何完成度登记的项目，看板覆盖率应为 0 而非报错。"""
    p = client.post(
        "/api/pubhealth-projects",
        json={"code": "PH02", "name": "无登记项目"},
        headers=admin,
    ).json()
    dash = client.get("/api/pubhealth-projects/dashboard?year=2026", headers=admin).json()
    row = next(r for r in dash["projects"] if r["project_id"] == p["id"])
    assert row["target_count"] == 0 and row["coverage_pct"] == 0
