"""平台办理出院时转归「死亡」不派出院随访、不发「出院随访安排」（P2-878，第二十四批「通知、提醒与待办」扫描 Z2-4；P2-385 的
补充事实）。

平台办理出院必须先有病案首页（没有就 409「病案首页未填写，不可出院」），转归此时已知；`discharge_admission` 却不看
`summary.outcome`，照样派出院随访（`spawn_discharge_followup`）、照样给患者与代管家属发「您已办理出院，我们将在 7 天内电话
随访」。术中记录转归「死亡」早就不排随访、不发消息（P2-499）。P2-385 暂缓的理由是「出院时病案首页可能还没写」，这只对
HL7 A03 那一路成立。修后平台出院这一路照 P2-499：死亡转归不派、不发；出院事件照发（载荷与订阅方随 P2-385 待裁定）。
"""
import pytest

from app.database import SessionLocal
from app.models import FollowupTask, Notification, SmsCode


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2878 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2878 呼吸科"}).json()["id"]
    return {"org": org, "ward": ward}


def _discharged(client, admin, world, n, outcome):
    phone = f"1370011287{n}"
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2878 患者{n}", "id_card": f"33010219400101287{n}", "phone": phone}).json()["id"]
    with SessionLocal() as db:
        db.query(SmsCode).filter(SmsCode.phone == phone).delete()
        db.commit()
    code = client.post("/api/portal/auth/sms/code", json={"phone": phone, "purpose": "login"}).json()["debug_code"]
    login = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code})
    assert login.status_code == 200, login.text   # 居民端账户：出院消息有人收
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": world["ward"], "bed_no": f"P2878-{n}"})
    admitted = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": world["ward"], "bed_id": bed.json()["id"], "doctor_name": "县医生",
        "diagnosis_name": "重症肺炎"})
    assert admitted.status_code == 201, admitted.text
    admission = admitted.json()["id"]
    summary = client.post(f"/api/inpatient/admissions/{admission}/case-summary", headers=admin,
                          json={"discharge_diagnosis": "重症肺炎", "outcome": outcome})
    assert summary.status_code == 201, summary.text
    discharged = client.post(f"/api/inpatient/admissions/{admission}/discharge", headers=admin)
    assert discharged.status_code == 200, discharged.text
    with SessionLocal() as db:
        tasks = db.query(FollowupTask).filter(FollowupTask.category == "discharge",
                                              FollowupTask.source_id == admission).count()
        notices = db.query(Notification).filter(Notification.link_type == "admission",
                                                Notification.link_id == admission).count()
    return tasks, notices


def test_转归死亡出院_不派出院随访不发随访安排(client, admin, world):
    assert _discharged(client, admin, world, 1, "死亡") == (0, 0)   # 修前 (1, 1)


def test_其他转归照旧派随访发通知(client, admin, world):
    assert _discharged(client, admin, world, 2, "好转") == (1, 1)
