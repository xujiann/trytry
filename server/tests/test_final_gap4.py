"""终审轮批4：血液管理、档案调阅授权、直播申请审核、老年健康预警、HL7 ACK、登录限速。"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app
from app.routers.auth import IP_FAIL_LIMIT, _reset_login_failures


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
        json={"name": "终审协同医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    township = client.post(
        "/api/organizations",
        json={"name": "终审协同卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "终审协同患者", "id_card": "330281195001013003", "birth_date": "1950-01-01"},
        headers=admin,
    ).json()
    users = {}
    for username, role in [
        ("fg4_doc", "doctor"),
        ("fg4_dir", "director"),
        ("fg4_op", "operator"),
        ("fg4_ph", "public_health"),
    ]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
        users[role] = login(client, username, "pass123456")
    return {"org": org, "township": township, "patient": patient, **users}


def test_blood_management_flow(client, setup):
    op = setup["operator"]
    # 血库登记（累加）
    client.post("/api/blood/stocks", json={"blood_type": "A", "component": "rbc", "quantity_ml": 400}, headers=op)
    client.post("/api/blood/stocks", json={"blood_type": "A", "component": "rbc", "quantity_ml": 200}, headers=op)
    stocks = client.get("/api/blood/stocks", headers=op).json()
    assert stocks[0]["quantity_ml"] == 600
    # 用血申请限医师
    assert (
        client.post(
            "/api/blood/requests",
            json={
                "patient_id": setup["patient"]["id"],
                "org_id": setup["org"]["id"],
                "blood_type": "A",
                "component": "rbc",
                "quantity_ml": 400,
            },
            headers=op,
        ).status_code
        == 403
    )
    req = client.post(
        "/api/blood/requests",
        json={
            "patient_id": setup["patient"]["id"],
            "org_id": setup["org"]["id"],
            "blood_type": "A",
            "component": "rbc",
            "quantity_ml": 400,
            "reason": "术中备血",
        },
        headers=setup["doctor"],
    )
    assert req.status_code == 201
    rid = req.json()["id"]
    # 未审批不可发血
    assert client.post(f"/api/blood/requests/{rid}/issue", headers=op).status_code == 409
    assert (
        client.post(f"/api/blood/requests/{rid}/review?approve=true", headers=setup["director"]).status_code
        == 200
    )
    issued = client.post(f"/api/blood/requests/{rid}/issue", headers=op)
    assert issued.status_code == 200 and issued.json()["stock_remaining_ml"] == 200
    # 库存不足拦截
    req2 = client.post(
        "/api/blood/requests",
        json={
            "patient_id": setup["patient"]["id"],
            "org_id": setup["org"]["id"],
            "blood_type": "A",
            "component": "rbc",
            "quantity_ml": 300,
        },
        headers=setup["doctor"],
    ).json()
    client.post(f"/api/blood/requests/{req2['id']}/review?approve=true", headers=setup["director"])
    assert client.post(f"/api/blood/requests/{req2['id']}/issue", headers=op).status_code == 409


def test_archive_authorization_model(client, setup):
    doc = setup["doctor"]
    pid = setup["patient"]["id"]
    grant = client.post(
        f"/api/patients/{pid}/authorizations",
        json={"grantee_org_id": setup["township"]["id"], "scope": "encounter", "expire_date": "2026-12-31"},
        headers=doc,
    )
    assert grant.status_code == 201
    aid = grant.json()["id"]
    # 范围内允许、范围外拒绝
    check = client.get(
        f"/api/patients/{pid}/authorizations/check?org_id={setup['township']['id']}&scope=encounter&today=2026-08-11",
        headers=doc,
    ).json()
    assert check["allowed"] is True
    check_exam = client.get(
        f"/api/patients/{pid}/authorizations/check?org_id={setup['township']['id']}&scope=exam&today=2026-08-11",
        headers=doc,
    ).json()
    assert check_exam["allowed"] is False
    # 过期后失效
    expired = client.get(
        f"/api/patients/{pid}/authorizations/check?org_id={setup['township']['id']}&scope=encounter&today=2027-01-01",
        headers=doc,
    ).json()
    assert expired["allowed"] is False
    # 撤销后失效
    client.post(f"/api/patients/{pid}/authorizations/{aid}/revoke", headers=setup["operator"])
    revoked = client.get(
        f"/api/patients/{pid}/authorizations/check?org_id={setup['township']['id']}&scope=encounter&today=2026-08-11",
        headers=doc,
    ).json()
    assert revoked["allowed"] is False
    listed = client.get(f"/api/patients/{pid}/authorizations", headers=doc).json()
    assert len(listed) == 1 and listed[0]["status"] == "revoked"


def test_live_session_review_flow(client, setup):
    req = client.post(
        "/api/education/live-sessions",
        json={"title": "基层心衰规范化诊疗直播", "speaker": "王主任", "planned_at": "2026-09-01 19:00"},
        headers=setup["doctor"],
    )
    assert req.status_code == 201
    sid = req.json()["id"]
    # 审核限管理层
    assert (
        client.post(f"/api/education/live-sessions/{sid}/review?approve=true", headers=setup["doctor"]).status_code
        == 403
    )
    # 未审核不可结束
    assert (
        client.post(f"/api/education/live-sessions/{sid}/finish", headers=setup["operator"]).status_code
        == 409
    )
    approved = client.post(
        f"/api/education/live-sessions/{sid}/review?approve=true&comment=同意排期",
        headers=setup["director"],
    )
    assert approved.status_code == 200 and approved.json()["status"] == "approved"
    finished = client.post(f"/api/education/live-sessions/{sid}/finish", headers=setup["operator"])
    assert finished.json()["status"] == "finished"
    listed = client.get("/api/education/live-sessions?status=finished", headers=setup["doctor"]).json()
    assert len(listed) == 1


def test_eldercare_alerts(client, setup):
    doc = setup["doctor"]
    pid = setup["patient"]["id"]
    # 一年前的重度失能评估 → 两类预警
    client.post(
        "/api/eldercare/assessments",
        json={"patient_id": pid, "adl_score": 30, "assessed_date": "2025-06-01"},
        headers=doc,
    )
    alerts = client.get("/api/eldercare/alerts?today=2026-08-11", headers=doc).json()
    types = {a["alert_type"] for a in alerts["alerts"] if a["patient_id"] == pid}
    assert types == {"severe_disability", "reassess_due"}
    # 新评估（能力完好）后不再预警
    client.post(
        "/api/eldercare/assessments",
        json={"patient_id": pid, "adl_score": 100, "assessed_date": "2026-08-01"},
        headers=doc,
    )
    alerts2 = client.get("/api/eldercare/alerts?today=2026-08-11", headers=doc).json()
    assert not [a for a in alerts2["alerts"] if a["patient_id"] == pid]


def test_hl7_ack_response(client, setup):
    msg = (
        "MSH|^~\\&|HIS|TOWNSHIP|MEDPLAT|COUNTY|20260811||ADT^A04|MSG00042|P|2.4\n"
        "PID|1||330281199003035005||终审HL7患者||19900303|F|||||13800000000"
    )
    resp = client.post(
        "/api/integration/hl7v2/patient",
        json={"message": msg},
        headers=setup["operator"],
    )
    assert resp.status_code == 201, resp.text
    ack = resp.json()["ack"]
    # ACK 回执：MSA|AA|原消息控制ID
    assert "MSA|AA|MSG00042" in ack and ack.startswith("MSH|")


def test_login_ip_rate_limit(client):
    """浙#80 资源控制：同 IP 登录失败超阈值返回 429（跨用户名不可绕过）。"""
    _reset_login_failures()
    try:
        last_status = None
        for i in range(IP_FAIL_LIMIT):
            last_status = client.post(
                "/api/auth/login", json={"username": f"rl_u{i}", "password": "wrongpass1"}
            ).status_code
        assert last_status == 401  # 阈值内为正常失败
        over = client.post(
            "/api/auth/login", json={"username": "rl_over", "password": "wrongpass1"}
        )
        assert over.status_code == 429
    finally:
        _reset_login_failures()
