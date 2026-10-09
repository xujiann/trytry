"""FHIR Encounter 入站认 `diagnosis[].condition` 指向的内联 Condition，按规范 / 平台出站写法送来的诊断不再丢空
（P2-1763，第五十二批扫描 AP2-5）。

《接口对接规范》映射表写的是「Encounter + Condition」：diagnosis_code→内联 Condition.code、diagnosis_name→Condition.code.text，
平台自己出站（`fhir_encounter_resource`）也是内联 Condition + `diagnosis[0].condition=#dx`。入站原先只读 `reasonCode`
（规范里根本没提）——修前实测：平台登记一次诊断为 I10「原发性高血压」的就诊，出站后原样 POST 回来，201，新行是
`('outpatient', '张医生', '', '')`：就诊识别、按编码的统计与质控都认不出这次就诊。

修法：入站同时读 `diagnosis[].condition` 指向的内联 Condition（按 P2-1080 挑 ICD-10 那条 coding，text 当名称），reasonCode
照旧支持，两处都有时以 diagnosis 为准；规范写明入站认哪两处。
"""
import pytest

from app.database import SessionLocal
from app.models import Encounter
from app.routers.integration import fhir_encounter_resource

ICD10 = "http://hl7.org/fhir/sid/icd-10"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21763 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21763 门诊患者", "id_card": "330102196001011763"}).json()
    return {"org": org, "patient": patient}


def _post(client, admin, resource):
    resp = client.post("/api/integration/fhir/Encounter", headers=admin, json=resource)
    assert resp.status_code == 201, resp.text
    with SessionLocal() as db:
        encounter = db.get(Encounter, resp.json()["encounter_id"])
        return encounter.diagnosis_code, encounter.diagnosis_name


def _resource(world, **extra):
    return {"resourceType": "Encounter", "status": "finished", "class": {"code": "AMB"},
            "subject": {"reference": f"Patient/{world['patient']['ehc_no']}"},
            "serviceProvider": {"reference": f"Organization/{world['org']}"}, **extra}


def _condition(cid, codings, text=""):
    return {"resourceType": "Condition", "id": cid, "code": {"coding": codings, **({"text": text} if text else {})}}


def test_平台出站的Encounter原样回灌_诊断不丢(client, admin, world):
    created = client.post("/api/encounters", headers=admin, json={
        "patient_id": world["patient"]["id"], "org_id": world["org"], "doctor_name": "张医生",
        "encounter_type": "outpatient", "diagnosis_code": "I10", "diagnosis_name": "原发性高血压"})
    assert created.status_code == 201, created.text
    with SessionLocal() as db:
        outbound = fhir_encounter_resource(db.get(Encounter, created.json()["id"]), world["patient"]["ehc_no"])
    assert "reasonCode" not in outbound and outbound["diagnosis"] == [{"condition": {"reference": "#dx"}}]
    outbound.pop("id")
    assert _post(client, admin, outbound) == ("I10", "原发性高血压")   # 修前 ("", "")


def test_只带reasonCode的照旧(client, admin, world):
    resource = _resource(world, reasonCode=[{"coding": [{"system": ICD10, "code": "J06.9"}], "text": "急性上呼吸道感染"}])
    assert _post(client, admin, resource) == ("J06.9", "急性上呼吸道感染")


def test_两处都有以diagnosis为准_内联Condition按system挑ICD10(client, admin, world):
    resource = _resource(
        world,
        contained=[_condition("c1", [{"system": "http://snomed.info/sct", "code": "44054006"},
                                     {"system": ICD10, "code": "E11.9", "display": "2型糖尿病"}])],
        diagnosis=[{"condition": {"reference": "#c1"}, "rank": 1}],
        reasonCode=[{"coding": [{"system": ICD10, "code": "R73.9"}], "text": "高血糖"}])
    assert _post(client, admin, resource) == ("E11.9", "2型糖尿病")   # 修前取 reasonCode：("R73.9", "高血糖")


def test_diagnosis指不到内联Condition的_回落reasonCode(client, admin, world):
    resource = _resource(
        world,
        contained=[_condition("other", [{"system": ICD10, "code": "I10"}], "原发性高血压")],
        diagnosis=[{"condition": {"reference": "Condition/123"}}, {"condition": {"reference": "#missing"}}],
        reasonCode=[{"coding": [{"system": ICD10, "code": "K29.7"}], "text": "胃炎"}])
    assert _post(client, admin, resource) == ("K29.7", "胃炎")


def test_只带diagnosis_第一条没有编码的取下一条(client, admin, world):
    resource = _resource(
        world,
        contained=[{"resourceType": "Condition", "id": "empty", "code": {}},
                   _condition("dx2", [{"system": ICD10, "code": "I25.1"}], "冠心病")],
        diagnosis=[{"condition": {"reference": "#empty"}}, {"condition": {"reference": "#dx2"}}])
    assert _post(client, admin, resource) == ("I25.1", "冠心病")   # 修前 ("", "")
