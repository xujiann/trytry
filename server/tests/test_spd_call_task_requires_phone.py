"""档案没留电话的患者「转呼叫」422，不建空号的待呼叫（P2-881，第二十四批「通知、提醒与待办」扫描 Z2-10）。

`create_call_task` 请求体与档案都没有号码时照样建一条 `phone=''` 的待呼叫，回执 `{'accepted': True, 'note': '待人工外呼'}`，页面提示
「已转呼叫」，待呼叫台账里号码为空、谁也拨不出去；同一患者的宣教短信没号码则如实置失败（「患者档案没有手机号」）。电话补上
之后再转呼叫，又被这条空号任务挡住 409「该患者对同一对象已有待呼叫任务」。修后没有号码 422，不建占位的待呼叫。
"""
from app.database import SessionLocal
from app.models import Patient

B = "/api/spd"


def test_没有电话_422不建待呼叫_补上之后照常转呼叫(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2881 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2881 患者", "id_card": "330102195501012881"}).json()["id"]   # 不留电话
    rule = client.post(f"{B}/followup-rules", headers=admin, json={
        "code": "P2881_FR", "name": "P2881 随访", "points": [0]})
    assert rule.status_code == 201, rule.text
    plan = client.post(f"{B}/followup-plans", headers=admin, json={
        "patient_id": patient, "rule_id": rule.json()["id"], "org_id": org})
    assert plan.status_code in (200, 201), plan.text
    record = plan.json()["items"][0]["id"]
    body = {"patient_id": patient, "ref_type": "followup", "ref_id": record}

    got = client.post(f"{B}/call-tasks", headers=admin, json=body)
    assert got.status_code == 422, got.text   # 修前 201 {'phone': '', 'dispatch': {'accepted': True, 'note': '待人工外呼'}}
    assert "没有电话" in got.json()["detail"]

    with SessionLocal() as db:   # 档案补上电话（经 ORM 写，加密开关开着也照常）
        db.get(Patient, patient).phone = "13800002881"
        db.commit()
    got = client.post(f"{B}/call-tasks", headers=admin, json=body)
    assert got.status_code == 201, got.text   # 修前 409：被那条空号的待呼叫挡住
    assert got.json()["phone"] == "13800002881"
