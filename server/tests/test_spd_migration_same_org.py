"""慢专病「跨机构迁出」的迁入机构不能是档案当前的管理机构（P2-1273，第三十七批「一次请求、一次导入里的重复元素」扫描
AA2-3）。

登记生命周期事件原先只看填没填 `target_org_id`：迁入机构 ID 填成本机构照收，本机构工作台多一条「待确认迁入」；本机构自己
点确认，在办任务取消、按方案排的随访移除、原档案变「已迁出」，同机构另起一份没有主管医生的在管档案——患者还在本机构管，
排好的工作却全没了。docstring 写的是「跨机构迁出需目标机构确认」，平台转诊、会诊、派驻、药品调拨都拦「两端同一机构」。

修后登记 422。修前登记下的存量同机构事件归进「这次迁出不再生效」（`service.migration_void_reason`：确认接口、生命周期
清单、工作台「待确认迁入」同一句，P2-592）：确认 409、清单不画「确认迁入」、两处工作台不再算它，在办工作不动。迁出事件
没有驳回 / 撤回入口，这样它就离开了待确认，不另造状态。跨机构迁出照旧。
"""
import pytest

from conftest import login

from app import clock
from app.database import SessionLocal

B = "/api/spd"
SAME_ORG = "迁入机构与当前管理机构相同"


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = {key: client.post("/api/organizations", headers=admin, json={
        "name": f"P1273 {key}卫生院", "org_type": "township", "level": "township"}).json()["id"] for key in ("甲", "乙")}
    doctor = client.post("/api/users", headers=admin, json={
        "username": "p1273_doc", "password": "passw0rd1", "full_name": "P1273 甲医生", "role": "doctor",
        "org_id": orgs["甲"]})
    assert doctor.status_code in (200, 201), doctor.text
    rule = client.post(f"{B}/followup-rules", headers=admin, json={
        "code": "p1273_htn", "name": "P1273 高血压随访", "scene": "outpatient", "program_code": "hypertension",
        "points": [30, 90]})
    assert rule.status_code == 201, rule.text
    return {"orgs": orgs, "doctor": doctor.json()["id"], "head": login(client, "p1273_doc", "passw0rd1"),
            "rule": rule.json()["id"], "n": 0}


def _work(patient):
    """这位患者名下的档案（编号, 机构, 状态, 主管医生）、任务（编号, 状态）、随访（编号, 状态）。"""
    from app.spd.models import SpdEnrollment, SpdFollowupRecord, SpdTask

    with SessionLocal() as db:
        return (sorted((e.id, e.org_id, e.status, e.doctor_user_id)
                       for e in db.query(SpdEnrollment).filter(SpdEnrollment.patient_id == patient)),
                sorted((t.id, t.status) for t in db.query(SpdTask).filter(SpdTask.patient_id == patient)),
                sorted((f.id, f.status) for f in db.query(SpdFollowupRecord).filter(SpdFollowupRecord.patient_id == patient)))


def _managed(client, admin, world):
    """甲院在管、有主管医生的一份高血压档案，挂着一条在办任务与按方案排的两条随访；返回（患者, 档案, 现状）。"""
    world["n"] += 1
    head, org = world["head"], world["orgs"]["甲"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P1273 患者{world['n']}", "id_card": f"33010219650101127{world['n']}"}).json()["id"]
    served = client.post("/api/encounters", headers=admin, json={   # 甲院接诊过，甲院医生才看得见
        "patient_id": patient, "org_id": org, "diagnosis_name": "高血压"})
    assert served.status_code in (200, 201), served.text
    enrolled = client.post(f"{B}/enrollments", headers=head, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org, "risk_level": "mid",
        "doctor_user_id": world["doctor"]})
    assert enrolled.status_code == 201, enrolled.text
    task = client.post(f"{B}/tasks", headers=head, json={
        "patient_id": patient, "title": "血压复测", "enrollment_id": enrolled.json()["id"],
        "program_code": "hypertension", "org_id": org})
    assert task.status_code == 201, task.text
    plan = client.post(f"{B}/followup-plans", headers=head, json={
        "patient_id": patient, "rule_id": world["rule"], "org_id": org})
    assert plan.status_code == 201, plan.text
    work = _work(patient)
    assert work[0] == [(enrolled.json()["id"], org, "active", world["doctor"])] and len(work[1]) == 1 and len(work[2]) == 2
    return patient, enrolled.json()["id"], work


def _pending_counts(client, admin):
    return (client.get(f"{B}/workbench/admin", headers=admin).json()["alerts"]["pending_migrations"],
            client.get(f"{B}/workbench/center", headers=admin).json()["lifecycle"]["pending_migrations"])


def _pending_row(client, admin, event_id):
    return next(r for r in client.get(f"{B}/lifecycle-events", headers=admin,
                                      params={"event": "migrate", "confirmed": "false", "limit": 200}).json()
                if r["id"] == event_id)


def test_登记迁出_迁入机构填本机构_422_档案与在办工作不动(client, admin, world):
    patient, enrollment, before = _managed(client, admin, world)
    resp = client.post(f"{B}/enrollments/{enrollment}/lifecycle", headers=world["head"], json={
        "event": "migrate", "reason": "迁往外地", "target_org_id": world["orgs"]["甲"]})
    assert resp.status_code == 422, resp.text   # 修前 200、pending_confirm=True
    assert resp.json()["detail"] == SAME_ORG
    assert _work(patient) == before
    assert [e for e in client.get(f"{B}/lifecycle-events", headers=admin, params={"limit": 200}).json()
            if e["enrollment_id"] == enrollment] == []


def test_存量同机构待确认事件_确认被拒_清单不画按钮_工作台不算_在办工作不动(client, admin, world):
    from app.spd.models import SpdLifecycleEvent

    patient, enrollment, before = _managed(client, admin, world)
    counts = _pending_counts(client, admin)
    with SessionLocal() as db:   # 修前登记下的存量：本机构 → 本机构、待确认
        stale = SpdLifecycleEvent(enrollment_id=enrollment, event="migrate", reason="迁往外地",
                                  target_org_id=world["orgs"]["甲"], confirmed=False, operator_id=world["doctor"],
                                  occurred_at=clock.today().isoformat())
        db.add(stale)
        db.commit()
        event_id = stale.id
    void = f"{SAME_ORG}，这次迁出不再生效"
    row = _pending_row(client, admin, event_id)
    assert (row["void_reason"], row["can_confirm"]) == (void, False)   # 修前 ("", True)：页面照画「确认迁入」
    assert _pending_counts(client, admin) == counts   # 修前两处「待确认迁入」各多算一条
    for head in (world["head"], admin):
        got = client.post(f"{B}/lifecycle-events/{event_id}/confirm", headers=head)
        assert got.status_code == 409, got.text   # 修前 200
        assert got.json()["detail"] == void   # 与清单写的同一句
    assert _work(patient) == before   # 修前：任务取消、随访移除、原档案已迁出、同机构另起一份没有主管医生的档案


def test_跨机构迁出照旧(client, admin, world):
    patient, enrollment, _ = _managed(client, admin, world)
    moved = client.post(f"{B}/enrollments/{enrollment}/lifecycle", headers=world["head"], json={
        "event": "migrate", "reason": "迁往外地", "target_org_id": world["orgs"]["乙"]})
    assert moved.status_code == 200 and moved.json()["pending_confirm"] is True, moved.text
    row = _pending_row(client, admin, moved.json()["event_id"])
    assert (row["void_reason"], row["can_confirm"]) == ("", True)
    confirmed = client.post(f"{B}/lifecycle-events/{moved.json()['event_id']}/confirm", headers=admin)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["enrollment"]["status"] == "migrated"
    assert confirmed.json()["incoming_enrollment"]["org_id"] == world["orgs"]["乙"]
    assert [status for _, status in _work(patient)[1]] == ["cancelled"]   # 原机构的在办任务照旧随迁出收尾
