"""随访办结 / 移除 / 档案结束之后，挂在它上面的外呼照旧「待呼叫」（P2-498，第九批「终止类收尾」扫描 W1-4）。

呼叫任务把随访转成人工外呼（`ref_type=followup`、`ref_id` 指随访记录）；随访在门诊当面做完、被手工移除、患者死亡结案
一并收走之后，`close_followup_record` / `adjust_followup_record` / `close_open_work` 都不动呼叫任务——坐席队列里照旧
「待呼叫」，照单打过去，打给的是已经随访过的人，甚至是死者家属。

修法：三处收尾连带把**待人工外呼**的取消（结果写明缘由）；已接通 / 未接通的是通话留痕不动；失访不收（还能补录）；
接了呼叫中心网关的不收——已派发的呼叫撤不回，置成取消只会让网关回调 409、丢掉真实发生过的通话记录。
"""
import pytest

from app import clock

B = "/api/spd"
PROGRAM = "p2498_htn"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2498 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2498 患者", "id_card": "330281196006062498", "phone": "13900024980"}).json()["id"]
    assert client.post(f"{B}/programs", headers=admin, json={
        "code": PROGRAM, "name": "P2498 高血压", "category": "chronic"}).status_code == 201
    with SessionLocal() as db:
        enrollment = SpdEnrollment(patient_id=patient, program_code=PROGRAM, org_id=org, status="active")
        db.add(enrollment)
        db.commit()
        return {"org": org, "patient": patient, "enrollment": enrollment.id}


def _record(world):
    from app.database import SessionLocal
    from app.spd.models import SpdFollowupRecord

    with SessionLocal() as db:
        record = SpdFollowupRecord(patient_id=world["patient"], program_code=PROGRAM, org_id=world["org"],
                                   planned_at=clock.today().isoformat(), status="planned")
        db.add(record)
        db.commit()
        return record.id


def _call(client, admin, world, record_id):
    resp = client.post(f"{B}/call-tasks", headers=admin, json={
        "patient_id": world["patient"], "ref_type": "followup", "ref_id": record_id})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _call_state(task_id):
    from app.database import SessionLocal
    from app.spd.models import SpdCallTask

    with SessionLocal() as db:
        row = db.get(SpdCallTask, task_id)
        return row.status, row.result


def test_门诊当面做完_外呼取消_失访的不收(client, admin, world):
    done, lost = _record(world), _record(world)
    done_call, lost_call = _call(client, admin, world, done), _call(client, admin, world, lost)
    resp = client.post(f"{B}/followup-records/{done}/execute", headers=admin,
                       json={"answers": {}, "channel": "visit", "result": "门诊当面随访"})
    assert resp.status_code == 200, resp.text
    assert _call_state(done_call) == ("cancelled", "随访已办结，呼叫取消")   # 修前 pending
    resp = client.post(f"{B}/followup-records/{lost}/execute", headers=admin,
                       json={"answers": {}, "channel": "phone", "unreachable": True})
    assert resp.status_code == 200, resp.text
    assert _call_state(lost_call)[0] == "pending"   # 失访还能补录，外呼接着打


def test_手工移除_外呼取消_已接通的留痕不动(client, admin, world):
    record = _record(world)
    connected = _call(client, admin, world, record)
    settled = client.post(f"{B}/call-tasks/{connected}/result", headers=admin,
                          json={"status": "connected", "duration_s": 30, "result": "约了下周复诊"})
    assert settled.status_code == 200, settled.text
    waiting = _call(client, admin, world, record)
    resp = client.patch(f"{B}/followup-records/{record}", headers=admin, json={"status": "removed"})
    assert resp.status_code == 200, resp.text
    assert _call_state(waiting) == ("cancelled", "随访已移除，呼叫取消")   # 修前 pending
    assert _call_state(connected) == ("connected", "约了下周复诊")
    # 已取消的外呼不再接受回写（与「结果只回写一次」同一句）
    late = client.post(f"{B}/call-tasks/{waiting}/result", headers=admin, json={"status": "connected"})
    assert late.status_code == 409, late.text


def test_接了呼叫中心网关的_已派发的不收(client, admin, world):
    from app.spd.callcenter import set_call_provider

    class Gateway:
        name = "http"

        def dispatch(self, task_id, phone, ref_type):
            return True, "已派发至呼叫中心"

    set_call_provider(Gateway())
    try:
        record = _record(world)
        in_flight = _call(client, admin, world, record)
        resp = client.post(f"{B}/followup-records/{record}/execute", headers=admin,
                           json={"answers": {}, "channel": "visit"})
        assert resp.status_code == 200, resp.text
        assert _call_state(in_flight)[0] == "pending"   # 网关照打，结果照旧由回调回写
        callback = client.post(f"{B}/call-tasks/{in_flight}/result", headers=admin,
                               json={"status": "connected", "duration_s": 12, "record_url": "https://rec.example/1"})
        assert callback.status_code == 200, callback.text
    finally:
        set_call_provider(None)


def test_死亡结案_随访一并移除_外呼一并取消(client, admin, world):
    record = _record(world)
    waiting = _call(client, admin, world, record)
    resp = client.post(f"{B}/enrollments/{world['enrollment']}/lifecycle", headers=admin,
                       json={"event": "death", "reason": "病故"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["closed"]["followups"] >= 1, resp.text
    assert _call_state(waiting) == ("cancelled", "随访随档案结束移除（death:病故），呼叫取消")   # 修前 pending
