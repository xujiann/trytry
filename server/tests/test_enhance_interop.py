"""E9 互操作标准化：FHIR 资源扩展、CDA 导出、省平台上报连接器骨架。"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def _login(client, u, p):
    tok = client.post("/api/auth/login", json={"username": u, "password": p}).json()
    return {"Authorization": f"Bearer {tok['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return _login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def setup(client, admin):
    org = client.post("/api/organizations", json={"name": "互操作县医院", "org_type": "lead_hospital", "level": "county"}, headers=admin).json()
    org2 = client.post("/api/organizations", json={"name": "互操作他院", "org_type": "township", "level": "township"}, headers=admin).json()
    client.post("/api/users", json={"username": "e9_op", "password": "pw123456", "full_name": "对接员", "role": "operator", "org_id": org["id"]}, headers=admin)
    client.post("/api/users", json={"username": "e9_op2", "password": "pw123456", "full_name": "他院对接员", "role": "operator", "org_id": org2["id"]}, headers=admin)
    pat = client.post("/api/patients", json={"name": "互操作居民", "id_card": "330782198501011234"}, headers=admin).json()
    from app.database import SessionLocal
    from app.models import Encounter, Prescription, ExamRequest, ExamReport, Admission, Referral, Ward, Bed
    ids = {}
    with SessionLocal() as db:
        enc = Encounter(patient_id=pat["id"], org_id=org["id"], doctor_name="王医生", diagnosis_code="I10", diagnosis_name="高血压", summary="随诊")
        db.add(enc); db.flush(); ids["enc"] = enc.id
        pres = Prescription(patient_id=pat["id"], org_id=org["id"], diagnosis_name="高血压", status="auto_passed", created_by=1)
        db.add(pres); db.flush(); ids["pres"] = pres.id
        req = ExamRequest(patient_id=pat["id"], from_org_id=org["id"], center_type="lab", item_code="X", item_name="血脂", status="reported", created_by=1)
        db.add(req); db.flush()
        rep = ExamReport(request_id=req.id, conclusion="正常", critical=False)
        db.add(rep); db.flush(); ids["report"] = rep.id
        ward = Ward(org_id=org["id"], name="内科病区"); db.add(ward); db.flush()
        bed = Bed(ward_id=ward.id, bed_no="B1"); db.add(bed); db.flush()
        adm = Admission(patient_id=pat["id"], org_id=org["id"], ward_id=ward.id, bed_id=bed.id, doctor_name="王医生", diagnosis_name="高血压", status="admitted", created_by=1)
        db.add(adm); db.flush(); ids["adm"] = adm.id
        ref = Referral(patient_id=pat["id"], from_org_id=org2["id"], to_org_id=org["id"], direction="up", reason="上级诊治", created_by=1)
        db.add(ref); db.flush(); ids["ref"] = ref.id
        db.commit()
    return {"org": org, "org2": org2, "pat": pat, "ids": ids,
            "op": _login(client, "e9_op", "pw123456"), "op2": _login(client, "e9_op2", "pw123456")}


def test_fhir_resources(client, setup):
    op = setup["op"]; ids = setup["ids"]
    enc = client.get(f"/api/integration/fhir/Encounter/{ids['enc']}", headers=op)
    assert enc.status_code == 200 and enc.json()["resourceType"] == "Encounter"
    cond = client.get(f"/api/integration/fhir/Condition/encounter/{ids['enc']}", headers=op).json()
    assert cond["resourceType"] == "Condition" and cond["code"]["text"] == "高血压"
    med = client.get(f"/api/integration/fhir/MedicationRequest/{ids['pres']}", headers=op).json()
    assert med["resourceType"] == "MedicationRequest"
    dr = client.get(f"/api/integration/fhir/DiagnosticReport/{ids['report']}", headers=op).json()
    assert dr["resourceType"] == "DiagnosticReport" and dr["conclusion"] == "正常"


def test_fhir_bundle_roundtrip(client, setup):
    op = setup["op"]
    ehc = setup["pat"]["ehc_no"]
    b = client.get(f"/api/integration/fhir/Bundle/patient/{ehc}", headers=op).json()
    assert b["resourceType"] == "Bundle" and b["total"] >= 1
    # 入站 Bundle：一条 Patient
    inb = client.post("/api/integration/fhir/Bundle", json={"resourceType": "Bundle", "entry": [
        {"resource": {"resourceType": "Patient", "name": [{"text": "入站居民"}],
                      "identifier": [{"system": "urn:oid:2.16.156.10011.1.3", "value": "330782199003033456"}],
                      "gender": "male", "birthDate": "1990-03-03"}}]}, headers=op)
    assert inb.status_code == 200, inb.text
    assert inb.json()["succeeded"] == 1


def test_cda_export(client, setup):
    op = setup["op"]; ids = setup["ids"]
    d = client.get(f"/api/integration/cda/discharge/{ids['adm']}", headers=op)
    assert d.status_code == 200 and "ClinicalDocument" in d.text and "出院小结" in d.text
    r = client.get(f"/api/integration/cda/referral/{ids['ref']}", headers=op)
    assert r.status_code == 200 and "ClinicalDocument" in r.text


def test_provincial_report(client, setup):
    op = setup["op"]
    r = client.post("/api/integration/provincial/reports", json={"report_type": "infectious", "org_id": setup["org"]["id"], "payload": {"n": 3}}, headers=op)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "acked"
    # 他院对接员为本院上报 → 403
    assert client.post("/api/integration/provincial/reports", json={"report_type": "infectious", "org_id": setup["org"]["id"], "payload": {}}, headers=setup["op2"]).status_code == 403
    lst = client.get(f"/api/integration/provincial/reports?org_id={setup['org']['id']}", headers=op).json()
    assert len(lst) >= 1
