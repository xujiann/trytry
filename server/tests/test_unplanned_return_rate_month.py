"""非计划重返手术室率的分子分母同按术中记录的时间归月（P2-201）。

原先分子（标了非计划重返且已完成的手术）按申请的建单时间归月，分母（已出术中记录台次）按术中记录的时间归月：
8 月 30 日提的重返申请 9 月 1 日做，8 月「1 / 0」、9 月「0 / 1」——这一台从它自己的月份里消失；
8 月提了 3 台重返、当月只出了 2 台术中记录时算出 150%。
"""
from datetime import datetime

import pytest

from app.database import SessionLocal
from app.models import Admission, Bed, Organization, Patient, SurgeryRecord, SurgeryRequest, User, Ward


@pytest.fixture(scope="module")
def org_id(client):
    """直接落库：要把申请与术中记录的时间放到不同月份。"""
    db = SessionLocal()
    try:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        org = Organization(name="P2201 县医院", org_type="lead_hospital", level="county")
        patient = Patient(ehc_no="EHC-P2201-01", name="P2201 患者", id_card="330106195505051578")
        db.add_all([org, patient])
        db.flush()
        ward = Ward(org_id=org.id, name="P2201 外科")
        db.add(ward)
        db.flush()
        bed = Bed(ward_id=ward.id, bed_no="1")
        db.add(bed)
        db.flush()
        adm = Admission(patient_id=patient.id, org_id=org.id, ward_id=ward.id, bed_id=bed.id, status="admitted",
                        admitted_at=datetime(2026, 8, 20, 9, 0), created_by=admin_id)
        db.add(adm)
        db.flush()
        # 8/30 提的非计划重返申请，9/1 做；另一台 9/2 提、9/2 做的普通手术
        for name, unplanned, requested, performed in (("再探查术", True, datetime(2026, 8, 30, 20, 0), datetime(2026, 9, 1, 10, 0)),
                                                      ("阑尾切除术", False, datetime(2026, 9, 2, 8, 0), datetime(2026, 9, 2, 11, 0))):
            req = SurgeryRequest(admission_id=adm.id, patient_id=patient.id, org_id=org.id, surgery_name=name,
                                 unplanned_return=unplanned, status="completed", created_by=admin_id, created_at=requested)
            db.add(req)
            db.flush()
            db.add(SurgeryRecord(request_id=req.id, actual_surgery_name=name, created_by=admin_id, created_at=performed))
        db.commit()
        return org.id
    finally:
        db.close()


def _unplanned(client, admin, org_id, period):
    body = client.get(f"/api/quality/clinical-indicators?period={period}&org_id={org_id}", headers=admin).json()
    row = next(i for i in body["indicators"] if i["key"] == "unplanned_return")
    return row["numerator"], row["denominator"]


def test_重返手术算在做手术的那个月(client, admin, org_id):
    assert _unplanned(client, admin, org_id, "2026-08") == (0, 0)   # 修前 (1, 0)：分子比分母大
    assert _unplanned(client, admin, org_id, "2026-09") == (1, 2)   # 修前 (0, 2)：这一台从自己的月份里消失
