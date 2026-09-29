"""还有没结束的召回，不再召回（P2-788，第二十一批「重复提交与重放」扫描 N2-8）。

`POST /api/spd/enrollments/{id}/lifecycle` 登记「召回」原先照收：连点两下生出两条待联系召回，登记其中一条「已召回」、
档案恢复在管，另一条照旧挂在待联系清单里、还能再登记一次「已召回」（多记一条「恢复」事件）。修后：这份档案还有
待联系 / 已联系的召回时再召回 409；上一次召回失败了的照样能重新发起（登记进度那里就是这么叫人做的）。
「有没有未结束的召回」与插召回记录压不进一条 SQL，圈进这份档案那一行的临界区（`serialized_on`）：两路同时召回只生出一条。

并发时序（同 `test_publichealth_event_close_race.py`）：A 路往召回表发 INSERT 之前（引擎的 before_cursor_execute）起 B 路
召回、等它最多两秒。只有预检、没有临界区时，B 的预检赶在 A 提交之前、读到「没有召回」，A 提交后 B 照样插一条；
圈进临界区后 B 卡在块外，A 提交出块后 B 才判、读到 A 那条、409。
"""
import threading

import pytest
from fastapi import HTTPException
from sqlalchemy import event as sa_event

from app.database import SessionLocal, engine

B = "/api/spd"
PROGRAM = "p2788_htn"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2788 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    assert client.post(f"{B}/programs", headers=admin, json={
        "code": PROGRAM, "name": "P2788 高血压", "category": "chronic"}).status_code == 201
    return {"org": org, "n": 0}


def _enrollment(client, admin, world):
    from app.spd.models import SpdEnrollment

    world["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2788 患者{world['n']}", "id_card": f"33028119800202{world['n']:03d}X"}).json()["id"]
    with SessionLocal() as db:
        enrollment = SpdEnrollment(patient_id=patient, program_code=PROGRAM, org_id=world["org"], status="active")
        db.add(enrollment)
        db.commit()
        return enrollment.id


def _recall(client, admin, enrollment_id):
    return client.post(f"{B}/enrollments/{enrollment_id}/lifecycle", headers=admin,
                       json={"event": "recall", "reason": "失访三个月"})


def _recalls(enrollment_id):
    from app.spd.models import SpdRecall

    with SessionLocal() as db:
        return [(r.id, r.status) for r in db.query(SpdRecall).filter(SpdRecall.enrollment_id == enrollment_id)
                .order_by(SpdRecall.id)]


def test_召回中再召回_409_只有一条待联系(client, admin, world):
    enrollment = _enrollment(client, admin, world)
    assert _recall(client, admin, enrollment).status_code == 200
    [(recall, _)] = _recalls(enrollment)
    again = _recall(client, admin, enrollment)
    assert again.status_code == 409, again.text   # 修前 200，多出第二条待联系召回
    assert again.json()["detail"] == f"该档案已在召回中（召回记录 {recall} 尚未结束），不能重复召回"
    assert _recalls(enrollment) == [(recall, "pending")]
    # 已联系也算没结束
    assert client.post(f"{B}/recalls/{recall}/progress", headers=admin, json={
        "status": "contacted", "contact_note": "电话未接"}).status_code == 200
    assert _recall(client, admin, enrollment).status_code == 409


def test_上次召回失败了_照样能重新发起(client, admin, world):
    enrollment = _enrollment(client, admin, world)
    assert _recall(client, admin, enrollment).status_code == 200
    [(first, _)] = _recalls(enrollment)
    assert client.post(f"{B}/recalls/{first}/progress", headers=admin, json={
        "status": "failed", "result": "号码停机"}).status_code == 200
    resp = _recall(client, admin, enrollment)
    assert resp.status_code == 200, resp.text
    assert [status for _, status in _recalls(enrollment)] == ["failed", "pending"]


def test_召回成功恢复在管之后_再召回照样能发起(client, admin, world):
    enrollment = _enrollment(client, admin, world)
    assert _recall(client, admin, enrollment).status_code == 200
    [(first, _)] = _recalls(enrollment)
    assert client.post(f"{B}/recalls/{first}/progress", headers=admin, json={
        "status": "returned", "result": "已重新纳管"}).status_code == 200
    assert _recall(client, admin, enrollment).status_code == 200
    assert [status for _, status in _recalls(enrollment)] == ["returned", "pending"]


def _recall_direct(enrollment_id):
    from app.models import User
    from app.spd.routers.population import LifecycleIn, lifecycle_event

    with SessionLocal() as db:
        user = db.query(User).filter(User.username == "admin").one()
        try:
            lifecycle_event(enrollment_id, LifecycleIn(event="recall", reason="失访三个月"), db=db, user=user)
            return 200
        except HTTPException as exc:
            return exc.status_code


def test_两路同时召回_只生出一条(client, admin, world):
    enrollment = _enrollment(client, admin, world)
    other: dict = {}
    fired: list = []

    def run_other():
        other["code"] = _recall_direct(enrollment)

    def listener(conn, cursor, statement, parameters, context, executemany):
        if not fired and statement.lstrip().upper().startswith("INSERT INTO SPD_RECALLS"):
            fired.append(True)
            thread = threading.Thread(target=run_other)
            thread.start()
            thread.join(timeout=2)   # 只有预检时 B 判完、卡在写锁上；圈进临界区后 B 卡在块外
            fired.append(thread)

    sa_event.listen(engine, "before_cursor_execute", listener)
    try:
        mine = _recall_direct(enrollment)
    finally:
        sa_event.remove(engine, "before_cursor_execute", listener)
    fired[1].join(timeout=30)

    assert fired and mine == 200
    assert other["code"] == 409, "修前：两路都读到没有召回，各插一条"
    assert [status for _, status in _recalls(enrollment)] == ["pending"]
