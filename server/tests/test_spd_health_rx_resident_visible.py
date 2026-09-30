"""健康处方：管理端开具页写「居民端『干预』页可见」，居民端却没有任何接口读得到它，也收不到消息（P2-1051，第三十批「居民端 vs
管理端」扫描 D4-2）。

`care.create_health_prescription` 开具（用药指导 / 康复训练 / 日常健康管理），管理端能按患者查；居民端 `my_interventions` 只查
干预方案表，m.js 里一处调用都没有，居民令牌调业务接口 401。医生以为开出的指导居民手机上看得到。

修法：居民端补只读清单（本人与代管家属同口径、读留痕），开具时发一条站内消息，「干预」页并列显示。
"""
import pytest

B = "/api/spd"
P = "/api/portal/spd"


def _resident(client, phone, name, id_card):
    code = client.post("/api/portal/auth/sms/code", json={"phone": phone}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    bound = client.post("/api/portal/auth/realname", headers=headers, json={"name": name, "id_card": id_card})
    assert bound.status_code in (200, 409), bound.text
    return headers


@pytest.fixture(scope="module")
def world(client, admin):
    people = []
    for i, (phone, card) in enumerate((("13900010510", "330199198502021051"), ("13900010511", "330199198603031051"))):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P21051 居民{i}", "id_card": card, "gender": "男", "birth_date": "1985-02-02", "phone": phone}).json()
        people.append({**patient, "headers": _resident(client, phone, patient["name"], card)})
    rx = client.post(f"{B}/health-prescriptions", headers=admin, json={
        "patient_id": people[0]["id"], "program_code": "hypertension",
        "drug_advice": "氨氯地平 5mg 每日一次，晨起服", "life_advice": "每日食盐不超过5克"})
    assert rx.status_code == 201, rx.text
    return {"people": people, "rx": rx.json()["id"]}


def test_居民本人在干预页读得到健康处方(client, world):
    me = world["people"][0]
    got = client.get(f"{P}/health-prescriptions", headers=me["headers"])
    assert got.status_code == 200, got.text   # 修前 404（没有这条接口）
    (rx,) = got.json()
    assert (rx["id"], rx["drug_advice"], rx["life_advice"], rx["program_name"]) == (
        world["rx"], "氨氯地平 5mg 每日一次，晨起服", "每日食盐不超过5克", "高血压")


def test_别人按患者号读不到(client, world):
    me, other = world["people"]
    got = client.get(f"{P}/health-prescriptions", headers=other["headers"], params={"patient_id": me["id"]})
    assert got.status_code in (403, 404), got.text
    assert client.get(f"{P}/health-prescriptions", headers=other["headers"]).json() == []


def test_开具时居民收到站内消息(client, world):
    me = world["people"][0]
    rows = client.get("/api/portal/me/notifications", headers=me["headers"]).json()
    rows = rows["items"] if isinstance(rows, dict) else rows
    assert any(n.get("category") == "spd_health_rx" and n.get("link_id") == world["rx"] for n in rows), rows   # 修前一条都没有
