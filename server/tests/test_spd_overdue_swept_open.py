"""超期扫描把随访 / 复诊翻成「已超期」之后，只认 planned 的计数与收尾都漏了它们（P1-128）。

P0-2 起超期扫描（`service.sweep_overdue`）把过了日期的随访记录与复诊从 planned 置为 overdue，「查询一律按 status 过滤，
口径只剩一个」。可有几处还是扫描之前的写法：「planned 且日期已过」算超期、「planned 且日期不晚于今天」算到期、「planned」
算没做完。扫描一过这些行都成了 overdue——而管理端工作台、任务汇总、随访清单、定时任务进来都先扫一遍：

- 管理端工作台的「超期随访」提醒：先扫描、再按「planned 且已过期」数，**恒为 0**；卫健工作台与区域统计的超期随访
  同样只数得到两次扫描之间的那几条；
- 团队工作台的「到期随访」「到期复诊」只剩今天的，过期没做的一条不数；
- 居民端首页的「待随访」不数已超期的（手机上照样能自助随访它）；
- 患者死亡 / 迁出 / 排除时结案：已超期的复诊不移除；暂停中的路径不取消（路径的「未结束」是执行中 + 暂停，同一个形状）；
- 同一模板有暂停中的路径时照样能再启动一条——恢复暂停的那条后两条并行、各派一份任务（启动接口要防的正是这个）；
- 高危复评：已有一条超期的高危复诊，再开一条。

修法：随访记录与复诊的「未完成」（planned + overdue）、路径实例的「未结束」（running + paused）、超期随访的判定
（已标超期 + 扫描间隙里过了日期的）在 service 各定义一处，各处改用。
"""
import pytest

B = "/api/spd"
PAST = "2020-01-01"
_seq = {"n": 0}


@pytest.fixture(scope="module")
def world(client, admin):
    from conftest import login

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P128 超期卫生院", "org_type": "township", "level": "township"}).json()["id"]
    resp = client.post("/api/users", headers=admin, json={
        "username": "p128_doc", "password": "passw0rd1", "role": "doctor", "full_name": "P128 医生", "org_id": org})
    assert resp.status_code == 201, resp.text
    return {"org": org, "doctor_id": resp.json()["id"], "doctor": login(client, "p128_doc", "passw0rd1")}


def _enrolled(client, admin, world, phone=None):
    """新开一位在管患者（主管医生是本模块的医生）。返回 (患者 id, 纳管档案 id)。"""
    _seq["n"] += 1
    body = {"name": f"P128 患者{_seq['n']}", "id_card": f"33012719690404{_seq['n']:04d}"}
    if phone:
        body["phone"] = phone
    patient = client.post("/api/patients", headers=admin, json=body)
    assert patient.status_code == 201, patient.text
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient.json()["id"], "program_code": "hypertension", "org_id": world["org"],
        "doctor_user_id": world["doctor_id"]})
    assert enrollment.status_code == 201, enrollment.text
    return patient.json()["id"], enrollment.json()["id"]


def _add(row):
    from app.database import SessionLocal

    with SessionLocal() as db:
        db.add(row)
        db.commit()
        return row.id


def _followup(world, patient):
    from app.spd.models import SpdFollowupRecord

    return _add(SpdFollowupRecord(patient_id=patient, program_code="hypertension", org_id=world["org"],
                                  planned_at=PAST, status="planned"))


def _revisit(world, patient, source="manual"):
    from app.spd.models import SpdRevisit

    return _add(SpdRevisit(patient_id=patient, program_code="hypertension", plan_date=PAST,
                           doctor_user_id=world["doctor_id"], source=source, status="planned"))


def _sweep(client, admin):
    """进任务汇总顺手扫一次超期（与定时任务、各工作台同一个函数）。"""
    assert client.get(f"{B}/tasks/summary", headers=admin).status_code == 200


def _status(model, row_id):
    from app.database import SessionLocal

    with SessionLocal() as db:
        return db.get(model, row_id).status


def test_超期随访_扫描之后各处照样数得到(client, admin, world):
    from app.spd.models import SpdFollowupRecord

    def admin_alert():
        resp = client.get(f"{B}/workbench/admin", headers=admin)   # 进来先扫一遍
        assert resp.status_code == 200, resp.text
        return resp.json()["alerts"]["overdue_followups"]

    before = admin_alert()
    patient, _ = _enrolled(client, admin, world)
    record = _followup(world, patient)
    assert admin_alert() == before + 1   # 修前恒为 0：先扫成 overdue、再按 planned 数
    assert _status(SpdFollowupRecord, record) == "overdue"
    commission = client.get(f"{B}/workbench/health-commission?org_id={world['org']}", headers=admin).json()
    assert commission["followups"]["overdue"] == 1   # 修前 0
    region = client.get(f"{B}/stats/region?org_id={world['org']}", headers=admin).json()
    assert region["followups"]["overdue"] == 1   # 修前 0
    team = client.get(f"{B}/workbench/team?role=member", headers=world["doctor"]).json()
    assert team["plans"]["due_followups"] == 1   # 修前 0：只剩计划在今天的


def test_超期复诊_到期数与死亡结案都收它(client, admin, world):
    from app.spd.models import SpdRevisit

    patient, enrollment = _enrolled(client, admin, world)
    revisit = _revisit(world, patient)
    _sweep(client, admin)
    assert _status(SpdRevisit, revisit) == "overdue"
    team = client.get(f"{B}/workbench/team?role=member", headers=world["doctor"]).json()
    assert team["plans"]["due_revisits"] == 1   # 修前 0
    resp = client.post(f"{B}/enrollments/{enrollment}/lifecycle", headers=admin,
                       json={"event": "death", "reason": "病故"})
    assert resp.status_code == 200 and resp.json()["closed"]["revisits"] == 1, resp.text   # 修前 0
    assert _status(SpdRevisit, revisit) == "removed"   # 修前仍是 overdue：死者名下挂着一条逾期复诊


def test_暂停的路径_挡住重复启动_死亡结案一并取消(client, admin, world):
    from app.database import SessionLocal
    from app.spd.models import SpdPathInstance, SpdPathNode, SpdPathTemplate, SpdProgram

    patient, enrollment = _enrolled(client, admin, world)
    with SessionLocal() as db:
        program = db.query(SpdProgram).filter_by(code="hypertension").one()
        template = SpdPathTemplate(program_id=program.id, code="P128_PATH", name="P128 路径", status="published")
        db.add(template)
        db.flush()
        db.add(SpdPathNode(template_id=template.id, key="n1", name="首诊", seq=1))
        db.commit()
        template_id = template.id
    paused = _add(SpdPathInstance(enrollment_id=enrollment, template_id=template_id, status="paused",
                                  current_node_key="n1"))
    resp = client.post(f"{B}/path-instances", headers=admin, json={"enrollment_id": enrollment, "template_id": template_id})
    assert resp.status_code == 409, resp.text   # 修前 201：恢复暂停的那条后两条并行、各派一份任务
    assert resp.json() == {"detail": "该路径有一条暂停中的实例，请恢复或取消后再启动"}
    resp = client.post(f"{B}/enrollments/{enrollment}/lifecycle", headers=admin,
                       json={"event": "death", "reason": "病故"})
    assert resp.status_code == 200 and resp.json()["closed"]["instances"] == 1, resp.text   # 修前 0
    assert _status(SpdPathInstance, paused) == "cancelled"   # 修前仍是 paused，还能「恢复」


def test_已有超期的高危复诊_高危复评不再开一条(client, admin, world):
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment, SpdRevisit
    from app.spd.routers.care import _auto_intervene

    patient, enrollment = _enrolled(client, admin, world)
    _revisit(world, patient, source="high_risk")
    _sweep(client, admin)
    with SessionLocal() as db:
        _auto_intervene(db, db.get(SpdEnrollment, enrollment), "high")
        rows = db.query(SpdRevisit).filter_by(patient_id=patient, source="high_risk").all()
    assert [r.status for r in rows] == ["overdue"]   # 修前多开一条 planned


def test_居民端首页待随访_含已超期的(client, admin, world):
    from app.config import settings
    from app.database import SessionLocal
    from app.models import ResidentAccount

    phone = "13912801128"
    patient, _ = _enrolled(client, admin, world, phone=phone)
    with SessionLocal() as db:
        db.add(ResidentAccount(phone=phone, patient_id=patient, nickname="P128", wechat_openid="", status="active"))
        db.commit()
    old = settings.sms_debug_echo
    settings.sms_debug_echo = True
    try:
        code = client.post("/api/portal/auth/sms/code", json={"phone": phone, "purpose": "login"}).json()["debug_code"]
        token = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code}).json()["access_token"]
    finally:
        settings.sms_debug_echo = old
    _followup(world, patient)
    _sweep(client, admin)
    home = client.get("/api/portal/spd/home", headers={"Authorization": f"Bearer {token}"}).json()
    assert home["todo"]["followups"] == 1   # 修前 0（手机上照样能自助随访这一条）
