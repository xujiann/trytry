"""E3 双向转诊闭环深化：病历随转、下转康复计划/回复单、转诊号源预留。"""
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


@pytest.fixture(scope="module")
def setup(client, admin):
    up = client.post("/api/organizations", json={"name": "转诊县医院", "org_type": "lead_hospital", "level": "county"}, headers=admin).json()
    down = client.post("/api/organizations", json={"name": "转诊卫生院", "org_type": "township", "level": "township"}, headers=admin).json()
    other = client.post("/api/organizations", json={"name": "转诊无关院", "org_type": "township", "level": "township"}, headers=admin).json()
    client.post("/api/users", json={"username": "e3_doc_up", "password": "pw123456", "full_name": "上级医生", "role": "doctor", "org_id": up["id"]}, headers=admin)
    client.post("/api/users", json={"username": "e3_doc_down", "password": "pw123456", "full_name": "基层医生", "role": "doctor", "org_id": down["id"]}, headers=admin)
    client.post("/api/users", json={"username": "e3_doc_other", "password": "pw123456", "full_name": "无关医生", "role": "doctor", "org_id": other["id"]}, headers=admin)
    pat = client.post("/api/patients", json={"name": "转诊居民", "id_card": "320000198801011234"}, headers=admin).json()
    # 上转：基层→县医院
    ref = client.post("/api/referrals", json={"patient_id": pat["id"], "from_org_id": down["id"], "to_org_id": up["id"], "direction": "up", "reason": "需上级诊治"}, headers=_login(client, "e3_doc_down", "pw123456")).json()
    # 县医院开一个门诊号源
    slot = client.post("/api/appointments/slots", json={"org_id": up["id"], "resource_type": "outpatient", "resource_name": "专家门诊", "slot_date": "2026-09-01", "capacity": 2}, headers=admin).json()
    return {"up": up, "down": down, "other": other, "pat": pat, "ref": ref, "slot": slot,
            "doc_up": _login(client, "e3_doc_up", "pw123456"),
            "doc_down": _login(client, "e3_doc_down", "pw123456"),
            "doc_other": _login(client, "e3_doc_other", "pw123456")}


def test_clinical_ref_travels(client, setup):
    rid = setup["ref"]["id"]
    r = client.post(f"/api/referrals/{rid}/clinical-refs", json={"ref_type": "summary", "summary": "高血压3级，建议住院"}, headers=setup["doc_down"])
    assert r.status_code == 201, r.text
    lst = client.get(f"/api/referrals/{rid}/clinical-refs", headers=setup["doc_up"]).json()
    assert len(lst) == 1 and lst[0]["ref_type"] == "summary"
    # 无关机构医生看不到
    assert client.get(f"/api/referrals/{rid}/clinical-refs", headers=setup["doc_other"]).status_code == 403


def test_down_referral_plan_and_reply(client, setup):
    rid = setup["ref"]["id"]
    # 上级出下转康复计划
    p = client.post(f"/api/referrals/{rid}/plans", json={"plan_type": "rehab_plan", "content": "降压达标后下转，二级随访"}, headers=setup["doc_up"])
    assert p.status_code == 201, p.text
    # 基层回复单
    rep = client.post(f"/api/referrals/{rid}/plans", json={"plan_type": "reply", "content": "已接收，纳入慢病随访"}, headers=setup["doc_down"])
    assert rep.status_code == 201
    plans = client.get(f"/api/referrals/{rid}/plans", headers=setup["doc_up"]).json()
    assert {x["plan_type"] for x in plans} == {"rehab_plan", "reply"}


def test_slot_reservation(client, setup):
    rid = setup["ref"]["id"]
    r = client.post(f"/api/referrals/{rid}/reserve-slot", json={"slot_id": setup["slot"]["id"]}, headers=setup["doc_up"])
    assert r.status_code == 201, r.text
    assert r.json()["appointment_id"] > 0
    # 重复预留 409
    assert client.post(f"/api/referrals/{rid}/reserve-slot", json={"slot_id": setup["slot"]["id"]}, headers=setup["doc_up"]).status_code == 409
    got = client.get(f"/api/referrals/{rid}/reservation", headers=setup["doc_down"]).json()
    assert got["slot_id"] == setup["slot"]["id"]
    # 无关机构无权预留
    assert client.post(f"/api/referrals/{rid}/reserve-slot", json={"slot_id": setup["slot"]["id"]}, headers=setup["doc_other"]).status_code == 403


def test_returned_transition(client, setup):
    rid = setup["ref"]["id"]
    # 接诊
    assert client.patch(f"/api/referrals/{rid}/status", json={"status": "accepted"}, headers=setup["doc_up"]).status_code == 200
    # 退回（E3 新增）
    assert client.patch(f"/api/referrals/{rid}/status", json={"status": "returned"}, headers=setup["doc_up"]).status_code == 200
    # 退回后可再次接诊
    assert client.patch(f"/api/referrals/{rid}/status", json={"status": "accepted"}, headers=setup["doc_up"]).status_code == 200
