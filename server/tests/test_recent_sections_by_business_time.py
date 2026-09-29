"""「最近 N 次」按就诊 / 开方 / 结算 / 入院时刻截取，不按编号（P2-846，第二十三批「"最新 / 最近一次"取的是哪一条」扫描 Y2-4）。

存量导入（`scripts/import_legacy.py`）把就诊、处方、结算的 created_at 写成业务日期，住院写 admitted_at；编号却是入库先后——
机构上线之后再补导的历史记录编号更大。360 视图、就诊清单、居民端档案、慢专病居民全周期档案、随访前置资料原先一律按编号
倒序截取：前几十条全是几年前的导入记录，今天那次急性心梗被截掉（360 只标一个 has_more，医生移动端连日期都不印）。
修后按业务时刻倒序、编号兜底；没有导入数据时顺序不变。
"""
from datetime import datetime, timedelta

import pytest

from app.database import SessionLocal
from app.models import Admission, Bed, Encounter, Prescription, Settlement, SmsCode, User, Ward

B = "/api/spd"
IMPORTED = 55   # 比每段上限（360 / 居民端 50、慢专病档案 30、随访前置资料 10 / 5）都多


def _old(k: int) -> datetime:
    return datetime(2016, 1, 1) + timedelta(days=30 * k)


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2846 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2846 老张", "id_card": "330102195501012846", "phone": "13700112846"}).json()
    today = client.post("/api/encounters", headers=admin, json={
        "patient_id": patient["id"], "org_id": org, "diagnosis_name": "急性心肌梗死"})
    assert today.status_code in (200, 201), today.text
    with SessionLocal() as db:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        ward = Ward(org_id=org, name="P2846 心内科")
        db.add(ward)
        db.flush()
        bed = Bed(ward_id=ward.id, bed_no="P2846-1")
        db.add(bed)
        db.flush()
        common = {"patient_id": patient["id"], "org_id": org}
        latest = {
            "prescription": Prescription(**common, diagnosis_name="今日处方", created_by=admin_id),
            "settlement": Settlement(**common, bill_type="outpatient", total_amount=12.5, created_by=admin_id),
            "admission": Admission(**common, ward_id=ward.id, bed_id=bed.id, diagnosis_name="今日入院",
                                   created_by=admin_id),
        }
        db.add_all(latest.values())
        db.flush()
        # 上线之后补导的历史记录：编号更大、业务时刻更早
        for k in range(IMPORTED):
            db.add(Encounter(**common, diagnosis_name="高血压复诊", created_at=_old(k)))
            db.add(Prescription(**common, diagnosis_name="历史处方", created_by=admin_id, created_at=_old(k)))
            db.add(Settlement(**common, bill_type="outpatient", total_amount=3, created_by=admin_id,
                              created_at=_old(k)))
        for k in range(6):
            db.add(Admission(**common, ward_id=ward.id, bed_id=bed.id, diagnosis_name="历史住院", status="discharged",
                             admitted_at=_old(k), discharged_at=_old(k) + timedelta(days=7), created_by=admin_id))
        db.commit()
        latest_ids = {key: row.id for key, row in latest.items()}
        db.query(SmsCode).delete()
        db.commit()
    code = client.post("/api/portal/auth/sms/code",
                       json={"phone": "13700112846", "purpose": "login"}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login",
                        json={"phone": "13700112846", "code": code}).json()["access_token"]
    return {"org": org, "patient": patient, "encounter": today.json()["id"], **latest_ids,
            "me": {"Authorization": f"Bearer {token}"}}


def test_就诊清单_第一条是今天那次(client, admin, world):
    rows = client.get("/api/encounters", headers=admin, params={"patient_id": world["patient"]["id"], "limit": 10})
    assert rows.status_code == 200, rows.text
    assert rows.json()[0]["id"] == world["encounter"], rows.json()[:2]   # 修前是编号最大的导入记录


def test_360视图_各段最近的在最前_截掉的是最老的(client, admin, world):
    view = client.get(f"/api/archive/{world['patient']['ehc_no']}", headers=admin).json()
    assert view["has_more"]["encounters"] and len(view["encounters"]) == 50
    assert view["encounters"][0]["id"] == world["encounter"]   # 修前今天那次根本不在这 50 条里
    assert view["prescriptions"][0]["id"] == world["prescription"]
    assert view["settlements"][0]["id"] == world["settlement"]
    assert world["encounter"] not in [row["id"] for row in view["encounters"][1:]]


def test_居民端档案与慢专病全周期档案_今天那次在最前(client, world):
    archive = client.get("/api/portal/me/archive", headers=world["me"])
    assert archive.status_code == 200, archive.text
    assert archive.json()["encounters"][0]["diagnosis_name"] == "急性心肌梗死"   # 修前是「高血压复诊」
    spd = client.get("/api/portal/spd/archive", headers=world["me"], params={"patient_id": world["patient"]["id"]})
    assert spd.status_code == 200, spd.text
    assert spd.json()["timeline"][0]["title"] == "急性心肌梗死"   # 修前取的 30 次全是导入的，今天那次不在时间轴上


def test_随访前置资料_最近就诊与住院按时刻取(client, admin, world):
    patient = world["patient"]["id"]
    enrolled = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": world["org"]})
    assert enrolled.status_code == 201, enrolled.text
    rule = client.post(f"{B}/followup-rules", headers=admin, json={
        "code": "P2846_FR", "name": "P2846 高血压随访", "scene": "outpatient", "program_code": "hypertension",
        "points": [0], "questionnaire_code": "q_chronic"})
    assert rule.status_code == 201, rule.text
    plan = client.post(f"{B}/followup-plans", headers=admin, json={
        "patient_id": patient, "rule_id": rule.json()["id"], "org_id": world["org"]})
    assert plan.status_code in (200, 201), plan.text
    record = client.get(f"{B}/followup-records", headers=admin, params={"patient_id": patient}).json()[0]["id"]
    context = client.get(f"{B}/followup-records/{record}/context", headers=admin)
    assert context.status_code == 200, context.text
    body = context.json()
    assert body["encounters"][0]["diagnosis_name"] == "急性心肌梗死", body["encounters"][:2]
    assert body["admissions"][0]["id"] == world["admission"], body["admissions"][:2]   # 修前 5 条全是历史住院
