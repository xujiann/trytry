"""入院登记建了住院就诊记录，却不发「就诊登记」事件（P2-370）。

事件清单写 `ENCOUNTER_CREATED = "encounter.created"  # 门急诊/住院就诊登记`。FHIR 入站的住院就诊走 `create_encounter`，发；
入院登记（`create_admission`，HL7 A01 也走它）同样往 encounters 里记一条住院就诊，却不发——开了就诊识别
（`MEDPLAT_SPD_AUTO_IDENTIFY_ON_ENCOUNTER`）的县，住院患者按病种纳入规则一个也识别不到。

修法：入院登记记完住院就诊即发同一个事件，订阅者的写入与住院记录同一次提交。
"""
import pytest

from app.database import SessionLocal


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2370 住院医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2370 病区"}).json()["id"]
    return {"org": org, "ward": ward, "n": 0}


def _patient(client, admin, world):
    world["n"] += 1
    return client.post("/api/patients", headers=admin, json={
        "name": f"P2370 患者{world['n']}", "id_card": f"33010219580808{world['n']:03d}0", "gender": "女",
        "birth_date": "1958-08-08"}).json()["id"]


def _admit(client, admin, world, patient, diagnosis="原发性高血压"):
    bed = client.post("/api/inpatient/beds", headers=admin, json={
        "ward_id": world["ward"], "bed_no": f"P2370-{patient}"}).json()["id"]
    resp = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": world["ward"], "bed_id": bed, "diagnosis_name": diagnosis})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_入院登记发就诊登记事件(client, admin, world, monkeypatch):
    from app import events

    seen = []
    monkeypatch.setitem(events._subscribers, events.ENCOUNTER_CREATED, [lambda db, payload: seen.append(payload)])
    patient = _patient(client, admin, world)
    _admit(client, admin, world, patient)
    monkeypatch.undo()
    assert len(seen) == 1, seen   # 修前 0
    assert {k: seen[0][k] for k in ("patient_id", "org_id", "encounter_type", "diagnosis_name")} == {
        "patient_id": patient, "org_id": world["org"], "encounter_type": "inpatient", "diagnosis_name": "原发性高血压"}


def test_开了就诊识别_入院的患者照样按纳入规则识别(client, admin, world, monkeypatch):
    from app.config import settings
    from app.spd.models import SpdCandidate

    patient = _patient(client, admin, world)
    # 先有一条带高血压编码的门诊记录（识别开关关着时建，不进池）
    got = client.post("/api/encounters", headers=admin, json={
        "patient_id": patient, "org_id": world["org"], "encounter_type": "outpatient",
        "diagnosis_code": "I10", "diagnosis_name": "原发性高血压"})
    assert got.status_code == 201, got.text
    monkeypatch.setattr(settings, "spd_auto_identify_on_encounter", True)
    _admit(client, admin, world, patient)
    with SessionLocal() as db:
        row = db.query(SpdCandidate).filter(SpdCandidate.patient_id == patient,
                                            SpdCandidate.program_code == "hypertension").first()
        assert row is not None and (row.status, row.source) == ("suspect", "event")   # 修前 None
