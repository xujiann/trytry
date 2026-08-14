import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def setup(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "县人民医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    township = client.post(
        "/api/organizations",
        json={"name": "东镇卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "测试患者", "id_card": "320981199003031234"},
        headers=admin,
    ).json()
    for username, role in [("dr_wang", "doctor"), ("ph_li", "pharmacist"), ("op_zhao", "operator"), ("dir_qian", "director")]:
        resp = client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
        assert resp.status_code == 201
    return {
        "org": org,
        "township": township,
        "patient": patient,
        "doctor": login(client, "dr_wang", "pass123456"),
        "pharmacist": login(client, "ph_li", "pass123456"),
        "operator": login(client, "op_zhao", "pass123456"),
        "director": login(client, "dir_qian", "pass123456"),
    }


def test_user_management_requires_admin(client, setup):
    denied = client.post(
        "/api/users",
        json={"username": "hacker", "password": "pass123456", "role": "admin"},
        headers=setup["operator"],
    )
    assert denied.status_code == 403
    assert client.get("/api/users", headers=setup["doctor"]).status_code == 403


def test_exam_diagnosis_requires_doctor(client, admin, setup):
    request = client.post(
        "/api/exams",
        json={
            "patient_id": setup["patient"]["id"],
            "from_org_id": setup["township"]["id"],
            "center_type": "imaging",
            "item_code": "CT-HEAD",
            "item_name": "头颅CT",
        },
        headers=setup["operator"],
    ).json()

    # 经办人员不能领取诊断任务
    assert client.post(f"/api/exams/{request['id']}/claim", headers=setup["operator"]).status_code == 403
    # 药师也不行
    assert client.post(f"/api/exams/{request['id']}/claim", headers=setup["pharmacist"]).status_code == 403
    # 医师可以
    claimed = client.post(f"/api/exams/{request['id']}/claim", headers=setup["doctor"])
    assert claimed.status_code == 200
    report = client.post(
        f"/api/exams/{request['id']}/report",
        json={"conclusion": "未见明显异常"},
        headers=setup["doctor"],
    )
    assert report.status_code == 201


def test_prescription_review_requires_pharmacist(client, admin, setup):
    client.post(
        "/api/prescriptions/rules",
        json={"drug_code": "ASPIRIN", "max_daily_dose": 300, "dose_unit": "mg"},
        headers=admin,
    )
    over = client.post(
        "/api/prescriptions",
        json={
            "patient_id": setup["patient"]["id"],
            "org_id": setup["township"]["id"],
            "diagnosis_name": "冠心病",
            "items": [{"drug_code": "ASPIRIN", "drug_name": "阿司匹林", "daily_dose": 600, "days": 7}],
        },
        headers=setup["doctor"],
    ).json()
    assert over["status"] == "pending_review"

    # 医师不能自审处方
    denied = client.post(
        f"/api/prescriptions/{over['id']}/review",
        json={"approve": True},
        headers=setup["doctor"],
    )
    assert denied.status_code == 403
    approved = client.post(
        f"/api/prescriptions/{over['id']}/review",
        json={"approve": True, "comment": "短期使用可接受"},
        headers=setup["pharmacist"],
    )
    assert approved.status_code == 200


def test_performance_requires_director(client, admin, setup):
    assert client.get("/api/performance/orgs", headers=setup["doctor"]).status_code == 403
    assert client.get("/api/performance/orgs", headers=setup["director"]).status_code == 200
    assert client.get("/api/performance/orgs", headers=admin).status_code == 200


def test_change_password_flow(client, setup):
    wrong = client.post(
        "/api/auth/change-password",
        json={"current_password": "bad", "new_password": "newpass123"},
        headers=setup["operator"],
    )
    assert wrong.status_code == 400
    ok = client.post(
        "/api/auth/change-password",
        json={"current_password": "pass123456", "new_password": "newpass123"},
        headers=setup["operator"],
    )
    assert ok.status_code == 200
    # 旧密码失效，新密码可登录
    assert client.post("/api/auth/login", json={"username": "op_zhao", "password": "pass123456"}).status_code == 401
    login(client, "op_zhao", "newpass123")


def test_audit_log_records_mutations(client, admin, setup):
    denied = client.get("/api/audit", headers=setup["doctor"])
    assert denied.status_code == 403

    logs = client.get("/api/audit?limit=200", headers=admin).json()
    assert logs, "审计日志不应为空"
    paths = {(log["username"], log["method"], log["path"]) for log in logs}
    # 医师出报告、药师审方、管理员建用户均有留痕
    assert any(u == "dr_wang" and p.endswith("/report") for u, m, p in paths)
    assert any(u == "ph_li" and p.endswith("/review") for u, m, p in paths)
    assert any(u == "admin" and p == "/api/users" for u, m, p in paths)
    # 登录不落审计
    assert not any(p == "/api/auth/login" for _, _, p in paths)
    # 按用户过滤
    only_doctor = client.get("/api/audit?username=dr_wang", headers=admin).json()
    assert only_doctor and all(log["username"] == "dr_wang" for log in only_doctor)
