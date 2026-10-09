"""FHIR Observation 取编码优先取 `http://loinc.org` 的那条 coding（P2-1764，第五十二批扫描 AP2-10）。

`_do_fhir_observation` 的 `loinc_code` 原先只取第一条带 code 的 coding、不看 `coding.system`：HIS 把本地码排在 LOINC
前面（`[{"system": "urn:local:his", "code": "SBP"}, {"system": "http://loinc.org", "code": "8480-6"}]`）的合法观测，修前实测
422「未识别到支持的观测指标（血压/血糖 LOINC）」。Encounter 早就按 coding.system 挑 ICD-10（P2-1080），对接规范写的是 LOINC。

修法：优先取 system 为 `http://loinc.org` 的 coding，没有再退回第一条；只有 LOINC 的照旧。
"""
import pytest

LOINC = "http://loinc.org"
LOCAL = "urn:local:his"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21764 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21764 慢病患者", "id_card": "330102196501011764", "gender": "男", "birth_date": "1965-01-01"}).json()
    for disease in ("hypertension", "diabetes"):
        resp = client.post("/api/chronic", headers=admin, json={
            "patient_id": patient["id"], "disease": disease, "managed_by_org_id": org})
        assert resp.status_code in (200, 201), resp.text
    return {"ehc_no": patient["ehc_no"]}


def _observe(client, admin, world, **body):
    subject = {"reference": f"Patient/{world['ehc_no']}"}
    return client.post("/api/integration/fhir/Observation", headers=admin, json={
        "resourceType": "Observation", "status": "final", "subject": subject, **body})


def _component(codings, value):
    return {"code": {"coding": codings}, "valueQuantity": {"value": value}}


def test_本地码排在LOINC前面_照收(client, admin, world):
    resp = _observe(client, admin, world, component=[
        _component([{"system": LOCAL, "code": "SBP"}, {"system": LOINC, "code": "8480-6"}], 150),
        _component([{"system": LOCAL, "code": "DBP"}, {"system": LOINC, "code": "8462-4"}], 95)])
    assert resp.status_code == 201, resp.text   # 修前 422「未识别到支持的观测指标」
    assert (resp.json()["disease"], resp.json()["values"]) == ("hypertension", {"sbp": 150.0, "dbp": 95.0})


def test_顶层code同样优先取LOINC(client, admin, world):
    resp = _observe(client, admin, world, code={"coding": [{"system": LOCAL, "code": "GLU"},
                                                           {"system": LOINC, "code": "15074-8"}]},
                    valueQuantity={"value": 6.1, "code": "mmol/L"})
    assert resp.status_code == 201, resp.text   # 修前 422
    assert (resp.json()["disease"], resp.json()["values"]) == ("diabetes", {"glucose": 6.1})


def test_只有LOINC或没写system的照旧(client, admin, world):
    only_loinc = _observe(client, admin, world, component=[_component([{"system": LOINC, "code": "8480-6"}], 138),
                                                         _component([{"code": "8462-4"}], 88)])
    assert only_loinc.status_code == 201, only_loinc.text
    assert only_loinc.json()["values"] == {"sbp": 138.0, "dbp": 88.0}
    local_only = _observe(client, admin, world, component=[_component([{"system": LOCAL, "code": "SBP"}], 150)])
    assert (local_only.status_code, local_only.json()) == (422, {"detail": "未识别到支持的观测指标（血压/血糖 LOINC）"})
