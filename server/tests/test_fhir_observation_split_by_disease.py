"""FHIR Observation 同时带血压和血糖：按指标归病种、各归各的档案（P2-848，第二十三批「同一患者名下多份并存」扫描 Y3-2）。

`_FIELD_DISEASE` 的注释写的就是「指标 → 慢病病种（用于定位随访归属档案）」，入站也本来就收多个分量；原先却整条挂到
第一个分量的病种：两份档案的患者推一条 150/95 + 血糖 16.7，全部记进高血压档案（血糖 16.7 也存在那里），糖尿病档案
仍是 1 级、随访史为空；分量顺序反过来，血压全进糖尿病档案；只有糖尿病档案的患者推同一条，404「该患者无 hypertension
慢病档案」，血糖一起丢了。修后每个有档案的病种各建一条随访、各自分级；缺档案的在回执 `unfiled` 里点名，不连累其余；
一个都归不了的照旧 404。只报一个病种的回执字节不变。
"""
import pytest

from app.database import SessionLocal
from app.models import ChronicPatient

BP = [{"code": {"coding": [{"code": "8480-6"}]}, "valueQuantity": {"value": 150}},
      {"code": {"coding": [{"code": "8462-4"}]}, "valueQuantity": {"value": 95}}]
GLUCOSE = [{"code": {"coding": [{"code": "15074-8"}]}, "valueQuantity": {"value": 16.7, "code": "mmol/L"}}]


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2848 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    made = {}
    for n, (tag, diseases) in enumerate((("两份", ["hypertension", "diabetes"]), ("两份反序", ["hypertension", "diabetes"]),
                                         ("只有糖尿病", ["diabetes"]), ("没有档案", []))):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P2848 {tag}", "id_card": f"33010219650101284{n}", "gender": "男",
            "birth_date": "1965-01-01"}).json()
        chronic = {}
        for disease in diseases:
            resp = client.post("/api/chronic", headers=admin, json={
                "patient_id": patient["id"], "disease": disease, "managed_by_org_id": org})
            assert resp.status_code in (200, 201), resp.text
            chronic[disease] = resp.json()["id"]
        made[tag] = {"ehc_no": patient["ehc_no"], "chronic": chronic}
    return made


def _observe(client, admin, ehc_no, components):
    return client.post("/api/integration/fhir/Observation", headers=admin, json={
        "resourceType": "Observation", "status": "final", "subject": {"reference": f"Patient/{ehc_no}"},
        "component": components})


def _followups(client, admin, chronic_id):
    return [(f["sbp"], f["dbp"], f["glucose"]) for f in
            client.get(f"/api/chronic/{chronic_id}/followups", headers=admin).json()]


def _level(chronic_id):
    with SessionLocal() as db:
        return db.get(ChronicPatient, chronic_id).level


def test_血压血糖一起报_各归各的档案_各自分级(client, admin, world):
    both = world["两份"]
    resp = _observe(client, admin, both["ehc_no"], BP + GLUCOSE)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["disease"], body["values"]) == ("hypertension", {"sbp": 150.0, "dbp": 95.0}), body
    assert [(o["disease"], o["values"], o["level"]) for o in body["others"]] == [
        ("diabetes", {"glucose": 16.7}, 3)], body   # 修前没有这一份：血糖 16.7 记在高血压档案里
    assert "unfiled" not in body
    assert _followups(client, admin, both["chronic"]["hypertension"]) == [(150, 95, None)]
    assert _followups(client, admin, both["chronic"]["diabetes"]) == [(None, None, 16.7)]   # 修前 []
    assert _level(both["chronic"]["diabetes"]) == 3   # 修前仍是 1 级


def test_分量顺序反过来_归属不变(client, admin, world):
    both = world["两份反序"]
    resp = _observe(client, admin, both["ehc_no"], GLUCOSE + BP)
    assert resp.status_code == 201, resp.text
    assert (resp.json()["disease"], [o["disease"] for o in resp.json()["others"]]) == ("diabetes", ["hypertension"])
    assert _followups(client, admin, both["chronic"]["hypertension"]) == [(150, 95, None)]   # 修前 []：血压全进了糖尿病档案
    assert _followups(client, admin, both["chronic"]["diabetes"]) == [(None, None, 16.7)]


def test_缺一个病种的档案_其余照归_回执点名(client, admin, world):
    only = world["只有糖尿病"]
    resp = _observe(client, admin, only["ehc_no"], BP + GLUCOSE)
    assert resp.status_code == 201, resp.text   # 修前 404「该患者无 hypertension 慢病档案」，血糖一起丢了
    assert (resp.json()["disease"], resp.json()["unfiled"]) == ("diabetes", ["hypertension"]), resp.json()
    assert _followups(client, admin, only["chronic"]["diabetes"]) == [(None, None, 16.7)]


def test_一个都归不了照旧404_只报一个病种的回执不多键(client, admin, world):
    resp = _observe(client, admin, world["没有档案"]["ehc_no"], BP + GLUCOSE)
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"] == "该患者无 hypertension、diabetes 慢病档案，无法归档随访"
    single = _observe(client, admin, world["两份"]["ehc_no"], BP)
    assert single.status_code == 201, single.text
    assert list(single.json()) == ["followup_id", "chronic_id", "disease", "values", "level"]
