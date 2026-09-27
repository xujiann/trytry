"""用血申请人能审批自己的申请：路由写着「申请/审批分离」，只靠角色把医师与管理层分开（P2-505，第九批扫描视角外所见）。

`POST /api/blood/requests/{id}/review` 挂着 `require_roles("director")` 与注释「用血审批=管理层（申请/审批分离）」，却从不比对
申请人：既能申请又能审批的账号——平台管理员，或被授了两个权限点的自定义角色——自己申请、自己批，200。同一仓库里手术审批
（「申请人不得自批——职责分离」）、双通道 / 特病申报（P2-398 / P2-399）早就比对了。

修法：审批人是申请人本人即 403，与手术审批同一句。
"""


def test_申请人不能审批自己的用血申请_别人照常审(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2505 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2505 患者", "id_card": "330127196612122505"}).json()["id"]
    body = {"patient_id": patient, "org_id": org, "blood_type": "O", "component": "rbc", "quantity_ml": 400}
    mine = client.post("/api/blood/requests", headers=admin, json=body)
    assert mine.status_code == 201, mine.text
    self_review = client.post(f"/api/blood/requests/{mine.json()['id']}/review?approve=true", headers=admin)
    assert self_review.status_code == 403 and "本人" in self_review.json()["detail"], self_review.text   # 修前 200

    assert client.post("/api/users", headers=admin, json={
        "username": "p2505_doc", "password": "passw0rd1", "full_name": "P2505 医师", "role": "doctor",
        "org_id": org}).status_code in (200, 201)
    token = client.post("/api/auth/login", json={"username": "p2505_doc", "password": "passw0rd1"}).json()
    doctor = {"Authorization": f"Bearer {token['access_token']}"}
    theirs = client.post("/api/blood/requests", headers=doctor, json=body)
    assert theirs.status_code == 201, theirs.text
    reviewed = client.post(f"/api/blood/requests/{theirs.json()['id']}/review?approve=true", headers=admin)
    assert reviewed.status_code == 200 and reviewed.json()["status"] == "approved", reviewed.text
