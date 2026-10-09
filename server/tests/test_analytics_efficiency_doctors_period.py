"""运行效率的医师数按期末取：期末之后才建档的医师不进往月（P2-1705，第五十批扫描 AN3-5 的新入职一半）。

医师数原先只按此刻 `status == "active"` 数，同一函数里床位已按期末存量 `Bed.created_at < end_dt` 取（P2-1074）——
10 月新招 6 名医师后再查 8 月：医师 2 → 8、日均担负 1.0 → 0.25，已经报出去的 8 月数字复现不出来，期末综合绩效报告的
公式变量 `doctors` 也跟着变成 8。员工表没有入职日期列，`employees.created_at`（建档时刻）是唯一现成的上界。
期内离职的要靠人员变动的生效日期才能还原，不在本条（随 P2-749 另定）。
"""
from datetime import datetime

import pytest

from app import clock
from app.database import SessionLocal
from app.models import Employee, Encounter, Organization, Patient
from app.routers.analytics import build_variable_index


@pytest.fixture(scope="module")
def hospital(client, admin):
    with SessionLocal() as db:
        org = Organization(name="P21705 县人民医院", org_type="lead_hospital", level="county")
        db.add(org)
        db.flush()
        db.add_all([Employee(org_id=org.id, name=f"P21705 老医师{i}", position="主治医师",
                             created_at=datetime(2026, 7, 1)) for i in range(2)])
        patient = Patient(ehc_no="EHC-P21705", name="P21705 患者", id_card="330106196001011705")
        db.add(patient)
        db.flush()
        for day in range(1, 32):   # 8 月每天 2 人次门诊，共 62
            db.add_all([Encounter(patient_id=patient.id, org_id=org.id, encounter_type="outpatient",
                                  created_at=datetime(2026, 8, day, 3)) for _ in range(2)])
        db.commit()
        return org.id


def _august(client, admin, org_id):
    rows = client.get("/api/analytics/efficiency?period=2026-08", headers=admin)
    assert rows.status_code == 200, rows.text
    row = next(r for r in rows.json() if r["org_id"] == org_id)
    return row["visits"], row["doctors"], row["visits_per_doctor_per_day"]


def test_期末之后建档的医师不进往月(client, admin, hospital):
    assert _august(client, admin, hospital) == (62, 2, 1.0)
    for i in range(6):   # 今天新招 6 名医师
        resp = client.post("/api/mgmt/employees", headers=admin,
                           json={"org_id": hospital, "name": f"P21705 新医师{i}", "position": "住院医师"})
        assert resp.status_code == 201, resp.text
    assert _august(client, admin, hospital) == (62, 2, 1.0)   # 修前 (62, 8, 0.25)
    with SessionLocal() as db:
        assert build_variable_index(db, "2026-08")[hospital]["doctors"] == 2.0   # 修前 8.0


def test_当期照数今天建档的医师(client, admin, hospital):
    period = clock.today().isoformat()[:7]
    rows = client.get(f"/api/analytics/efficiency?period={period}", headers=admin).json()
    assert next(r for r in rows if r["org_id"] == hospital)["doctors"] == 8
