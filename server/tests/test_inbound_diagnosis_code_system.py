"""入站诊断编码按编码系统挑 ICD-10（P2-1080，第三十一批「编码体系与术语」扫描 C4-5）。

对接规范写 Condition.code 是 ICD-10，FHIR 入站的 docstring 也写「coding[0].code→diagnosis_code（ICD-10）」——取的却是
第一条 coding，不看 system；HL7 的 DG1-3 取第一组件，不看 DG1-3.3 编码系统，也不看 4～6 的备用三元组。上游把 SNOMED /
本地码排在前面时，`38341003` 当 ICD-10 落库、再按 ICD-10 导出，慢专病按诊断编码认不出高血压。修后 FHIR 挑 system 是
ICD-10 的那条，HL7 在主三元组明写了别的编码系统、备用三元组是 ICD-10 时取备用的。一条 ICD-10 都没有时照旧取第一条
（丢弃、透传还是拒收待裁定，与 P2-744 一并定）。
"""
import pytest

from app.database import SessionLocal
from app.models import Encounter
from app.spd.platform import diagnosis_codes

SNOMED = {"system": "http://snomed.info/sct", "code": "38341003", "display": "Hypertensive disorder"}
ICD10 = {"system": "http://hl7.org/fhir/sid/icd-10", "code": "I10", "display": "原发性高血压"}
WARD = "P21080病区"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21080 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21080 患者", "id_card": "330281196501012080"}).json()
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": WARD}).json()
    for bed_no in ("1", "2"):
        assert client.post("/api/inpatient/beds", headers=admin,
                           json={"ward_id": ward["id"], "bed_no": bed_no}).status_code == 201
    return {"org": org, "patient": patient}


def _fhir(client, admin, world, codings, text=""):
    resp = client.post("/api/integration/fhir/Encounter", headers=admin, json={
        "resourceType": "Encounter", "status": "finished", "class": {"code": "AMB"},
        "subject": {"reference": f"Patient/{world['patient']['ehc_no']}"},
        "serviceProvider": {"reference": f"Organization/{world['org']}"},
        "reasonCode": [{"coding": codings, **({"text": text} if text else {})}]})
    assert resp.status_code == 201, resp.text
    with SessionLocal() as db:
        enc = db.get(Encounter, resp.json()["encounter_id"])
        return enc.diagnosis_code, enc.diagnosis_name


def test_FHIR_SNOMED排在前面_落的是ICD10那条(client, admin, world):
    assert _fhir(client, admin, world, [SNOMED, ICD10]) == ("I10", "原发性高血压")   # 修前 ("38341003", "Hypertensive disorder")
    with SessionLocal() as db:
        assert "I10" in diagnosis_codes(db, world["patient"]["id"])


def test_FHIR_没有ICD10或没写system的照旧取第一条(client, admin, world):
    assert _fhir(client, admin, world, [SNOMED])[0] == "38341003"   # 待裁定，照旧
    assert _fhir(client, admin, world, [{"code": "J06.9"}, {"code": "J00"}], text="上感") == ("J06.9", "上感")


def _a01(client, admin, world, bed_no, dg1):
    message = "\r".join([
        f"MSH|^~\\&|HIS|XZYY|MEDPLAT|COUNTY|20260930090000||ADT^A01|P21080-{bed_no}|P|2.4",
        f"PID|1||33028119650{bed_no}012080^^^CN^ID||P21080 住院{bed_no}||19650101|M",
        f"PV1|1|I|{WARD}^1^{bed_no}||||1001^王^主任",
        dg1,
    ])
    resp = client.post("/api/integration/hl7v2/adt", headers=admin, json={"message": message})
    assert resp.status_code == 201, resp.text
    (row,) = [e for e in client.get(f"/api/encounters?patient_id={resp.json()['patient']['id']}", headers=admin).json()
              if e["encounter_type"] == "inpatient"]
    return row["diagnosis_code"], row["diagnosis_name"]


def test_HL7_DG1主三元组是SNOMED_取备用的ICD10(client, admin, world):
    got = _a01(client, admin, world, "1", "DG1|1||38341003^Hypertensive disorder^SCT^I10^原发性高血压^I10")
    assert got == ("I10", "原发性高血压")   # 修前 ("38341003", "Hypertensive disorder")


def test_HL7_DG1没写编码系统的照旧取第一组件(client, admin, world):
    assert _a01(client, admin, world, "2", "DG1|1||I10^高血压") == ("I10", "高血压")
