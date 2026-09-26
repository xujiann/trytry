"""抗菌药物使用强度的「收治人天」与平均住院日同一个「出院者占用总床日」（P1-151）。

强度 docstring 与口径文案都写「÷ 收治人天（出院者占用总床日）」「与国家监测口径一致」——国家口径是出院人数 ×
平均住院日；同一文件的平均住院日也叫「出院者占用总床日 ÷ 出院人次」，按日期差算（当日入出院计 1 天），成本核算与
DRG 也是日期差。只有强度的分母用时刻级天数差（整 24 小时向下取整）：下午入院、上午出院的住院每例少算一天，
强度被系统性抬高——两项都写进考核，同一次住院在一处 8 天、另一处 7 天。
"""
from datetime import datetime

import pytest

from app.database import SessionLocal
from app.models import Admission, Bed, DrugRule, Organization, Patient, Prescription, PrescriptionItem, User, Ward

PERIOD = "2026-05"


@pytest.fixture(scope="module")
def org_id(client):
    """直接落库：要精确控制入出院的钟点，走接口做不到。"""
    db = SessionLocal()
    try:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        org = Organization(name="P1151 县医院", org_type="lead_hospital", level="county")
        patient = Patient(ehc_no="EHC-P1151-01", name="P1151 患者", id_card="330106196505051519")
        db.add_all([org, patient])
        db.flush()
        ward = Ward(org_id=org.id, name="P1151 内科")
        db.add(ward)
        db.flush()
        bed = Bed(ward_id=ward.id, bed_no="1")
        db.add(bed)
        db.flush()
        # 5/1 下午入院、5/9 上午出院：日期差 8 天，时刻差 7 天 18 小时
        db.add(Admission(patient_id=patient.id, org_id=org.id, ward_id=ward.id, bed_id=bed.id,
                         status="discharged", admitted_at=datetime(2026, 5, 1, 15, 0),
                         discharged_at=datetime(2026, 5, 9, 9, 0), created_by=admin_id))
        db.add(DrugRule(drug_code="P1151-ABX", max_daily_dose=4, antibiotic=True, active=True, ddd=1.0))
        rx = Prescription(patient_id=patient.id, org_id=org.id, created_by=admin_id,
                          created_at=datetime(2026, 5, 2, 9, 0))
        db.add(rx)
        db.flush()
        db.add(PrescriptionItem(prescription_id=rx.id, drug_code="P1151-ABX", drug_name="P1151 抗菌药",
                                daily_dose=1.0, days=7))   # 7 DDDs
        db.commit()
        return org.id
    finally:
        db.close()


def test_收治人天与平均住院日同一口径(client, admin, org_id):
    eff = client.get(f"/api/analytics/efficiency?period={PERIOD}&org_id={org_id}", headers=admin).json()
    row = next(r for r in eff if r["org_id"] == org_id)
    assert (row["discharges"], row["avg_length_of_stay"]) == (1, 8.0)
    use = client.get(f"/api/analytics/drug-use?period={PERIOD}&org_id={org_id}", headers=admin).json()
    drug = next(r for r in use["orgs"] if r["org_id"] == org_id)
    assert drug["bed_days"] == 8                                  # 修前 7（时刻差向下取整）
    assert drug["antibiotic_intensity"] == 87.5                   # 7 × 100 ÷ 8；修前 100.0
