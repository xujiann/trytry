"""随访问卷异常派出的处置任务不落主管医生：停在待接收、谁的待办里都没有，重度异常也不通知任何人（P1-184）。

`service.spawn_task` 的注释写着「**所有任务都从这里出**……责任人缺省顺序：显式指定 > 节点执行角色对应的团队成员 >
纳管档案的主管医生」；`spawn_followup_abnormal_task`（医护执行与居民自助作答共用）却自己拼 `SpdTask`，挂着纳管档案
也不落责任人、不落团队。预置问卷的处置动作写的正是「通知主管医师」「立即联系手术医师」；居民自助作答的重度异常，
原先要等有人去翻任务中心才看得见。

修法：改走 `spawn_task`（按档案缺省责任人与团队）；重度异常另给责任人发一条站内消息（与催办同一个 `notify_user`）。
"""
import pytest

from app import clock

B = "/api/spd"
P = "/api/portal/spd"
PROGRAM = "p1184_htn"
PHONE = "13900011840"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1184 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    doctor = client.post("/api/users", headers=admin, json={
        "username": "p1184_doc", "password": "passw0rd1", "full_name": "P1184 主管医生", "role": "doctor",
        "org_id": org})
    assert doctor.status_code in (200, 201), doctor.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P1184 居民", "id_card": "330281199104041848", "gender": "男",
        "birth_date": "1991-04-04", "phone": PHONE}).json()["id"]
    assert client.post(f"{B}/programs", headers=admin, json={
        "code": PROGRAM, "name": "P1184 高血压", "category": "chronic"}).status_code == 201
    questionnaire = client.post(f"{B}/questionnaires", headers=admin, json={
        "code": "p1184_q", "name": "P1184 随访问卷", "scene": "inpatient",
        "items": [{"key": "dizzy", "title": "头晕程度", "type": "number"}],
        "abnormal_rules": [{"when": {"field": "dizzy", "op": ">=", "value": 7}, "level": "high",
                            "action": "通知主管医师"},
                           {"when": {"field": "dizzy", "op": ">=", "value": 4}, "level": "mid", "action": ""}]})
    assert questionnaire.status_code == 201, questionnaire.text
    with SessionLocal() as db:
        enrollment = SpdEnrollment(patient_id=patient, program_code=PROGRAM, org_id=org, status="active",
                                   doctor_user_id=doctor.json()["id"])
        db.add(enrollment)
        db.commit()
        enrollment_id = enrollment.id

    code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code}).json()["access_token"]
    ph = {"Authorization": f"Bearer {token}"}
    bound = client.post("/api/portal/auth/realname", json={"name": "P1184 居民", "id_card": "330281199104041848"},
                        headers=ph)
    assert bound.status_code in (200, 409), bound.text
    return {"org": org, "patient": patient, "enrollment": enrollment_id, "ph": ph, "doctor": doctor.json()["id"]}


def _record(world):
    from app.database import SessionLocal
    from app.spd.models import SpdFollowupRecord

    with SessionLocal() as db:
        record = SpdFollowupRecord(patient_id=world["patient"], program_code=PROGRAM, questionnaire_code="p1184_q",
                                   org_id=world["org"], planned_at=clock.today().isoformat(), status="planned")
        db.add(record)
        db.commit()
        return record.id


def _latest_task(world, prefix):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        task = (db.query(SpdTask).filter(SpdTask.patient_id == world["patient"], SpdTask.title.like(f"{prefix}%"))
                .order_by(SpdTask.id.desc()).first())
        return task.id, task.assignee_id


def _notices(world):
    from app.database import SessionLocal
    from app.models import Notification

    with SessionLocal() as db:
        return [(n.title, n.link_id) for n in db.query(Notification).filter(
            Notification.user_id == world["doctor"]).order_by(Notification.id)]


def test_居民自助作答_重度异常落主管医生并通知(client, world):
    before = _notices(world)
    resp = client.post(f"{P}/followups/{_record(world)}/self-answer", headers=world["ph"],
                       json={"patient_id": world["patient"], "answers": {"dizzy": 9}})
    assert resp.status_code == 200 and resp.json()["abnormal_level"] == "high", resp.text
    task_id, assignee = _latest_task(world, "自助随访异常处置")
    assert assignee == world["doctor"]   # 修前 None：停在待接收，谁的待办里都没有
    assert _notices(world)[len(before):] == [("随访重度异常待处置", task_id)]   # 修前没有任何通知


def test_中度异常落主管医生_不另发消息(client, world):
    before = _notices(world)
    resp = client.post(f"{P}/followups/{_record(world)}/self-answer", headers=world["ph"],
                       json={"patient_id": world["patient"], "answers": {"dizzy": 5}})
    assert resp.status_code == 200 and resp.json()["abnormal_level"] == "mid", resp.text
    assert _latest_task(world, "自助随访异常处置")[1] == world["doctor"]
    assert _notices(world) == before


def test_医护执行派的处置任务同样落主管医生(client, admin, world):
    resp = client.post(f"{B}/followup-records/{_record(world)}/execute", headers=admin,
                       json={"answers": {"dizzy": 8}, "channel": "phone"})
    assert resp.status_code == 200, resp.text
    assert _latest_task(world, "随访异常处置")[1] == world["doctor"]   # 修前 None
