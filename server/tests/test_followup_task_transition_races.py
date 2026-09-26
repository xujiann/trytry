"""平台随访任务的完成与取消不看状态是不是刚被别人改了：已完成的随访被改成已取消（P2-346）。

`POST /api/followups/{id}/complete` 与 `/cancel` 都是「内存里判待随访 → 赋值 → commit」，UPDATE 只有 `WHERE id = ?`。
完成与取消交错，已完成的随访被改成已取消——随访结果还挂在上面，完成率里却少了这一条；两人同时完成都 200，
先记的随访结果被后记的盖掉。

修法：两处走条件翻转（`concurrency.move_row`，`WHERE status = 'pending'`），抢输的一路按库里此刻的状态 409。
这里把「判过了、还没写」钉成确定的时序：归属校验之后、写入之前，让另一路先把任务走完并提交。
"""
import pytest

from app.database import SessionLocal

B = "/api/followups"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2346 随访卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2346 随访患者", "id_card": "330106197004042346"}).json()["id"]
    return {"org": org, "patient": patient}


def _task(client, admin, world):
    created = client.post(B, headers=admin, json={
        "patient_id": world["patient"], "org_id": world["org"], "category": "chronic", "due_date": "2026-09-30"})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _meanwhile(monkeypatch, task_id, **values):
    """归属校验之后（状态判定、写入之前），另一路把随访任务改成 `values` 并提交。"""
    from app.models import FollowupTask
    from app.routers import followups

    real, fired = followups.assert_obj_org_writable, []

    def racing(*args, **kwargs):
        result = real(*args, **kwargs)
        if not fired:
            fired.append(True)
            with SessionLocal() as other:
                row = other.get(FollowupTask, task_id)
                for key, value in values.items():
                    setattr(row, key, value)
                other.commit()
        return result

    monkeypatch.setattr(followups, "assert_obj_org_writable", racing)
    return fired


def _row(task_id):
    from app.models import FollowupTask

    with SessionLocal() as db:
        row = db.get(FollowupTask, task_id)
        return row.status, row.result


def test_取消与完成交错_已完成的不被改成已取消(client, admin, world, monkeypatch):
    task = _task(client, admin, world)
    fired = _meanwhile(monkeypatch, task, status="done", result="血压控制良好")
    resp = client.post(f"{B}/{task}/cancel", headers=admin)
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200
    assert resp.json() == {"detail": "当前状态 已完成 不可取消"}
    assert _row(task) == ("done", "血压控制良好")   # 修前 cancelled


def test_两人同时完成_先记的随访结果不被盖掉(client, admin, world, monkeypatch):
    task = _task(client, admin, world)
    fired = _meanwhile(monkeypatch, task, status="done", result="甲：血压控制良好")
    resp = client.post(f"{B}/{task}/complete", headers=admin, json={"result": "乙：头晕复诊"})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text
    assert resp.json() == {"detail": "当前状态 已完成 不可完成"}
    assert _row(task) == ("done", "甲：血压控制良好")


def test_没有竞争时照常完成与取消(client, admin, world):
    done = _task(client, admin, world)
    resp = client.post(f"{B}/{done}/complete", headers=admin, json={"result": "按时随访"})
    assert resp.status_code == 200 and resp.json() == {"id": done, "status": "done"}, resp.text
    cancelled = _task(client, admin, world)
    resp = client.post(f"{B}/{cancelled}/cancel", headers=admin)
    assert resp.status_code == 200 and resp.json() == {"id": cancelled, "status": "cancelled"}, resp.text
