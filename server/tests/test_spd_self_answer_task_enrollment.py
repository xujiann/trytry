"""居民自助随访作答派的异常处置任务与医护执行同一套：挂纳管档案、中度三天到期（P2-131）。

自助作答接口的说明写着「异常分级与派单逻辑与医护执行时完全一致」，实现却是另写的一份：派出的处置任务不挂纳管档案、
重度中度一律次日到期。不挂档案的任务，结案收尾（`close_open_work` 按档案找任务）取消不到——患者登记死亡后它照旧
到期、超期；档案的 360 画像、居民端的就医旅程也都按档案看任务，看不见它。

修法：两条通道共用 `service.spawn_followup_abnormal_task`。
"""
from datetime import timedelta

import pytest

from app import clock

B = "/api/spd"
P = "/api/portal/spd"
PROGRAM = "p2131_htn"
PHONE = "13900021310"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2131 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2131 居民", "id_card": "330281199104041310", "gender": "男",
        "birth_date": "1991-04-04", "phone": PHONE}).json()["id"]
    assert client.post(f"{B}/programs", headers=admin, json={
        "code": PROGRAM, "name": "P2131 高血压", "category": "chronic"}).status_code == 201
    questionnaire = client.post(f"{B}/questionnaires", headers=admin, json={
        "code": "p2131_q", "name": "P2131 随访问卷", "scene": "inpatient",
        "items": [{"key": "dizzy", "title": "头晕程度", "type": "number"}],
        "abnormal_rules": [{"when": {"field": "dizzy", "op": ">=", "value": 7}, "level": "high", "action": "立即评估"},
                           {"when": {"field": "dizzy", "op": ">=", "value": 4}, "level": "mid", "action": ""}]})
    assert questionnaire.status_code == 201, questionnaire.text
    with SessionLocal() as db:
        enrollment = SpdEnrollment(patient_id=patient, program_code=PROGRAM, org_id=org, status="active")
        db.add(enrollment)
        db.commit()
        enrollment_id = enrollment.id

    code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code}).json()["access_token"]
    ph = {"Authorization": f"Bearer {token}"}
    bound = client.post("/api/portal/auth/realname", json={"name": "P2131 居民", "id_card": "330281199104041310"},
                        headers=ph)
    assert bound.status_code in (200, 409), bound.text
    return {"org": org, "patient": patient, "enrollment": enrollment_id, "ph": ph}


def _record(world):
    from app.database import SessionLocal
    from app.spd.models import SpdFollowupRecord

    with SessionLocal() as db:
        record = SpdFollowupRecord(patient_id=world["patient"], program_code=PROGRAM, questionnaire_code="p2131_q",
                                   org_id=world["org"], planned_at=clock.today().isoformat(), status="planned")
        db.add(record)
        db.commit()
        return record.id


def _task_of(title_prefix, world):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        rows = (db.query(SpdTask).filter(SpdTask.patient_id == world["patient"], SpdTask.title.like(f"{title_prefix}%"))
                .order_by(SpdTask.id).all())
        return [(t.id, t.enrollment_id, t.due_date, t.status) for t in rows]


def _answer(client, world, dizzy):
    resp = client.post(f"{P}/followups/{_record(world)}/self-answer", headers=world["ph"],
                       json={"patient_id": world["patient"], "answers": {"dizzy": dizzy}})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_自助作答派的处置任务挂上档案_中度三天重度次日(client, world):
    assert _answer(client, world, 5)["abnormal_level"] == "mid"
    assert _answer(client, world, 8)["abnormal_level"] == "high"
    today = clock.today()
    rows = _task_of("自助随访异常处置", world)
    assert [(enrollment, due) for _, enrollment, due, _ in rows] == [
        (world["enrollment"], (today + timedelta(days=3)).isoformat()),   # 修前 (None, 次日)
        (world["enrollment"], (today + timedelta(days=1)).isoformat()),   # 修前 (None, 次日)
    ]


def test_登记死亡后_自助作答派的任务一并取消(client, admin, world):
    died = client.post(f"{B}/enrollments/{world['enrollment']}/lifecycle", headers=admin,
                       json={"event": "death", "reason": "P2131 死亡登记"})
    assert died.status_code == 200, died.text
    statuses = {status for *_, status in _task_of("自助随访异常处置", world)}
    assert statuses == {"cancelled"}   # 修前 {"pending"}：不挂档案，结案收尾找不到它，照旧到期、超期
