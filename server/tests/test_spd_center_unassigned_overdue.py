"""中心端工作台「无人认领」只数待接收的：没人接的任务一超期就从这一格里消失（P2-245）。

`GET /api/spd/workbench/center` 先跑超期扫描，把过了截止日的待接收任务翻成「超期」，再数「无人认领」——原先只数
`status == "pending"`：没人接、已超期的任务恰是最该有人去接的，接收接口也照收它们（`TASK_CLAIMABLE_STATUSES`），
这一格却不数。同一栏的「我的待办」「全部待办」按病种筛，这一格原先也不看病种。

修法：按「没有责任人 + 能被接收（待接收、已超期）」数，病种筛选与同栏同口径。
"""
import pytest

PROGRAM = "P2245_PG"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2245 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2245 患者", "id_card": "330106197205052245", "gender": "男", "birth_date": "1972-05-05"}).json()["id"]
    return {"org": org, "patient": patient}


def _new_task(world, status, *, due_date=None, assignee_id=None):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        task = SpdTask(patient_id=world["patient"], org_id=world["org"], program_code=PROGRAM, task_type="followup",
                       title="P2245 随访", status=status, due_date=due_date, assignee_id=assignee_id)
        db.add(task)
        db.commit()
        return task.id


def _unassigned(client, admin, **params):
    resp = client.get("/api/spd/workbench/center", headers=admin, params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()["todo"]["unassigned"]


def test_没人接的任务超期之后仍算无人认领(client, admin, world):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    before = _unassigned(client, admin)
    overdue = _new_task(world, "pending", due_date="2020-01-01")   # 这一趟的超期扫描把它翻成超期
    _new_task(world, "pending")
    assert _unassigned(client, admin) == before + 2   # 修前 +1：超期的那条不数
    with SessionLocal() as db:
        assert db.get(SpdTask, overdue).status == "overdue"   # 防空转：真被扫成了超期


def test_无人认领按病种筛_有人接的与待审核的不算(client, admin, world):
    from app.database import SessionLocal
    from app.models import User

    with SessionLocal() as db:
        someone = db.query(User).filter_by(username="admin").one().id
    _new_task(world, "claimed", assignee_id=someone)
    _new_task(world, "overdue", assignee_id=someone)
    _new_task(world, "submitted")
    _new_task(world, "done")
    mine = _unassigned(client, admin, program_code=PROGRAM)
    # 本模块里没有责任人、能被接收的：上一条用例的超期与待接收各一（修前不看病种，数的是全库的待接收）
    assert mine == 2
    assert _unassigned(client, admin, program_code="P2245_NONE") == 0
