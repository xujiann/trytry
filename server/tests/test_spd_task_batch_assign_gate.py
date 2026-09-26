"""批量分配与单条分配是两种结果，且不看任务是不是刚被别人办结（P2-347）。

单条分配（`assign_task`，P2-114）判「未结束」、把待接收的转成已接收、写责任人是同一条 SQL；同一个文件里的批量分配
却是「内存里判过 → 往对象上赋值」：待接收的分配完还是待接收（单条是已接收），载入整批之后别人刚办结的任务照样被改了
责任人——计分记在原责任人名下，任务却显示归新人。

修法：批量分配走与单条同一个状态闸门（`move_task` + 待接收转已接收的 CASE），抢输的进 skipped。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2347 批量分配卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2347 患者", "id_card": "330127196901012347"}).json()["id"]
    users = {}
    for name in ("p2347_a", "p2347_b"):
        resp = client.post("/api/users", headers=admin, json={
            "username": name, "password": "passw0rd1", "role": "doctor", "full_name": name, "org_id": org})
        assert resp.status_code == 201, resp.text
        users[name] = resp.json()["id"]
    return {"org": org, "patient": patient, **users}


def _task(client, admin, world, title):
    resp = client.post(f"{B}/tasks", headers=admin, json={
        "patient_id": world["patient"], "title": title, "task_type": "followup", "org_id": world["org"]})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _row(task_id):
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        row = db.get(SpdTask, task_id)
        return row.status, row.assignee_id


def test_批量分配待接收的任务_与单条一样转成已接收(client, admin, world):
    single, batched = _task(client, admin, world, "P2347 单条"), _task(client, admin, world, "P2347 批量")
    assert client.post(f"{B}/tasks/{single}/assign", headers=admin,
                       json={"assignee_id": world["p2347_a"]}).status_code == 200
    resp = client.post(f"{B}/tasks/batch", headers=admin,
                       json={"task_ids": [batched], "action": "assign", "assignee_id": world["p2347_a"]})
    assert resp.status_code == 200 and resp.json() == {"processed": 1, "skipped": []}, resp.text
    assert _row(single) == _row(batched) == ("claimed", world["p2347_a"])   # 修前批量那条还是 pending


def test_载入整批之后任务被办结_批量分配跳过_不改责任人(client, admin, world, monkeypatch):
    from app.spd.models import SpdTask
    from app.spd.routers import tasks

    task = _task(client, admin, world, "P2347 竞争")
    assert client.post(f"{B}/tasks/{task}/assign", headers=admin,
                       json={"assignee_id": world["p2347_a"]}).status_code == 200
    real, fired = tasks.assert_org_writable, []

    def racing(*args, **kwargs):
        result = real(*args, **kwargs)
        if not fired:
            fired.append(True)
            with SessionLocal() as other:
                other.get(SpdTask, task).status = "done"
                other.commit()
        return result

    monkeypatch.setattr(tasks, "assert_org_writable", racing)
    resp = client.post(f"{B}/tasks/batch", headers=admin,
                       json={"task_ids": [task], "action": "assign", "assignee_id": world["p2347_b"]})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"processed": 0, "skipped": [{"id": task, "reason": "任务已结束"}]}   # 修前 processed 1
    assert _row(task) == ("done", world["p2347_a"])   # 修前责任人被改成 b
