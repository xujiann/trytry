"""主管医生被改成经办 / 药师之后，慢专病新派生的工作不再挂给他；显式指派给角色办不了的人 422（P2-798，第二十二批
「人员与机构变动之后」扫描 X3-1）。

改角色（`PATCH /api/users/{id}/role`）只改 `role`，名下的档案一概不动。系统替人挑责任人的地方（`spawn_task` 的缺省
责任人、高危自动干预与复诊、居民在线咨询的接诊医生）只看账号停没停用（P1-205），角色一眼不看：主管医生改成经办后，
录一个收缩压 190，处置任务照派给他——他接收、办结都 403；别人接收 409（已有责任人）；中心端「未分配」不数它。管理端
转派、建任务、分发目标患者、建档挂主管医生、派复诊与随访，显式指给已是经办 / 药师的人同样照收。修后系统挑人按办理
角色过一遍（办不了的落成空，别人接收得了），显式指派 422；只是知会的站内信（路径暂停）不按角色筛。已经挂在他名下的
存量怎么转交属 P1-206。
"""
import inspect
import itertools

import pytest
from conftest import login

B = "/api/spd"
PROGRAM = "p2798_htn"
MISSING = 987654321
_CARDS = itertools.count(1)

#: 综合风险量表（与病种无关）的满分答法：18 分 → very_high，落进自动干预与高危复诊的触发区间
VERY_HIGH_ANSWERS = {"control": "未达标", "adherence": "差", "complication": "2项及以上", "selfcare": "完全依赖"}


def _user(client, admin, name, org, role="doctor"):
    resp = client.post("/api/users", headers=admin, json={
        "username": name, "password": "pass123456", "role": role, "org_id": org})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _patient(client, admin):
    n = next(_CARDS)
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2798 患者{n}", "id_card": f"33019219710202{3100 + n:04d}", "gender": "男",
        "birth_date": "1971-02-02", "phone": f"1391798{3100 + n:04d}"})
    assert patient.status_code == 201, patient.text
    return patient.json()


def _enrolled(client, admin, org, doctor, program="hypertension"):
    patient = _patient(client, admin)
    resp = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient["id"], "program_code": program, "org_id": org, "doctor_user_id": doctor})
    assert resp.status_code == 201, resp.text
    return {**patient, "enrollment": resp.json()["id"]}


def _set_role(client, admin, user_id, role):
    resp = client.patch(f"/api/users/{user_id}/role", headers=admin, json={"role": role})
    assert resp.status_code == 200 and resp.json()["role"] == role, resp.text


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2798 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    template = client.post(f"{B}/intervention-templates", headers=admin, json={
        "code": "p2798_auto_vh", "name": "P2798 极高危自动干预包", "program_code": "hypertension", "category": "drug",
        "content": "药物调整", "measures": "两周内复查血压", "frequency": "每周一次", "auto_risk_level": "very_high"})
    assert template.status_code == 201, template.text
    # 两节点路径：办完首节点，第二节点的进入条件（风险高）不满足 → 暂停
    prog = client.post(f"{B}/programs", headers=admin, json={"code": PROGRAM, "name": "P2798 高血压", "category": "chronic"})
    assert prog.status_code == 201, prog.text
    path = client.post(f"{B}/path-templates", headers=admin, json={
        "program_id": prog.json()["id"], "code": "P2798-PATH", "name": "P2798 条件路径"}).json()["id"]
    for node in ({"key": "p2798_n1", "name": "首次随访", "seq": 1, "due_days": 7},
                 {"key": "p2798_n2", "name": "风险复评", "seq": 2, "due_days": 5,
                  "enter_condition": [{"field": "risk_level", "op": "==", "value": "high"}]}):
        assert client.post(f"{B}/path-templates/{path}/nodes", headers=admin, json=node).status_code == 201
    assert client.post(f"{B}/path-templates/{path}/status", headers=admin,
                       json={"status": "published"}).status_code == 200
    users = {name: _user(client, admin, f"p2798_{name}", org) for name in ("moved", "ph", "other")}
    users["op"] = _user(client, admin, "p2798_op", org, role="operator")
    users["pharm"] = _user(client, admin, "p2798_pharm", org, role="pharmacist")
    # 先挂好档案（那时都还是医师），再改角色：甲调去做经办，乙改成公卫（仍办得了慢专病服务、回复不了咨询）
    patients = {key: _enrolled(client, admin, org, users["moved"]) for key in ("measure", "assess", "consult")}
    patients["pause"] = _enrolled(client, admin, org, users["moved"], program=PROGRAM)
    patients["measure_ph"] = _enrolled(client, admin, org, users["ph"])
    patients["consult_ph"] = _enrolled(client, admin, org, users["ph"])
    patients["consult_doctor"] = _enrolled(client, admin, org, users["other"])
    _set_role(client, admin, users["moved"], "operator")
    _set_role(client, admin, users["ph"], "public_health")
    return {"org": org, "users": users, "patients": patients, "path": path}


def _disposal_task(patient_id):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        (task,) = db.query(SpdTask).filter(SpdTask.patient_id == patient_id, SpdTask.title.like("指标异常处置%")).all()
        return task.id, task.assignee_id


def _task(client, admin, world):
    resp = client.post(f"{B}/tasks", headers=admin, json={
        "patient_id": _patient(client, admin)["id"], "title": "P2798 手工任务", "org_id": world["org"]})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_主管医生改成经办之后_处置任务不挂他_别人接收得了(client, admin, world):
    pid = world["patients"]["measure"]["id"]
    resp = client.post(f"{B}/measurements", headers=admin, json={"patient_id": pid, "metric": "bp_sys", "value": 190})
    assert resp.status_code == 201 and resp.json()["level"] == "high", resp.text
    task_id, assignee = _disposal_task(pid)
    assert assignee is None   # 修前挂给已是经办的主管医生：他办理 403
    claimed = client.post(f"{B}/tasks/{task_id}/claim", headers=login(client, "p2798_other", "pass123456"))
    assert claimed.status_code == 200 and claimed.json()["assignee_id"] == world["users"]["other"], claimed.text  # 修前 409


def test_主管医生改成公卫_照旧派给他(client, admin, world):
    pid = world["patients"]["measure_ph"]["id"]
    resp = client.post(f"{B}/measurements", headers=admin, json={"patient_id": pid, "metric": "bp_sys", "value": 190})
    assert resp.status_code == 201, resp.text
    assert _disposal_task(pid)[1] == world["users"]["ph"]   # 公卫在办理角色里：不因这次修改少派


def test_高危自动干预与复诊不挂改成经办的主管医生(client, admin, world):
    from app.database import SessionLocal
    from app.spd.models import SpdIntervention, SpdRevisit

    pid = world["patients"]["assess"]["id"]
    resp = client.post(f"{B}/assessments", headers=admin, json={
        "patient_id": pid, "scale_code": "assess_risk_common", "program_code": "hypertension",
        "answers": VERY_HIGH_ANSWERS})
    assert resp.status_code == 201 and resp.json()["risk_level"] == "very_high", resp.text
    with SessionLocal() as db:
        owners = ([i.owner_id for i in db.query(SpdIntervention).filter(SpdIntervention.patient_id == pid)],
                  [r.doctor_user_id for r in db.query(SpdRevisit).filter(SpdRevisit.patient_id == pid)])
    assert owners == ([None], [None])   # 修前两处都是已改成经办的主管医生


def _consult_doctor(client, patient):
    from app.database import SessionLocal
    from app.spd.models import SpdConsult

    code = client.post("/api/portal/auth/sms/code", json={"phone": patient["phone"]}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login", json={"phone": patient["phone"], "code": code})
    assert token.status_code == 200, token.text
    ph = {"Authorization": f"Bearer {token.json()['access_token']}"}
    bound = client.post("/api/portal/auth/realname", headers=ph, json={
        "name": patient["name"], "id_card": patient["id_card"]})
    assert bound.status_code == 200 or bound.json() == {"detail": "该账户已完成实名绑定"}, bound.text
    resp = client.post("/api/portal/spd/consults", headers=ph, json={"program_code": "hypertension", "content": "血压偏高"})
    assert resp.status_code == 201, resp.text
    with SessionLocal() as db:
        return db.get(SpdConsult, resp.json()["consult_id"]).doctor_id


def test_居民咨询不派给回复不了咨询的主管医生(client, world):
    assert _consult_doctor(client, world["patients"]["consult"]) is None   # 修前派给经办：他回复 403
    assert _consult_doctor(client, world["patients"]["consult_ph"]) is None   # 公卫办得了任务，回复不了咨询
    assert _consult_doctor(client, world["patients"]["consult_doctor"]) == world["users"]["other"]   # 医师照旧


def test_路径暂停的知会照发改成经办的主管医生(client, admin, world):
    """知会不是派活：改成经办的人登得上、看得到、能转告（`usable_or_none(..., roles=())`）；派活才按角色挑人。"""
    from app.database import SessionLocal
    from app.models import Notification, User

    instance = client.post(f"{B}/path-instances", headers=admin, json={
        "enrollment_id": world["patients"]["pause"]["enrollment"], "template_id": world["path"]})
    assert instance.status_code == 201, instance.text
    iid = instance.json()["id"]
    first = client.get(f"{B}/path-instances/{iid}", headers=admin).json()["nodes"][0]["tasks"][0]
    assert first["assignee_id"] is None   # 首节点任务同样不挂经办
    done = client.post(f"{B}/tasks/{first['id']}/complete", headers=admin, json={})
    assert done.status_code == 200 and done.json()["advanced"]["status"] == "paused", done.text
    with SessionLocal() as db:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        got = sorted(n.user_id for n in db.query(Notification).filter(
            Notification.link_type == "spd_path_instance", Notification.link_id == iid,
            Notification.title == "专病路径已暂停"))
    assert got == sorted([admin_id, world["users"]["moved"]])


def test_转派_建任务_批量分配给角色办不了的人_422(client, admin, world):
    op, ph = world["users"]["op"], world["users"]["ph"]
    task = _task(client, admin, world)
    r = client.post(f"{B}/tasks/{task}/assign", headers=admin, json={"assignee_id": op})
    assert r.status_code == 422 and r.json()["detail"] == "责任人是经办人员，派过去办不了这条任务", r.text  # 修前 200
    r = client.post(f"{B}/tasks/{task}/assign", headers=admin, json={"assignee_id": world["users"]["moved"]})
    assert r.status_code == 422, r.text   # 改过角色的同样
    r = client.post(f"{B}/tasks/{task}/assign", headers=admin, json={"assignee_id": MISSING})
    assert r.status_code == 404 and r.json()["detail"] == "责任人不存在", r.text   # 原有的先后与文案不变
    r = client.post(f"{B}/tasks/batch", headers=admin, json={"task_ids": [task], "action": "assign", "assignee_id": op})
    assert r.status_code == 422 and r.json()["detail"] == "责任人是经办人员，派过去办不了这些任务", r.text  # 修前 200
    r = client.post(f"{B}/tasks", headers=admin, json={
        "patient_id": _patient(client, admin)["id"], "title": "P2798 指给药师", "org_id": world["org"],
        "assignee_id": world["users"]["pharm"]})
    assert r.status_code == 422 and r.json()["detail"] == "责任人是药师，派过去办不了这条任务", r.text  # 修前 201
    r = client.post(f"{B}/tasks/{task}/assign", headers=admin, json={"assignee_id": ph})
    assert r.status_code == 200 and r.json()["assignee_id"] == ph, r.text   # 公卫照常


def test_分发目标患者给角色办不了的人_422(client, admin, world):
    r = client.post(f"{B}/candidates/distribute", headers=admin, json={
        "candidate_ids": [MISSING], "assigned_user_id": world["users"]["op"]})
    assert r.status_code == 422 and r.json()["detail"] == "指派人是经办人员，分过去办不了这些记录", r.text  # 修前 200
    r = client.post(f"{B}/candidates/distribute", headers=admin, json={
        "candidate_ids": [MISSING], "assigned_user_id": world["users"]["ph"]})
    assert r.status_code == 200 and r.json() == {"distributed": 0, "not_found": 1}, r.text


def test_建档主管医生角色要办得了_个案管理师不查_改档原样带回的不挡(client, admin, world):
    op = world["users"]["op"]
    body = {"patient_id": _patient(client, admin)["id"], "program_code": "hypertension", "org_id": world["org"]}
    r = client.post(f"{B}/enrollments", headers=admin, json={**body, "doctor_user_id": op})
    assert r.status_code == 422 and r.json()["detail"] == f"主管医生是经办人员，办不了慢专病服务（doctor_user_id={op}）", \
        r.text  # 修前 201
    r = client.post(f"{B}/enrollments", headers=admin, json={**body, "manager_user_id": op})
    assert r.status_code == 201, r.text   # 个案管理师不派活，不查角色
    enrollment = world["patients"]["consult"]["enrollment"]   # 主管医生是后来改成经办的甲
    r = client.patch(f"{B}/enrollments/{enrollment}", headers=admin, json={
        "doctor_user_id": world["users"]["moved"], "risk_level": "high"})
    assert r.status_code == 200 and r.json()["risk_level"] == "high", r.text   # 原样带回现值，不挡


def test_复诊医生_随访执行人_路径负责人角色要办得了(client, admin, world):
    op, pharm = world["users"]["op"], world["users"]["pharm"]
    r = client.post(f"{B}/revisits", headers=admin, json={
        "patient_id": _patient(client, admin)["id"], "plan_date": "2026-12-01", "doctor_user_id": op})
    assert r.status_code == 422 and r.json()["detail"] == f"复诊医生是经办人员，办不了复诊（doctor_user_id={op}）", r.text

    rule = client.post(f"{B}/followup-rules", headers=admin, json={"code": "p2798_rule", "name": "P2798 随访方案",
                                                                   "points": [7]})
    assert rule.status_code == 201, rule.text
    body = {"patient_id": _patient(client, admin)["id"], "rule_id": rule.json()["id"], "org_id": world["org"]}
    r = client.post(f"{B}/followup-plans", headers=admin, json={**body, "executor_id": pharm})
    assert r.status_code == 422 and r.json()["detail"] == f"随访执行人是药师，办不了随访（executor_id={pharm}）", r.text
    r = client.post(f"{B}/followup-plans", headers=admin, json={**body, "executor_id": op})
    assert r.status_code == 201, r.text   # 经办执行得了随访（FOLLOWUP_ROLES）
    record = r.json()["items"][0]["id"]
    r = client.patch(f"{B}/followup-records/{record}", headers=admin, json={"executor_id": pharm})
    assert r.status_code == 422 and "随访执行人是药师" in r.json()["detail"], r.text

    instance = client.post(f"{B}/path-instances", headers=admin, json={
        "enrollment_id": _enrolled(client, admin, world["org"], world["users"]["other"], program=PROGRAM)["enrollment"],
        "template_id": world["path"]})
    assert instance.status_code == 201, instance.text
    iid = instance.json()["id"]
    r = client.patch(f"{B}/path-instances/{iid}", headers=admin, json={"owner_user_id": op})
    assert r.status_code == 422 and r.json()["detail"] == f"路径负责人是经办人员，办不了路径（owner_user_id={op}）", r.text
    r = client.patch(f"{B}/path-instances/{iid}", headers=admin, json={"owner_user_id": world["users"]["ph"]})
    assert r.status_code == 200 and r.json()["owner_user_id"] == world["users"]["ph"], r.text


def test_管理员与自定义角色不按角色拦():
    from app.database import SessionLocal
    from app.models import User
    from app.spd.platform import role_unfit, usable_or_none
    from app.spd.service import SERVICE_ROLES

    with SessionLocal() as db:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        custom = User(username="p2798_custom", password_hash="x", full_name="P2798 自定义角色", role="p2798_custom")
        db.add(custom)
        db.commit()
        assert role_unfit(db, admin_id, SERVICE_ROLES) == ""   # 平台管理员过一切角色门
        assert role_unfit(db, custom.id, SERVICE_ROLES) == ""   # 自定义角色按权限点放行，这里不猜
        operator = db.query(User.id).filter(User.username == "p2798_op").scalar()
        assert role_unfit(db, operator, SERVICE_ROLES) == "经办人员"
        assert usable_or_none(db, operator, roles=()) == operator   # 知会不按角色筛
        assert usable_or_none(db, operator, roles=SERVICE_ROLES) is None


def test_挑人用的角色组与办理端点的角色门同一组():
    from app.spd.routers import care, population, referral, tasks
    from app.spd.service import CONSULT_ROLES, SERVICE_ROLES

    assert SERVICE_ROLES == tasks.SERVICE_ROLES == care.SERVICE_ROLES == population.SERVICE_ROLES == referral.SERVICE_ROLES

    def gate(module, endpoint):
        (route,) = [r for r in module.router.routes if getattr(r, "endpoint", None) is endpoint]
        (roles,) = [inspect.getclosurevars(d.call).nonlocals["roles"] for d in route.dependant.dependencies
                    if getattr(d.call, "__qualname__", "").startswith("require_roles.")]
        return roles

    assert gate(care, care.reply_consult) == gate(care, care.close_consult) == CONSULT_ROLES
    assert gate(tasks, tasks.claim_task) == gate(tasks, tasks.complete_task) == SERVICE_ROLES
