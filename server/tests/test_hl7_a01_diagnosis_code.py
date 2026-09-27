"""HL7 A01 入院不再丢掉 DG1 的 ICD 编码（P2-632，第十三批「同一事实多入口」扫描 Q2-4）。

DG1-3 是 `编码^名称`，原先只取名称：入院那条住院就诊上没有诊断编码——按诊断编码匹配的慢专病识别
（`spd.platform.diagnosis_codes`）、诊断编码必填 / 字典校验的数据质控、FHIR 出站的 reasonCode 都认不出这次住院；
同一件事走 FHIR Encounter 入站是带编码的。修法：住院登记收一个可选的诊断编码（住院表不改，记在入院那条住院就诊上），
A01 把 DG1 的编码传进去；就诊登记事件带上它。
"""
import pytest

WARD = "P2633病区"


@pytest.fixture(scope="module")
def ward(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2633 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": WARD}).json()
    for bed_no in ("1", "2", "3"):
        assert client.post("/api/inpatient/beds", headers=admin,
                           json={"ward_id": ward["id"], "bed_no": bed_no}).status_code == 201
    return ward


def _a01(client, admin, id_card, bed_no, dg1):
    message = "\r".join([
        f"MSH|^~\\&|HIS|XZYY|MEDPLAT|COUNTY|20260927090000||ADT^A01|P2633-{bed_no}|P|2.4",
        f"PID|1||{id_card}^^^CN^ID||P2633 住院{bed_no}||19700101|M",
        f"PV1|1|I|{WARD}^1^{bed_no}||||1001^王^主任",
        dg1,
    ])
    resp = client.post("/api/integration/hl7v2/adt", headers=admin, json={"message": message})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _inpatient_encounter(client, admin, patient_id):
    (row,) = [e for e in client.get(f"/api/encounters?patient_id={patient_id}", headers=admin).json()
              if e["encounter_type"] == "inpatient"]
    return row


def test_A01的诊断编码记到住院就诊上(client, admin, ward):
    body = _a01(client, admin, "330281197001012633", "1", "DG1|1||I10^高血压")
    row = _inpatient_encounter(client, admin, body["patient"]["id"])
    assert (row["diagnosis_code"], row["diagnosis_name"]) == ("I10", "高血压")   # 修前 ("", "高血压")

    from app.database import SessionLocal
    from app.spd.platform import diagnosis_codes
    with SessionLocal() as db:
        assert "I10" in diagnosis_codes(db, body["patient"]["id"])   # 慢专病按诊断编码识别认得出这次住院


def test_DG1只有编码_名称回落编码(client, admin, ward):
    body = _a01(client, admin, "330281197001022633", "2", "DG1|1||E11")
    row = _inpatient_encounter(client, admin, body["patient"]["id"])
    assert (row["diagnosis_code"], row["diagnosis_name"]) == ("E11", "E11")


def test_住院登记接口同样收编码_住院回执形状不变(client, admin, ward):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2633 手工入院", "id_card": "330281197001032633"}).json()["id"]
    beds = client.get(f"/api/inpatient/beds?ward_id={ward['id']}", headers=admin).json()
    bed = next(b for b in beds if b["bed_no"] == "3")
    resp = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": ward["id"], "bed_id": bed["id"],
        "diagnosis_name": "慢性肾病", "diagnosis_code": "N18"})
    assert resp.status_code == 201, resp.text
    assert "diagnosis_code" not in resp.json()   # 住院表不存编码，回执照旧
    row = _inpatient_encounter(client, admin, patient)
    assert (row["diagnosis_code"], row["diagnosis_name"]) == ("N18", "慢性肾病")
