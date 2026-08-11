"""M11 集成平台底座：交换日志落库、解析异常兜底 422、失败率统计。"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def operator(client, admin):
    client.post(
        "/api/users",
        json={"username": "ex_op", "password": "pass123456", "role": "operator"},
        headers=admin,
    )
    return login(client, "ex_op", "pass123456")


def test_inbound_success_logged_with_source_system(client, operator):
    hl7 = "MSH|^~\\&|HIS|XY|MEDPLAT|County|20260811||ADT^A04|1|P|2.4\nPID|1||320981199001019001||张交换||19900101|M|||||13800009001"
    resp = client.post(
        "/api/integration/hl7v2/patient",
        json={"message": hl7},
        headers={**operator, "X-Source-System": "XY-HIS"},
    )
    assert resp.status_code == 201, resp.text
    logs = client.get("/api/integration/exchange-logs", headers=operator).json()
    assert logs["total"] == 1
    assert logs["failed"] == 0
    entry = logs["logs"][0]
    assert entry["message_type"] == "hl7v2_patient"
    assert entry["source_system"] == "XY-HIS"
    assert entry["success"] is True
    assert entry["direction"] == "inbound"


def test_inbound_validation_failure_logged(client, operator):
    # 缺 PID 段：既有 422 校验路径也须落失败日志（含错误详情）
    resp = client.post(
        "/api/integration/hl7v2/patient",
        json={"message": "MSH|^~\\&|HIS|XY"},
        headers={**operator, "X-Source-System": "XY-HIS"},
    )
    assert resp.status_code == 422
    logs = client.get(
        "/api/integration/exchange-logs?success=false", headers=operator
    ).json()
    assert any("PID" in log["error_detail"] for log in logs["logs"])


def test_unexpected_parse_error_returns_422_and_logged(client, operator):
    # identifier 传字符串（畸形结构）：未预期解析异常 → 422 而非 500，且落日志
    resp = client.post(
        "/api/integration/fhir/Patient",
        json={"resourceType": "Patient", "identifier": "oops", "name": [{"text": "坏结构"}]},
        headers={**operator, "X-Source-System": "BAD-SYS"},
    )
    assert resp.status_code == 422
    assert "交换日志" in resp.json()["detail"]
    logs = client.get(
        "/api/integration/exchange-logs?message_type=fhir_patient", headers=operator
    ).json()
    failure = next(log for log in logs["logs"] if not log["success"])
    assert "解析异常" in failure["error_detail"]
    assert failure["source_system"] == "BAD-SYS"


def test_exchange_failure_rate_stats(client, operator):
    # 前三个用例：成功1、失败2 → 失败率 66.67%
    logs = client.get("/api/integration/exchange-logs", headers=operator).json()
    assert logs["total"] == 3
    assert logs["failed"] == 2
    assert logs["failure_rate_pct"] == round(2 * 100.0 / 3, 2)
    by_type = {r["message_type"]: r for r in logs["by_type"]}
    assert by_type["hl7v2_patient"]["count"] == 2
    assert by_type["hl7v2_patient"]["failed"] == 1
    assert by_type["fhir_patient"]["failed"] == 1

    # 过滤查询：按来源系统
    filtered = client.get(
        "/api/integration/exchange-logs?source_system=BAD-SYS", headers=operator
    ).json()
    assert len(filtered["logs"]) == 1


def test_fhir_observation_success_logged(client, admin, operator):
    # 建慢病档案后 Observation 入站成功 → 日志计入
    patient = client.post(
        "/api/patients",
        json={"name": "观测患者", "id_card": "320981199001019002"},
        headers=admin,
    ).json()
    client.post(
        "/api/chronic",
        json={
            "patient_id": patient["id"],
            "disease": "hypertension",
            "managed_by_org_id": _org(client, admin),
        },
        headers=admin,
    )
    resp = client.post(
        "/api/integration/fhir/Observation",
        json={
            "resourceType": "Observation",
            "subject": {"reference": f"Patient/{patient['ehc_no']}"},
            "component": [
                {"code": {"coding": [{"code": "8480-6"}]}, "valueQuantity": {"value": 128}},
                {"code": {"coding": [{"code": "8462-4"}]}, "valueQuantity": {"value": 82}},
            ],
        },
        headers=operator,
    )
    assert resp.status_code == 201, resp.text
    logs = client.get(
        "/api/integration/exchange-logs?message_type=fhir_observation", headers=operator
    ).json()
    assert any(log["success"] for log in logs["logs"])


def _org(client, admin) -> int:
    orgs = client.get("/api/organizations", headers=admin).json()
    if orgs:
        return orgs[0]["id"]
    return client.post(
        "/api/organizations",
        json={"name": "交换测试机构", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()["id"]
