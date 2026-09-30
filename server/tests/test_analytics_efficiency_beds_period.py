"""运行效率的「实际开放床位数」按期末取（P2-1074，第三十一批「统计与报表按哪个日期归期」扫描 C1-2）。

床位只能新增（`POST /api/inpatient/beds`，没有删除或停用），`beds.created_at` 现成；运行效率却数「此刻」的床位——
9 月底加了 10 张床，再查 8 月：床位数 10 → 20、周转次数 1.0 → 0.5、使用率 96.77% → 48.39%，已经报出去的 8 月数字
复现不出来。期末综合绩效报告的公式变量（beds / bed_turnover / bed_occupancy_rate_pct）取自同一处。
在管慢病人数早按「期末存量：只设上界」修过（P2-766），床位数是同一种存量。
"""
from datetime import datetime

import pytest

from app import clock
from app.database import SessionLocal
from app.models import Admission, Bed, Organization, Patient, User, Ward


@pytest.fixture(scope="module")
def ward(client, admin):
    with SessionLocal() as db:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        org = Organization(name="P21074 县医院", org_type="lead_hospital", level="county")
        db.add(org)
        db.flush()
        ward = Ward(org_id=org.id, name="P21074 内科", created_at=datetime(2026, 7, 1))
        db.add(ward)
        db.flush()
        beds = [Bed(ward_id=ward.id, bed_no=f"P21074-{i}", created_at=datetime(2026, 7, 1)) for i in range(4)]
        db.add_all(beds)
        db.flush()
        for i, bed in enumerate(beds):   # 8 月：4 张床，4 位患者 8-01 入、8-31 出
            patient = Patient(ehc_no=f"EHC-P21074-{i}", name=f"P21074 住院{i}", id_card=f"3301061955050521{i:02d}")
            db.add(patient)
            db.flush()
            db.add(Admission(patient_id=patient.id, org_id=org.id, ward_id=ward.id, bed_id=bed.id, status="discharged",
                             admitted_at=datetime(2026, 8, 1, 9), discharged_at=datetime(2026, 8, 31, 9),
                             created_by=admin_id))
        db.commit()
        return {"org": org.id, "ward": ward.id}


def _august(client, admin, org_id):
    rows = client.get("/api/analytics/efficiency?period=2026-08", headers=admin)
    assert rows.status_code == 200, rows.text
    row = next(r for r in rows.json() if r["org_id"] == org_id)
    return row["beds"], row["bed_turnover"], row["bed_occupancy_rate_pct"]


def test_期末之后加的床不进往期(client, admin, ward):
    before = _august(client, admin, ward["org"])
    assert before[0] == 4 and before[1] == 1.0
    for i in range(4, 8):   # 今天加 4 张床
        resp = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward["ward"], "bed_no": f"P21074-{i}"})
        assert resp.status_code == 201, resp.text
    assert _august(client, admin, ward["org"]) == before   # 修前床位数 8、周转 0.5、使用率减半


def test_当期照数今天加的床(client, admin, ward):
    this_month = clock.today().isoformat()[:7]
    rows = client.get(f"/api/analytics/efficiency?period={this_month}", headers=admin).json()
    assert next(r for r in rows if r["org_id"] == ward["org"])["beds"] == 8
