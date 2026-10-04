"""诊间提醒只说「当前有 N 起突发公卫事件处置中，注意相关症状问诊」，不说是哪起、什么病（P2-1435，第四十二批扫描 AF2-9）。

`clinic_reminders` 只取了处置中事件的个数，事件表本身有病种与级别。实测（修前）：处置中的是诺如病毒感染（IV 级），提醒是
「当前有 1 起突发公卫事件处置中，注意相关症状问诊」——医生不知道该问腹泻还是发热。

修法：返回结构不变（仍是一条 `active_ph_event`，两键），detail 列出病种与级别：「突发公卫事件处置中：诺如病毒感染（IV级），
注意相关症状问诊」。级别照事件列表的写法（页面 `${ev.level}级`）；没填病种的印事件名称；多起用顿号隔开、按立案先后倒序
（同事件列表），超过 3 起只列最新 3 起、写「等 N 起」。
"""
import pytest

B = "/api/publichealth"


@pytest.fixture(scope="module")
def patient(client, admin):
    return client.post("/api/patients", headers=admin, json={
        "name": "P21435 患者", "id_card": "330106198001011435", "gender": "男"}).json()["id"]


def _event(client, admin, title, level, disease_name=""):
    resp = client.post(f"{B}/events", headers=admin, json={"title": title, "level": level, "disease_name": disease_name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _ph_rows(client, admin, patient):
    body = client.get(f"{B}/reminders/{patient}", headers=admin).json()
    return [r for r in body["reminders"] if r["type"] == "active_ph_event"]


def test_处置中的事件列出病种与级别_一起两起四起(client, admin, patient):
    assert _ph_rows(client, admin, patient) == []   # 没有处置中的事件不提示
    _event(client, admin, "乙校诺如聚集", "IV", "诺如病毒感染")
    assert _ph_rows(client, admin, patient) == [   # 修前「当前有 1 起突发公卫事件处置中，注意相关症状问诊」
        {"type": "active_ph_event", "detail": "突发公卫事件处置中：诺如病毒感染（IV级），注意相关症状问诊"}]
    _event(client, admin, "甲镇学校聚集性发热", "I", "流感")
    assert _ph_rows(client, admin, patient) == [   # 两起：顿号隔开，新立的在前
        {"type": "active_ph_event", "detail": "突发公卫事件处置中：流感（I级）、诺如病毒感染（IV级），注意相关症状问诊"}]
    _event(client, admin, "丙村不明原因皮疹聚集", "III")   # 没填病种：印事件名称
    fourth = _event(client, admin, "丁镇手足口聚集", "II", "手足口病")
    assert _ph_rows(client, admin, patient) == [   # 四起：只列最新 3 起，写「等 4 起」
        {"type": "active_ph_event",
         "detail": "突发公卫事件处置中：手足口病（II级）、丙村不明原因皮疹聚集（III级）、流感（I级）等 4 起，注意相关症状问诊"}]
    assert client.post(f"{B}/events/{fourth}/close", headers=admin).status_code == 200
    assert _ph_rows(client, admin, patient) == [   # 结案的不列；正好 3 起不写「等」
        {"type": "active_ph_event",
         "detail": "突发公卫事件处置中：丙村不明原因皮疹聚集（III级）、流感（I级）、诺如病毒感染（IV级），注意相关症状问诊"}]
