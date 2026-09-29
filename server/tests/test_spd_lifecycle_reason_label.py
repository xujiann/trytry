"""生命周期收尾的理由写事件中文名，不再把编码拼进给人看的文字（P2-767，第二十批「界面文案 vs 行为」扫描 M2-4；P2-73 同族）。

登记死亡 / 排除 / 迁出 / 召回会收尾在途工作（`service.close_open_work`），理由原先拼成 `f"{event}:{reason}"`——「death:心源性
猝死」落进被取消任务的审核意见、复诊日志与外呼撤回原因，居民端健康任务、管理端任务详情照印（标题还是「审核意见」）。
自动干预的目标同形（「high风险自动干预」），改写中文分层，由 `test_spd_intervention_auto_race.py` 钉住。
"""
import pytest

from app.database import SessionLocal
from app.spd.models import SpdCallTask, SpdRevisit, SpdTask

B = "/api/spd"


def _login(client, username):
    token = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2767 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p2767_doc", "password": "passw0rd1", "full_name": "P2767 张医生", "role": "doctor", "org_id": org})
    assert created.status_code in (200, 201), created.text
    return {"org": org, "doctor": _login(client, "p2767_doc"), "n": 0}


def _enrolled_with_work(client, admin, world):
    """一份在管档案，名下一条待办任务、一条待复诊、一条从复诊转出的待呼叫。"""
    world["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2767 患者{world['n']}", "id_card": f"33010219500101{2766 + world['n']:04d}"}).json()["id"]
    doctor = world["doctor"]
    client.post("/api/encounters", headers=doctor, json={"patient_id": patient, "org_id": world["org"],
                                                         "diagnosis_name": "高血压"})
    enrollment = client.post(f"{B}/enrollments", headers=doctor, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": world["org"]})
    assert enrollment.status_code == 201, enrollment.text
    task = client.post(f"{B}/tasks", headers=doctor, json={
        "patient_id": patient, "program_code": "hypertension", "task_type": "followup", "title": "上门测血压",
        "org_id": world["org"]})
    assert task.status_code == 201, task.text
    revisit = client.post(f"{B}/revisits", headers=doctor, json={
        "patient_id": patient, "program_code": "hypertension", "plan_date": "2026-12-01"})
    assert revisit.status_code == 201, revisit.text
    call = client.post(f"{B}/call-tasks", headers=doctor, json={
        "patient_id": patient, "ref_type": "revisit", "ref_id": revisit.json()["id"]})
    assert call.status_code == 201, call.text
    return enrollment.json()["id"], task.json()["id"], revisit.json()["id"], call.json()["id"]


def _texts(task_id, revisit_id, call_id):
    with SessionLocal() as db:
        return (db.get(SpdTask, task_id).review_note, db.get(SpdRevisit, revisit_id).log[-1]["note"],
                db.get(SpdCallTask, call_id).result)


def test_登记死亡_收尾理由写中文事件名(client, admin, world):
    enrollment, task, revisit, call = _enrolled_with_work(client, admin, world)
    resp = client.post(f"{B}/enrollments/{enrollment}/lifecycle", headers=world["doctor"],
                       json={"event": "death", "reason": "心源性猝死"})
    assert resp.status_code == 200, resp.text
    note, log, withdrawn = _texts(task, revisit, call)
    assert note == "死亡：心源性猝死"   # 修前 death:心源性猝死，居民端「审核意见」照印
    assert log == "死亡：心源性猝死"
    assert withdrawn == "复诊随档案结束移除（死亡：心源性猝死），撤出待呼叫"
    assert "death" not in note + log + withdrawn


def test_不写原因的只写事件名(client, admin, world):
    enrollment, task, revisit, call = _enrolled_with_work(client, admin, world)
    resp = client.post(f"{B}/enrollments/{enrollment}/lifecycle", headers=world["doctor"], json={"event": "exclude"})
    assert resp.status_code == 200, resp.text
    assert _texts(task, revisit, call)[0] == "排除"   # 修前「exclude:」
