"""上门服务工单完成与取消、在线咨询回复的真并发取证（P2-404，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_homevisit_consult_pg_races.py -q

原先都是「内存里判状态 → 赋值 → commit」，UPDATE 只有 `WHERE id = ?`：PG 的 READ COMMITTED 下并发的几路都读到同一个
旧状态、都往下走——完成与取消交错，完成、取消都 200，工单成了「已取消」却挂着服务记录；几位医生同时回复，都 200，库里
留下最后提交的那份答复。修后走 `concurrency.move_row`，行锁让后到的一路等前一路提交、再按新状态重判。SQLite 一侧用
确定时序钉逻辑（`test_homevisit_consult_transition_races.py`）。

**这套库是多人共用的**：只用带随机后缀的自建数据，跑完自己收拾，从不 DROP SCHEMA；空库就地补跑迁移（只做加法）。
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import sessionmaker

from test_disease_enrollment_unique_races import _warm_pool  # 连接池热身，理由见 test_cost_allocation_races
from test_postgres_real import _race_on_pg  # Barrier 真并发的既有夹具，不另造一份

from app.models import HomeVisitOrder, OnlineConsult, Organization, Patient, User
from app.routers.homevisits import VisitComplete, cancel_visit, complete_visit
from app.routers.telemedicine import ReplyBody, reply

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
    if not all(sa_inspect(engine).has_table(t) for t in ("home_visit_orders", "online_consults")):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_DIR, env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"迁移在 PG 上失败：\n{result.stderr[-2000:]}"
    _warm_pool(engine, RACERS)
    yield engine
    engine.dispose()


def _sessions(pg_engine):
    """与 `app.database.SessionLocal` 同一个配置（autoflush 关）。"""
    return sessionmaker(bind=pg_engine, autoflush=False)


@pytest.fixture(scope="module")
def world(pg_engine):
    """一家卫生院、本院医生（过机构写权限）、一位患者；跑完把自己造的行全收拾掉。"""
    Session = _sessions(pg_engine)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG上门院{tag}", org_type="township", level="township")
        patient = Patient(name=f"上门并发患者{tag}", id_card=f"3322{uuid.uuid4().int % 10**14:014d}",
                          ehc_no=f"PG-HV-{tag}")
        db.add_all([org, patient])
        db.flush()
        doctor = User(username=f"pg_hv_doc_{tag}", password_hash="x", full_name="上门医生", role="doctor",
                      org_id=org.id)
        db.add(doctor)
        db.commit()
        ids = {"org": org.id, "patient": patient.id, "doctor": doctor.id}

    yield ids

    with Session() as db:  # 共用库：自己造的行自己收拾（先子后父）
        db.query(HomeVisitOrder).filter_by(org_id=ids["org"]).delete()
        db.query(OnlineConsult).filter_by(org_id=ids["org"]).delete()
        db.query(User).filter_by(id=ids["doctor"]).delete()
        db.query(Patient).filter_by(id=ids["patient"]).delete()
        db.query(Organization).filter_by(id=ids["org"]).delete()
        db.commit()


def _race(Session, world, act):
    def worker(index):
        with Session() as db:
            try:
                act(db, db.get(User, world["doctor"]), index)
                return ("ok", index)
            except HTTPException as exc:
                return (exc.status_code, index)

    results, errors = _race_on_pg(worker, RACERS)
    assert not errors, errors
    assert len(results) == RACERS, results
    assert all(r[0] in ("ok", 409) for r in results), results
    return results


def test_工单完成与取消交错_要么完成_要么取消_不会两样都成(pg_engine, world):
    Session = _sessions(pg_engine)
    with Session() as db:
        order = HomeVisitOrder(patient_id=world["patient"], org_id=world["org"], service_type="nursing",
                               status="dispatched", assignee_name="护士甲", created_by=world["doctor"])
        db.add(order)
        db.commit()
        oid = order.id

    def act(db, user, i):
        if i % 2:
            cancel_visit(oid, db=db, user=user)
        else:
            complete_visit(oid, VisitComplete(service_note=f"服务记录{i}"), db=db, user=user)

    results = _race(Session, world, act)
    completed = [i for code, i in results if code == "ok" and i % 2 == 0]
    cancelled = [i for code, i in results if code == "ok" and i % 2]
    with Session() as db:
        row = db.get(HomeVisitOrder, oid)
        if completed:   # 完成先到：只能完成一次，之后的取消都是「已完成工单不可取消」
            assert (len(completed), cancelled, row.status, row.service_note) == (
                1, [], "completed", f"服务记录{completed[0]}"), results
        else:           # 取消先到：之后的完成都 409，工单上没有服务记录
            assert (row.status, row.service_note) == ("cancelled", ""), results   # 修前：取消了却挂着服务记录


def test_八位医生同时回复_恰一路成功_留下的就是它写的(pg_engine, world):
    Session = _sessions(pg_engine)
    with Session() as db:
        consult = OnlineConsult(patient_id=world["patient"], org_id=world["org"], question="并发取证")
        db.add(consult)
        db.commit()
        cid = consult.id

    results = _race(Session, world, lambda db, user, i: reply(
        cid, ReplyBody(reply=f"答复{i}", doctor_name=f"医生{i}"), db=db, user=user))
    winners = [i for code, i in results if code == "ok"]
    assert len(winners) == 1, results   # 修前：八路都 200
    with Session() as db:
        row = db.get(OnlineConsult, cid)
        assert (row.status, row.reply, row.doctor_name) == ("replied", f"答复{winners[0]}", f"医生{winners[0]}")
