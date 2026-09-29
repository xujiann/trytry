"""待审核的慢专病任务不能再「保存草稿 / 提交审核」一遍（P2-758，第二十批「状态机迁移」扫描 M4-1）。

`submit_task` 原先只挡已办结 / 已取消，两处翻转按「未结束」放行（含待审核）：甲提交随访（收缩压 150/95）等审核，
乙在没刷新的页面上点「保存草稿」（上传佐证走的也是它）照 200——任务回到办理中、审核队列里没了，甲的结果被换成
`{note: ""}`，审核人再审 409；乙点「提交审核」则把结果换成乙写的，审核人通过的是乙的内容。P2-244 早定了「提交了等审核
的任务只能由审核人审」，两端界面对待审核的也不给提交与上传佐证，接口没挡。

修法：待审核的回 409（与办结同一句），两处翻转只从 `TASK_COMPLETABLE_STATUSES` 翻；已退回的照旧能改了再交。
"""
import pytest

PROGRAM = "P2758_PG"
RESULT = {"bp": "150/95", "note": "甲的随访结果：血压控制不佳"}


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2758 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2758 患者", "id_card": "330106197104042758", "gender": "女", "birth_date": "1971-04-04"}).json()["id"]
    with SessionLocal() as db:
        enrollment = SpdEnrollment(patient_id=patient, program_code=PROGRAM, org_id=org, status="active")
        db.add(enrollment)
        db.commit()
        return {"org": org, "patient": patient, "enrollment": enrollment.id}


def _new_task(world, status):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        task = SpdTask(patient_id=world["patient"], enrollment_id=world["enrollment"], org_id=world["org"],
                       program_code=PROGRAM, task_type="followup", title="P2758 随访", status=status, result=RESULT)
        db.add(task)
        db.commit()
        return task.id


def _state(task_id):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        task = db.get(SpdTask, task_id)
        return task.status, task.result


@pytest.mark.parametrize("body", [{"result": {"note": ""}, "draft": True}, {"result": {"note": "乙随手写的"}}],
                         ids=["保存草稿", "再次提交审核"])
def test_待审核的任务再提交_409且结果不动_审核人照常审(client, admin, world, body):
    from app.spd.routers.tasks import AWAITING_REVIEW

    task = _new_task(world, "submitted")
    resp = client.post(f"/api/spd/tasks/{task}/submit", headers=admin, json=body)
    assert resp.status_code == 409, resp.text   # 修前 200：草稿拉回办理中、移出审核队列；再提交把甲的结果换成乙的
    assert resp.json()["detail"] == AWAITING_REVIEW
    assert _state(task) == ("submitted", RESULT)
    reviewed = client.post(f"/api/spd/tasks/{task}/review", headers=admin, json={"approved": True, "note": "同意"})
    assert reviewed.status_code == 200, reviewed.text
    assert (reviewed.json()["status"], reviewed.json()["result"]) == ("done", RESULT)


def test_锁外读到办理中_这时别人刚提交审核_草稿不把它拉回来(client, admin, world, monkeypatch):
    from app.database import SessionLocal
    from app.spd.models import SpdTask
    from app.spd.routers import tasks as tasks_router
    from app.spd.routers.tasks import AWAITING_REVIEW

    task = _new_task(world, "doing")
    real_move = tasks_router.move_task
    fired = []

    def submitted_meanwhile(db, task_id, to_status, **kwargs):
        if not fired:   # 草稿那一路读完「办理中」、条件翻转之前：办理人那一路先提交审核
            fired.append(True)
            with SessionLocal() as other:
                other.get(SpdTask, task_id).status = "submitted"
                other.commit()
        return real_move(db, task_id, to_status, **kwargs)

    monkeypatch.setattr(tasks_router, "move_task", submitted_meanwhile)
    resp = client.post(f"/api/spd/tasks/{task}/submit", headers=admin, json={"result": {"note": ""}, "draft": True})
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200：刚提交审核的被拉回办理中
    assert resp.json()["detail"] == AWAITING_REVIEW
    assert _state(task) == ("submitted", RESULT)


@pytest.mark.parametrize("status", ["pending", "claimed", "doing", "rejected", "overdue"])
def test_没提交的与已退回的照旧能存草稿_能提交(client, admin, world, status):
    task = _new_task(world, status)
    draft = client.post(f"/api/spd/tasks/{task}/submit", headers=admin, json={"result": {"note": "草稿"}, "draft": True})
    assert draft.status_code == 200, draft.text
    assert _state(task) == ("doing", {"note": "草稿"})
    submitted = client.post(f"/api/spd/tasks/{task}/submit", headers=admin, json={"result": {"note": "办完"}})
    assert submitted.status_code == 200, submitted.text
    assert _state(task) == ("submitted", {"note": "办完"})


def test_已结束的照旧回已结束(client, admin, world):
    task = _new_task(world, "cancelled")
    resp = client.post(f"/api/spd/tasks/{task}/submit", headers=admin, json={"result": {}})
    assert resp.status_code == 409 and resp.json()["detail"] == "该任务已结束"
