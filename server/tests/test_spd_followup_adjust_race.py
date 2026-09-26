"""手工调整随访记录是锁外读改写：护士点「移除」的同时医生执行了这条随访，已完成被改成「已移除」（P2-287）。

`update_followup_record` 原先「读 → 判不是已完成 → 逐字段赋值 → commit」，UPDATE 只有 `WHERE id = ?`。执行随访
（`execute_followup` / 居民自助作答）走的是 `close_followup_record` 的条件翻转 planned → done，并在命中异常时派处置任务；
护士这边随后提交，done 被改成 removed——办完的随访从完成数、工作量、质控抽样池里消失，处置任务挂在一条「已移除」的随访上。

修法：改的列与「还没完成」压进同一条 `UPDATE … WHERE status IN (除已完成外的四个)`（`service.adjust_followup_record`），
影响 0 行就回滚、给与顺序请求同一句 409。这里把「这一路读到 planned 之后、写入之前，另一路先把它执行完」钉成确定的时序：
在两者之间必经的机构归属判定里插进另一路的提交。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2287 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2287 患者", "id_card": "330127197309092287"}).json()["id"]
    return {"org": org, "patient": patient}


def _record(world, status="planned"):
    from app.spd.models import SpdFollowupRecord

    with SessionLocal() as db:
        record = SpdFollowupRecord(patient_id=world["patient"], org_id=world["org"], planned_at="2026-09-20",
                                   status=status)
        db.add(record)
        db.commit()
        return record.id


def _status(record_id):
    from app.spd.models import SpdFollowupRecord

    with SessionLocal() as db:
        return db.get(SpdFollowupRecord, record_id).status


def _executed_behind(monkeypatch, record_id):
    """这一路读到记录之后，另一路先把它执行完并提交。"""
    from app.spd.models import SpdFollowupRecord
    from app.spd.routers import followup

    real = followup.assert_org_writable

    def racing(db, user, org_id):
        real(db, user, org_id)
        with SessionLocal() as other:
            other.get(SpdFollowupRecord, record_id).status = "done"
            other.commit()

    monkeypatch.setattr(followup, "assert_org_writable", racing)


def test_移除与执行并发_已完成不被改成已移除(client, admin, world, monkeypatch):
    record_id = _record(world)
    _executed_behind(monkeypatch, record_id)
    got = client.patch(f"{B}/followup-records/{record_id}", headers=admin, json={"status": "removed"})
    monkeypatch.undo()
    assert got.status_code == 409, got.text   # 修前 200
    assert got.json()["detail"] == "已完成的随访不可修改"   # 与顺序请求同一句
    assert _status(record_id) == "done"   # 修前 removed


def test_失访恢复与补录执行并发_已完成不被改回待随访(client, admin, world, monkeypatch):
    record_id = _record(world, status="unreachable")
    _executed_behind(monkeypatch, record_id)
    got = client.patch(f"{B}/followup-records/{record_id}", headers=admin,
                       json={"status": "planned", "planned_at": "2026-10-08"})
    monkeypatch.undo()
    assert got.status_code == 409, got.text   # 修前 200：能再执行一次、再派一条处置任务
    assert _status(record_id) == "done"


def test_不并发时照常调整(client, admin, world):
    record_id = _record(world)
    moved = client.patch(f"{B}/followup-records/{record_id}", headers=admin,
                         json={"planned_at": "2026-10-01", "channel": "wechat"})
    assert moved.status_code == 200, moved.text
    assert (moved.json()["planned_at"], moved.json()["channel"], moved.json()["status"]) == ("2026-10-01", "wechat", "planned")
    removed = client.patch(f"{B}/followup-records/{record_id}", headers=admin, json={"status": "removed"})
    assert removed.status_code == 200 and removed.json()["status"] == "removed", removed.text
    restored = client.patch(f"{B}/followup-records/{record_id}", headers=admin, json={"status": "planned"})
    assert restored.status_code == 200 and restored.json()["status"] == "planned", restored.text
