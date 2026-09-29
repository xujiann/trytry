"""随访重度异常的处置任务换了责任人，接手的人收到「随访重度异常待处置」（P2-885，第二十四批「通知、提醒与待办」扫描 Z2-2）。

`spawn_followup_abnormal_task` 只在派生那一刻给当时的责任人发这条消息（P1-184 的理由：重度异常原先要等有人去翻任务
中心才看得见）。转派恰好改的就是责任人：管理端把任务从医生一转给医生二（200，已接收），医生二 0 条消息；手册教的
「停用账号名下的任务逐条转派」、P1-209 无责任人任务的事后分派，接手的人都收不到推送。修后单条与批量分配换了责任人
都给接手的人发同一条；原责任人不再新增；其余任务（建的时候就不发）转派也不发。
"""
import pytest

from app import clock
from app.database import SessionLocal
from app.models import Notification
from app.spd.models import SpdFollowupRecord, SpdTask

B = "/api/spd"
NOTICE = "随访重度异常待处置"


def _notices(user_id, task_id):
    with SessionLocal() as db:
        return db.query(Notification).filter(Notification.user_id == user_id, Notification.title == NOTICE,
                                             Notification.link_id == task_id).count()


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2885 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    doctors = {}
    for name in ("p2885_d1", "p2885_d2", "p2885_d3"):
        made = client.post("/api/users", headers=admin, json={
            "username": name, "password": "passw0rd1", "full_name": name, "role": "doctor", "org_id": org})
        assert made.status_code in (200, 201), made.text
        doctors[name] = made.json()["id"]
    assert client.post(f"{B}/programs", headers=admin, json={
        "code": "p2885_prog", "name": "P2885 病种", "category": "chronic"}).status_code == 201
    q = client.post(f"{B}/questionnaires", headers=admin, json={
        "code": "p2885_q", "name": "P2885 问卷", "scene": "inpatient",
        "items": [{"key": "pain", "title": "疼痛评分", "type": "number"}],
        "abnormal_rules": [{"when": {"field": "pain", "op": ">=", "value": 7}, "level": "high",
                            "action": "立即联系主管医师"}]})
    assert q.status_code == 201, q.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2885 患者", "id_card": "330102196501012885"}).json()["id"]
    enrolled = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "p2885_prog", "org_id": org, "doctor_user_id": doctors["p2885_d1"]})
    assert enrolled.status_code == 201, enrolled.text
    with SessionLocal() as db:
        record = SpdFollowupRecord(patient_id=patient, program_code="p2885_prog", questionnaire_code="p2885_q",
                                   org_id=org, planned_at=clock.today().isoformat(), status="planned")
        db.add(record)
        db.commit()
        record_id = record.id
    done = client.post(f"{B}/followup-records/{record_id}/execute", headers=admin, json={"answers": {"pain": 9}})
    assert done.status_code == 200 and done.json()["abnormal_level"] == "high", done.text
    with SessionLocal() as db:
        task = db.query(SpdTask).filter(SpdTask.patient_id == patient, SpdTask.source == "followup").one()
        task_id, assignee = task.id, task.assignee_id
    assert assignee == doctors["p2885_d1"] and _notices(assignee, task_id) == 1   # 派生时发给医生一（P1-184）
    manual = client.post(f"{B}/tasks", headers=admin, json={
        "patient_id": patient, "title": "P2885 手工特急任务", "task_type": "report", "org_id": org, "priority": 3,
        "assignee_id": doctors["p2885_d1"]})
    assert manual.status_code == 201, manual.text
    return {"doctors": doctors, "task": task_id, "manual": manual.json()["id"]}


def test_单条转派_接手的人收到_原责任人不再新增(client, admin, world):
    d = world["doctors"]
    got = client.post(f"{B}/tasks/{world['task']}/assign", headers=admin,
                      json={"assignee_id": d["p2885_d2"], "note": "医生一休假"})
    assert got.status_code == 200, got.text
    assert _notices(d["p2885_d2"], world["task"]) == 1   # 修前 0
    assert _notices(d["p2885_d1"], world["task"]) == 1   # 原责任人不再新增


def test_批量分配_与单条同一句(client, admin, world):
    d = world["doctors"]
    got = client.post(f"{B}/tasks/batch", headers=admin, json={
        "task_ids": [world["task"]], "action": "assign", "assignee_id": d["p2885_d3"]})
    assert got.status_code == 200 and got.json()["processed"] == 1, got.text
    assert _notices(d["p2885_d3"], world["task"]) == 1   # 修前 0


def test_其余任务转派不发(client, admin, world):
    d = world["doctors"]
    got = client.post(f"{B}/tasks/{world['manual']}/assign", headers=admin, json={"assignee_id": d["p2885_d2"]})
    assert got.status_code == 200, got.text
    assert _notices(d["p2885_d2"], world["manual"]) == 0   # 手工建的特急任务建时不发、转派也不发
