"""任务中心「已升级」把办结、取消了的也数进来：这一格只增不减、永远标红（P2-247）。

`GET /api/spd/tasks/summary` 的「已升级」原先数全部 `escalated=true` 的任务——升级过的任务办完了、取消了，照样算在里头，
任务中心顶部这一格只增不减，大于 0 就标红，永远是红的。同一个数字在中心端工作台（`workbench._task_stats`）与医生移动
工作台的升级提醒里一直只数未结束的；同一个接口里「超期」「今日到期」也都只数未结束的。

修法：只数未结束的里头升级过的（`TASK_OPEN_STATUSES`）。
"""
import pytest

PROGRAM = "P2247_PG"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2247 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2247 患者", "id_card": "330106197407072247", "gender": "男", "birth_date": "1974-07-07"}).json()["id"]
    return {"org": org, "patient": patient}


def _new_task(world, status, escalated):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        task = SpdTask(patient_id=world["patient"], org_id=world["org"], program_code=PROGRAM, task_type="followup",
                       title="P2247 随访", status=status, escalated=escalated, priority=2 if escalated else 1)
        db.add(task)
        db.commit()
        return task.id


def test_已升级只数未结束的_与中心端工作台同口径(client, admin, world):
    for status in ("claimed", "overdue", "submitted", "rejected"):
        _new_task(world, status, escalated=True)
    for status in ("done", "cancelled"):
        _new_task(world, status, escalated=True)
    _new_task(world, "claimed", escalated=False)
    summary = client.get("/api/spd/tasks/summary", headers=admin, params={"program_code": PROGRAM})
    assert summary.status_code == 200, summary.text
    assert summary.json()["escalated"] == 4   # 修前 6：办结、取消了的两条也算
    center = client.get("/api/spd/workbench/center", headers=admin, params={"program_code": PROGRAM}).json()
    assert center["todo"]["all"]["escalated"] == summary.json()["escalated"]
