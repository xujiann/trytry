"""告知书挂到某次就诊上：就诊得在、得是同一个人；挂上的才进这次就诊的文书完整度（P2-161）。

就诊文书完整度（`/api/outpatient/encounters/{id}/completeness`）按「挂在本次就诊上的告知书」数「告知书 / 待签署」，
说明写「真正该追的是'待签'」。门急诊文书页生成告知书的表单却从不挂——两格恒为 0；接口对挂到哪次就诊也不查，
挂到别人的就诊上照收，别人的告知书就进了这次就诊的「待签署」。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2161 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p2161_doc", "password": "passw0rd1", "role": "doctor", "org_id": org})
    assert created.status_code in (200, 201), created.text
    token = client.post("/api/auth/login", json={"username": "p2161_doc", "password": "passw0rd1"}).json()["access_token"]
    mine, other = (client.post("/api/patients", headers=admin, json={
        "name": f"P2161 患者{i}", "id_card": f"33010619880808{i:04d}"}).json()["id"] for i in (1, 2))
    encounter = client.post("/api/encounters", headers=admin, json={
        "patient_id": mine, "org_id": org, "diagnosis_name": "急性胃肠炎"}).json()["id"]
    return {"org": org, "doctor": {"Authorization": f"Bearer {token}"}, "mine": mine, "other": other,
            "encounter": encounter}


def _consent(client, world, patient, related_id):
    return client.post("/api/outpatient/consents", headers=world["doctor"], json={
        "patient_id": patient, "org_id": world["org"], "consent_type": "treatment", "title": "静脉输液知情告知",
        "content": "输液可能出现静脉炎、过敏反应……", "related_type": "encounter", "related_id": related_id})


def test_挂到本次就诊的进完整度(client, world):
    assert _consent(client, world, world["mine"], world["encounter"]).status_code == 201
    done = client.get(f"/api/outpatient/encounters/{world['encounter']}/completeness", headers=world["doctor"]).json()
    assert (done["consents_total"], done["consents_pending"]) == (1, 1)


def test_挂到别人的就诊上拒收_就诊不存在拒收(client, world):
    wrong = _consent(client, world, world["other"], world["encounter"])
    assert wrong.status_code == 422, wrong.text   # 修前 201：别人的告知书进了这次就诊的「待签署」
    assert "不是同一人" in wrong.json()["detail"]
    missing = _consent(client, world, world["mine"], 99999999)
    assert missing.status_code == 404, missing.text
    done = client.get(f"/api/outpatient/encounters/{world['encounter']}/completeness", headers=world["doctor"]).json()
    assert done["consents_total"] == 1


def test_编号0照旧当不挂(client, world):
    assert _consent(client, world, world["mine"], 0).status_code == 201
