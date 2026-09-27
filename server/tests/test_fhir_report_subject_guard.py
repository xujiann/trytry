"""FHIR DiagnosticReport 入站核 subject 是不是申请单患者本人（P1-195，第十二批「批量 vs 单条」扫描 Z4-2）。

同一类报告走 HL7 ORU 早就按 PID 核本人（P1-143）；走 FHIR 原先 subject 写的是谁都不看：给甲的申请单回传一份
subject 写着乙、带危急值扩展的报告，201，报告连同危急值闭环落到甲名下。
"""
import base64

import pytest

from app.database import SessionLocal
from app.models import ExamReport

URL = "/api/integration/fhir/DiagnosticReport"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1195 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patients = {}
    for tag, card in (("甲", "330127196001011950"), ("乙", "330127196001021950")):
        patients[tag] = client.post("/api/patients", headers=admin, json={
            "name": f"P1195 {tag}", "id_card": card}).json()
    return {"org": org, "patients": patients}


def _request(client, admin, world) -> int:
    resp = client.post("/api/exams", headers=admin, json={
        "patient_id": world["patients"]["甲"]["id"], "from_org_id": world["org"], "center_type": "lab",
        "item_code": "BG", "item_name": "血气分析"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _report(request_id: int, **extra) -> dict:
    return {
        "resourceType": "DiagnosticReport", "status": "final",
        "basedOn": [{"reference": f"ServiceRequest/{request_id}"}],
        "conclusion": "血钾 6.9 mmol/L，危急值",
        "presentedForm": [{"contentType": "text/plain", "data": base64.b64encode("K 6.9 HH".encode()).decode()}],
        "extension": [{"url": "urn:medplat:critical", "valueBoolean": True}],
        **extra,
    }


def _reports_on(request_id: int) -> int:
    with SessionLocal() as db:
        return db.query(ExamReport).filter(ExamReport.request_id == request_id).count()


def test_subject写的是别人_拒收且不落报告(client, admin, world):
    rid = _request(client, admin, world)
    other = world["patients"]["乙"]["ehc_no"]
    resp = client.post(URL, headers=admin, json=_report(rid, subject={"reference": f"Patient/{other}"}))
    assert resp.status_code == 422 and "不一致" in resp.json()["detail"], resp.text   # 修前 201
    assert _reports_on(rid) == 0
    logs = client.get("/api/integration/exchange-logs?message_type=fhir_diagnostic_report", headers=admin).json()
    assert any(not log["success"] and "不一致" in log["error_detail"] for log in logs["logs"])   # 拒收照样留痕


def test_subject是申请单患者本人_照常入站(client, admin, world):
    rid = _request(client, admin, world)
    mine = world["patients"]["甲"]["ehc_no"]
    resp = client.post(URL, headers=admin, json=_report(rid, subject={"reference": f"Patient/{mine}"}))
    assert resp.status_code == 201 and resp.json()["critical"] is True, resp.text


def test_不给subject_照旧入站(client, admin, world):
    rid = _request(client, admin, world)
    assert client.post(URL, headers=admin, json=_report(rid)).status_code == 201   # 与 ORU 的 PID 段一样是可选的


@pytest.mark.parametrize("subject", [{"reference": "Practitioner/1"}, "Patient/EHC1"])
def test_subject写法不对_拒收(client, admin, world, subject):
    rid = _request(client, admin, world)
    resp = client.post(URL, headers=admin, json=_report(rid, subject=subject))
    assert resp.status_code == 422 and "Patient/{ehc_no}" in resp.json()["detail"], resp.text
    assert _reports_on(rid) == 0
