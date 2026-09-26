"""慢专病批量升级是读改写：另一路刚把优先级调到「特急」，批量升级写回的 `max(旧值, 2)` 把它压回「紧急」（P2-246）。

单条升级（`escalate_task`）早在 P2-194 改成两条带条件的 UPDATE、只往上抬；批量处理（`POST /api/spd/tasks/batch`，
action=escalate）还是 `task.escalated, task.priority = True, max(task.priority, 2)`——用的是整批载入时读到的旧优先级。
读改写闸门只认单个属性的赋值，元组赋值这一形状整个看不见，批量版就躲在这个盲区里。

修法：单条与批量共用 `_mark_escalated`；闸门认元组赋值（自证见 `test_stage14_concurrency.py`）。
这里用「批量那一路载入整批之后、写库之前，另一路先把优先级调到特急并提交」把并发窗口钉成确定的时序。
"""
import pytest

PROGRAM = "P2246_PG"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2246 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2246 患者", "id_card": "330106197306062246", "gender": "女", "birth_date": "1973-06-06"}).json()["id"]
    return {"org": org, "patient": patient}


def _new_task(world, priority=1):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        task = SpdTask(patient_id=world["patient"], org_id=world["org"], program_code=PROGRAM, task_type="followup",
                       title="P2246 随访", status="claimed", priority=priority)
        db.add(task)
        db.commit()
        return task.id


def _row(task_id):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        task = db.get(SpdTask, task_id)
        return task.escalated, task.priority


def test_批量升级载入之后别人调成特急_不被压回紧急(client, admin, world, monkeypatch):
    from app.database import SessionLocal
    from app.spd.models import SpdTask
    from app.spd.routers import tasks as tasks_router

    task = _new_task(world, priority=1)
    real_guard = tasks_router.assert_org_writable
    fired = []

    def raised_meanwhile(db, user, org_id):
        if not fired:   # 批量那一路载入整批之后、写库之前：另一路先把它调成特急
            fired.append(True)
            with SessionLocal() as other:
                other.get(SpdTask, task).priority = 3
                other.commit()
        return real_guard(db, user, org_id)

    monkeypatch.setattr(tasks_router, "assert_org_writable", raised_meanwhile)
    resp = client.post("/api/spd/tasks/batch", headers=admin, json={"task_ids": [task], "action": "escalate"})
    assert resp.status_code == 200, resp.text
    assert fired and resp.json()["processed"] == 1
    assert _row(task) == (True, 3)   # 修前 (True, 2)：特急被压回紧急


def test_批量升级照旧只往上抬到紧急(client, admin, world):
    low, high = _new_task(world, priority=1), _new_task(world, priority=3)
    resp = client.post("/api/spd/tasks/batch", headers=admin, json={"task_ids": [low, high], "action": "escalate"})
    assert resp.status_code == 200 and resp.json()["processed"] == 2
    assert _row(low) == (True, 2) and _row(high) == (True, 3)
