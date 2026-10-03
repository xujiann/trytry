"""目标机构确认迁入与原机构登记死亡同时到：死亡被改回「已迁出」，目标机构给已故患者新建在管档案（P2-1175，第三十四批
扫描 L1-1）。

迁出登记之后、确认之前患者离世，这次迁出不再生效（P1-111 / P2-527）。`confirm_migration` 在锁外判
`migration_void_reason(enrollment.status)`，之后往对象上赋 `status = "migrated"`、`confirmed = True`，flush 出来的 UPDATE
只有 `WHERE id = ?`。读到「在管」之后原机构刚登记死亡并提交，修前两路都 200：原档案从 dead 变成 migrated，死亡被抹掉；
目标机构新建一份 active 档案，随访、宣教照常派给已故患者。顺序发生（先死亡后确认）时是 409「该患者已登记死亡，这次
迁出不再生效」。

修法：事件「还没确认」与档案「没成作废状态」各压进同一条 UPDATE（`concurrency.move_row`），抢输了回滚、按库里的现状给出
与顺序发生时同一句 409。这里照 `test_spd_lifecycle_death_race._dies_meanwhile` 把「判过了、还没写」钉成确定的时序：
在判定之后、写入之前让另一路动作提交。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = {}
    for key in ("from", "to"):
        orgs[key] = client.post("/api/organizations", headers=admin, json={
            "name": f"P21175 {key}卫生院", "org_type": "township", "level": "township"}).json()["id"]
    return {"orgs": orgs, "n": 0}


def _pending_migration(client, admin, world):
    """原机构在管的一份档案，登记了迁往目标机构、待确认；返回（档案编号, 迁出事件编号, 患者编号）。"""
    world["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P21175 患者{world['n']}", "id_card": f"33012719750321{world['n']:04d}"}).json()["id"]
    got = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": world["orgs"]["from"]})
    assert got.status_code == 201, got.text
    enrollment = got.json()["id"]
    moved = client.post(f"{B}/enrollments/{enrollment}/lifecycle", headers=admin, json={
        "event": "migrate", "reason": "P21175", "target_org_id": world["orgs"]["to"]})
    assert moved.status_code == 200 and moved.json()["pending_confirm"], moved.text
    return enrollment, moved.json()["event_id"], patient


def _meanwhile(monkeypatch, action):
    """`population.migration_void_reason` 调完之后（判定之后、写入之前），另一路做完 `action(另一个会话)` 并提交。"""
    from app.spd.routers import population

    real, fired = population.migration_void_reason, []

    def racing(*args, **kwargs):
        result = real(*args, **kwargs)
        if not fired:
            fired.append(True)
            with SessionLocal() as other:
                action(other)
                other.commit()
        return result

    monkeypatch.setattr(population, "migration_void_reason", racing)
    return fired


def _at(patient, org):
    from app.spd.models import SpdEnrollment

    with SessionLocal() as db:
        return [(e.status, e.migrated_from_id) for e in db.query(SpdEnrollment).filter(
            SpdEnrollment.patient_id == patient, SpdEnrollment.org_id == org).order_by(SpdEnrollment.id)]


def _event(event_id):
    from app.spd.models import SpdLifecycleEvent

    with SessionLocal() as db:
        row = db.get(SpdLifecycleEvent, event_id)
        return row.confirmed, row.confirmed_by


def test_确认迁入与登记死亡并发_409_死亡不被抹掉_不给已故患者新建档案(client, admin, world, monkeypatch):
    from app.spd.models import SpdEnrollment

    enrollment, event_id, patient = _pending_migration(client, admin, world)

    def dies(other):
        other.get(SpdEnrollment, enrollment).status = "dead"

    fired = _meanwhile(monkeypatch, dies)
    resp = client.post(f"{B}/lifecycle-events/{event_id}/confirm", headers=admin)
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200
    assert resp.json() == {"detail": "该患者已登记死亡，这次迁出不再生效"}   # 与顺序发生时同一句
    assert _at(patient, world["orgs"]["from"]) == [("dead", None)]   # 修前 migrated：死亡被抹掉
    assert _at(patient, world["orgs"]["to"]) == []   # 修前目标机构新建了一份 active 档案
    assert _event(event_id) == (False, None)   # 整笔回滚，迁出事件没被记成已确认


def test_两家同时确认同一笔迁出_后到的一路回该迁出已确认(client, admin, world, monkeypatch):
    """事件同样条件翻转：另一路刚确认并提交，后到的一路与顺序发生时同一句「该迁出已确认」，不再接一次关系。"""
    from app.models import User
    from app.spd.models import SpdEnrollment, SpdLifecycleEvent

    enrollment, event_id, patient = _pending_migration(client, admin, world)

    def confirmed_by_other(other):
        admin_id = other.query(User.id).filter(User.username == "admin").scalar()
        row = other.get(SpdLifecycleEvent, event_id)
        row.confirmed, row.confirmed_by = True, admin_id
        other.get(SpdEnrollment, enrollment).status = "migrated"
        other.add(SpdEnrollment(patient_id=patient, program_code="hypertension", org_id=world["orgs"]["to"],
                                status="active", source="migrate", migrated_from_id=enrollment))

    fired = _meanwhile(monkeypatch, confirmed_by_other)
    resp = client.post(f"{B}/lifecycle-events/{event_id}/confirm", headers=admin)
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200：同一笔迁出确认了两次
    assert resp.json() == {"detail": "该迁出已确认"}
    assert _at(patient, world["orgs"]["to"]) == [("active", enrollment)]   # 先确认的那一路建的，只此一份


def test_没有竞争时照常确认迁入(client, admin, world):
    enrollment, event_id, patient = _pending_migration(client, admin, world)
    resp = client.post(f"{B}/lifecycle-events/{event_id}/confirm", headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enrollment"]["status"] == "migrated"
    assert body["incoming_enrollment"]["status"] == "active"
    assert body["incoming_enrollment"]["org_id"] == world["orgs"]["to"]
    assert _at(patient, world["orgs"]["from"]) == [("migrated", None)]
    assert _at(patient, world["orgs"]["to"]) == [("active", enrollment)]
    confirmed, confirmed_by = _event(event_id)
    assert confirmed is True and confirmed_by is not None
    again = client.post(f"{B}/lifecycle-events/{event_id}/confirm", headers=admin)
    assert again.status_code == 409 and again.json() == {"detail": "该迁出已确认"}
