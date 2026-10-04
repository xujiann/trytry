"""门急诊护理记录不填护士即整条无署名（P2-1308，第三十八批扫描 AB3-7）。

同一张 `nursing_records` 的住院入口（`clinical_docs.create_nursing_record`）写 `body.nurse_name or user.full_name`，住院体征的
记录人同样缺省取登录人；门急诊入口（`outpatient_docs.create_outpatient_nursing`）原先照收 `body.nurse_name`——页面上护士
一栏选填，不填就存空串，清单里一律「护士 —」，出参又不带录入账号，这条护理记录谁记的无从看起。P2-480 让门诊护理只能由
医师 / 公卫来写，护士一栏更常空着。实测：不填护士 201、`nurse_name ''`。

修法：照住院入口缺省取登录人的姓名；填了照存填的。门急诊处置的执行人可能不是录入人，不改它的缺省。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21308 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    assert client.post("/api/users", headers=admin, json={
        "username": "p21308_doc", "password": "passw0rd1", "full_name": "P21308 门诊医师", "role": "doctor",
        "org_id": org}).status_code in (200, 201)
    token = client.post("/api/auth/login", json={"username": "p21308_doc", "password": "passw0rd1"}).json()
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21308 患者", "id_card": "330106197404081305"}).json()["id"]
    encounter = client.post("/api/encounters", headers=admin, json={
        "patient_id": patient, "org_id": org, "diagnosis_name": "急性上呼吸道感染"})
    assert encounter.status_code in (200, 201), encounter.text
    return {"encounter": encounter.json()["id"], "doctor": {"Authorization": f"Bearer {token['access_token']}"}}


def _nursing(client, world, **extra):
    resp = client.post(f"/api/outpatient/encounters/{world['encounter']}/nursing-records", headers=world["doctor"],
                       json={"content": "输液观察，滴速 40 滴/分", **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"], resp.json()["nurse_name"]


def _listed(client, world, record_id):
    rows = client.get(f"/api/outpatient/encounters/{world['encounter']}/nursing-records", headers=world["doctor"]).json()
    return next(r["nurse_name"] for r in rows if r["id"] == record_id)


def test_不填护士_存登录人的姓名(client, world):
    record_id, nurse = _nursing(client, world)
    assert nurse == "P21308 门诊医师"   # 修前 ''
    assert _listed(client, world, record_id) == "P21308 门诊医师"


def test_填了护士_照存填的(client, world):
    record_id, nurse = _nursing(client, world, nurse_name="P21308 护士")
    assert nurse == "P21308 护士"
    assert _listed(client, world, record_id) == "P21308 护士"
