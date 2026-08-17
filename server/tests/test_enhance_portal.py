"""E8 居民端可办：居家自报、报告查看、在线咨询、账单自助支付、续方申请+审核。"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def _login(client, u, p):
    tok = client.post("/api/auth/login", json={"username": u, "password": p}).json()
    return {"Authorization": f"Bearer {tok['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return _login(client, "admin", "admin123")


def _send_code(client, phone):
    from app.database import SessionLocal
    from app.models import SmsCode
    with SessionLocal() as db:
        db.query(SmsCode).filter(SmsCode.phone == phone).delete()
        db.commit()
    return client.post("/api/portal/auth/sms/code", json={"phone": phone, "purpose": "login"}).json()["debug_code"]


def _resident(client, phone, name, id_card):
    code = _send_code(client, phone)
    body = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code}).json()
    h = {"Authorization": f"Bearer {body['access_token']}"}
    client.post("/api/portal/auth/realname", json={"name": name, "id_card": id_card}, headers=h)
    return h


@pytest.fixture(scope="module")
def setup(client, admin):
    org = client.post("/api/organizations", json={"name": "居民端卫生院", "org_type": "township", "level": "township"}, headers=admin).json()
    org2 = client.post("/api/organizations", json={"name": "居民端他院", "org_type": "township", "level": "township"}, headers=admin).json()
    client.post("/api/users", json={"username": "e8_doc", "password": "pw123456", "full_name": "续方医生", "role": "doctor", "org_id": org["id"]}, headers=admin)
    client.post("/api/users", json={"username": "e8_doc2", "password": "pw123456", "full_name": "他院医生", "role": "doctor", "org_id": org2["id"]}, headers=admin)
    p1 = client.post("/api/patients", json={"name": "居民甲", "id_card": "330782199001011234", "phone": "13800990011"}, headers=admin).json()
    p2 = client.post("/api/patients", json={"name": "居民乙", "id_card": "330782199202022345", "phone": "13800990022"}, headers=admin).json()
    r1 = _resident(client, "13800990011", "居民甲", "330782199001011234")
    r2 = _resident(client, "13800990022", "居民乙", "330782199202022345")
    return {"org": org, "org2": org2, "p1": p1, "p2": p2, "r1": r1, "r2": r2,
            "doc": _login(client, "e8_doc", "pw123456"), "doc2": _login(client, "e8_doc2", "pw123456")}


def test_home_readings(client, setup):
    r = client.post("/api/portal/me/home-readings", json={"reading_type": "bp", "value1": 150, "value2": 95, "measured_at": "2026-08-17"}, headers=setup["r1"])
    assert r.status_code == 201, r.text
    assert r.json()["abnormal"] is True
    # 血压缺舒张压 422
    assert client.post("/api/portal/me/home-readings", json={"reading_type": "bp", "value1": 130}, headers=setup["r1"]).status_code == 422
    lst = client.get("/api/portal/me/home-readings", headers=setup["r1"]).json()
    assert len(lst) == 1


def test_online_consult(client, setup):
    r = client.post("/api/portal/me/online-consults", json={"org_id": setup["org"]["id"], "question": "血压偏高怎么办"}, headers=setup["r1"])
    assert r.status_code == 201, r.text
    assert client.get("/api/portal/me/online-consults", headers=setup["r1"]).json()[0]["status"] == "open"


def test_exam_report_view(client, setup):
    from app.database import SessionLocal
    from app.models import ExamRequest, ExamReport
    with SessionLocal() as db:
        req = ExamRequest(patient_id=setup["p1"]["id"], from_org_id=setup["org"]["id"], center_type="lab", item_code="X01", item_name="血常规", status="reported", created_by=1)
        db.add(req); db.flush()
        db.add(ExamReport(request_id=req.id, conclusion="未见异常", critical=False, reported_by="李医生"))
        db.commit()
    reports = client.get("/api/portal/me/exam-reports", headers=setup["r1"]).json()
    assert any(x["item_name"] == "血常规" for x in reports)


def test_bill_self_pay(client, setup):
    from app.database import SessionLocal
    from app.models import Settlement
    with SessionLocal() as db:
        s = Settlement(patient_id=setup["p1"]["id"], org_id=setup["org"]["id"], bill_type="outpatient", total_amount=100, insurance_pay=0, self_pay=100, created_by=1)
        db.add(s); db.commit(); sid = s.id
    r = client.post(f"/api/portal/me/bills/{sid}/pay", json={"channel": "online"}, headers=setup["r1"])
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "paid"
    # 重复支付无余额 409
    assert client.post(f"/api/portal/me/bills/{sid}/pay", json={"channel": "online"}, headers=setup["r1"]).status_code == 409
    # 他人不能支付本单 403
    assert client.post(f"/api/portal/me/bills/{sid}/pay", json={"channel": "online"}, headers=setup["r2"]).status_code == 403


def test_self_pay_capped_at_self_pay_not_total(client, setup):
    """封顶必须是 self_pay：total>self_pay 时不能重复自付把医保段也付了。"""
    from app.database import SessionLocal
    from app.models import Settlement
    with SessionLocal() as db:
        s = Settlement(patient_id=setup["p1"]["id"], org_id=setup["org"]["id"], bill_type="outpatient",
                       total_amount=1000, insurance_pay=800, self_pay=200, created_by=1)
        db.add(s); db.commit(); sid = s.id
    # 首次自付 200 成功
    r1 = client.post(f"/api/portal/me/bills/{sid}/pay", json={"channel": "online"}, headers=setup["r1"])
    assert r1.status_code == 201 and r1.json()["amount"] == 200
    # 再付即 409（自付段已付满），旧逻辑会一路放行到 total=1000
    assert client.post(f"/api/portal/me/bills/{sid}/pay", json={"channel": "online"}, headers=setup["r1"]).status_code == 409


def test_refill_flow(client, setup):
    r = client.post("/api/portal/me/refill-requests", json={"org_id": setup["org"]["id"], "drug_summary": "氨氯地平 5mg×30"}, headers=setup["r1"])
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    # 他院医生审核 → 403（跨机构）
    assert client.patch(f"/api/refill-requests/{rid}/review", json={"decision": "approved"}, headers=setup["doc2"]).status_code == 403
    # 本院医生审核通过
    assert client.patch(f"/api/refill-requests/{rid}/review", json={"decision": "approved"}, headers=setup["doc"]).status_code == 200
    # 已审再审 409
    assert client.patch(f"/api/refill-requests/{rid}/review", json={"decision": "rejected"}, headers=setup["doc"]).status_code == 409
    # 配送（药师/经办）——用 doc 无 pharmacist 角色会 403，改用 admin 全通
    admin = _login(client, "admin", "admin123")
    assert client.patch(f"/api/refill-requests/{rid}/dispense", headers=admin).status_code == 200
    mine = client.get("/api/portal/me/refill-requests", headers=setup["r1"]).json()
    assert mine[0]["status"] == "dispensed"
