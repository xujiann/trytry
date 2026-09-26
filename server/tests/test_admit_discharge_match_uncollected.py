"""入出院诊断符合率：入院诊断没填的计「未采集」，不算「不符合」（P2-202）。

入院诊断是选填的（办入院表单不要求），原先拿空串去比出院诊断、一律算不符合——两例出院、一例诊断一致、一例入院
诊断空着，符合率印 50% 而不是 100%，也不说有一例没采集。同一个函数里的术前术后诊断符合率早就写明
「没填的是"未采集"，不是"不符合"」并单列未采集数。
"""
from datetime import datetime

import pytest

from app.database import SessionLocal
from app.models import Admission, Bed, CaseSummary, Organization, Patient, User, Ward


@pytest.fixture(scope="module")
def org_id(client):
    db = SessionLocal()
    try:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        org = Organization(name="P2202 县医院", org_type="lead_hospital", level="county")
        db.add(org)
        db.flush()
        ward = Ward(org_id=org.id, name="P2202 内科")
        db.add(ward)
        db.flush()
        for n, (admit_dx, discharge_dx) in enumerate((("肺炎", "肺炎"), ("", "胆囊炎"))):
            patient = Patient(ehc_no=f"EHC-P2202-{n}", name=f"P2202 患者{n}", id_card=f"33010619600101{1580 + n}")
            bed = Bed(ward_id=ward.id, bed_no=str(n))
            db.add_all([patient, bed])
            db.flush()
            adm = Admission(patient_id=patient.id, org_id=org.id, ward_id=ward.id, bed_id=bed.id, status="discharged",
                            diagnosis_name=admit_dx, admitted_at=datetime(2026, 8, 1, 9, 0),
                            discharged_at=datetime(2026, 8, 6, 9, 0), created_by=admin_id)
            db.add(adm)
            db.flush()
            db.add(CaseSummary(admission_id=adm.id, discharge_diagnosis=discharge_dx, outcome="治愈",
                               created_at=datetime(2026, 8, 6, 9, 30)))
        db.commit()
        return org.id
    finally:
        db.close()


def test_入院诊断没填的计未采集(client, admin, org_id):
    body = client.get(f"/api/quality/clinical-indicators?period=2026-08&org_id={org_id}", headers=admin).json()
    row = next(i for i in body["indicators"] if i["key"] == "admit_discharge_match")
    assert (row["numerator"], row["denominator"], row["rate_pct"]) == (1, 1, 100.0)   # 修前 1 / 2 = 50.0
    assert row["uncollected"] == 1                                                     # 修前没有这一项
    cure = next(i for i in body["indicators"] if i["key"] == "cure_improve")
    assert cure["denominator"] == 2                                                    # 治愈好转率的出院人次不受影响
