"""呼叫结果「只回写一次」只是锁外预检：重发的回调与首次回写交错，两路都 200，随访记录被追加两遍（P2-289）。

`record_call_result` 的注释写「结果只回写一次（P2-87）……已有结果的再写一次，只会把接通的通话、要回听的录音地址与沟通
结果事后改掉」，实现却是「判待呼叫 → 逐字段赋值 → commit」，UPDATE 只有 `WHERE id = ?`。网关超时重发回调、或网关回调
与坐席手工回写同时到，两路都读到待呼叫：都 200，接通时两路先后往关联的随访记录各追加一遍沟通结果与录音地址。

修法：翻转压进一条 `WHERE status = 'pending'` 的 UPDATE（`service.settle_call_task`），后到的一路 409、不再追加。
这里把「这一路读到待呼叫之后、写入之前，另一路先回写完」钉成确定的时序：在两者之间必经的随访记录机构判定里插进
另一路的提交。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2289 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2289 患者", "id_card": "330127197309092289"}).json()["id"]
    return {"org": org, "patient": patient}


def _task_with_record(world):
    from app.spd.models import SpdCallTask, SpdFollowupRecord

    with SessionLocal() as db:
        record = SpdFollowupRecord(patient_id=world["patient"], org_id=world["org"], planned_at="2026-09-26")
        db.add(record)
        db.flush()
        task = SpdCallTask(patient_id=world["patient"], phone="13800002289", ref_type="followup", ref_id=record.id)
        db.add(task)
        db.commit()
        return task.id, record.id


def _first_writeback_behind(monkeypatch, task_id, record_id):
    """这一路读到待呼叫之后，另一路（首次回调）先把结果回写完并提交。"""
    from app.spd.models import SpdCallTask, SpdFollowupRecord
    from app.spd.routers import followup

    real = followup.assert_org_writable

    def racing(db, user, org_id):
        real(db, user, org_id)
        with SessionLocal() as other:
            task = other.get(SpdCallTask, task_id)
            task.status, task.result, task.record_url = "connected", "血压控制平稳", "https://rec/2289.mp3"
            record = other.get(SpdFollowupRecord, record_id)
            record.result, record.evidence = "血压控制平稳", ["https://rec/2289.mp3"]
            other.commit()

    monkeypatch.setattr(followup, "assert_org_writable", racing)


def test_重发回调与首次回写交错_只成一路_随访记录不追加两遍(client, admin, world, monkeypatch):
    from app.spd.models import SpdFollowupRecord

    task_id, record_id = _task_with_record(world)
    _first_writeback_behind(monkeypatch, task_id, record_id)
    got = client.post(f"{B}/call-tasks/{task_id}/result", headers=admin, json={
        "status": "connected", "duration_s": 95, "record_url": "https://rec/2289.mp3", "result": "血压控制平稳"})
    monkeypatch.undo()
    assert got.status_code == 409, got.text   # 修前 200
    assert got.json()["detail"] == "该呼叫任务已回写过结果"   # 与顺序重复回写同一句
    with SessionLocal() as db:
        record = db.get(SpdFollowupRecord, record_id)
        assert record.result == "血压控制平稳"   # 修前「血压控制平稳 血压控制平稳」
        assert record.evidence == ["https://rec/2289.mp3"]   # 修前同一录音地址两遍


def test_不并发时照常回写且接通结果写回随访记录(client, admin, world):
    from app.spd.models import SpdCallTask, SpdFollowupRecord

    task_id, record_id = _task_with_record(world)
    got = client.post(f"{B}/call-tasks/{task_id}/result", headers=admin, json={
        "status": "connected", "duration_s": 60, "record_url": "https://rec/2289b.mp3", "result": "按时服药"})
    assert got.status_code == 200, got.text
    assert (got.json()["status"], got.json()["duration_s"]) == ("connected", 60)
    with SessionLocal() as db:
        task = db.get(SpdCallTask, task_id)
        assert task.started_at is not None and task.operator_id is not None
        record = db.get(SpdFollowupRecord, record_id)
        assert (record.result, record.evidence) == ("按时服药", ["https://rec/2289b.mp3"])
    again = client.post(f"{B}/call-tasks/{task_id}/result", headers=admin, json={"status": "failed"})
    assert again.status_code == 409, again.text
