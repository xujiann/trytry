"""「直接办结」接口收已提交待审核的任务：点一下办结就绕过了审核（P2-244）。

`POST /api/spd/tasks/{id}/complete` 的 docstring 写着「直接办结（不走审核的任务类型）」，审核接口只收待审核的，
管理端任务中心对待审核的只摆「审核」、医生移动端待审核的卡片不摆动作（P2-84 修前恰是「待审核的点办结则绕过了审核」）——
可接口本身按「未结束」放行：待审核的任务调一下办结就成了已办结、计了分、推进了路径，审核人再也审不到它。
两端界面挡住了按钮，接口没挡，条件翻转也按「未结束」翻：锁外读到办理中、这时办理人刚提交审核的，同样绕过审核。

修法：`service.TASK_COMPLETABLE_STATUSES`（未结束的除了待审核）——办结接口前置判定与条件 UPDATE 同一个集合；
待审核的回 409 并说清须由审核人审。
"""
import pytest

PROGRAM = "P2244_PG"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2244 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    doctor = client.post("/api/users", headers=admin, json={
        "username": "p2244_vd", "password": "pw123456", "full_name": "P2244 村医", "role": "doctor", "org_id": org})
    assert doctor.status_code in (200, 201), doctor.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2244 患者", "id_card": "330106197104042244", "gender": "女", "birth_date": "1971-04-04"}).json()["id"]
    with SessionLocal() as db:
        enrollment = SpdEnrollment(patient_id=patient, program_code=PROGRAM, org_id=org, status="active",
                                   village_doctor_id=doctor.json()["id"])
        db.add(enrollment)
        db.commit()
        return {"org": org, "patient": patient, "enrollment": enrollment.id}


def _new_task(world, status):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        task = SpdTask(patient_id=world["patient"], enrollment_id=world["enrollment"], org_id=world["org"],
                       program_code=PROGRAM, task_type="followup", title="P2244 随访", status=status)
        db.add(task)
        db.commit()
        return task.id


def _state(task_id):
    """（状态, 这条任务记了几笔随访分）"""
    from app.database import SessionLocal
    from app.spd.models import SpdPointRecord, SpdTask

    with SessionLocal() as db:
        points = db.query(SpdPointRecord).filter_by(ref_type="task", ref_id=task_id, direction="in").count()
        return db.get(SpdTask, task_id).status, points


def test_待审核的任务点办结_409且不动_审核通过才办结(client, admin, world):
    from app.spd.routers.tasks import AWAITING_REVIEW

    task = _new_task(world, "submitted")
    resp = client.post(f"/api/spd/tasks/{task}/complete", headers=admin, json={"result": {"note": "直接办了"}})
    assert resp.status_code == 409, resp.text   # 修前 200：已办结、计了分，审核人再也审不到
    assert resp.json()["detail"] == AWAITING_REVIEW
    assert _state(task) == ("submitted", 0)
    reviewed = client.post(f"/api/spd/tasks/{task}/review", headers=admin, json={"approved": True, "note": "同意"})
    assert reviewed.status_code == 200, reviewed.text
    assert _state(task) == ("done", 1)


def test_锁外读到办理中_这时办理人刚提交审核_办结同样不绕过审核(client, admin, world, monkeypatch):
    from app.database import SessionLocal
    from app.spd.models import SpdTask
    from app.spd.routers import tasks as tasks_router
    from app.spd.routers.tasks import AWAITING_REVIEW

    task = _new_task(world, "doing")
    real_now = tasks_router.now_naive
    fired = []

    def submitted_meanwhile():
        if not fired:   # 办结那一路读完「办理中」、条件翻转之前：办理人那一路先提交审核
            fired.append(True)
            with SessionLocal() as other:
                other.get(SpdTask, task).status = "submitted"
                other.commit()
        return real_now()

    monkeypatch.setattr(tasks_router, "now_naive", submitted_meanwhile)
    resp = client.post(f"/api/spd/tasks/{task}/complete", headers=admin, json={})
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200：刚提交审核的被直接办结
    assert resp.json()["detail"] == AWAITING_REVIEW
    assert _state(task) == ("submitted", 0)


@pytest.mark.parametrize("status", ["pending", "claimed", "doing", "rejected", "overdue"])
def test_不走审核的照旧能直接办结(client, admin, world, status):
    task = _new_task(world, status)
    resp = client.post(f"/api/spd/tasks/{task}/complete", headers=admin, json={"result": {"note": "办完"}})
    assert resp.status_code == 200, resp.text
    assert _state(task) == ("done", 1)


def test_已结束的照旧回已结束(client, admin, world):
    task = _new_task(world, "done")
    resp = client.post(f"/api/spd/tasks/{task}/complete", headers=admin, json={})
    assert resp.status_code == 409 and resp.json()["detail"] == "该任务已结束"
