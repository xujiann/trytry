"""死因报告卡与导出的「签发人」写姓名，与死亡证明打印件同一句（P2-566，第十一批「导出 / 打印 vs 清单」扫描 Y1-1）。

打印件用 `printing._user_name`（姓名，没填的退回账号）；单张报告卡与 CSV 导出取的却是 `username`——这份 CSV 是拿去照着
手工网报的法定字段，誊录进去的是医生的登录账号。修后两处与打印件同一句。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2566 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    heads = {}
    for username, full_name in (("p2566_wang", "王建国"), ("p2566_bare", "")):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "pass123456", "role": "doctor", "org_id": org,
            "full_name": full_name})
        assert created.status_code == 201, created.text
        token = client.post("/api/auth/login", json={"username": username, "password": "pass123456"}).json()
        heads[username] = {"Authorization": f"Bearer {token['access_token']}"}
    certs = {}
    for n, username in enumerate(heads):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P2566 亡故者{n}", "id_card": f"33012719400101256{n}", "gender": "男"}).json()["id"]
        cert = client.post("/api/certs", headers=heads[username], json={
            "cert_type": "death", "name": f"P2566 亡故者{n}", "gender": "男", "event_date": "2026-09-01",
            "detail": "P2566 冠心病", "org_id": org, "patient_id": patient})
        assert cert.status_code == 201, cert.text
        certs[username] = cert.json()
    return certs


def test_报告卡与导出的签发人写姓名_没填姓名的退回账号(client, admin, world):
    card = client.get(f"/api/certs/{world['p2566_wang']['id']}/death-report-card", headers=admin).json()
    assert card["issued_by"] == "王建国"   # 修前 p2566_wang（打印件上是王建国）
    bare = client.get(f"/api/certs/{world['p2566_bare']['id']}/death-report-card", headers=admin).json()
    assert bare["issued_by"] == "p2566_bare"
    csv = client.get("/api/certs/death-report-cards/export.csv", headers=admin).text
    line = next(row for row in csv.splitlines() if world["p2566_wang"]["cert_no"] in row)
    assert ",王建国," in line and "p2566_wang" not in line
