"""没过完的月份查运行效率 / 床日成本，床日与期间天数截到今天（P2-729，第十九批「单位与量纲」扫描 K4-5）。

两处 docstring 都写「实际占用床日」，实现却把在院患者的出院日视同期末（次月首日）：本月的占用床日含还没到的日子，床日
成本因此被摊低；还没开始的月份也有占用床日和使用率；医师日均担负除以整月天数，月中查看只有真实值的「已过天数 / 整月
天数」。修法：期末取「次月首日」与「明天」中较早的那个（`deps.month_bounds_elapsed`），床日与期间天数都截到今天；
整月已过的月份结果不变。
"""
from datetime import date, datetime

import pytest
from conftest import freeze_business_date

from app.database import SessionLocal
from app.models import Admission, Employee, Encounter, User

ADMITTED = datetime(2026, 11, 5, 8)   # 两人 11 月 5 日入院、仍在院


@pytest.fixture(scope="module")
def org(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2729 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2729 病区"}).json()["id"]
    beds = [client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": f"P2729-{i}"}).json()["id"]
            for i in range(4)]
    with SessionLocal() as db:
        creator = db.query(User).filter_by(username="admin").one().id
        for i in range(2):
            patient = client.post("/api/patients", headers=admin, json={
                "name": f"P2729 患者{i}", "id_card": f"33010619700505{2729 + i:04d}"}).json()["id"]
            db.add(Admission(patient_id=patient, org_id=org, ward_id=ward, bed_id=beds[i], status="admitted",
                             admitted_at=ADMITTED, created_by=creator))
            for day in range(1, 11):   # 11 月 1～10 日每位各一次门诊，共 20 人次
                db.add(Encounter(patient_id=patient, org_id=org, encounter_type="outpatient",
                                 created_at=datetime(2026, 11, day, 9)))
        db.add(Employee(org_id=org, name="P2729 王医生", position="主治医师"))
        db.commit()
    return org


def _efficiency(client, admin, org, period):
    (row,) = [r for r in client.get(f"/api/analytics/efficiency?period={period}&org_id={org}", headers=admin).json()
              if r["org_id"] == org]
    return row


def test_月中查本月_床日与日均都截到今天(client, admin, org):
    with freeze_business_date(date(2026, 11, 10)):
        row = _efficiency(client, admin, org, "2026-11")
        cost = client.get(f"/api/cost/unit-cost?period=2026-11&org_id={org}", headers=admin).json()
    assert row["occupied_bed_days"] == 12                 # 两人各 5～10 日共 6 天；修前记到月末各 26 天 = 52
    assert row["bed_occupancy_rate_pct"] == 30.0          # 12 ÷（4 床 × 已过 10 天）；修前 52 ÷ 120
    assert row["visits_per_doctor_per_day"] == 2.0        # 20 人次 ÷ 1 名医师 ÷ 已过 10 天；修前 ÷ 30 天 = 0.67
    assert cost["occupied_bed_days"] == 12, cost          # 床日成本的分母同一口径；修前 52


def test_还没开始的月份没有床日(client, admin, org):
    with freeze_business_date(date(2026, 11, 10)):
        row = _efficiency(client, admin, org, "2026-12")
        cost = client.get(f"/api/cost/unit-cost?period=2026-12&org_id={org}", headers=admin).json()
    assert (row["occupied_bed_days"], row["bed_occupancy_rate_pct"]) == (0, 0.0)   # 修前 62 床日、使用率 50%
    assert cost["occupied_bed_days"] == 0


def test_整月已过的月份不变(client, admin, org):
    with freeze_business_date(date(2026, 12, 15)):
        row = _efficiency(client, admin, org, "2026-11")
    assert row["occupied_bed_days"] == 52                 # 5 日到月末各 26 天
    assert row["visits_per_doctor_per_day"] == round(20 / 30, 2)
