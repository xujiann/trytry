"""「转呼叫」与执行随访（办结）同时到：已办结的随访又进了坐席队列，还派发了外呼（P2-1181，第三十四批扫描 L1-11）。

`followup.create_call_task` 锁外判随访未结束 → 建待呼叫 → 派发到呼叫通道；`execute_followup` 在随访记录那一行的临界区里
办结，并撤回挂在它上面的待呼叫（P2-498）——只撤当时已在队列里的。判完未结束之后别人刚办结并提交，这一路照建待呼叫、照样
派发（http 通道下真的外拨）：坐席打给已经随访过的人，死亡收尾一并移除随访时同理打给家属。顺序版是 P2-761（办结后再转呼叫
409「该随访已结束」）。

修法：建待呼叫进执行随访同一把随访记录行锁（`serialized_on(db, SpdFollowupRecord, …)`），锁里重读再判；提交后仍是待呼叫才
派发——出了临界区、派发之前被办结撤回的，不再推给呼叫通道。两种都与顺序发生时同一句 409。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"


class _RecordingProvider:
    """记下派发了哪些呼叫任务，不真的外拨。"""

    def __init__(self):
        self.dispatched: list[int] = []

    def dispatch(self, task_id, phone, ref_type):
        self.dispatched.append(task_id)
        return True, "待人工外呼"


@pytest.fixture()
def provider():
    from app.spd.callcenter import set_call_provider

    recorder = _RecordingProvider()
    set_call_provider(recorder)
    yield recorder
    set_call_provider(None)


@pytest.fixture(scope="module")
def world(client, admin):
    from app.spd.models import SpdFollowupRule

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21181 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    with SessionLocal() as db:
        rule = db.query(SpdFollowupRule).filter(SpdFollowupRule.code == "fr_chronic").one().id
    return {"org": org, "rule": rule, "n": 0}


def _planned_followup(client, admin, world):
    """现造一位有电话的患者 + 一份随访计划，返回 (patient_id, 第一条待随访记录号)。"""
    world["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P21181 患者{world['n']}", "id_card": f"33012719650505{world['n']:04d}",
        "phone": f"139111811{world['n']:02d}"})
    assert patient.status_code == 201, patient.text
    plan = client.post(f"{B}/followup-plans", headers=admin, json={
        "patient_id": patient.json()["id"], "rule_id": world["rule"], "org_id": world["org"]})
    assert plan.status_code == 201, plan.text
    return patient.json()["id"], plan.json()["items"][0]["id"]


def _call_tasks(record_id):
    from app.spd.models import SpdCallTask

    with SessionLocal() as db:
        return [(t.status, t.result) for t in db.query(SpdCallTask).filter_by(ref_type="followup", ref_id=record_id)]


def _done_by_other(record_id):
    """另一位随访员把这条随访办结并提交：与 `execute_followup` 同一个终态跃迁（办结时撤回挂在它上面的待呼叫）。"""
    from app.spd.service import close_followup_record

    with SessionLocal() as other:
        assert close_followup_record(other, record_id, "done", allowed_from=("planned", "overdue", "unreachable"))
        other.commit()


def test_判完未结束之后别人刚办结_409_不建待呼叫不派发(client, admin, world, provider, monkeypatch):
    from app.spd.routers import followup

    patient, record = _planned_followup(client, admin, world)
    real, fired = followup.SpdCallTask, []

    def racing(**kwargs):   # 锁外判过「未结束」、建待呼叫之前
        if not fired:
            fired.append(True)
            _done_by_other(record)
        return real(**kwargs)

    monkeypatch.setattr(followup, "SpdCallTask", racing)
    resp = client.post(f"{B}/call-tasks", headers=admin, json={"patient_id": patient, "ref_type": "followup", "ref_id": record})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 201：待呼叫、已派发
    assert resp.json() == {"detail": "该随访已结束"}   # 与顺序发生时同一句
    assert _call_tasks(record) == []
    assert provider.dispatched == []


def test_建好待呼叫之后派发之前被办结撤回_不再派发(client, admin, world, provider, monkeypatch):
    from app.spd.routers import followup

    patient, record = _planned_followup(client, admin, world)
    real, fired = followup.insert_or_conflict, []

    def racing(*args, **kwargs):   # 待呼叫已提交、派发之前：办结把它撤出队列
        task = real(*args, **kwargs)
        if not fired:
            fired.append(True)
            _done_by_other(record)
        return task

    monkeypatch.setattr(followup, "insert_or_conflict", racing)
    resp = client.post(f"{B}/call-tasks", headers=admin, json={"patient_id": patient, "ref_type": "followup", "ref_id": record})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409 and resp.json() == {"detail": "该随访已结束"}, resp.text   # 修前 201、派发了
    assert provider.dispatched == []
    assert _call_tasks(record) == [("withdrawn", "随访已办结，撤出待呼叫")]   # 撤回的那条留痕照旧


def test_没有竞争时照常转呼叫并派发_办结后再转409(client, admin, world, provider):
    patient, record = _planned_followup(client, admin, world)
    resp = client.post(f"{B}/call-tasks", headers=admin, json={"patient_id": patient, "ref_type": "followup", "ref_id": record})
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "pending" and resp.json()["dispatch"] == {"accepted": True, "note": "待人工外呼"}
    assert provider.dispatched == [resp.json()["id"]]
    executed = client.post(f"{B}/followup-records/{record}/execute", headers=admin, json={"answers": {}, "result": "当面随访"})
    assert executed.status_code == 200, executed.text
    assert _call_tasks(record) == [("withdrawn", "随访已办结，撤出待呼叫")]
    again = client.post(f"{B}/call-tasks", headers=admin, json={"patient_id": patient, "ref_type": "followup", "ref_id": record})
    assert again.status_code == 409 and again.json() == {"detail": "该随访已结束"}, again.text
    assert provider.dispatched == [resp.json()["id"]]


# ---------------------------------------------------------------- 跟进：复诊一类同形（与 `care.update_revisit` 同一把复诊行锁）
def _planned_revisit(client, admin, world):
    """现造一位有电话的患者 + 一条已排期的复诊，返回 (patient_id, revisit_id)。"""
    from datetime import date, timedelta

    world["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P21181 复诊患者{world['n']}", "id_card": f"33012719650505{world['n']:04d}",
        "phone": f"139111811{world['n']:02d}"})
    assert patient.status_code == 201, patient.text
    revisit = client.post(f"{B}/revisits", headers=admin, json={
        "patient_id": patient.json()["id"], "plan_date": (date.today() + timedelta(days=7)).isoformat(),
        "items": "P21181 复诊"})
    assert revisit.status_code == 201, revisit.text
    return patient.json()["id"], revisit.json()["id"]


def _revisit_calls(revisit_id):
    from app.spd.models import SpdCallTask

    with SessionLocal() as db:
        return [(t.status, t.result) for t in db.query(SpdCallTask).filter_by(ref_type="revisit", ref_id=revisit_id)]


def _revisit_done_by_other(revisit_id):
    """另一位医生把这条复诊办结并提交：与 `care.update_revisit` 办结同一个效果（办结时撤回挂在它上面的待呼叫）。"""
    from app.spd.models import SpdRevisit
    from app.spd.service import withdraw_calls

    with SessionLocal() as other:
        other.get(SpdRevisit, revisit_id).status = "done"
        withdraw_calls(other, "revisit", [revisit_id], "复诊已完成，撤出待呼叫")
        other.commit()


def test_复诊判完未结束之后别人刚办结_409_不建待呼叫不派发(client, admin, world, provider, monkeypatch):
    from app.spd.routers import followup

    patient, revisit = _planned_revisit(client, admin, world)
    real, fired = followup.SpdCallTask, []

    def racing(**kwargs):   # 锁外判过「未结束」、建待呼叫之前
        if not fired:
            fired.append(True)
            _revisit_done_by_other(revisit)
        return real(**kwargs)

    monkeypatch.setattr(followup, "SpdCallTask", racing)
    resp = client.post(f"{B}/call-tasks", headers=admin, json={"patient_id": patient, "ref_type": "revisit", "ref_id": revisit})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 201：待呼叫、已派发
    assert resp.json() == {"detail": "该复诊已结束"}   # 与顺序发生时同一句
    assert _revisit_calls(revisit) == []
    assert provider.dispatched == []


def test_复诊建好待呼叫之后派发之前被办结撤回_不再派发(client, admin, world, provider, monkeypatch):
    from app.spd.routers import followup

    patient, revisit = _planned_revisit(client, admin, world)
    real, fired = followup.insert_or_conflict, []

    def racing(*args, **kwargs):   # 待呼叫已提交、派发之前：办结把它撤出队列
        task = real(*args, **kwargs)
        if not fired:
            fired.append(True)
            _revisit_done_by_other(revisit)
        return task

    monkeypatch.setattr(followup, "insert_or_conflict", racing)
    resp = client.post(f"{B}/call-tasks", headers=admin, json={"patient_id": patient, "ref_type": "revisit", "ref_id": revisit})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409 and resp.json() == {"detail": "该复诊已结束"}, resp.text   # 修前 201、派发了
    assert provider.dispatched == []
    assert _revisit_calls(revisit) == [("withdrawn", "复诊已完成，撤出待呼叫")]


def test_复诊没有竞争时照常转呼叫并派发_办结后再转409(client, admin, world, provider):
    patient, revisit = _planned_revisit(client, admin, world)
    resp = client.post(f"{B}/call-tasks", headers=admin, json={"patient_id": patient, "ref_type": "revisit", "ref_id": revisit})
    assert resp.status_code == 201, resp.text
    assert provider.dispatched == [resp.json()["id"]]
    done = client.patch(f"{B}/revisits/{revisit}", headers=admin, json={"status": "done"})
    assert done.status_code == 200, done.text
    assert _revisit_calls(revisit) == [("withdrawn", "复诊已完成，撤出待呼叫")]
    again = client.post(f"{B}/call-tasks", headers=admin, json={"patient_id": patient, "ref_type": "revisit", "ref_id": revisit})
    assert again.status_code == 409 and again.json() == {"detail": "该复诊已结束"}, again.text
    assert provider.dispatched == [resp.json()["id"]]
