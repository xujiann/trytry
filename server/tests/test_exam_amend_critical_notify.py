"""报告修订为危急值：状态复位「已通知」就得真的通知申请机构（P2-129）。

出具危急值报告时会给申请机构的医师发站内消息、并定向广播；修订（`PATCH /api/exams/reports/{id}`）把一份普通报告
改判为危急值、或修改一份危急值报告的结论时，闭环状态复位为「已通知」、留痕写「复位为已通知」，却一条消息都不发、
也不广播——报告读着「已通知」，申请机构的医师什么都没收到，只能碰巧翻到未确认清单才发现。
"""
import pytest


def _login(client, username, password="passw0rd1"):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def setup(client, admin):
    town = client.post("/api/organizations", headers=admin, json={
        "name": "P2129 卫生院", "org_type": "township", "level": "township"}).json()
    county = client.post("/api/organizations", headers=admin, json={
        "name": "P2129 县医院", "org_type": "lead_hospital", "level": "county"}).json()
    for username, org in (("p2129_doc", town), ("p2129_other", county)):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "full_name": username, "role": "doctor", "org_id": org["id"]})
        assert created.status_code in (200, 201), created.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2129 患者", "id_card": "331782199002021299", "gender": "男"}).json()
    return {"town": town, "patient": patient,
            "doctor": _login(client, "p2129_doc"), "other": _login(client, "p2129_other")}


def _report(client, admin, setup, *, critical):
    req = client.post("/api/exams", headers=admin, json={
        "patient_id": setup["patient"]["id"], "from_org_id": setup["town"]["id"],
        "center_type": "lab", "item_code": "K-P2129", "item_name": "血钾(P2129)"}).json()
    client.post(f"/api/exams/{req['id']}/claim", headers=admin)
    resp = client.post(f"/api/exams/{req['id']}/report", headers=admin, json={
        "finding": "血钾 4.1mmol/L", "conclusion": "未见异常", "critical": critical, "reported_by": "检验科"})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _critical_msgs(client, headers, report_id):
    return [n for n in client.get("/api/notifications", headers=headers).json()
            if n["category"] == "critical_value" and n["link_id"] == report_id]


def test_普通报告改判为危急值_申请机构医师收到消息(client, admin, setup):
    report_id = _report(client, admin, setup, critical=False)
    amended = client.patch(f"/api/exams/reports/{report_id}", headers=setup["doctor"], json={
        "conclusion": "复核：血钾 7.0mmol/L，重度高钾血症", "critical": True, "reason": "复核改判"})
    assert amended.status_code == 200, amended.text
    assert amended.json()["critical_status"] == "notified"
    msgs = _critical_msgs(client, setup["doctor"], report_id)
    assert len(msgs) == 1, msgs   # 修前 0：状态写着「已通知」，一条消息都没有
    assert msgs[0]["title"] == "危急值（报告修订）：血钾(P2129)" and "重度高钾血症" in msgs[0]["body"]
    assert _critical_msgs(client, setup["other"], report_id) == []   # 别家机构的医师照旧收不到


def test_危急值报告改结论_复位已通知时再通知一遍(client, admin, setup):
    report_id = _report(client, admin, setup, critical=True)
    assert len(_critical_msgs(client, setup["doctor"], report_id)) == 1   # 出具时那一条
    client.patch(f"/api/exams/reports/{report_id}", headers=setup["doctor"], json={
        "conclusion": "修订：血钾 7.4mmol/L", "reason": "数值更正"})
    assert len(_critical_msgs(client, setup["doctor"], report_id)) == 2   # 修前仍是 1


def test_解除危急标记不发消息(client, admin, setup):
    report_id = _report(client, admin, setup, critical=True)
    client.patch(f"/api/exams/reports/{report_id}", headers=setup["doctor"], json={
        "conclusion": "复核后正常", "critical": False, "reason": "误报"})
    assert len(_critical_msgs(client, setup["doctor"], report_id)) == 1   # 只有出具时那一条
