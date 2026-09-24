"""慢专病纳管档案的生命周期：登记死亡之后不许再被别的事件改写（P1-111）。

`lifecycle_event` 只在「恢复管理」时看了一眼死亡——「已登记死亡的档案不可恢复管理」——其余事件（排除 / 召回 /
同机构迁出 / 再登记一次死亡）一律照收、直接改写档案状态；目标机构确认跨机构迁入时，也只看迁出事件确认过没有，
不看档案现在是什么状态。2026-09-24 开发库实测（修前代码）：

- 登记死亡 → 排除 200（状态从「死亡」改成「已排除」）→ 恢复管理 200：**死亡档案回到在管**，那条规则整个被绕过；
- 登记死亡 → 召回 200：给已故患者开了一条召回记录，召回成功时档案同样回到在管；
- 登记跨机构迁出（待目标机构确认）→ 患者离世、登记死亡 → 目标机构确认迁入 200：原档案从「死亡」改成「已迁出」，
  **目标机构给已故患者新建了一份在管档案**，之后的随访、宣教照常派给他。

另一处同族：「恢复在管」与「召回成功」直接把状态改回在管，同一患者同一病种若已有另一份在管档案（跨机构迁入后目标机构
那份、召回期间别处新建的那份），撞上部分唯一索引 `uq_spd_enroll_active_patient_program`，commit 时 IntegrityError，**500**。

修法：死亡是终态——死亡档案上再登记任何生命周期事件一律 409；确认迁入时档案已登记死亡，同样 409（迁出不再生效）；
工作台「待确认迁入」不再数这种确认不了的迁出；恢复在管 / 召回成功先查另一份在管档案，有就 409 并说明在哪家机构在管
（并发下撞索引兜底，同一句）。修前本文件 9 条红（含两处 500）、1 条对照绿。
"""
from __future__ import annotations

import itertools

import pytest

_ids = itertools.count(1)


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin,
                      json={"name": "P1111 迁出卫生院", "org_type": "township", "level": "township"}).json()["id"]
    target = client.post("/api/organizations", headers=admin,
                         json={"name": "P1111 迁入卫生院", "org_type": "township", "level": "township"}).json()["id"]
    resp = client.post("/api/spd/programs", headers=admin,
                       json={"code": "p1111_prog", "name": "生命周期终态病种", "category": "chronic"})
    assert resp.status_code == 201, resp.text

    def enrollment() -> int:
        n = next(_ids)
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P1111 患者{n}", "id_card": f"33019219790101{n:04d}"}).json()["id"]
        resp = client.post("/api/spd/enrollments", headers=admin,
                           json={"patient_id": patient, "program_code": "p1111_prog", "org_id": org})
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    return {"org": org, "target": target, "enrollment": enrollment}


def _event(client, admin, enrollment_id, event, **extra):
    return client.post(f"/api/spd/enrollments/{enrollment_id}/lifecycle", headers=admin,
                       json={"event": event, "reason": "P1111", **extra})


def _status(enrollment_id) -> str:
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment

    with SessionLocal() as db:
        return db.get(SpdEnrollment, enrollment_id).status


@pytest.mark.parametrize("event", ["exclude", "recall", "migrate", "death"])
def test_死亡档案不再登记生命周期事件(client, admin, world, event):
    eid = world["enrollment"]()
    assert _event(client, admin, eid, "death").status_code == 200
    resp = _event(client, admin, eid, event)
    assert resp.status_code == 409, resp.text
    assert _status(eid) == "dead"


def test_排除再恢复绕不过死亡不可恢复(client, admin, world):
    eid = world["enrollment"]()
    assert _event(client, admin, eid, "death").status_code == 200
    _event(client, admin, eid, "exclude")
    resp = _event(client, admin, eid, "resume")
    assert resp.status_code == 409, resp.text
    assert _status(eid) == "dead"


def test_迁出待确认期间离世_目标机构确认不了_也不给已故患者建档(client, admin, world):
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment

    eid = world["enrollment"]()
    pending = _event(client, admin, eid, "migrate", target_org_id=world["target"])
    assert pending.status_code == 200 and pending.json()["pending_confirm"] is True, pending.text
    assert _event(client, admin, eid, "death").status_code == 200

    resp = client.post(f"/api/spd/lifecycle-events/{pending.json()['event_id']}/confirm", headers=admin)
    assert resp.status_code == 409, resp.text
    assert _status(eid) == "dead"
    with SessionLocal() as db:
        patient_id = db.get(SpdEnrollment, eid).patient_id
        assert db.query(SpdEnrollment).filter(SpdEnrollment.patient_id == patient_id,
                                              SpdEnrollment.org_id == world["target"]).count() == 0


def test_在管档案照常登记与确认迁入(client, admin, world):
    """对照：终态之外的流转不受影响——在管档案迁出、目标机构确认，原档案已迁出、目标机构新建在管档案。"""
    eid = world["enrollment"]()
    pending = _event(client, admin, eid, "migrate", target_org_id=world["target"])
    assert pending.status_code == 200, pending.text
    resp = client.post(f"/api/spd/lifecycle-events/{pending.json()['event_id']}/confirm", headers=admin)
    assert resp.status_code == 200, resp.text
    assert _status(eid) == "migrated"
    assert resp.json()["incoming_enrollment"]["status"] == "active"


# ---- 恢复在管撞上另一份在管档案（部分唯一索引只许一份）：修前 500
def test_确认迁入之后原档案恢复在管_409而不是500(client, admin, world):
    eid = world["enrollment"]()
    pending = _event(client, admin, eid, "migrate", target_org_id=world["target"])
    confirmed = client.post(f"/api/spd/lifecycle-events/{pending.json()['event_id']}/confirm", headers=admin)
    assert confirmed.status_code == 200, confirmed.text

    resp = _event(client, admin, eid, "resume")
    assert resp.status_code == 409, resp.text   # 修前 500：IntegrityError（目标机构那份已在管）
    assert "P1111 迁入卫生院" in resp.json()["detail"]
    assert _status(eid) == "migrated"


def test_召回期间另建了在管档案_召回成功时409而不是500(client, admin, world):
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment, SpdRecall

    eid = world["enrollment"]()
    assert _event(client, admin, eid, "recall").status_code == 200
    with SessionLocal() as db:
        patient_id = db.get(SpdEnrollment, eid).patient_id
        recall_id = db.query(SpdRecall).filter(SpdRecall.enrollment_id == eid).one().id
    # 召回中的档案不占在管名额，别处照常给他建了档
    again = client.post("/api/spd/enrollments", headers=admin,
                        json={"patient_id": patient_id, "program_code": "p1111_prog", "org_id": world["target"]})
    assert again.status_code == 201, again.text

    resp = client.post(f"/api/spd/recalls/{recall_id}/progress", headers=admin, json={"status": "returned"})
    assert resp.status_code == 409, resp.text   # 修前 500
    assert _status(eid) == "recalled"
    with SessionLocal() as db:
        assert db.get(SpdRecall, recall_id).status != "returned"


def test_待确认迁入不数迁出期间离世的(client, admin, world):
    def pending_count():
        r = client.get("/api/spd/workbench/center", headers=admin)
        assert r.status_code == 200, r.text
        return r.json()["lifecycle"]["pending_migrations"]

    base = pending_count()
    eid = world["enrollment"]()
    assert _event(client, admin, eid, "migrate", target_org_id=world["target"]).status_code == 200
    assert pending_count() == base + 1
    assert _event(client, admin, eid, "death").status_code == 200
    assert pending_count() == base   # 修前仍是 base + 1：一条永远确认不了的待办
