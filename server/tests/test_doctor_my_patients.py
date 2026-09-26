"""医生移动端「我的患者」不是本人的患者（P2-373）。

慢专病页签的「我的患者」调 `/api/spd/enrollments?limit=30`，不带任何筛选：列出来的是可见机构最新 30 份在管档案——
多半是别的医生的患者；页头「在管患者」却是本人是责任医生的计数（`patients.mine`），两个数对不上。村医的页头恒 0
（签约居民记在 `village_doctor_id` 上，计数在 `patients.village`，页面从来不显示）。

修法：列表按本人筛（村医按签约村医、其余按责任医生），页头显示同一口径的计数。村医签约居民的档案挂在别家机构时
列表仍按可见机构筛（权限口径，另行登记待裁定）。
"""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "app" / "static" / "m" / "doctor.js").read_text(encoding="utf-8")


def _body(name):
    start = SRC.index(f"async function {name}(")
    return SRC[start:SRC.index("\n}\n", start)]


def test_我的患者按本人筛():
    body = _body("loadSpdPatients")
    call = re.search(r'api\(`/api/spd/enrollments\?[^`]*`\)', body)
    assert call, body
    assert "${mine}" in call.group(0), call.group(0)   # 修前 "/api/spd/enrollments?limit=30"
    assert "village_doctor_id=${me.id}" in body and "doctor_user_id=${me.id}" in body, body


def test_页头计数与列表同一口径():
    body = _body("loadSpdTab")
    assert "wb.patients.village" in body and "wb.patients.mine" in body, body   # 修前只有 mine，村医恒 0
    assert "spdMe = wb.user" in body


def test_按责任医生筛只出本人的档案(client, admin):
    """后端既有的筛选口径（页面靠它）：记下来，防后端筛选键被改名而页面静默拿到全量。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2373 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    doctors = []
    for name in ("p2373_a", "p2373_b"):
        got = client.post("/api/users", headers=admin, json={
            "username": name, "password": "passw0rd1", "role": "doctor", "full_name": name, "org_id": org})
        assert got.status_code == 201, got.text
        doctors.append(got.json()["id"])
    for n, doctor in enumerate(doctors):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P2373 患者{n}", "id_card": f"33010219690303{n:03d}3"}).json()["id"]
        got = client.post("/api/spd/enrollments", headers=admin, json={
            "patient_id": patient, "program_code": "hypertension", "org_id": org, "doctor_user_id": doctor})
        assert got.status_code == 201, got.text
    rows = client.get(f"/api/spd/enrollments?limit=30&doctor_user_id={doctors[0]}", headers=admin).json()
    assert {r["doctor_user_id"] for r in rows} == {doctors[0]} and len(rows) == 1
