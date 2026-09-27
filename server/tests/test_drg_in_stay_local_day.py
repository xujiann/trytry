"""DRG 事中预警的在院天数按本地入院日算（第十五批「时间边界」扫描 S2-2）。

在院天数原先是 `本地业务日 − 入院时刻的 UTC 日期`：一个减法两把尺子，东八区 0–8 点入院的多算 1 天、提前报警——
同一本地日早上 07:30 与 09:30 入院的两位，住到同一天，一位 3 天、一位 2 天。修后入院日换成本地日期再减
（与出院随访起算 P2-545、传染病报告日 P2-528 同一做法）。基线（已出院病例的出入院日期差）两头是同一把尺子，
按哪个时区算日界随 P1-105 定，这里不动。
"""
import time
from datetime import datetime

import pytest

REF_DAY = "2031-05-22"


@pytest.fixture
def east_eight(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        yield
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.fixture(scope="module")
def seeded(client, admin):
    from app.database import SessionLocal
    from app.models import Admission

    org = client.post("/api/organizations", headers=admin, json={
        "name": "S2-2 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "S2-2 神经内科"}).json()["id"]

    def admit(i):
        bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": f"S22-{i}"}).json()
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"S2-2 患者{i}", "id_card": f"33010619810303{2200 + i:04d}"}).json()["id"]
        adm = client.post("/api/inpatient/admissions", headers=admin, json={
            "patient_id": patient, "ward_id": ward, "bed_id": bed["id"], "diagnosis_name": "癫痫"}).json()
        resp = client.post(f"/api/inpatient/admissions/{adm['id']}/case-summary", headers=admin, json={
            "discharge_diagnosis": "癫痫", "total_cost": 3000, "outcome": "好转"})
        assert resp.status_code == 201, resp.text
        assert resp.json()["drg_code"] == "BU21"
        return adm["id"]

    history = [admit(i) for i in range(5)]
    for aid in history:
        assert client.post(f"/api/inpatient/admissions/{aid}/discharge", headers=admin).status_code == 200
    early, late = admit(5), admit(6)
    with SessionLocal() as db:
        for aid in history:   # 基线五例都住 2 天（UTC 正午入出院，两把尺子算出来一样）
            row = db.get(Admission, aid)
            row.admitted_at, row.discharged_at = datetime(2031, 5, 1, 4), datetime(2031, 5, 3, 4)
        # 同一本地日 2031-05-20：早上 07:30 入院（落库是前一天 UTC 23:30）、09:30 入院（UTC 01:30）
        db.get(Admission, early).admitted_at = datetime(2031, 5, 19, 23, 30)
        db.get(Admission, late).admitted_at = datetime(2031, 5, 20, 1, 30)
        db.commit()
    return {"org": org, "early": early, "late": late}


def test_同一本地日入院的两位住到同一天在院天数相同(client, admin, seeded, east_eight):
    body = client.get("/api/drgs/in-stay-alerts", headers=admin, params={
        "org_id": seeded["org"], "today": REF_DAY, "los_multiplier": 1.0}).json()
    # 基线 2 天、倍数 1.0：住满 2 天不报。修前早上 07:30 入院的那位算成 3 天、报了超均值 1.5 倍
    assert [a["admission_id"] for a in body["alerts"]] == [], body["alerts"]


def test_到第三天两位一起报(client, admin, seeded, east_eight):
    body = client.get("/api/drgs/in-stay-alerts", headers=admin, params={
        "org_id": seeded["org"], "today": "2031-05-23", "los_multiplier": 1.0}).json()
    stayed = {a["admission_id"]: a["stayed_days"] for a in body["alerts"]}
    assert stayed == {seeded["early"]: 3, seeded["late"]: 3}, body["alerts"]
