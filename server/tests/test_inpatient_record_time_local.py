"""住院病程 / 护理记录没填记录时间时显示落库的 UTC 时刻：同一张护理记录表，门诊那条 10:21、住院这条 02:21（P2-455）。

门急诊护理（`outpatient_docs`）与室内质控（P2-171）没填记录时间时都落 `clock.now_local()`——clock 模块把它留给的正是
「记录时间默认值」这种给人看的字符串。住院这边不落缺省，读出时拿 `created_at`（naive UTC）现拼；桌面端表单与医生移动端
都不送 `recorded_at`，于是从界面写的每一条病程 / 住院护理都显示 UTC 时刻，东八区部署下差 8 小时，与门诊护理、与手填了
时间的相邻一条对不上。修后：写入时落本地时刻；修前落库、没有记录时间的存量，按落库时刻换成本地时间显示。
"""
import time
from datetime import datetime

import pytest

from app.database import SessionLocal

FIXED = datetime(2026, 9, 27, 10, 21)


@pytest.fixture(scope="module")
def admission(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2455 住院医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2455 住院患者", "id_card": "330106197009092455"}).json()["id"]
    ward = client.post("/api/inpatient/wards", json={"name": "P2455 病区", "org_id": org}, headers=admin).json()
    bed = client.post("/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": "P2455-1"}, headers=admin).json()
    adm = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "org_id": org, "ward_id": ward["id"], "bed_id": bed["id"],
        "doctor_name": "P2455 医生", "diagnosis_name": "肺炎"})
    assert adm.status_code == 201, adm.text
    return adm.json()["id"]


def test_没填记录时间_落此刻的本地时间(client, admin, admission, monkeypatch):
    from app.routers import clinical_docs

    monkeypatch.setattr(clinical_docs, "now_local", lambda: FIXED)
    note = client.post(f"/api/inpatient/admissions/{admission}/progress-notes", headers=admin,
                       json={"note_type": "daily", "content": "P2455 日常病程"})
    nursing = client.post(f"/api/inpatient/admissions/{admission}/nursing-records", headers=admin,
                          json={"nursing_level": "level2", "content": "P2455 巡视"})
    assert (note.status_code, nursing.status_code) == (201, 201), (note.text, nursing.text)
    assert note.json()["recorded_at"] == nursing.json()["recorded_at"] == "2026-09-27 10:21"   # 修前是落库的 UTC 时刻
    listed = client.get(f"/api/inpatient/admissions/{admission}/nursing-records", headers=admin).json()
    assert [r["recorded_at"] for r in listed if r["content"] == "P2455 巡视"] == ["2026-09-27 10:21"]


def test_手填的记录时间原样(client, admin, admission):
    nursing = client.post(f"/api/inpatient/admissions/{admission}/nursing-records", headers=admin,
                          json={"nursing_level": "level1", "content": "P2455 手填", "recorded_at": "2026-09-26 08:00"})
    assert nursing.json()["recorded_at"] == "2026-09-26 08:00"


def test_存量没有记录时间的_按本地时间显示(client, admin, admission, monkeypatch):
    from app.models import NursingRecord, User

    with SessionLocal() as db:
        author = db.query(User).filter(User.username == "admin").one().id
        db.add(NursingRecord(admission_id=admission, nursing_level="level3", content="P2455 存量", recorded_at="",
                             created_by=author, created_at=datetime(2026, 9, 27, 2, 21)))   # naive UTC
        db.commit()
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        listed = client.get(f"/api/inpatient/admissions/{admission}/nursing-records", headers=admin).json()
    finally:
        monkeypatch.undo()
        time.tzset()
    assert [r["recorded_at"] for r in listed if r["content"] == "P2455 存量"] == ["2026-09-27 10:21"]   # 修前 02:21
