"""慢专病任务的非终态写入不看终态：锁外读到「未结束」的一路把别人刚办结的任务改回未结束，再办一次随访计分再记一笔（P2-114）。

办结（`_finish_task`）早是一条条件 UPDATE（`WHERE status NOT IN 已结束`），「只计一次分、只推进一次路径」全靠这一下兜着——
积分流水没有唯一键。可别的写入口还是「内存里判未结束 → `task.status = …` → commit」，flush 出来的 UPDATE 只有 `WHERE id = ?`：

- 两个人同时审同一条：一路通过（办结、计分），一路退回——退回无条件写回，已办结的任务成了「已退回」，办理人重新提交、
  再审一次，随访计分再记一笔；
- 居民在手机上提交与医护当面办结同时到：已办结的任务被改回「待审核」，再审一次同样再记一笔；
- 转派与办结同时到：已办结的任务被改回「已接收」、回到待办；
- 批量取消与办结同时到：办完的任务被改成「已取消」，从完成数里消失；
- 超期扫描 / 结案收尾一趟载入整批、逐条改完才提交，窗口是整趟（这两处读的是查询，SQLite 上摆不出确定时序，真 PG 档见
  test_spd_task_terminal_overwrite_races.py）。

修法：`service.move_task` —— 状态翻转一律条件 UPDATE（`WHERE id = :id AND status IN (期望态)`），没翻到即 409 / 跳过；
审核通过与退回都只从「待审核」翻。

这里用「一路拿着先读到的对象、另一路先提交」把并发窗口钉成确定的时序（SQLite 与 PG 同样成立）。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.models import ResidentAccount
    from app.spd.models import SpdEnrollment

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2114 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    doctor = client.post("/api/users", headers=admin, json={
        "username": "p2114_vd", "password": "pw123456", "full_name": "P2114 村医", "role": "doctor", "org_id": org})
    assert doctor.status_code in (200, 201), doctor.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2114 患者", "id_card": "330106197003031134", "gender": "女", "birth_date": "1970-03-03"}).json()["id"]
    with SessionLocal() as db:
        enrollment = SpdEnrollment(patient_id=patient, program_code="P2114_PG", org_id=org, status="active",
                                   village_doctor_id=doctor.json()["id"])
        account = ResidentAccount(patient_id=patient, nickname="P2114 居民")
        db.add_all([enrollment, account])
        db.commit()
        return {"org": org, "patient": patient, "enrollment": enrollment.id, "account": account.id,
                "doctor": doctor.json()["id"]}


def _new_task(world, status):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        task = SpdTask(patient_id=world["patient"], enrollment_id=world["enrollment"], org_id=world["org"],
                       program_code="P2114_PG", task_type="followup", title="P2114 随访", status=status)
        db.add(task)
        db.commit()
        return task.id


def _status(task_id):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        return db.get(SpdTask, task_id).status


def _points(task_id):
    """这条任务记了几笔随访分（积分流水按 ref 找）。"""
    from app.database import SessionLocal
    from app.spd.models import SpdPointRecord

    with SessionLocal() as db:
        return db.query(SpdPointRecord).filter_by(ref_type="task", ref_id=task_id, direction="in").count()


def _admin(db):
    from app.models import User

    return db.query(User).filter_by(username="admin").one()


def test_两人同时审核_一人通过一人退回_已办结的不会被改成已退回_也不会再记一笔分(world):
    from fastapi import HTTPException

    from app.database import SessionLocal
    from app.spd.models import SpdTask
    from app.spd.routers.tasks import ReviewTaskIn, SubmitIn, review_task, submit_task

    task_id = _new_task(world, "submitted")
    with SessionLocal() as racer, SessionLocal() as winner:
        held = racer.get(SpdTask, task_id)   # noqa: F841 — 退回那一路先读到待审核；留着引用，身份映射是弱引用
        review_task(task_id, ReviewTaskIn(approved=True), db=winner, user=_admin(winner))   # 通过：办结、计分
        with pytest.raises(HTTPException) as exc:
            review_task(task_id, ReviewTaskIn(approved=False, note="重填"), db=racer, user=_admin(racer))
    assert exc.value.status_code == 409   # 修前不报错
    assert exc.value.detail == "只有待审核的任务可以审核"   # 与顺序重复审核同一句
    assert _status(task_id) == "done"   # 修前 rejected
    # 修前那条「已退回」的任务重新提交、再审通过，随访分再记一笔；修后它是已办结的，提交即 409
    with SessionLocal() as db, pytest.raises(HTTPException) as again:
        submit_task(task_id, SubmitIn(), db=db, user=_admin(db))
    assert again.value.status_code == 409 and again.value.detail == "该任务已结束"
    assert _points(task_id) == 1


def test_居民提交与医护办结同时到_已办结的不会被改回待审核(world):
    from fastapi import HTTPException

    from app.database import SessionLocal
    from app.models import ResidentAccount
    from app.spd.models import SpdTask
    from app.spd.routers.portal import TaskSubmitIn
    from app.spd.routers.portal import submit_task as portal_submit
    from app.spd.routers.tasks import SubmitIn, complete_task

    task_id = _new_task(world, "pending")
    with SessionLocal() as racer, SessionLocal() as winner:
        held = racer.get(SpdTask, task_id)   # noqa: F841 — 居民那一路先读到待办
        complete_task(task_id, SubmitIn(), db=winner, user=_admin(winner))              # 医护当面办结：计分
        with pytest.raises(HTTPException) as exc:
            portal_submit(task_id, TaskSubmitIn(result={"bp": "130/80"}), db=racer,
                          account=racer.get(ResidentAccount, world["account"]))
    assert exc.value.status_code == 409 and exc.value.detail == "该任务已结束"   # 修前不报错
    assert _status(task_id) == "done"   # 修前 submitted：回到审核队列，再审一次再记一笔
    assert _points(task_id) == 1


def test_转派与办结同时到_已办结的不会被改回已接收(world):
    from fastapi import HTTPException

    from app.database import SessionLocal
    from app.spd.models import SpdTask
    from app.spd.routers.tasks import AssignIn, SubmitIn, assign_task, complete_task

    task_id = _new_task(world, "pending")
    with SessionLocal() as racer, SessionLocal() as winner:
        held = racer.get(SpdTask, task_id)   # noqa: F841 — 转派那一路先读到待接收
        complete_task(task_id, SubmitIn(), db=winner, user=_admin(winner))
        with pytest.raises(HTTPException) as exc:
            assign_task(task_id, AssignIn(assignee_id=world["doctor"]), db=racer, user=_admin(racer))
    assert exc.value.status_code == 409 and exc.value.detail == "已结束的任务不可再分配"   # 修前不报错
    assert _status(task_id) == "done"   # 修前 claimed：回到待办


def test_转派只把待接收的改成已接收_别的状态照旧(world):
    """转派的状态改写按行上的现值判（CASE），不按锁外读到的：办理中的转派后仍是办理中。"""
    from app.database import SessionLocal
    from app.spd.routers.tasks import AssignIn, assign_task

    pending, doing = _new_task(world, "pending"), _new_task(world, "doing")
    for task_id in (pending, doing):
        with SessionLocal() as db:
            assign_task(task_id, AssignIn(assignee_id=world["doctor"]), db=db, user=_admin(db))
    assert (_status(pending), _status(doing)) == ("claimed", "doing")


def test_批量取消与办结同时到_办完的不会被改成已取消(world):
    from app.database import SessionLocal
    from app.spd.models import SpdTask
    from app.spd.routers.tasks import BatchTaskIn, SubmitIn, batch_tasks, complete_task

    task_id = _new_task(world, "pending")
    with SessionLocal() as racer, SessionLocal() as winner:
        held = racer.get(SpdTask, task_id)   # noqa: F841 — 批量那一路先读到待办（同一会话里再查也还是这份旧值）
        complete_task(task_id, SubmitIn(), db=winner, user=_admin(winner))
        out = batch_tasks(BatchTaskIn(task_ids=[task_id], action="cancel"), db=racer, user=_admin(racer))
    assert out == {"processed": 0, "skipped": [{"id": task_id, "reason": "任务已结束"}]}   # 修前 processed 1
    assert _status(task_id) == "done"   # 修前 cancelled：办完的工作从完成数里消失
