"""诊疗人次不含住院类就诊记录：驾驶舱基层占比、运行效率的医师日均担负、公式变量同一条口径（P2-199）。

每办一次入院会同时建一条 `encounter_type="inpatient"` 的就诊记录。运营月报的门急诊人次（P2-153）与成本核算的门诊人次
早已把它排除，驾驶舱「基层诊疗人次占比」（监测指标 7）的分子分母、运行效率的「医师日均担负」、自定义绩效公式的
「期间诊疗人次」却照数——县医院 3000 门诊 + 500 入院、乡镇 2000 门诊：基层占比印 36.36% 而不是 40%。
"""
from datetime import datetime

import pytest

from app.database import SessionLocal
from app.models import Employee, Encounter, Organization, Patient


@pytest.fixture(scope="module")
def world(client):
    """直接落库：要把就诊时间放进指定月份。县医院 3 门诊 + 1 住院类，卫生院 2 门诊。"""
    db = SessionLocal()
    try:
        county = Organization(name="P2199 县医院", org_type="lead_hospital", level="county")
        town = Organization(name="P2199 卫生院", org_type="township", level="township")
        patient = Patient(ehc_no="EHC-P2199-01", name="P2199 患者", id_card="330106197001011559")
        db.add_all([county, town, patient])
        db.flush()
        db.add(Employee(org_id=county.id, name="P2199 医师", position="内科医师", status="active"))
        when = datetime(2026, 7, 10, 9, 0)
        for org_id, kind in ([(county.id, "outpatient")] * 3 + [(county.id, "inpatient")] + [(town.id, "outpatient")] * 2):
            db.add(Encounter(patient_id=patient.id, org_id=org_id, doctor_name="医师", diagnosis_name="诊断",
                             encounter_type=kind, created_at=when))
        db.commit()
        return {"county": county.id, "town": town.id}
    finally:
        db.close()


def test_驾驶舱基层占比分子分母都不含住院类(client, admin, world):
    division = client.get("/api/metrics/overview", headers=admin).json()["service_division"]
    assert (division["encounters_total"], division["grassroots_encounters"]) == (5, 2)   # 修前 6：住院类也算
    assert division["grassroots_encounter_ratio_pct"] == 40.0


def test_运行效率与公式变量的诊疗人次不含住院类(client, admin, world):
    rows = client.get(f"/api/analytics/efficiency?period=2026-07&org_id={world['county']}", headers=admin).json()
    county = next(r for r in rows if r["org_id"] == world["county"])
    assert county["visits"] == 3                                                         # 修前 4
    from app.routers.analytics import build_variable_index

    db = SessionLocal()
    try:
        assert build_variable_index(db, "2026-07")[world["county"]]["encounters"] == 3.0  # 修前 4.0
    finally:
        db.close()
