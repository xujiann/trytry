"""占用床日的 1 天下限只给当日入出院：月初那天出院的，原先在当月凭空多记 1 床日（P2-135）。

床日按 [入院, 出院) 与统计期的交集天数算，「当日入当日出计 1 床日——住了一天就是一天」。实现却把 1 天的下限给了
**所有**交集：8 月 20 日入、9 月 1 日出，与 9 月的交集是空的，9 月照样记 1 床日；8 月 31 日入、9 月 1 日出（住了一晚），
8 月、9 月各记 1 天。成本核算的床日成本（`cost._occupied_bed_days`）与运行效率的床位使用率
（`analytics._efficiency_rows`）同一个形状。
"""
from datetime import date, datetime

import pytest

STAYS = [   # (入院, 出院)：跨月住到 9 月 1 日出院 / 8 月 31 日入 9 月 1 日出 / 9 月 15 日当日入出
    (datetime(2026, 8, 20, 9), datetime(2026, 9, 1, 10)),
    (datetime(2026, 8, 31, 20), datetime(2026, 9, 1, 8)),
    (datetime(2026, 9, 15, 8), datetime(2026, 9, 15, 17)),
]


@pytest.fixture(scope="module")
def org(client, admin):
    from app.database import SessionLocal
    from app.models import Admission, User

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2135 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2135 病区"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P2135-1"}).json()["id"]
    with SessionLocal() as db:
        creator = db.query(User).filter_by(username="admin").one().id
        for i, (admitted, discharged) in enumerate(STAYS):
            patient = client.post("/api/patients", headers=admin, json={
                "name": f"P2135 患者{i}", "id_card": f"33010619650505{i:04d}", "gender": "男"}).json()["id"]
            db.add(Admission(patient_id=patient, org_id=org, ward_id=ward, bed_id=bed, status="discharged",
                             admitted_at=admitted, discharged_at=discharged, created_by=creator))
        db.commit()
    return org


def test_成本核算的床日不在月初凭空多一天(org):
    from app.database import SessionLocal
    from app.routers.cost import _occupied_bed_days

    with SessionLocal() as db:
        august = _occupied_bed_days(db, org, date(2026, 8, 1), date(2026, 9, 1))
        september = _occupied_bed_days(db, org, date(2026, 9, 1), date(2026, 10, 1))
    assert (august, september) == (13, 1)   # 8 月：12 + 1；9 月：只有当日入出那一天。修前 9 月是 3


def test_床位使用率的占用床日同一口径(client, admin, org):
    rows = client.get(f"/api/analytics/efficiency?period=2026-09&org_id={org}", headers=admin).json()
    (row,) = [r for r in rows if r["org_id"] == org]
    assert row["occupied_bed_days"] == 1   # 修前 3
    august = client.get(f"/api/analytics/efficiency?period=2026-08&org_id={org}", headers=admin).json()
    (row,) = [r for r in august if r["org_id"] == org]
    assert row["occupied_bed_days"] == 13
