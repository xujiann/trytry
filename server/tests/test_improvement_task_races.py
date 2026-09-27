"""绩效整改任务的确认与登记进展不看是不是刚被别人改过：两边各一半拼进同一行（P2-463）。

`POST /api/performance/improvements/{id}/verify` 与 `/progress` 原先判状态、赋值、commit，UPDATE 只有 `WHERE id = ?`：

- 两位管理者一个点「确认关闭」、一个点「退回」，两路都 200——库里拼出两边各一半：整改中却带着确认时间，
  确认意见与确认人记成后写的那位（按顺序点第二下是 409「仅已提交完成的任务可确认」）；
- 「提交完成」与另一路「登记进展」交错，后写的照旧把刚提交的改回整改中、完成时间还留着——P2-192 挡住的那一步
  从并发绕了回来，任务悄悄退出管理层的待确认队列。

修法：两处都走条件翻转（`concurrency.move_row`，确认要 `status = 'completed'`，登记进展要 `status IN (open, in_progress)`），
抢输的一路 409、与顺序调用同一句。这里把「判过了、还没写」钉成确定的时序：机构归属校验之后、写入之前，
让另一路先改并提交。真并发取证见 `test_improvement_task_pg_races.py`。
"""
from datetime import datetime

import pytest

from app.database import SessionLocal


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P2463 卫生院", "org_type": "township", "level": "township"}).json()["id"]


def _task(client, admin, org, *, submitted: bool) -> int:
    created = client.post("/api/performance/improvements", headers=admin, json={
        "org_id": org, "problem": "P2463 慢病随访率偏低", "owner_name": "张三", "due_date": "2030-01-01"})
    assert created.status_code == 201, created.text
    tid = created.json()["id"]
    if submitted:
        done = client.post(f"/api/performance/improvements/{tid}/progress", headers=admin,
                           json={"complete": True, "completion_note": "已补做随访 30 人"})
        assert done.status_code == 200 and done.json()["status"] == "completed", done.text
    return tid


def _meanwhile(monkeypatch, tid, **values):
    """机构归属校验之后（状态判定、写入之前），另一路把这条任务改成 values 并提交。"""
    from app.models import ImprovementTask
    from app.routers import performance

    real, fired = performance.assert_obj_org_writable, []

    def racing(*args, **kwargs):
        result = real(*args, **kwargs)
        if not fired:
            fired.append(True)
            with SessionLocal() as other:
                row = other.get(ImprovementTask, tid)
                for key, value in values.items():
                    setattr(row, key, value)
                other.commit()
        return result

    monkeypatch.setattr(performance, "assert_obj_org_writable", racing)
    return fired


def _row(tid):
    from app.models import ImprovementTask

    with SessionLocal() as db:
        return db.get(ImprovementTask, tid)


def test_一个确认关闭一个退回_后到的409_先确认的那份不被拼坏(client, admin, org, monkeypatch):
    tid = _task(client, admin, org, submitted=True)
    fired = _meanwhile(monkeypatch, tid, status="verified", verified_at=datetime(2026, 9, 27, 8, 0),
                       verified_by="管理者甲", verify_comment="同意关闭")
    resp = client.post(f"/api/performance/improvements/{tid}/verify", headers=admin,
                       json={"approve": False, "comment": "佐证不足"})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200
    assert resp.json() == {"detail": "仅已提交完成的任务可确认"}
    row = _row(tid)
    # 修前：status 被改回 in_progress、completed_at 被清空，verified_at 却还是甲写的——整改中带着确认时间
    assert (row.status, row.verified_by, row.verify_comment) == ("verified", "管理者甲", "同意关闭")
    assert row.completed_at is not None and row.verified_at is not None


def test_提交完成之后另一路登记进展_后到的409_不把任务改回整改中(client, admin, org, monkeypatch):
    tid = _task(client, admin, org, submitted=False)
    fired = _meanwhile(monkeypatch, tid, status="completed", completion_note="已补做随访 30 人",
                       completed_at=datetime(2026, 9, 27, 8, 0))
    resp = client.post(f"/api/performance/improvements/{tid}/progress", headers=admin,
                       json={"measures": "再补 5 人"})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200、状态改回 in_progress
    assert resp.json() == {"detail": "已提交完成、待确认——确认不通过退回后再登记进展"}
    row = _row(tid)
    assert (row.status, row.completion_note, row.measures) == ("completed", "已补做随访 30 人", "")


def test_已被确认关闭之后另一路登记进展_409说已确认关闭(client, admin, org, monkeypatch):
    tid = _task(client, admin, org, submitted=False)
    fired = _meanwhile(monkeypatch, tid, status="verified", completion_note="已补做随访 30 人",
                       completed_at=datetime(2026, 9, 27, 8, 0), verified_at=datetime(2026, 9, 27, 9, 0),
                       verified_by="管理者甲")
    resp = client.post(f"/api/performance/improvements/{tid}/progress", headers=admin,
                       json={"complete": True, "completion_note": "又补了 5 人"})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200、已关闭的任务被改回待确认
    assert resp.json() == {"detail": "任务已确认关闭"}
    row = _row(tid)
    assert (row.status, row.completion_note) == ("verified", "已补做随访 30 人")


def test_按顺序走完整条链照旧(client, admin, org):
    tid = _task(client, admin, org, submitted=False)
    base = f"/api/performance/improvements/{tid}"
    step = client.post(f"{base}/progress", headers=admin, json={"measures": "组织专项培训"})
    assert step.status_code == 200 and step.json()["status"] == "in_progress", step.text
    assert step.json()["measures"] == "组织专项培训"
    step = client.post(f"{base}/progress", headers=admin, json={"complete": True, "completion_note": "抽查达标"})
    assert step.status_code == 200 and step.json()["status"] == "completed", step.text
    assert step.json()["completed_at"] and step.json()["measures"] == "组织专项培训"
    step = client.post(f"{base}/verify", headers=admin, json={"approve": False, "comment": "样本不足"})
    assert step.status_code == 200, step.text
    assert (step.json()["status"], step.json()["completed_at"], step.json()["verify_comment"]) == (
        "in_progress", None, "样本不足")
    step = client.post(f"{base}/progress", headers=admin, json={"complete": True, "completion_note": "补足样本"})
    assert step.status_code == 200 and step.json()["status"] == "completed", step.text
    step = client.post(f"{base}/verify", headers=admin, json={"approve": True, "comment": "同意"})
    assert step.status_code == 200 and step.json()["status"] == "verified", step.text
    again = client.post(f"{base}/verify", headers=admin, json={"approve": False})
    assert again.status_code == 409 and again.json() == {"detail": "仅已提交完成的任务可确认"}
    again = client.post(f"{base}/progress", headers=admin, json={"measures": "再补"})
    assert again.status_code == 409 and again.json() == {"detail": "任务已确认关闭"}
    row = _row(tid)
    assert row.verified_at is not None and row.completed_at is not None and row.verified_by
