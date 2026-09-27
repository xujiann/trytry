"""FHIR 入站不收作废态的资源（P2-626，第十三批「正向 vs 逆向」扫描 Q1-6）。

Observation / Encounter / DiagnosticReport 三处入站原先都不读 `status`：来源系统发现录错、发同一资源
`status=entered-in-error`（或 `cancelled`），平台当新数据再记一遍——随访多一次、慢病分级按错值升不回来、就诊人次多一条
（还触发慢专病的就诊识别）、检查报告照样出具（带危急值标记的还走危急值闭环）。出站一侧早用 status 表达修订
（final / amended），入站完全忽略这一字段。

修法：作废态（entered-in-error / cancelled）一律 422 拒收、交换日志记失败；撤回既往数据属人工更正，不在入站里自动冲销。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2626 对接卫生院", "org_type": "township", "level": "township"}).json()
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2626 对接患者", "id_card": "330281199001012626", "gender": "男", "birth_date": "1990-01-01"}).json()
    chronic = client.post("/api/chronic", headers=admin, json={
        "patient_id": patient["id"], "disease": "hypertension", "managed_by_org_id": org["id"]}).json()
    return {"org": org, "patient": patient, "chronic": chronic}


def _observation(world, status):
    return {
        "resourceType": "Observation", "status": status,
        "code": {"coding": [{"system": "http://loinc.org", "code": "85354-9"}]},
        "subject": {"reference": f"Patient/{world['patient']['ehc_no']}"},
        "component": [
            {"code": {"coding": [{"system": "http://loinc.org", "code": "8480-6"}]}, "valueQuantity": {"value": 182}},
            {"code": {"coding": [{"system": "http://loinc.org", "code": "8462-4"}]}, "valueQuantity": {"value": 112}},
        ],
    }


def _encounter(world, status):
    return {
        "resourceType": "Encounter", "status": status, "class": {"code": "AMB"},
        "subject": {"reference": f"Patient/{world['patient']['ehc_no']}"},
        "serviceProvider": {"reference": f"Organization/{world['org']['id']}"},
        "reasonCode": [{"coding": [{"code": "I10"}], "text": "原发性高血压"}],
    }


def _failures(client, admin, message_type):
    logs = client.get(f"/api/integration/exchange-logs?message_type={message_type}", headers=admin).json()["logs"]
    return [log for log in logs if not log["success"]]


@pytest.mark.parametrize("status", ["entered-in-error", "cancelled"])
def test_作废态的观测拒收_不再记一次随访(client, admin, world, status):
    before = len(client.get(f"/api/chronic/{world['chronic']['id']}/followups", headers=admin).json())
    got = client.post("/api/integration/fhir/Observation", headers=admin, json=_observation(world, status))
    assert got.status_code == 422, got.text   # 修前 201：按错值再记一次随访、分级升到高危
    assert status in got.json()["detail"]
    assert len(client.get(f"/api/chronic/{world['chronic']['id']}/followups", headers=admin).json()) == before
    assert any(status in log["error_detail"] for log in _failures(client, admin, "fhir_observation"))


def test_作废态的就诊拒收_不多一条就诊(client, admin, world):
    before = len(client.get(f"/api/encounters?patient_id={world['patient']['id']}", headers=admin).json())
    got = client.post("/api/integration/fhir/Encounter", headers=admin, json=_encounter(world, "cancelled"))
    assert got.status_code == 422, got.text   # 修前 201
    assert len(client.get(f"/api/encounters?patient_id={world['patient']['id']}", headers=admin).json()) == before


def test_作废态的报告拒收_申请单不变成已报告(client, admin, world):
    request = client.post("/api/exams", headers=admin, json={
        "patient_id": world["patient"]["id"], "from_org_id": world["org"]["id"], "center_type": "imaging",
        "item_code": "P2626CT", "item_name": "P2626 胸部CT"}).json()
    got = client.post("/api/integration/fhir/DiagnosticReport", headers=admin, json={
        "resourceType": "DiagnosticReport", "status": "entered-in-error",
        "basedOn": [{"reference": f"ServiceRequest/{request['id']}"}], "conclusion": "录错的结论",
        "extension": [{"url": "urn:medplat:critical", "valueBoolean": True}]})
    assert got.status_code == 422, got.text   # 修前 201：报告出具、危急值闭环启动
    reported = client.get("/api/exams?status=reported", headers=admin).json()
    assert all(row["id"] != request["id"] for row in reported)


def test_正常状态照旧入站(client, admin, world):
    assert client.post("/api/integration/fhir/Observation", headers=admin,
                       json=_observation(world, "final")).status_code == 201
    assert client.post("/api/integration/fhir/Encounter", headers=admin,
                       json=_encounter(world, "finished")).status_code == 201
    no_status = _encounter(world, "finished")
    no_status.pop("status")
    assert client.post("/api/integration/fhir/Encounter", headers=admin, json=no_status).status_code == 201
