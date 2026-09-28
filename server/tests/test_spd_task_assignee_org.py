"""显式指定的慢专病任务责任人得能以任务所属机构的名义办它（第十六批「通知收件人」扫描 T2-1 显式部分）。

办任务的每个写接口都经 `_load_task` 要求「能以任务所属机构的名义写入」；建任务、单条转派、批量分配却只查责任人在不在、
停没停用。实测（修前）：甲院的任务派给乙院医生 201 / 200——乙院医生打开这条任务 403，甲院同事认领 409（已有责任人），
这条任务谁都办不了；催办消息带着患者姓名发进了乙院。修后：单条建 / 转派 422 说明原因，批量分配逐条跳过并写明；
全域角色（县级中心）照常可派。系统替人挑责任人（档案主管医生在别家机构）那一类另行裁定。
"""
import pytest

B = "/api/spd"


def _login(client, username):
    token = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = {k: client.post("/api/organizations", headers=admin, json={
        "name": f"T2-1 {k}院", "org_type": "township", "level": "township"}).json()["id"] for k in ("甲", "乙")}
    users = {}
    for key, org, role in (("a1", "甲", "doctor"), ("b1", "乙", "doctor"), ("dir", "乙", "director")):
        r = client.post("/api/users", headers=admin, json={
            "username": f"t21_{key}", "password": "passw0rd1", "full_name": f"t21_{key}", "role": role,
            "org_id": orgs[org]})
        assert r.status_code in (200, 201), r.text
        users[key] = r.json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "T2-1 患者", "id_card": "330281197604042219"}).json()["id"]
    return {"orgs": orgs, "users": users, "patient": patient}


def _task(client, admin, world, org, assignee=None):
    body = {"patient_id": world["patient"], "title": "T2-1 随访", "org_id": world["orgs"][org]}
    if assignee:
        body["assignee_id"] = world["users"][assignee]
    return client.post(f"{B}/tasks", headers=admin, json=body)


def test_建任务_责任人不在任务所属机构_422(client, admin, world):
    bad = _task(client, admin, world, "甲", "b1")
    assert (bad.status_code, bad.json()) == (422, {"detail": "责任人不在任务所属机构，派过去打不开这条任务"}), bad.text  # 修前 201
    assert _task(client, admin, world, "甲", "a1").status_code == 201
    assert _task(client, admin, world, "甲", "dir").status_code == 201   # 全域角色照常


def test_转派_责任人不在任务所属机构_422_派过去的人确实办不了(client, admin, world):
    task = _task(client, admin, world, "甲").json()["id"]
    bad = client.post(f"{B}/tasks/{task}/assign", headers=admin, json={"assignee_id": world["users"]["b1"]})
    assert bad.status_code == 422, bad.text                                   # 修前 200
    # 修前派过去之后的样子：乙院医生认领 / 打开这条任务都是 403
    assert client.post(f"{B}/tasks/{task}/claim", headers=_login(client, "t21_b1")).status_code == 403
    ok = client.post(f"{B}/tasks/{task}/assign", headers=admin, json={"assignee_id": world["users"]["a1"]})
    assert ok.status_code == 200 and ok.json()["assignee_id"] == world["users"]["a1"], ok.text


def test_批量分配_别家机构的任务逐条跳过并写明(client, admin, world):
    mine = _task(client, admin, world, "甲").json()["id"]
    other = _task(client, admin, world, "乙").json()["id"]
    resp = client.post(f"{B}/tasks/batch", headers=admin, json={
        "task_ids": [mine, other], "action": "assign", "assignee_id": world["users"]["a1"]})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"processed": 1, "skipped": [{"id": other, "reason": "责任人不在该任务所属机构"}]}  # 修前两条都派了
