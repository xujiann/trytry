"""预约状态转换的真并发取证（P2-109，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_appointment_status_transition_races.py -q

预约的三条状态转换原先都是「读状态 → 判 → 改」：取消（booked → cancelled，号源已约数 -1）、到诊核销
（booked → fulfilled）、已取消的重约（cancelled → booked，号源已约数 +1）。PG 的 READ COMMITTED 下并发的几路
都读到同一个旧状态、都往下走——号源一侧的条件 UPDATE（H3）只保证已约数不越界，保证不了「一次转换只动一次号」：

- 八路同时取消同一条预约：八路都成功，已约数扣八次（扣到 0 为止）——之后能超卖；
- 取消与核销同时到：两路都成功，最后落「已就诊」、号却已被放出去；
- 八路同时重约同一条已取消的预约：八路都成功，已约数加八次——一条预约占了八个号。

SQLite 的库级写锁把窗口压平，开发库上同一份代码永远绿（SQLite 一侧用「拿着旧对象接着转」的确定时序钉逻辑，
见 test_appointment_status_transition.py）。这里钉 PG 上真并发的不变量：**每一轮恰一路成功、其余 409，且号源
已约数 = 该号源上「已预约」与「已就诊」的预约条数**（取消放号、核销不放号、重约占号）。

**这套库是多人共用的**：只用带随机后缀的自建数据，跑完自己收拾，从不 DROP SCHEMA；空库就地补跑迁移（只做加法）。
"""
import os
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import sessionmaker

from test_disease_enrollment_unique_races import _warm_pool  # 连接池热身，理由见 test_cost_allocation_races
from test_postgres_real import _race_on_pg  # Barrier 真并发的既有夹具，不另造一份

from app import clock
from app.models import Appointment, AppointmentSlot, Organization, Patient, User
from app.routers.appointments import book_slot, fulfill, release_appointment

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"),
]

RACERS = 8
SERVER_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pg_engine():
    """连既有测试库；空库就地补跑迁移，任何情况下都不清 schema（理由同 test_insurance_apply_unique_races）。
    池子按参赛路数开并先热身，否则冷池下测出来的是排队而不是并发（见 test_cost_allocation_races）。"""
    engine = create_engine(PG_URL, pool_size=RACERS, max_overflow=RACERS)
    if not all(sa_inspect(engine).has_table(t) for t in ("appointment_slots", "appointments")):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_DIR, env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"迁移在 PG 上失败：\n{result.stderr[-2000:]}"
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def world(pg_engine):
    """一家机构、一名本机构医生（核销要过机构写权限）；各用例自己开号源与患者（名字带随机后缀）。"""
    Session = sessionmaker(bind=pg_engine)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG预约并发院{tag}", org_type="township", level="township")
        db.add(org)
        db.flush()
        user = User(username=f"pg_appt_{tag}", password_hash="x", full_name=f"预约经办{tag}",
                    role="doctor", org_id=org.id)
        db.add(user)
        db.commit()
        ids = {"tag": tag, "org_id": org.id, "user_id": user.id, "slot_ids": [], "patient_ids": []}

    yield ids

    with Session() as db:  # 共用库：自己造的行自己收拾（先子后父）
        db.query(Appointment).filter(Appointment.slot_id.in_(ids["slot_ids"])).delete(synchronize_session=False)
        db.query(AppointmentSlot).filter(AppointmentSlot.id.in_(ids["slot_ids"])).delete(synchronize_session=False)
        db.query(Patient).filter(Patient.id.in_(ids["patient_ids"])).delete(synchronize_session=False)
        db.query(User).filter_by(id=ids["user_id"]).delete()
        db.query(Organization).filter_by(id=ids["org_id"]).delete()
        db.commit()


def _new_booking(db, world, status="booked"):
    """新开一个容量 5 的号源：两位旁人各约一个号，再加一条要抢的预约（按 status 落库），号源已约数与之相符。

    旁人的号是必须的：号源一侧的条件 UPDATE 不会把已约数扣成负数，只有一条预约时「多扣」会被 0 兜住、看不出来。
    返回 (号源 id, 要抢的预约 id, 它的患者 id)。"""
    suffix = uuid.uuid4().hex[:10]
    slot = AppointmentSlot(org_id=world["org_id"], resource_type="outpatient", resource_name=f"并发全科{suffix}",
                           slot_date=(clock.today() + timedelta(days=3)).isoformat(), capacity=5,
                           booked=2 if status == "cancelled" else 3)
    patients = [
        Patient(name=f"预约并发患者{suffix}-{i}", id_card=f"3312{uuid.uuid4().int % 10**14:014d}",
                gender="女", birth_date="1970-07-07", ehc_no=f"PG-APPT-{suffix}-{i}")
        for i in range(3)
    ]
    db.add_all([slot, *patients])
    db.flush()
    world["slot_ids"].append(slot.id)
    world["patient_ids"].extend(p.id for p in patients)
    target = Appointment(slot_id=slot.id, patient_id=patients[0].id, status=status)
    db.add_all([target, *(Appointment(slot_id=slot.id, patient_id=p.id, status="booked") for p in patients[1:])])
    db.commit()
    return slot.id, target.id, patients[0].id


def _ledger(db, slot_id, appt_id):
    """(号源已约数, 该号源上占号的预约条数, 要抢的那条预约的状态)——前两个必须相等。"""
    booked = db.get(AppointmentSlot, slot_id).booked
    statuses = [a.status for a in db.query(Appointment).filter(Appointment.slot_id == slot_id)]
    holding = sum(1 for s in statuses if s in ("booked", "fulfilled"))
    return booked, holding, db.get(Appointment, appt_id).status


def _outcomes(results):
    ok = [r for r in results if r[0] == "ok"]
    rejected = [r for r in results if r[0] == "rejected"]
    return ok, rejected


def test_八路并发取消同一条预约_号源只释放一次(pg_engine, world):
    Session = sessionmaker(bind=pg_engine)
    with Session() as db:
        slot_id, appt_id, _ = _new_booking(db, world)

    def worker(_i):
        with Session() as db:
            try:
                release_appointment(db, db.get(Appointment, appt_id))
            except HTTPException as exc:
                return ("rejected", exc.status_code, exc.detail)
            return ("ok",)

    _warm_pool(pg_engine, RACERS)
    results, errors = _race_on_pg(worker, times=RACERS)
    assert not errors, f"异常不该漏给调用方：{errors}"
    ok, rejected = _outcomes(results)
    assert len(ok) == 1, f"应恰一路取消成功，实际 {results}"   # 修前：八路都成功
    assert all(r[1] == 409 for r in rejected), rejected
    with Session() as db:
        booked, holding, status = _ledger(db, slot_id, appt_id)
    assert status == "cancelled"
    assert booked == holding == 2, (booked, holding)   # 修前：已约数 0（扣到底）、两位旁人还占着号——之后能超卖两个


def test_取消与核销同时到_只成一路_号源与状态对得上(pg_engine, world):
    Session = sessionmaker(bind=pg_engine)
    with Session() as db:
        slot_id, appt_id, _ = _new_booking(db, world)

    def worker(i):
        with Session() as db:
            try:
                if i % 2:
                    release_appointment(db, db.get(Appointment, appt_id))
                else:
                    fulfill(appt_id, db=db, user=db.get(User, world["user_id"]))
            except HTTPException as exc:
                return ("rejected", exc.status_code, exc.detail)
            return ("ok", "cancel" if i % 2 else "fulfill")

    _warm_pool(pg_engine, RACERS)
    results, errors = _race_on_pg(worker, times=RACERS)
    assert not errors, f"异常不该漏给调用方：{errors}"
    ok, rejected = _outcomes(results)
    assert len(ok) == 1, f"取消与核销应只成一路，实际 {results}"   # 修前：取消与核销都成功
    assert all(r[1] == 409 for r in rejected), rejected
    with Session() as db:
        booked, holding, status = _ledger(db, slot_id, appt_id)
    assert status == ("cancelled" if ok[0][1] == "cancel" else "fulfilled"), (ok, status)
    assert booked == holding == (2 if status == "cancelled" else 3), (booked, holding, status)
    # 修前：几路取消各放一次号（扣到底）、核销又落「已就诊」——已约数 0，三条还占着号


def test_八路并发重约同一条已取消的预约_号源只占一次(pg_engine, world):
    Session = sessionmaker(bind=pg_engine)
    with Session() as db:
        slot_id, appt_id, patient_id = _new_booking(db, world, status="cancelled")

    def worker(_i):
        with Session() as db:
            try:
                book_slot(db, slot_id, patient_id)
            except HTTPException as exc:
                return ("rejected", exc.status_code, exc.detail)
            return ("ok",)

    _warm_pool(pg_engine, RACERS)
    results, errors = _race_on_pg(worker, times=RACERS)
    assert not errors, f"异常不该漏给调用方：{errors}"
    ok, rejected = _outcomes(results)
    assert len(ok) == 1, f"应恰一路重约成功，实际 {results}"   # 修前：三路成功（剩三个号，占满为止）
    assert all(r[1] == 409 for r in rejected), rejected
    with Session() as db:
        booked, holding, status = _ledger(db, slot_id, appt_id)
    assert status == "booked"
    assert booked == holding == 3, (booked, holding)   # 修前：已约数 5（约满）——一条预约占了三个号
