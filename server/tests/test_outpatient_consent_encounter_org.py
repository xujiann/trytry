"""别家机构能把告知书挂到本院的就诊上：本院这次就诊的完整度多出一份永远清不掉的「待签署」（P2-452）。

`create_consent` 挂就诊时只查了就诊在、是同一个人（P2-161），不查就诊是不是开告知书这家机构的。实测：乙院医生读甲院
这次就诊的完整度是 403，可以乙院名义开一份告知书、`related_id` 填甲院的就诊，201——甲院完整度里 `consents_pending`
多出 1；甲院的清单按本机构收口列不出它（`?related_id=` 回空），签署 / 拒签按归属校验 403，这份「待签」永远清不掉。
同文件的处置、护理写入挂就诊时都按就诊的机构归属校验。修后：就诊不是开具机构的即 422。
"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = {}
    for tag in ("甲", "乙"):
        orgs[tag] = client.post("/api/organizations", headers=admin, json={
            "name": f"P2452 {tag}院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
        client.post("/api/users", headers=admin, json={
            "username": f"p2452_{'a' if tag == '甲' else 'b'}", "password": "passw0rd1", "role": "doctor",
            "org_id": orgs[tag]})
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2452 门诊患者", "id_card": "330106197008082452"}).json()["id"]
    encounter = client.post("/api/encounters", headers=admin, json={
        "patient_id": patient, "org_id": orgs["甲"], "diagnosis_name": "急性胃肠炎"}).json()["id"]
    return {"orgs": orgs, "patient": patient, "encounter": encounter,
            "a": login(client, "p2452_a", "passw0rd1"), "b": login(client, "p2452_b", "passw0rd1")}


def _consent(client, headers, world, org):
    return client.post("/api/outpatient/consents", headers=headers, json={
        "patient_id": world["patient"], "org_id": org, "consent_type": "treatment", "title": "静脉输液知情告知",
        "content": "输液可能出现静脉炎、过敏反应……", "related_type": "encounter", "related_id": world["encounter"]})


def _pending(client, world):
    done = client.get(f"/api/outpatient/encounters/{world['encounter']}/completeness", headers=world["a"])
    assert done.status_code == 200, done.text
    return done.json()["consents_pending"]


def test_乙院把告知书挂到甲院的就诊上_422_甲院完整度不多出待签(client, world):
    before = _pending(client, world)
    resp = _consent(client, world["b"], world, world["orgs"]["乙"])
    assert resp.status_code == 422, resp.text   # 修前 201
    assert resp.json() == {"detail": "告知书的开具机构与关联的就诊机构不是同一家"}
    assert _pending(client, world) == before     # 修前多出 1，甲院列不出也签不了


def test_本院挂本院的就诊照常进完整度(client, world):
    before = _pending(client, world)
    assert _consent(client, world["a"], world, world["orgs"]["甲"]).status_code == 201
    assert _pending(client, world) == before + 1
