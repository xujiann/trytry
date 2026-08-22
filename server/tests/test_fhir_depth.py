"""工程包 I1：FHIR 深度——DiagnosticReport/Encounter 入站、批量导出的增量水位与幂等。"""
import base64
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.config import settings
from app.database import SessionLocal
from app.main import app

FHIR_OUT = Path(settings.upload_dir) / "fhir_out"


@pytest.fixture(scope="module")
def client():
    reset_database()
    # 上一轮测试残留的导出文件会干扰断言：水位在库里（每模块重建），文件在磁盘上
    shutil.rmtree(FHIR_OUT, ignore_errors=True)
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
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "FHIR深度卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def patient(client, admin):
    return client.post(
        "/api/patients",
        json={"name": "冯深度", "id_card": "330281199001015023", "gender": "男",
              "birth_date": "1990-01-01", "phone": "13800005555"},
        headers=admin,
    ).json()


# ---------------------------------------------------------------------------
# 入站：DiagnosticReport / Encounter
# ---------------------------------------------------------------------------


def test_fhir_diagnostic_report_inbound(client, admin, org, patient):
    request = client.post(
        "/api/exams",
        json={"patient_id": patient["id"], "from_org_id": org["id"], "center_type": "imaging",
              "item_code": "CT01", "item_name": "胸部CT"},
        headers=admin,
    ).json()
    resource = {
        "resourceType": "DiagnosticReport",
        "status": "final",
        "basedOn": [{"reference": f"ServiceRequest/{request['id']}"}],
        "conclusion": "右肺上叶磨玻璃结节，建议随访",
        "presentedForm": [
            {"contentType": "text/plain",
             "data": base64.b64encode("右肺上叶见 6mm 磨玻璃结节".encode()).decode()}
        ],
        "extension": [{"url": "urn:medplat:critical", "valueBoolean": False}],
    }
    resp = client.post("/api/integration/fhir/DiagnosticReport", json=resource, headers=admin)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["request_id"] == request["id"] and body["critical"] is False
    assert body["request_status"] == "reported"
    # presentedForm 的 base64 内容解到 finding
    reported = client.get("/api/exams?status=reported", headers=admin).json()
    assert any(r["id"] == request["id"] for r in reported)
    # 一单一报告：重复回传 409
    assert (
        client.post("/api/integration/fhir/DiagnosticReport", json=resource, headers=admin).status_code
        == 409
    )
    # 交换日志留证：首次成功与重复回传的 409 都在（成败双留痕）
    logs = client.get(
        "/api/integration/exchange-logs?message_type=fhir_diagnostic_report", headers=admin
    ).json()
    assert any(log["success"] for log in logs["logs"])
    assert any(not log["success"] and "409" in log["error_detail"] for log in logs["logs"])


def test_fhir_diagnostic_report_rejects_bad_reference(client, admin):
    base = {"resourceType": "DiagnosticReport", "conclusion": "x"}
    assert (
        client.post(
            "/api/integration/fhir/DiagnosticReport",
            json={**base, "basedOn": [{"reference": "ServiceRequest/999999"}]},
            headers=admin,
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/integration/fhir/DiagnosticReport",
            json={**base, "basedOn": [{"reference": "Patient/1"}]},
            headers=admin,
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/integration/fhir/DiagnosticReport", json={"resourceType": "Patient"}, headers=admin
        ).status_code
        == 422
    )


def test_fhir_encounter_inbound(client, admin, org, patient):
    resource = {
        "resourceType": "Encounter",
        "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB"},
        "subject": {"reference": f"Patient/{patient['ehc_no']}"},
        "serviceProvider": {"reference": f"Organization/{org['id']}"},
        "reasonCode": [{"coding": [{"system": "icd-10", "code": "J06.9"}], "text": "急性上呼吸道感染"}],
        "participant": [{"individual": {"display": "赵医师"}}],
    }
    resp = client.post("/api/integration/fhir/Encounter", json=resource, headers=admin)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["patient_id"] == patient["id"] and body["encounter_type"] == "outpatient"
    encounters = client.get(f"/api/encounters?patient_id={patient['id']}", headers=admin).json()
    enc = next(e for e in encounters if e["id"] == body["encounter_id"])
    assert enc["diagnosis_code"] == "J06.9" and enc["diagnosis_name"] == "急性上呼吸道感染"
    assert enc["doctor_name"] == "赵医师"
    # 患者不存在 404 / class 不支持 422
    assert (
        client.post(
            "/api/integration/fhir/Encounter",
            json={**resource, "subject": {"reference": "Patient/EHCNOTEXIST"}},
            headers=admin,
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/integration/fhir/Encounter",
            json={**resource, "class": {"code": "EMER"}},
            headers=admin,
        ).status_code
        == 422
    )


# ---------------------------------------------------------------------------
# 批量导出：增量水位推进与幂等
# ---------------------------------------------------------------------------


def _manifest_entries():
    path = FHIR_OUT / "manifest.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _run_export():
    from app.jobs import fhir_batch_export

    with SessionLocal() as db:
        return fhir_batch_export(db)


def test_batch_export_watermark_and_idempotency(client, admin, org, patient):
    affected, summary = _run_export()
    assert affected > 0, summary
    entries = _manifest_entries()
    by_type = {e["resource_type"]: e for e in entries}
    assert {"Patient", "Encounter", "DiagnosticReport"} <= set(by_type)

    # NDJSON 行数与 manifest 一致，且资源序列化符合映射表
    patient_file = FHIR_OUT / by_type["Patient"]["file"]
    lines = [json.loads(ln) for ln in patient_file.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == by_type["Patient"]["rows"]
    me = next(r for r in lines if r["id"] == patient["ehc_no"])
    assert me["resourceType"] == "Patient" and me["name"][0]["text"] == "冯深度"
    assert {"system": "urn:oid:2.16.156.10011.1.3", "value": "330281199001015023"} in me["identifier"]

    report_file = FHIR_OUT / by_type["DiagnosticReport"]["file"]
    report = json.loads(report_file.read_text(encoding="utf-8").splitlines()[0])
    assert report["resourceType"] == "DiagnosticReport"
    assert report["conclusion"] == "右肺上叶磨玻璃结节，建议随访"
    assert base64.b64decode(report["presentedForm"][0]["data"]).decode() == "右肺上叶见 6mm 磨玻璃结节"
    assert report["extension"] == [{"url": "urn:medplat:critical", "valueBoolean": False}]

    enc_file = FHIR_OUT / by_type["Encounter"]["file"]
    enc = [json.loads(ln) for ln in enc_file.read_text(encoding="utf-8").splitlines()]
    outpatient = next(e for e in enc if e["class"]["code"] == "AMB")
    assert outpatient["contained"][0]["code"]["text"] == "急性上呼吸道感染"

    # 水位存 system_params 且推进到最大主键
    params = {p["key"]: p["value"] for p in client.get("/api/mgmt/params", headers=admin).json()}
    assert int(params["fhir_export_wm_patient"]) == by_type["Patient"]["to_id"]

    # 幂等：无增量再跑不产文件、水位不动
    affected2, summary2 = _run_export()
    assert affected2 == 0 and "无增量" in summary2
    assert len(_manifest_entries()) == len(entries)

    # 新增 1 名患者 → 只导出这 1 条增量
    client.post(
        "/api/patients",
        json={"name": "增量患者", "id_card": "330281199505057012"},
        headers=admin,
    )
    affected3, _ = _run_export()
    assert affected3 == 1
    new_entries = _manifest_entries()
    assert len(new_entries) == len(entries) + 1
    delta = new_entries[-1]
    assert delta["resource_type"] == "Patient" and delta["rows"] == 1
