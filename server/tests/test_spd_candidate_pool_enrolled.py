"""直接签约建档的在管患者不在目标池里：此后每次就诊、每轮识别都被按疑似插回池里（P2-359）。

目标池的规矩是「已在池中（含已纳管）的不重复识别」（就诊事件识别）、「已纳管的不回退状态」（写入目标池）——认的都是
池里那一行。可签约建档（`POST /api/spd/enrollments`）只在池里已有行时把它改成已纳管，从不新建：没经过目标池、直接建档
的患者池里没有行，开了就诊识别后一次 I10 就诊就把在管患者插成「疑似」，认领、签约随之 409。

修法：直接建档时在池里记一行「已纳管」；池里没有行、却有在管档案的（修复前直接建档的存量），就诊识别跳过，
登记筛查与批量识别把新行记成已纳管而不是疑似。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2359 纳管卫生院", "org_type": "township", "level": "township"}).json()["id"]
    return {"org": org, "n": 0}


def _enrolled_patient(client, admin, world, *, legacy=False):
    """直接签约建档的在管患者；legacy=True 模拟修复前的存量（池里没有行）。"""
    from app.spd.models import SpdCandidate

    world["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2359 患者{world['n']}", "id_card": f"33077719650505{world['n']:04d}", "gender": "女",
        "birth_date": "1965-05-05"}).json()["id"]
    got = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": world["org"]})
    assert got.status_code == 201, got.text
    if legacy:
        with SessionLocal() as db:
            db.query(SpdCandidate).filter(SpdCandidate.patient_id == patient).delete()
            db.commit()
    return patient


def _candidate(patient):
    from app.spd.models import SpdCandidate

    with SessionLocal() as db:
        row = db.query(SpdCandidate).filter(SpdCandidate.patient_id == patient,
                                            SpdCandidate.program_code == "hypertension").first()
        return (row.status, row.source) if row else None


def test_直接签约建档_池里记一行已纳管(client, admin, world):
    patient = _enrolled_patient(client, admin, world)
    assert _candidate(patient) == ("enrolled", "enrollment")   # 修前 None


def test_开了就诊识别_在管患者不再被插成疑似(client, admin, world, monkeypatch):
    from app.config import settings

    patient = _enrolled_patient(client, admin, world, legacy=True)
    monkeypatch.setattr(settings, "spd_auto_identify_on_encounter", True)
    resp = client.post("/api/encounters", headers=admin, json={
        "patient_id": patient, "org_id": world["org"], "encounter_type": "outpatient",
        "diagnosis_code": "I10", "diagnosis_name": "原发性高血压"})
    assert resp.status_code == 201, resp.text
    assert _candidate(patient) is None   # 修前 ("suspect", "event")


def test_登记筛查判为疑似_在管患者记成已纳管(client, admin, world):
    patient = _enrolled_patient(client, admin, world, legacy=True)
    client.post("/api/encounters", headers=admin, json={
        "patient_id": patient, "org_id": world["org"], "encounter_type": "outpatient",
        "diagnosis_code": "I10", "diagnosis_name": "原发性高血压"})
    got = client.post(f"{B}/screenings", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": world["org"], "source": "active"})
    assert got.status_code in (200, 201), got.text
    assert got.json()["result"] == "suspect"            # 筛查结论照记
    assert _candidate(patient)[0] == "enrolled"          # 修前 suspect
