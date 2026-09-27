"""病案首页、出院小结两张打印件先判可见性、再判业务状态（P2-565，第十一批「导出 / 打印 vs 清单」扫描 Y1-4）。

两张打印件原先先回业务状态（病案首页 404「未填写」、出院小结 409「尚未出院」），之后才 `assert_patient_visible`；它们镜像的
明细 `GET /api/inpatient/admissions/{id}/case-summary` 是先判可见性的（`_admission_visible_or_404`）。于是与患者毫无关系的
别院医生按住院号挨个调，就能读出别院每一次住院在不在院、首页填没填——不留调阅痕迹。修后与明细同序：不存在 404，
看不见 403，看得见的再说状态。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2565 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    stranger_org = client.post("/api/organizations", headers=admin, json={
        "name": "P2565 丙卫生院", "org_type": "township", "level": "township"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2565 病区"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P2565-1"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2565 患者", "id_card": "330127196001012565"}).json()["id"]
    adm = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "肺炎"})
    assert adm.status_code == 201, adm.text
    created = client.post("/api/users", headers=admin, json={
        "username": "p2565_doc", "password": "pass123456", "role": "doctor", "org_id": stranger_org})
    assert created.status_code == 201, created.text
    token = client.post("/api/auth/login", json={"username": "p2565_doc", "password": "pass123456"}).json()
    return {"adm": adm.json()["id"], "stranger": {"Authorization": f"Bearer {token['access_token']}"}}


@pytest.mark.parametrize("path", ["case-summaries", "discharge-summaries"])
def test_别院医生调打印件先得403_读不出在不在院_首页填没填(client, world, path):
    resp = client.get(f"/api/print/{path}/{world['adm']}", headers=world["stranger"])
    assert resp.status_code == 403, resp.text   # 修前：病案首页 404「未填写」、出院小结 409「尚未出院」
    # 与镜像的明细同一个口径
    detail = client.get(f"/api/inpatient/admissions/{world['adm']}/case-summary", headers=world["stranger"])
    assert detail.status_code == 403


@pytest.mark.parametrize("path, status, word", [("case-summaries", 404, "未填写"),
                                                  ("discharge-summaries", 409, "尚未出院")])
def test_看得见的照旧先说状态(client, admin, world, path, status, word):
    resp = client.get(f"/api/print/{path}/{world['adm']}", headers=admin)
    assert resp.status_code == status and word in resp.json()["detail"]
