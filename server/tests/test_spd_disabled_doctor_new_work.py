"""主管医生停用之后，系统新派生的工作不再挂给他（第十五批「停用对象仍被引用」扫描 S1-1）。

P1-106 把「写接口显式指定停用账号」挡住了（404「责任人已停用」）；系统替人挑责任人的地方还照抄档案上的主管医生——
`spawn_task` 的缺省责任人（指标异常处置、异常上报处置、随访异常处置、路径节点、转诊到院跟踪……）、高危自动干预的
负责人与高危复诊医生、居民发起在线咨询的接诊医生。停用的账号登录不了；任务认领只许「空着或本人」，别人认领 409，
中心端「未分配」也只数空着的——新派生的处置任务进了一个没人登得上的待办箱。修后缺省值是停用账号的落成空
（待接收 / 未分配），在用的照旧挂主管医生。已经挂在停用账号名下的存量怎么转交另行裁定。
"""
import itertools

import pytest
from conftest import login

B = "/api/spd"
_CARDS = itertools.count(1)

#: 综合风险量表（与病种无关）的满分答法：18 分 → very_high，落进自动干预与高危复诊的触发区间
VERY_HIGH_ANSWERS = {"control": "未达标", "adherence": "差", "complication": "2项及以上", "selfcare": "完全依赖"}


def _user(client, admin, name, org):
    resp = client.post("/api/users", headers=admin, json={
        "username": name, "password": "pass123456", "role": "doctor", "org_id": org})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _enrolled(client, admin, org, doctor):
    n = next(_CARDS)
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"S1-1 患者{n}", "id_card": f"33019219700101{1100 + n:04d}", "gender": "男",
        "birth_date": "1970-01-01", "phone": f"1391100{1100 + n:04d}"})
    assert patient.status_code == 201, patient.text
    resp = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient.json()["id"], "program_code": "hypertension", "org_id": org, "doctor_user_id": doctor})
    assert resp.status_code == 201, resp.text
    return patient.json()


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "S1-1 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    template = client.post(f"{B}/intervention-templates", headers=admin, json={
        "code": "s11_auto_vh", "name": "S1-1 极高危自动干预包", "program_code": "hypertension", "category": "drug",
        "content": "药物调整", "measures": "两周内复查血压", "frequency": "每周一次", "auto_risk_level": "very_high"})
    assert template.status_code == 201, template.text
    gone, other, active = (_user(client, admin, f"s11_{name}", org) for name in ("gone", "other", "active"))
    # 先挂好档案再停用（停用的账号已挂不上新档案，P1-106）
    patients = {key: _enrolled(client, admin, org, gone) for key in ("measure", "assess", "consult")}
    control = _enrolled(client, admin, org, active)
    resp = client.patch(f"/api/users/{gone}/status", headers=admin, json={"status": "disabled"})
    assert resp.status_code == 200, resp.text
    return {"org": org, "gone": gone, "other": other, "active": active, "patients": patients, "control": control}


def _disposal_task(patient_id):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        (task,) = db.query(SpdTask).filter(SpdTask.patient_id == patient_id, SpdTask.title.like("指标异常处置%")).all()
        return task.id, task.assignee_id


def test_指标异常处置任务不派给停用的主管医生_别人认领得了(client, admin, world):
    pid = world["patients"]["measure"]["id"]
    resp = client.post(f"{B}/measurements", headers=admin, json={"patient_id": pid, "metric": "bp_sys", "value": 190})
    assert resp.status_code == 201 and resp.json()["level"] == "high", resp.text
    task_id, assignee = _disposal_task(pid)
    assert assignee is None   # 修前是停用的主管医生
    claimed = client.post(f"{B}/tasks/{task_id}/claim", headers=login(client, "s11_other", "pass123456"))
    assert claimed.status_code == 200 and claimed.json()["assignee_id"] == world["other"], claimed.text   # 修前 409


def test_在用的主管医生照旧派给他(client, admin, world):
    pid = world["control"]["id"]
    resp = client.post(f"{B}/measurements", headers=admin, json={"patient_id": pid, "metric": "bp_sys", "value": 190})
    assert resp.status_code == 201, resp.text
    assert _disposal_task(pid)[1] == world["active"]


def test_高危自动干预与复诊不挂停用的主管医生(client, admin, world):
    from app.database import SessionLocal
    from app.spd.models import SpdIntervention, SpdRevisit

    def assess(pid):
        resp = client.post(f"{B}/assessments", headers=admin, json={
            "patient_id": pid, "scale_code": "assess_risk_common", "program_code": "hypertension",
            "answers": VERY_HIGH_ANSWERS})
        assert resp.status_code == 201 and resp.json()["risk_level"] == "very_high", resp.text

    def owners(pid):
        with SessionLocal() as db:
            return (
                [i.owner_id for i in db.query(SpdIntervention).filter(SpdIntervention.patient_id == pid)],
                [r.doctor_user_id for r in db.query(SpdRevisit).filter(SpdRevisit.patient_id == pid)],
            )

    gone_pid, control_pid = world["patients"]["assess"]["id"], world["control"]["id"]
    assess(gone_pid)
    assess(control_pid)
    assert owners(gone_pid) == ([None], [None])                                  # 修前两处都是停用的主管医生
    assert owners(control_pid) == ([world["active"]], [world["active"]])         # 在用的照旧


def test_居民发起在线咨询不派给停用的主管医生(client, world):
    from app.database import SessionLocal
    from app.spd.models import SpdConsult

    patient = world["patients"]["consult"]
    code = client.post("/api/portal/auth/sms/code", json={"phone": patient["phone"]}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login", json={"phone": patient["phone"], "code": code})
    assert token.status_code == 200, token.text
    ph = {"Authorization": f"Bearer {token.json()['access_token']}"}
    bound = client.post("/api/portal/auth/realname", headers=ph, json={
        "name": patient["name"], "id_card": patient["id_card"]})
    assert bound.status_code == 200 or bound.json() == {"detail": "该账户已完成实名绑定"}, bound.text   # 手机号登录即已认上
    resp = client.post("/api/portal/spd/consults", headers=ph, json={"program_code": "hypertension", "content": "血压偏高"})
    assert resp.status_code == 201, resp.text
    with SessionLocal() as db:
        consult = db.get(SpdConsult, resp.json()["consult_id"])
        assert consult.doctor_id is None   # 修前派给停用的主管医生：谁回复都接不过来
