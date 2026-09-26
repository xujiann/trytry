"""执行随访整段覆盖结果与证据：先前接通的呼叫回写（沟通结果、录音地址）被抹掉（P2-291）。

「回写通话结果」写明「接通结果会同步写回随访记录」，`record_call_result` 为了两路回写不互相覆盖还专门进了临界区追加；
`execute_followup` 却是 `record.result = body.result`、有证据时 `record.evidence = body.evidence`——界面上「随访结果」
留空也照样覆盖成空串。电话随访的常见走法正是「先打电话回写、再执行随访填问卷」，走完一遍，通话的沟通结果就没了；
接口带证据执行时，录音地址也没了。

修法：执行时结果与证据改为追加，与呼叫回写同一个临界区（锁随访记录这一行、重读、再追加）。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"
URL = "https://rec/2291.mp3"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2291 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2291 患者", "id_card": "330127197309092291"}).json()["id"]
    return {"org": org, "patient": patient}


def _record(world, status="planned", result="", evidence=None):
    from app.spd.models import SpdFollowupRecord

    with SessionLocal() as db:
        record = SpdFollowupRecord(patient_id=world["patient"], org_id=world["org"], planned_at="2026-09-26",
                                   status=status, result=result, evidence=evidence or [])
        db.add(record)
        db.commit()
        return record.id


def _row(record_id):
    from app.spd.models import SpdFollowupRecord

    with SessionLocal() as db:
        record = db.get(SpdFollowupRecord, record_id)
        return record.status, record.result, record.evidence


def _call_back(client, admin, world, record_id):
    from app.spd.models import SpdCallTask

    with SessionLocal() as db:
        task = SpdCallTask(patient_id=world["patient"], phone="13800002291", ref_type="followup", ref_id=record_id)
        db.add(task)
        db.commit()
        task_id = task.id
    got = client.post(f"{B}/call-tasks/{task_id}/result", headers=admin, json={
        "status": "connected", "duration_s": 80, "record_url": URL, "result": "血压控制平稳"})
    assert got.status_code == 200, got.text


def test_先回写通话再执行_随访结果留空_通话结果与录音都在(client, admin, world):
    record_id = _record(world)
    _call_back(client, admin, world, record_id)
    done = client.post(f"{B}/followup-records/{record_id}/execute", headers=admin,
                       json={"channel": "phone", "result": "", "answers": {}})   # 界面上「随访结果」留空
    assert done.status_code == 200, done.text
    assert _row(record_id) == ("done", "血压控制平稳", [URL])   # 修前 ("done", "", [URL])


def test_执行带结果与证据_追加在通话回写之后(client, admin, world):
    record_id = _record(world)
    _call_back(client, admin, world, record_id)
    done = client.post(f"{B}/followup-records/{record_id}/execute", headers=admin,
                       json={"result": "按时服药", "evidence": ["https://pic/2291.jpg"], "answers": {}})
    assert done.status_code == 200, done.text
    assert done.json()["result"] == "血压控制平稳 按时服药"
    assert _row(record_id) == ("done", "血压控制平稳 按时服药", [URL, "https://pic/2291.jpg"])   # 修前只剩本次的


def test_记失访同样追加(client, admin, world):
    record_id = _record(world, result="首次电话占线")
    done = client.post(f"{B}/followup-records/{record_id}/execute", headers=admin,
                       json={"unreachable": True, "result": "三次未接通"})
    assert done.status_code == 200, done.text
    assert _row(record_id) == ("unreachable", "首次电话占线 三次未接通", [])


def test_读到记录之后才提交的通话回写_执行时照样留住(client, admin, world, monkeypatch):
    """锁外读到的记录是旧值：临界区里不先重读，就用旧的空结果把刚提交的通话回写盖掉。"""
    from app.spd.models import SpdFollowupRecord
    from app.spd.routers import followup

    record_id = _record(world)
    real = followup.assert_org_writable

    def racing(db, user, org_id):
        real(db, user, org_id)
        with SessionLocal() as other:   # 这一路读到记录之后，一条接通的回写先提交
            row = other.get(SpdFollowupRecord, record_id)
            row.result, row.evidence = "血压控制平稳", [URL]
            other.commit()

    monkeypatch.setattr(followup, "assert_org_writable", racing)
    done = client.post(f"{B}/followup-records/{record_id}/execute", headers=admin,
                       json={"result": "按时服药", "answers": {}})
    monkeypatch.undo()
    assert done.status_code == 200, done.text
    assert _row(record_id) == ("done", "血压控制平稳 按时服药", [URL])
