"""村医停用要被尊重（第十五批「停用对象仍被引用」扫描 S1-4）。

①村医档案与账号都停用之后，一次 PATCH `active=true` 就恢复启用：出绑定码、进考核对象，人却登录不了。团队成员恢复
在岗早已查账号（P2-313），新建村医也查（P1-106），只有这一处没跟上。修后照 P2-313：账号停用即 409。
②村医档案停用（账号仍在用）的人，建档照挂成签约村医、照记签约积分；别处一律把停用村医当「已回收」（不出绑定码、
不进考核对象、工作台不数）。修后有村医档案且已停用的 404「村医已停用」；没有村医档案的账号照旧不强求（存量档案里有）。
"""
import itertools

import pytest

B = "/api/spd"
_CARDS = itertools.count(1)


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "S1-4 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    users = {}
    for name in ("gone", "retired", "plain"):
        resp = client.post("/api/users", headers=admin, json={
            "username": f"s14_{name}", "password": "pass123456", "role": "doctor", "org_id": org})
        assert resp.status_code == 201, resp.text
        users[name] = resp.json()["id"]
    profiles = {}
    for name in ("gone", "retired"):
        resp = client.post(f"{B}/village-doctors", headers=admin, json={"user_id": users[name], "org_id": org})
        assert resp.status_code == 201, resp.text
        profiles[name] = resp.json()["id"]
        assert client.patch(f"{B}/village-doctors/{profiles[name]}", headers=admin,
                            json={"active": False}).status_code == 200
    resp = client.patch(f"/api/users/{users['gone']}/status", headers=admin, json={"status": "disabled"})
    assert resp.status_code == 200, resp.text
    return {"org": org, "users": users, "profiles": profiles}


def _enroll(client, admin, world, village_doctor):
    n = next(_CARDS)
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"S1-4 患者{n}", "id_card": f"33019219650101{1400 + n:04d}"}).json()["id"]
    return client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": world["org"],
        "village_doctor_id": village_doctor})


def test_账号停用的村医档案不能恢复启用(client, admin, world):
    resp = client.patch(f"{B}/village-doctors/{world['profiles']['gone']}", headers=admin, json={"active": True})
    assert resp.status_code == 409, resp.text                                        # 修前 200
    assert resp.json() == {"detail": "村医账号已停用，不能恢复启用"}
    # 账号在用的照常恢复；只改别的字段不受影响
    assert client.patch(f"{B}/village-doctors/{world['profiles']['gone']}", headers=admin,
                        json={"village": "东村"}).status_code == 200


def test_村医档案停用的人不再挂成新档案的签约村医(client, admin, world):
    resp = _enroll(client, admin, world, world["users"]["retired"])
    assert resp.status_code == 404, resp.text                                        # 修前 201、村医积分 +5
    assert resp.json() == {"detail": f"村医已停用（village_doctor_id={world['users']['retired']}）"}
    # 没有村医档案的账号照旧不强求
    assert _enroll(client, admin, world, world["users"]["plain"]).status_code == 201


def test_村医档案恢复启用之后照常挂(client, admin, world):
    resp = client.patch(f"{B}/village-doctors/{world['profiles']['retired']}", headers=admin, json={"active": True})
    assert resp.status_code == 200, resp.text   # 账号在用：照常恢复
    assert _enroll(client, admin, world, world["users"]["retired"]).status_code == 201
