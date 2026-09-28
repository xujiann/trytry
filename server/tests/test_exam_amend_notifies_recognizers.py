"""被互认的报告修订为危急值，互认了它、据此免做检查的机构一并收到通知（第十五批「源头更正后派生不跟」扫描 S3-3）。

互认建单只写 `recognized_from_id`，全仓原先没有别处读它；修订（P2-129）只通知源申请机构——甲院出「未见异常」，乙院给
同一患者开单时选了互认、不再另做；随后报告改判为危急值，甲院医师收到消息，乙院医师一条都没有，乙院的申请单仍是
「已互认」。修后乙院医师同样收到（站内消息 + 定向广播，口径与源机构同一套）；「已互认」要不要撤回另行裁定。
"""
import pytest


def _login(client, username):
    resp = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = {}
    for key, name in (("src", "S3-3 甲院"), ("rec", "S3-3 乙院"), ("far", "S3-3 丙院")):
        orgs[key] = client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": "township", "level": "township"}).json()["id"]
        created = client.post("/api/users", headers=admin, json={
            "username": f"s33_{key}", "password": "passw0rd1", "full_name": f"s33_{key}", "role": "doctor",
            "org_id": orgs[key]})
        assert created.status_code in (200, 201), created.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "S3-3 患者", "id_card": "331782199004043301", "gender": "男"}).json()["id"]
    source = client.post("/api/exams", headers=admin, json={
        "patient_id": patient, "from_org_id": orgs["src"], "center_type": "imaging",
        "item_code": "CT-S33", "item_name": "胸部CT(S3-3)"}).json()
    client.post(f"/api/exams/{source['id']}/claim", headers=admin)
    report = client.post(f"/api/exams/{source['id']}/report", headers=admin, json={
        "finding": "双肺纹理清晰", "conclusion": "未见异常", "critical": False, "reported_by": "影像科"})
    assert report.status_code in (200, 201), report.text
    recognized = client.post("/api/exams", headers=admin, json={
        "patient_id": patient, "from_org_id": orgs["rec"], "center_type": "imaging",
        "item_code": "CT-S33", "item_name": "胸部CT(S3-3)", "accept_recognition_of": source["id"]})
    assert recognized.status_code in (200, 201) and recognized.json()["status"] == "recognized", recognized.text
    return {"report": report.json()["id"], "doctors": {k: _login(client, f"s33_{k}") for k in orgs}}


def _critical_msgs(client, headers, report_id):
    return [n for n in client.get("/api/notifications", headers=headers).json()
            if n["category"] == "critical_value" and n["link_id"] == report_id]


def test_改判为危急值_互认方医师同样收到(client, admin, world):
    amended = client.patch(f"/api/exams/reports/{world['report']}", headers=admin, json={
        "conclusion": "右肺上叶占位，建议进一步检查（危急）", "critical": True, "reason": "复核改判"})
    assert amended.status_code == 200, amended.text
    src = _critical_msgs(client, world["doctors"]["src"], world["report"])
    rec = _critical_msgs(client, world["doctors"]["rec"], world["report"])
    assert len(src) == 1 and src[0]["title"] == "危急值（报告修订）：胸部CT(S3-3)"
    assert len(rec) == 1, rec                                                  # 修前 0
    assert rec[0]["title"] == "危急值（互认报告修订）：胸部CT(S3-3)" and "右肺上叶占位" in rec[0]["body"]
    assert _critical_msgs(client, world["doctors"]["far"], world["report"]) == []   # 与这份报告无关的机构照旧收不到
