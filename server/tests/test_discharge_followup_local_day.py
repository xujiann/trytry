"""出院随访从本地的出院日起算（P2-545，第十批「日期与期间边界」扫描 X3-7）。

平台出院随访任务的到期日按 `clock.today()`（本地业务日）起算；出院事件里的 `discharged_on` 却是 `now.date()`——naive UTC 的
日期，东八区 0–8 点出院的记成前一天，慢专病据此派生的出院随访比平台自己的出院随访早一天。「按患者特征自动匹配」回溯
出院 / 就诊记录时同样拿落库时刻的 UTC 日期当基准日。修后出院事件发本地业务日，自动匹配把落库时刻换成本地日期再起算。
"""
import time
from datetime import date, datetime, time as dtime, timedelta

import pytest

from conftest import freeze_business_date

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2545 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2545 病区"}).json()["id"]
    resp = client.post(f"{B}/followup-rules", headers=admin, json={
        "code": "P2545_inpatient", "name": "P2545 出院方案", "scene": "inpatient",
        "diagnosis_keywords": ["P2545出院病"], "points": [7]})
    assert resp.status_code == 201, resp.text
    return {"org": org, "ward": ward, "n": 0}


def _admit(client, admin, world):
    world["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2545 患者{world['n']}", "id_card": f"33012719660606{2545 + world['n']:04d}"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={
        "ward_id": world["ward"], "bed_no": f"P2545-{world['n']}"}).json()["id"]
    adm = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": world["ward"], "bed_id": bed, "diagnosis_name": "P2545出院病"})
    assert adm.status_code == 201, adm.text
    return patient, adm.json()["id"]


def _planned(patient_id):
    from app.database import SessionLocal
    from app.spd.models import SpdFollowupRecord

    with SessionLocal() as db:
        return [p for (p,) in db.query(SpdFollowupRecord.planned_at).filter(SpdFollowupRecord.patient_id == patient_id)]


def test_出院事件发本地业务日_与平台出院随访同一天起算(client, admin, world):
    from app.database import SessionLocal
    from app.models import FollowupTask

    patient, admission = _admit(client, admin, world)
    resp = client.post(f"/api/inpatient/admissions/{admission}/case-summary", headers=admin, json={
        "discharge_diagnosis": "P2545出院病", "total_cost": 1000, "outcome": "好转"})
    assert resp.status_code in (200, 201), resp.text
    day = date(2031, 3, 10)
    with freeze_business_date(day):
        assert client.post(f"/api/inpatient/admissions/{admission}/discharge", headers=admin).status_code == 200
    with SessionLocal() as db:
        platform_due = db.query(FollowupTask.due_date).filter(
            FollowupTask.category == "discharge", FollowupTask.source_id == admission).scalar()
    assert platform_due == (day + timedelta(days=7)).isoformat()
    # 修前：慢专病的出院随访按 UTC 日期（真实的今天）起算，与平台任务不是同一天
    assert _planned(patient) == [(day + timedelta(days=7)).isoformat()]


@pytest.fixture
def east_eight(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        yield
    finally:
        monkeypatch.undo()
        time.tzset()


def test_自动匹配按本地出院日起算(client, admin, world, east_eight):
    from app import clock
    from app.database import SessionLocal
    from app.models import Admission

    patient, admission = _admit(client, admin, world)
    local_day = clock.today() - timedelta(days=2)
    with SessionLocal() as db:
        row = db.get(Admission, admission)
        # 本地两天前早上 07:30 出院——落库是再前一天的 UTC 23:30
        row.status = "discharged"
        row.discharged_at = datetime.combine(local_day - timedelta(days=1), dtime(23, 30))
        db.commit()
    resp = client.post(f"{B}/followup-plans/auto-match", headers=admin, json={
        "scene": "inpatient", "org_id": world["org"], "days": 7})
    assert resp.status_code == 200, resp.text
    assert _planned(patient) == [(local_day + timedelta(days=7)).isoformat()]   # 修前早一天
