"""慢专病随访计划的执行人能指定别家机构的人：工作台显示有一条随访，清单里却看不见，执行 403（P2-1017，第二十九批「字段之间的约束」
扫描 E3-6）。

`generate_followup_plan` 与改执行人（`update_followup_record`）只查停用与角色：甲院生成随访计划、执行人填乙院医生 201，乙医生工作台
「今日随访」数着这条（按执行人计数、不看机构），「我的随访」清单按机构收口看不见，执行 403；本机构的人又不是执行人，这一整条计划
没人做。`platform.assignee_outside_org` 的 docstring 写明了规矩——显式指定的责任人须在记录所属机构，任务那边按 P1-210 已拦。

修法：生成计划与改执行人两处都按 `assignee_outside_org(executor_id, 随访所属机构)` 422；全域角色照派，本机构照收。
复诊计划的复诊医生本就可能约在别家，怎么放行另行待裁定，这里不动。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    def org(name):
        return client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": "township", "level": "township"}).json()["id"]

    a, b = org("P21017 甲镇卫生院"), org("P21017 乙镇卫生院")

    def user(name, org_id, role="doctor"):
        got = client.post("/api/users", headers=admin, json={
            "username": name, "password": "passw0rd1", "role": role, "org_id": org_id, "full_name": name})
        assert got.status_code in (200, 201), got.text
        return got.json()["id"]

    ids = {"a_doc": user("p21017_a", a), "b_doc": user("p21017_b", b), "a_doc2": user("p21017_a2", a)}
    token = client.post("/api/auth/login", json={"username": "p21017_a", "password": "passw0rd1"}).json()["access_token"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21017 随访", "id_card": "110101196501011233", "birth_date": "1965-01-01"}).json()
    enrolled = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient["id"], "program_code": "hypertension", "org_id": a})
    assert enrolled.status_code == 201, enrolled.text
    rule = client.post(f"{B}/followup-rules", headers=admin, json={
        "code": "P21017_R0", "name": "P21017 当日随访", "scene": "outpatient", "points": [0]}).json()
    return {"a": a, "b": b, **ids, "patient": patient["id"], "rule": rule["id"],
            "A": {"Authorization": f"Bearer {token}"}}


def _plan(client, world, executor_id):
    return client.post(f"{B}/followup-plans", headers=world["A"], json={
        "patient_id": world["patient"], "rule_id": world["rule"], "org_id": world["a"], "executor_id": executor_id})


def test_生成随访计划执行人在别家机构_422(client, world):
    got = _plan(client, world, world["b_doc"])
    assert got.status_code == 422, got.text   # 修前 201，乙医生工作台数着、清单看不见、执行 403
    assert "不在随访所属机构" in got.json()["detail"]


def test_生成随访计划执行人在本机构照收_全域角色照派(client, admin, world):
    assert _plan(client, world, world["a_doc2"]).status_code == 201
    from app.database import SessionLocal
    from app.models import User

    with SessionLocal() as db:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
    assert _plan(client, world, admin_id).status_code == 201


def test_改执行人到别家机构_422_改回本机构照收(client, world):
    record = _plan(client, world, world["a_doc2"]).json()["items"][0]
    moved = client.patch(f"{B}/followup-records/{record['id']}", headers=world["A"], json={"executor_id": world["b_doc"]})
    assert moved.status_code == 422, moved.text   # 修前 200
    same = client.patch(f"{B}/followup-records/{record['id']}", headers=world["A"], json={"executor_id": world["a_doc"]})
    assert same.status_code == 200, same.text
