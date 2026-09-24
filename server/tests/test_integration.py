"""M3-M4 对接适配层：HL7 v2 / FHIR R4 入站转换与出站导出。"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def admin_headers(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def operator_headers(client, admin_headers):
    client.post(
        "/api/users",
        json={"username": "op_hl7", "password": "hl7pass123", "role": "operator"},
        headers=admin_headers,
    )
    resp = client.post("/api/auth/login", json={"username": "op_hl7", "password": "hl7pass123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def doctor_headers(client, admin_headers):
    client.post(
        "/api/users",
        json={"username": "doc_hl7", "password": "docpass123", "role": "doctor"},
        headers=admin_headers,
    )
    resp = client.post("/api/auth/login", json={"username": "doc_hl7", "password": "docpass123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


HL7_ADT = (
    "MSH|^~\\&|HIS|XZYY|MEDPLAT|COUNTY|20260810120000||ADT^A01|MSG0001|P|2.4\n"
    "PID|1||320981199001011111^^^^ID||王^建国||19900101|M|||||13899998888\n"
)


def test_hl7v2_adt_creates_patient(client, operator_headers):
    resp = client.post(
        "/api/integration/hl7v2/patient", json={"message": HL7_ADT}, headers=operator_headers
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["created"] is True
    patient = data["patient"]
    assert patient["name"] == "王建国"
    # H1 整改：对接层回显对非 admin 角色统一脱敏
    assert patient["id_card"] == "3209**********1111"
    assert patient["gender"] == "男"
    assert patient["birth_date"] == "1990-01-01"
    assert patient["phone"] == "138******88"
    assert patient["ehc_no"].startswith("EHC")

    # EMPI 幂等：重复推送同一身份证号不重复建档
    again = client.post(
        "/api/integration/hl7v2/patient", json={"message": HL7_ADT}, headers=operator_headers
    )
    assert again.json()["created"] is False
    assert again.json()["patient"]["ehc_no"] == patient["ehc_no"]


def test_入站超长证件号_不再让患者列表整个500(client, operator_headers, admin_headers):
    """P1-65：`PatientOut` 继承了 `PatientCreate.id_card` 的 15–18 位约束，而 HL7/FHIR 入站只查
    "至少 15 位"、列能存 256 位。修复前：20 位证件号的患者**已经建进库**，接口却回 422
    "消息解析失败"（交换日志记成失败）；此后 `GET /api/patients` 对所有人 500。
    证件号该不该收非 15/18 位是对接口径（见 docs/待裁定事项清单.md），这里只管不 500。"""
    msg = (
        "MSH|^~\\&|HIS|XZYY|MEDPLAT|COUNTY|20260924120000||ADT^A04|MSG-P165|P|2.4\n"
        "PID|1||33028119900101123456^^^^ID||超长证件号患者||19900101|M\n"
    )
    hl7 = client.post("/api/integration/hl7v2/patient", json={"message": msg}, headers=operator_headers)
    assert hl7.status_code == 201, hl7.text
    fhir = client.post(
        "/api/integration/fhir/Patient",
        json={"resourceType": "Patient", "identifier": [{"value": "3302811990010112345678"}],
              "name": [{"text": "FHIR超长证件号"}]},
        headers=operator_headers,
    )
    assert fhir.status_code == 201, fhir.text
    listed = client.get("/api/patients", headers=admin_headers)
    assert listed.status_code == 200, listed.text[:200]
    assert {"超长证件号患者", "FHIR超长证件号"} <= {p["name"] for p in listed.json()}


def test_hl7v2_rejects_malformed_message(client, operator_headers):
    no_pid = "MSH|^~\\&|HIS|XZYY|MEDPLAT|COUNTY|20260810||ADT^A01|M2|P|2.4"
    resp = client.post(
        "/api/integration/hl7v2/patient", json={"message": no_pid}, headers=operator_headers
    )
    assert resp.status_code == 422

    no_msh = "PID|1||320981199001012222^^^^ID||测试||19900101|F"
    resp = client.post(
        "/api/integration/hl7v2/patient", json={"message": no_msh}, headers=operator_headers
    )
    assert resp.status_code == 422


def test_integration_requires_operator_or_admin(client, doctor_headers):
    resp = client.post(
        "/api/integration/hl7v2/patient", json={"message": HL7_ADT}, headers=doctor_headers
    )
    assert resp.status_code == 403


def test_fhir_patient_inbound(client, operator_headers):
    resource = {
        "resourceType": "Patient",
        "identifier": [{"system": "urn:oid:2.16.156.10011.1.3", "value": "320981198507073333"}],
        "name": [{"text": "刘芳"}],
        "gender": "female",
        "birthDate": "1985-07-07",
        "telecom": [{"system": "phone", "value": "13711112222"}],
    }
    resp = client.post("/api/integration/fhir/Patient", json=resource, headers=operator_headers)
    assert resp.status_code == 201
    patient = resp.json()["patient"]
    assert patient["name"] == "刘芳"
    assert patient["gender"] == "女"
    assert patient["birth_date"] == "1985-07-07"
    # H1 整改：非 admin 角色电话脱敏
    assert patient["phone"] == "137******22"

    bad = client.post(
        "/api/integration/fhir/Patient",
        json={"resourceType": "Observation"},
        headers=operator_headers,
    )
    assert bad.status_code == 422


def test_fhir_observation_creates_followup(client, admin_headers, operator_headers):
    # 准备：机构 + 高血压慢病档案
    org = client.post(
        "/api/organizations",
        json={"name": "FHIR测试卫生院", "org_type": "township", "level": "township"},
        headers=admin_headers,
    ).json()
    ehc_no = client.get("/api/patients?keyword=刘芳", headers=admin_headers).json()[0]["ehc_no"]
    patient_id = client.get(f"/api/patients/{ehc_no}", headers=admin_headers).json()["id"]
    chronic = client.post(
        "/api/chronic",
        json={"patient_id": patient_id, "disease": "hypertension", "managed_by_org_id": org["id"]},
        headers=admin_headers,
    ).json()

    observation = {
        "resourceType": "Observation",
        "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]},
        "subject": {"reference": f"Patient/{ehc_no}"},
        "component": [
            {
                "code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]},
                "valueQuantity": {"value": 165, "unit": "mmHg"},
            },
            {
                "code": {"coding": [{"system": "http://loinc.org", "code": "8462-4"}]},
                "valueQuantity": {"value": 98, "unit": "mmHg"},
            },
        ],
    }
    resp = client.post(
        "/api/integration/fhir/Observation", json=observation, headers=operator_headers
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["disease"] == "hypertension"
    assert data["values"] == {"sbp": 165.0, "dbp": 98.0}
    assert data["level"] == 3  # 收缩压≥160 → 高危

    # 随访已入档
    followups = client.get(
        f"/api/chronic/{chronic['id']}/followups", headers=admin_headers
    ).json()
    assert followups[0]["sbp"] == 165.0

    unknown = dict(observation, subject={"reference": "Patient/EHCNOTEXIST"})
    assert (
        client.post(
            "/api/integration/fhir/Observation", json=unknown, headers=operator_headers
        ).status_code
        == 404
    )


def test_fhir_patient_export(client, operator_headers, admin_headers):
    ehc_no = client.get("/api/patients?keyword=王建国", headers=admin_headers).json()[0]["ehc_no"]
    resp = client.get(f"/api/integration/fhir/Patient/{ehc_no}", headers=operator_headers)
    assert resp.status_code == 200
    resource = resp.json()
    assert resource["resourceType"] == "Patient"
    assert resource["id"] == ehc_no
    assert resource["gender"] == "male"
    assert resource["birthDate"] == "1990-01-01"
    # H1 整改：operator 导出身份证号必须脱敏（与 /api/patients 同口径）
    assert {"system": "urn:oid:2.16.156.10011.1.3", "value": "3209**********1111"} in resource[
        "identifier"
    ]
    assert not any("320981199001011111" in str(i.get("value", "")) for i in resource["identifier"])
    assert resource["name"][0]["text"] == "王建国"

    # admin 导出保留明文（数据导出场景，审计留痕）
    admin_resource = client.get(
        f"/api/integration/fhir/Patient/{ehc_no}", headers=admin_headers
    ).json()
    assert {"system": "urn:oid:2.16.156.10011.1.3", "value": "320981199001011111"} in admin_resource[
        "identifier"
    ]
