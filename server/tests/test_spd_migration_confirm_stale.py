"""跨机构迁入确认：原档案已迁出 / 已排除 / 已结案的，迟到的确认不再生效（P2-527）。

`confirm_migration` 只挡「已确认过」与「已登记死亡」（P1-111），原档案处于别的终态照样 200：

- 同一份档案先后登记迁往乙、丙两家（都待确认）。乙确认后原档案已迁出、乙那边建了在管档案；丙再确认照样 200，
  回执里的「迁入档案」是**乙家那份**——丙家什么也没有，页面上却显示迁入成功。
- 迁往丙的登记还没确认，原档案先被排除，患者又在乙家重新纳管；丙迟到的确认把「已排除」改成「已迁出」，
  还把乙家那份档案记成「从这里迁过去的」（`migrated_from_id`），这位患者从没迁去过乙。

根子在「目标机构已有同病种在管档案，只接关系」那条兜底取的是**任一机构**的在管档案：同病种在管档案全县只许一份
（部分唯一索引），原档案还在管时这条分支根本走不到——走到它的都是原档案已不在管的迟到确认。

修后：原档案已迁出 / 已排除 / 已结案即 409「原档案…，这次迁出不再生效」；兜底只接目标机构自己的那份，
撞上的是第三家的在管档案同样 409 并说明在哪家在管。
"""
from __future__ import annotations

import itertools

import pytest

_ids = itertools.count(1)


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = {}
    for key in ("a", "b", "c"):
        orgs[key] = client.post("/api/organizations", headers=admin, json={
            "name": f"P2527 {key}卫生院", "org_type": "township", "level": "township"}).json()["id"]
    resp = client.post("/api/spd/programs", headers=admin,
                       json={"code": "p2527_prog", "name": "迁入确认病种", "category": "chronic"})
    assert resp.status_code == 201, resp.text

    def patient() -> int:
        n = next(_ids)
        return client.post("/api/patients", headers=admin, json={
            "name": f"P2527 患者{n}", "id_card": f"33019219800202{n:04d}"}).json()["id"]

    def enroll(patient_id: int, org: str) -> int:
        resp = client.post("/api/spd/enrollments", headers=admin,
                           json={"patient_id": patient_id, "program_code": "p2527_prog", "org_id": orgs[org]})
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    return {"orgs": orgs, "patient": patient, "enroll": enroll}


def _event(client, admin, enrollment_id, event, **extra):
    resp = client.post(f"/api/spd/enrollments/{enrollment_id}/lifecycle", headers=admin,
                       json={"event": event, "reason": "P2527", **extra})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _confirm(client, admin, event_id):
    return client.post(f"/api/spd/lifecycle-events/{event_id}/confirm", headers=admin)


def _enrollment(enrollment_id):
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment

    with SessionLocal() as db:
        row = db.get(SpdEnrollment, enrollment_id)
        return row.status, row.org_id, row.migrated_from_id


def _count_at(patient_id, org_id) -> int:
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment

    with SessionLocal() as db:
        return db.query(SpdEnrollment).filter(SpdEnrollment.patient_id == patient_id,
                                              SpdEnrollment.org_id == org_id).count()


def test_同一档案迁往两家_先确认的生效_后确认的409(client, admin, world):
    pid = world["patient"]()
    eid = world["enroll"](pid, "a")
    to_b = _event(client, admin, eid, "migrate", target_org_id=world["orgs"]["b"])["event_id"]
    to_c = _event(client, admin, eid, "migrate", target_org_id=world["orgs"]["c"])["event_id"]
    assert _confirm(client, admin, to_b).status_code == 200

    late = _confirm(client, admin, to_c)
    assert late.status_code == 409, late.text   # 修前 200，回执里的迁入档案是乙家那份
    assert late.json()["detail"] == "原档案已迁出，这次迁出不再生效"
    assert _count_at(pid, world["orgs"]["c"]) == 0


def test_迁出待确认期间原档案被排除_迟到的确认不改写也不乱接关系(client, admin, world):
    pid = world["patient"]()
    eid = world["enroll"](pid, "a")
    to_c = _event(client, admin, eid, "migrate", target_org_id=world["orgs"]["c"])["event_id"]
    _event(client, admin, eid, "exclude")
    again_at_b = world["enroll"](pid, "b")

    late = _confirm(client, admin, to_c)
    assert late.status_code == 409, late.text   # 修前 200
    assert late.json()["detail"] == "原档案已排除，这次迁出不再生效"
    assert _enrollment(eid)[0] == "excluded"          # 修前被改成 migrated
    assert _enrollment(again_at_b)[2] is None         # 修前记成从甲迁过去的


def test_兜底只接目标机构的在管档案_第三家的不接(client, admin, world):
    pid = world["patient"]()
    eid = world["enroll"](pid, "a")
    to_c = _event(client, admin, eid, "migrate", target_org_id=world["orgs"]["c"])["event_id"]
    _event(client, admin, eid, "recall")               # 召回中不是终态，确认仍可走到建档那一步
    at_b = world["enroll"](pid, "b")

    late = _confirm(client, admin, to_c)
    assert late.status_code == 409, late.text   # 修前 200，把乙家那份当成迁入档案回给丙
    assert "P2527 b卫生院" in late.json()["detail"]
    assert _enrollment(at_b)[2] is None
    assert _enrollment(eid)[0] == "recalled"            # 整笔回滚：原档案不被改成已迁出
    assert _count_at(pid, world["orgs"]["c"]) == 0


def test_对照_在管档案照常确认迁入(client, admin, world):
    pid = world["patient"]()
    eid = world["enroll"](pid, "a")
    to_c = _event(client, admin, eid, "migrate", target_org_id=world["orgs"]["c"])["event_id"]
    resp = _confirm(client, admin, to_c)
    assert resp.status_code == 200, resp.text
    assert _enrollment(eid)[0] == "migrated"
    incoming = resp.json()["incoming_enrollment"]
    assert (incoming["status"], incoming["org_id"]) == ("active", world["orgs"]["c"])
