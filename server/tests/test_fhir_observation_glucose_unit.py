"""FHIR 入站血糖按单位折成 mmol/L（P2-639，第十四批「单位与量纲」扫描 R3-1）。

分级阈值按 mmol/L 配（空腹血糖 ≥10.0 三级、≥7.0 二级、≤3.9 低血糖三级），Observation 入站原先不读单位：按 LOINC 2339-0
（质量浓度，mg/dL）上送的 99 mg/dL——约 5.5 mmol/L、正常——当 99 mmol/L 判成三级高危、建议上转，再经采集器进慢专病监测。
修法：mg/dL ÷18 折算；认不出的单位 422、交换日志记失败；不带单位照旧按 mmol/L（对接规范一直这么收）；
另认摩尔浓度的 15074-8 / 14749-6。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2639 对接卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2639 糖尿病患者", "id_card": "330281197502022639", "gender": "女", "birth_date": "1975-02-02"}).json()
    chronic = client.post("/api/chronic", headers=admin, json={
        "patient_id": patient["id"], "disease": "diabetes", "managed_by_org_id": org}).json()
    return {"patient": patient, "chronic": chronic}


def _post(client, admin, world, code, value, unit=None):
    quantity = {"value": value} if unit is None else {"value": value, "unit": unit, "code": unit}
    return client.post("/api/integration/fhir/Observation", headers=admin, json={
        "resourceType": "Observation", "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": code}]},
        "subject": {"reference": f"Patient/{world['patient']['ehc_no']}"},
        "valueQuantity": quantity,
    })


def test_毫克每分升折算成毫摩尔每升_不再判高危(client, admin, world):
    got = _post(client, admin, world, "2339-0", 99, "mg/dL")
    assert got.status_code == 201, got.text
    assert (got.json()["values"], got.json()["level"]) == ({"glucose": 5.5}, 1)   # 修前 {"glucose": 99.0}、3 级
    followups = client.get(f"/api/chronic/{world['chronic']['id']}/followups", headers=admin).json()
    assert followups[0]["glucose"] == 5.5


def test_摩尔浓度编码与单位照收(client, admin, world):
    got = _post(client, admin, world, "15074-8", 8.2, "mmol/L")
    assert got.status_code == 201, got.text   # 修前 422「未识别到支持的观测指标」
    assert (got.json()["values"], got.json()["level"]) == ({"glucose": 8.2}, 2)


def test_不带单位的照旧按毫摩尔每升(client, admin, world):
    got = _post(client, admin, world, "2339-0", 12.0)
    assert got.status_code == 201 and got.json()["values"] == {"glucose": 12.0} and got.json()["level"] == 3


def test_认不出的单位拒收_交换日志记失败(client, admin, world):
    got = _post(client, admin, world, "2339-0", 1.1, "g/L")
    assert (got.status_code, got.json()["detail"]) == (422, "血糖单位 g/L 无法识别：请按 mmol/L 或 mg/dL 上送")
    logs = client.get("/api/integration/exchange-logs?message_type=fhir_observation", headers=admin).json()["logs"]
    assert any(not log["success"] and "g/L" in log["error_detail"] for log in logs)
