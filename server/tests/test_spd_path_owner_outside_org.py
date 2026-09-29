"""路径负责人不能改成别家机构的人（P2-879，第二十四批「通知、提醒与待办」扫描 Z2-3；与任务责任人 P1-210 同一句）。

`adjust_path_instance` 改负责人只查停用与角色；任务责任人早就要过 `assignee_outside_org`（P1-210：显式指定的责任人必须在任务
所属机构，否则 422「派过去打不开这条任务」）。路径负责人同样是显式指定、同样收通知：甲院把自家患者的路径负责人改成乙院
医生，原先 200；路径暂停时「专病路径已暂停」发进乙院，他打开实例 403、暂停清单里查不到、推进也 403。修后同一判据 422，
全域角色照常放行。
"""
import pytest

B = "/api/spd"


def _login(client, username):
    token = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    orgs, doctors = {}, {}
    for key in ("a", "b"):
        orgs[key] = client.post("/api/organizations", headers=admin, json={
            "name": f"P2879 {key}院", "org_type": "township", "level": "township"}).json()["id"]
    for name, org in (("p2879_a1", "a"), ("p2879_a2", "a"), ("p2879_b1", "b")):
        made = client.post("/api/users", headers=admin, json={
            "username": name, "password": "passw0rd1", "full_name": name, "role": "doctor", "org_id": orgs[org]})
        assert made.status_code in (200, 201), made.text
        doctors[name] = made.json()["id"]
    program = client.post(f"{B}/programs", headers=admin, json={
        "code": "p2879_prog", "name": "P2879 病种", "category": "chronic"})
    assert program.status_code == 201, program.text
    template = client.post(f"{B}/path-templates", headers=admin, json={
        "program_id": program.json()["id"], "code": "P2879_T", "name": "P2879 路径"})
    assert template.status_code == 201, template.text
    client.post(f"{B}/path-templates/{template.json()['id']}/nodes", headers=admin,
                json={"key": "n1", "name": "首节点", "seq": 1})
    client.post(f"{B}/path-templates/{template.json()['id']}/status", headers=admin, json={"status": "published"})
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2879 患者", "id_card": "330102196001012879"}).json()["id"]
    enrolled = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "p2879_prog", "org_id": orgs["a"]})
    assert enrolled.status_code == 201, enrolled.text
    doctor_a = _login(client, "p2879_a1")
    started = client.post(f"{B}/path-instances", headers=doctor_a, json={
        "enrollment_id": enrolled.json()["id"], "template_id": template.json()["id"]})
    assert started.status_code == 201, started.text
    return {"doctors": doctors, "instance": started.json()["id"], "doctor_a": doctor_a}


def test_负责人改成别家机构的人_422(client, world):
    got = client.patch(f"{B}/path-instances/{world['instance']}", headers=world["doctor_a"],
                       json={"owner_user_id": world["doctors"]["p2879_b1"]})
    assert got.status_code == 422, got.text   # 修前 200：通知发进别家，他打不开
    assert "不在该档案所属机构" in got.json()["detail"]


def test_本机构的人照改(client, world):
    got = client.patch(f"{B}/path-instances/{world['instance']}", headers=world["doctor_a"],
                       json={"owner_user_id": world["doctors"]["p2879_a2"]})
    assert got.status_code == 200, got.text
    assert got.json()["owner_user_id"] == world["doctors"]["p2879_a2"]
