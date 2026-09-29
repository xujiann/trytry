"""停用的村医（村医档案停用或账号停用）不再入积分：接手的人办结他签约档案的随访、上报异常，积分原先照记给停用的人
（P2-845，第二十二批「人员与机构变动之后」扫描 X3-2「不需要拍板的一半」；另一半——名下签约档案怎么办、停用期间的积分
记给谁——随 P2-840 待裁定）。

P2-663 定的口径：停用村医别处一律当「已回收」（不出绑定码、不进考核对象、建档不再挂他）；可积分入账（`award_points`）
不看账号状态、也不看村医档案。开发库实测（修前）：老村医签约后积分 5，停用村医档案和账号后，新村医办结一条随访、上报
一条异常，老村医积分 5→13。修后停用账号、停用村医档案都不入账；在用的照旧。
"""
import itertools

import pytest

from conftest import login

B = "/api/spd"
_CARDS = itertools.count(1)


def _balance(user_id):
    from app.database import SessionLocal
    from app.spd.models import SpdPointAccount

    with SessionLocal() as db:
        account = db.query(SpdPointAccount).filter(SpdPointAccount.user_id == user_id).first()
        return None if account is None else account.earned


@pytest.fixture(scope="module")
def world(client, admin):
    town = client.post("/api/organizations", headers=admin, json={
        "name": "P2845 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    village = client.post("/api/organizations", headers=admin, json={
        "name": "P2845 村卫生室", "org_type": "village", "level": "village", "parent_id": town}).json()["id"]
    users, profiles = {}, {}
    for key in ("gone_profile", "gone_account", "active", "new"):
        resp = client.post("/api/users", headers=admin, json={
            "username": f"p2845_{key}", "password": "passw0rd1", "full_name": f"p2845_{key}", "role": "doctor",
            "org_id": village})
        assert resp.status_code in (200, 201), resp.text
        users[key] = resp.json()["id"]
        vd = client.post(f"{B}/village-doctors", headers=admin, json={
            "user_id": users[key], "org_id": village, "village": "P2845村"})
        assert vd.status_code == 201, vd.text
        profiles[key] = vd.json()["id"]
    patients = {}
    for key in ("gone_profile", "gone_account", "active"):   # 各签一份档案（那时都还在用）
        n = next(_CARDS)
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P2845 患者{n}", "id_card": f"33019219700101{2845 + n:04d}"}).json()["id"]
        enrolled = client.post(f"{B}/enrollments", headers=admin, json={
            "patient_id": patient, "program_code": "hypertension", "org_id": village,
            "village_doctor_id": users[key]})
        assert enrolled.status_code == 201, enrolled.text
        patients[key] = patient
    stopped = client.patch(f"{B}/village-doctors/{profiles['gone_profile']}", headers=admin, json={"active": False})
    assert stopped.status_code == 200, stopped.text
    disabled = client.patch(f"/api/users/{users['gone_account']}/status", headers=admin, json={"status": "disabled"})
    assert disabled.status_code == 200, disabled.text
    return {"users": users, "patients": patients, "new": login(client, "p2845_new", "passw0rd1")}


def _followup_done(client, headers, patient):
    task = client.post(f"{B}/tasks", headers=headers, json={
        "patient_id": patient, "title": "P2845 季度随访", "task_type": "followup", "program_code": "hypertension"})
    assert task.status_code == 201 and task.json()["enrollment_id"], task.text
    done = client.post(f"{B}/tasks/{task.json()['id']}/complete", headers=headers, json={"result": {"note": "血压可"}})
    assert done.status_code == 200, done.text


@pytest.mark.parametrize("key", ["gone_profile", "gone_account"])
def test_停用的村医_接手的人办结随访_不再给他入账(client, world, key):
    before = _balance(world["users"][key])
    _followup_done(client, world["new"], world["patients"][key])
    report = client.post(f"{B}/case-reports", headers=world["new"], json={
        "patient_id": world["patients"][key], "program_code": "hypertension", "report_type": "followup",
        "content": "头晕"})
    assert report.status_code == 201, report.text
    assert _balance(world["users"][key]) == before   # 修前：随访 + 异常上报照记给停用的人


def test_在用的村医照旧入账(client, world):
    before = _balance(world["users"]["active"]) or 0
    _followup_done(client, world["new"], world["patients"]["active"])
    assert (_balance(world["users"]["active"]) or 0) > before
