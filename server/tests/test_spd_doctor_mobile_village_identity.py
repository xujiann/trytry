"""医生移动端的「是不是村医」认在用村医档案或团队村医角色（P2-1339，第三十九批「辖区与归属」扫描 AC2-4）。

「谁是村医」原先两套判据：建档自动填签约村医（P1-217；签约积分、随访积分随档案上的签约村医记）、考核对象、运行中枢
村医数看在用的村医档案；医生移动端（`workbench/doctor-mobile` 的 `is_village_doctor`，页头与「我的患者」按它分支）只看
团队成员角色，而成员角色缺省是「医生」。有在用村医档案、进团队时没改角色的村医：本人签约的档案签约村医记成他、签约
积分照拿，手机上却不是村医视角——页头数「在管患者」0、「我的患者」按责任医生筛成 0 条（扫描实测）。

修后移动端认「有在用村医档案，或团队角色是村医」：只放宽、不收窄——只有团队村医角色、没有档案的照旧是村医视角；
村医档案停用了、团队角色是医生的照旧不是。
"""
import pytest

B = "/api/spd"


def _login(client, username):
    token = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    town = client.post("/api/organizations", headers=admin, json={
        "name": "P21339 甲镇卫生院", "org_type": "township", "level": "township"}).json()["id"]
    village = client.post("/api/organizations", headers=admin, json={
        "name": "P21339 杨庄村卫生室", "org_type": "village", "level": "village", "parent_id": town}).json()["id"]
    team = client.post(f"{B}/teams", headers=admin, json={
        "name": "P21339 杨庄团队", "org_id": village, "level": "village", "program_codes": ["hypertension"]})
    assert team.status_code == 201, team.text
    users, headers = {}, {}
    for key in ("profile", "team", "retired"):
        made = client.post("/api/users", headers=admin, json={
            "username": f"p21339_{key}", "password": "passw0rd1", "full_name": f"p21339_{key}", "role": "doctor",
            "org_id": village})
        assert made.status_code in (200, 201), made.text
        users[key] = made.json()["id"]
        headers[key] = _login(client, f"p21339_{key}")
    # 村医甲：有在用村医档案，进团队时角色用了缺省的「医生」
    profile = client.post(f"{B}/village-doctors", headers=admin, json={
        "user_id": users["profile"], "org_id": village, "township": "甲镇", "village": "杨庄村"})
    assert profile.status_code == 201, profile.text
    # 村医乙：团队里是「村医」，没有村医档案
    # 丙：村医档案已停用（已回收），团队角色是缺省的「医生」
    retired = client.post(f"{B}/village-doctors", headers=admin, json={
        "user_id": users["retired"], "org_id": village, "township": "甲镇", "village": "杨庄村"})
    assert retired.status_code == 201, retired.text
    stopped = client.patch(f"{B}/village-doctors/{retired.json()['id']}", headers=admin, json={"active": False})
    assert stopped.status_code == 200, stopped.text
    for key, role in (("profile", None), ("team", "village_doctor"), ("retired", None)):
        body = {"user_id": users[key]} | ({"member_role": role} if role else {})
        joined = client.post(f"{B}/teams/{team.json()['id']}/members", headers=admin, json=body)
        assert joined.status_code == 201, joined.text
    return {"village": village, "users": users, "headers": headers}


def _sign(client, admin, world, key, n, **extra):
    """本人签约建档一位居民，返回档案。"""
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P21339 居民{n}", "id_card": f"33010219640404133{n}"}).json()["id"]
    served = client.post("/api/encounters", headers=world["headers"][key], json={
        "patient_id": patient, "org_id": world["village"]})
    assert served.status_code in (200, 201), served.text
    enrolled = client.post(f"{B}/enrollments", headers=world["headers"][key], json={
        "patient_id": patient, "program_code": "hypertension", **extra})
    assert enrolled.status_code == 201, enrolled.text
    return enrolled.json()


def _mobile(client, world, key):
    """移动端工作台按 is_village_doctor 分支出的页头（doctor.js 同一句）与「我的患者」清单。"""
    wb = client.get(f"{B}/workbench/doctor-mobile", headers=world["headers"][key])
    assert wb.status_code == 200, wb.text
    me, patients = wb.json()["user"], wb.json()["patients"]
    header = ("签约居民", patients["village"]) if me["is_village_doctor"] else ("在管患者", patients["mine"])
    mine = f"village_doctor_id={me['id']}" if me["is_village_doctor"] else f"doctor_user_id={me['id']}"
    listed = client.get(f"{B}/enrollments?limit=30&{mine}", headers=world["headers"][key])
    assert listed.status_code == 200, listed.text
    return me, header, [e["patient_id"] for e in listed.json()]


def test_有在用村医档案_团队角色是缺省医生的_移动端是村医视角(client, admin, world):
    signed = _sign(client, admin, world, "profile", 1)
    assert signed["village_doctor_id"] == world["users"]["profile"]   # 建档自动填签约村医认的是村医档案（P1-217）
    me, header, mine = _mobile(client, world, "profile")
    assert me["is_village_doctor"] is True   # 修前 False
    assert header == ("签约居民", 1)          # 修前 ('在管患者', 0)
    assert mine == [signed["patient_id"]]    # 修前 0 条
    assert (me["township"], me["village"]) == ("甲镇", "杨庄村")   # 页头「辖区」随村医视角显示


def test_只有团队村医角色的照旧是村医视角(client, admin, world):
    signed = _sign(client, admin, world, "team", 2, village_doctor_id=world["users"]["team"])
    me, header, mine = _mobile(client, world, "team")
    assert me["is_village_doctor"] is True
    assert header == ("签约居民", 1)
    assert mine == [signed["patient_id"]]


def test_村医档案已停用_团队角色是医生的照旧不是村医视角(client, admin, world):
    me, header, _ = _mobile(client, world, "retired")
    assert me["is_village_doctor"] is False
    assert header == ("在管患者", 0)
