"""预约的状态转换不是原子的：同一预约并发取消两次，号源的已约数被多扣一次（P2-109）。

预约的三条状态转换原先都是「读状态 → 判 → 改」：

- `release_appointment`（管理端取消与居民端取消共用）：判 booked → 改 cancelled → 号源已约数 -1；
- `fulfill`（到诊核销）：判 booked → 改 fulfilled；
- `book_slot` 的重约（同一患者同一号源、之前取消过的那条复用）：判 cancelled → 号源已约数 +1 → 改 booked。

PG 的 READ COMMITTED 下并发的几路都读到同一个旧状态、都往下走：

- 取消连点两下（或管理端与居民端同时取消）：两路都释放号源，已约数被多扣一次——容量 5、实约 2 的号源，已约数
  成了 0，之后能多约出一个号（超卖）；
- 一路取消、一路核销：取消释放了号源，核销又把状态改成「已就诊」——已就诊的人占着的号被放出去了；
- 同一条已取消的预约两路同时重约：两路都占了号——一条预约，已约数加了两次，白白少放出一个号。

号源一侧早就是条件 UPDATE（`WHERE booked < capacity` / `booked > 0`，H3），状态这一侧没有。修法：状态转换也用条件
UPDATE（`WHERE status = 'booked'` / `'cancelled'`），影响 0 行即 409、什么都不动，与转诊单的 `_advance_case` 同一个写法。

这里用「一路拿着先读到的对象、另一路先提交」把并发窗口钉成确定的时序（SQLite 与 PG 同样成立）；
PG 上真并发的不变量见 test_appointment_status_transition_races.py。
"""
from datetime import timedelta

import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    from app import clock

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2109 门诊部", "org_type": "township", "level": "township"}).json()["id"]
    slot = client.post("/api/appointments/slots", headers=admin, json={
        "org_id": org, "resource_type": "outpatient", "resource_name": "P2109 全科",
        "slot_date": (clock.today() + timedelta(days=3)).isoformat(), "capacity": 5})
    assert slot.status_code == 201, slot.text
    appts, patients = [], []
    for i in range(3):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P2109 患者{i}", "id_card": f"33012719690{i}092109"}).json()["id"]
        booked = client.post("/api/appointments", headers=admin, json={"slot_id": slot.json()["id"], "patient_id": patient})
        assert booked.status_code == 201, booked.text
        appts.append(booked.json()["id"])
        patients.append(patient)
    return {"org": org, "slot": slot.json()["id"], "appts": appts, "patients": patients}


def _slot_booked(slot_id):
    from app.database import SessionLocal
    from app.models import AppointmentSlot

    with SessionLocal() as db:
        return db.get(AppointmentSlot, slot_id).booked


def _status(appt_id):
    from app.database import SessionLocal
    from app.models import Appointment

    with SessionLocal() as db:
        return db.get(Appointment, appt_id).status


def test_同一预约并发取消两次_号源只释放一次(world):
    from fastapi import HTTPException

    from app.database import SessionLocal
    from app.models import Appointment
    from app.routers.appointments import release_appointment

    appt = world["appts"][0]
    before = _slot_booked(world["slot"])
    with SessionLocal() as racer, SessionLocal() as winner:
        stale = racer.get(Appointment, appt)                               # 第一路读到 booked
        release_appointment(winner, winner.get(Appointment, appt))          # 第二路先取消成功
        with pytest.raises(HTTPException) as exc:
            release_appointment(racer, stale)                                # 第一路拿着读到的 booked 接着取消
    assert exc.value.status_code == 409   # 修前不报错
    assert "已取消" in exc.value.detail and "不可取消" in exc.value.detail, exc.value.detail
    assert _slot_booked(world["slot"]) == before - 1   # 修前 -2：多释放了一个号


def test_取消与核销并发_已取消的不会再被核销成已就诊(world):
    from fastapi import HTTPException

    from app.database import SessionLocal
    from app.models import Appointment, User
    from app.routers.appointments import fulfill, release_appointment

    appt = world["appts"][1]
    with SessionLocal() as racer, SessionLocal() as winner:
        held = racer.get(Appointment, appt)   # noqa: F841 — 核销那一路先读到 booked；留着引用，身份映射是弱引用
        release_appointment(winner, winner.get(Appointment, appt))           # 取消那一路先提交：号源已释放
        admin = racer.query(User).filter_by(username="admin").one()
        with pytest.raises(HTTPException) as exc:
            fulfill(appt, db=racer, user=admin)
    assert exc.value.status_code == 409   # 修前不报错
    assert _status(appt) == "cancelled"   # 修前 fulfilled：已就诊的人占着的号被放出去了


def test_同一条已取消的预约并发重约两次_号源只占一次(world):
    from fastapi import HTTPException

    from app.database import SessionLocal
    from app.models import Appointment
    from app.routers.appointments import book_slot, release_appointment

    appt, patient = world["appts"][2], world["patients"][2]
    with SessionLocal() as db:
        release_appointment(db, db.get(Appointment, appt))
    before = _slot_booked(world["slot"])
    with SessionLocal() as racer, SessionLocal() as winner:
        held = racer.get(Appointment, appt)   # noqa: F841 — 第一路先读到 cancelled；留着引用，身份映射是弱引用
        book_slot(winner, world["slot"], patient)                            # 第二路先重约成功
        with pytest.raises(HTTPException) as exc:
            book_slot(racer, world["slot"], patient)                          # 第一路拿着读到的 cancelled 接着重约
    assert exc.value.status_code == 409   # 修前不报错
    assert exc.value.detail == "请勿重复预约"   # 与顺序重复重约同一句
    assert _slot_booked(world["slot"]) == before + 1   # 修前 +2：一条预约占了两个号
    assert _status(appt) == "booked"


def test_重约遇号源约满_预约照旧是已取消(client, admin, world):
    """先转预约、再占号：占号失败要把转回 booked 的那一步一并退掉，不能留下一条不占号的「已预约」。"""
    from app import clock

    slot = client.post("/api/appointments/slots", headers=admin, json={
        "org_id": world["org"], "resource_type": "outpatient", "resource_name": "P2109 专科",
        "slot_date": (clock.today() + timedelta(days=4)).isoformat(), "capacity": 1})
    assert slot.status_code == 201, slot.text
    slot_id = slot.json()["id"]
    first, second = world["patients"][0], world["patients"][1]
    booked = client.post("/api/appointments", headers=admin, json={"slot_id": slot_id, "patient_id": first})
    assert booked.status_code == 201, booked.text
    assert client.post(f"/api/appointments/{booked.json()['id']}/cancel", headers=admin).status_code == 200
    taken = client.post("/api/appointments", headers=admin, json={"slot_id": slot_id, "patient_id": second})
    assert taken.status_code == 201, taken.text   # 号被别人约走了，容量 1 已满

    again = client.post("/api/appointments", headers=admin, json={"slot_id": slot_id, "patient_id": first})
    assert again.status_code == 409 and again.json()["detail"] == "号源已约满", again.text
    assert _status(booked.json()["id"]) == "cancelled"
    assert _slot_booked(slot_id) == 1
