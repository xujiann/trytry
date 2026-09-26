"""门急诊文书接口收住院就诊：入院登记建的那条住院就诊照样能记门诊处置与门诊护理（P2-264）。

护理记录表写着「挂载点二选一：`admission_id` 是住院护理，`encounter_id` 是门急诊护理」；`POST /api/outpatient/encounters/{id}/
treatments` 与 `/nursing-records` 原先不看就诊类型——入院登记时一并建的住院就诊（`encounter_type="inpatient"`）照收，记下的
护理不在住院护理单上（住院按住院记录号取），也绕开了住院那一侧出院后不再收文书一类的守卫。修法：住院就诊 422，指去住院文书。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.models import Encounter

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2264 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2264 病区"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P2264-01"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2264 患者", "id_card": "330106198512122264"}).json()["id"]
    admitted = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "doctor_name": "王医师", "diagnosis_name": "肺炎"})
    assert admitted.status_code == 201, admitted.text
    outpatient = client.post("/api/encounters", headers=admin, json={
        "patient_id": patient, "org_id": org, "diagnosis_name": "上感"}).json()["id"]
    with SessionLocal() as db:
        inpatient = db.query(Encounter).filter_by(patient_id=patient, encounter_type="inpatient").one().id
    return {"inpatient": inpatient, "outpatient": outpatient}


@pytest.mark.parametrize("path, body", [
    ("treatments", {"treatment_name": "清创换药"}),
    ("nursing-records", {"content": "输液观察"}),
], ids=["处置", "护理"])
def test_住院就诊记门急诊文书_422_门诊就诊照收(client, admin, world, path, body):
    rejected = client.post(f"/api/outpatient/encounters/{world['inpatient']}/{path}", headers=admin, json=body)
    assert rejected.status_code == 422, rejected.text   # 修前 201：住院就诊记上了门急诊文书
    assert "住院文书" in rejected.json()["detail"]
    accepted = client.post(f"/api/outpatient/encounters/{world['outpatient']}/{path}", headers=admin, json=body)
    assert accepted.status_code == 201, accepted.text
