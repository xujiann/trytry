"""远程会诊与平台随访任务状态翻转的真并发取证（P2-345 / P2-346，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_consultation_followup_pg_races.py -q

两处原先都是「内存里判状态 → 赋值 → commit」，UPDATE 只有 `WHERE id = ?`：PG 的 READ COMMITTED 下并发的几路都读到同一个
旧状态、都往下走——几位专家同时出具意见都 200，库里留下的是最后提交的那一份；同时受理，受理专家记成后写的那位；几路同时
完成同一条随访，先记的结果被盖掉；完成与取消交错，已完成的被改成已取消。修后两处走 `concurrency.move_row`（「状态还是判过
的那个」与改值压进同一条 UPDATE），行锁让后到的一路等前一路提交、再按新状态重判。SQLite 一侧用确定时序钉逻辑（见
`test_consultation_transition_races.py`、`test_followup_task_transition_races.py`），真并发只有 PG 看得到。

这里钉 PG 上真并发的不变量：**每一轮恰一路成功、其余 409，库里留下的就是成功那一路写的**。

**这套库是多人共用的**：只用带随机后缀的自建数据，跑完自己收拾，从不 DROP SCHEMA；空库就地补跑迁移（只做加法）。
"""
import os
import subprocess
import sys
import uuid
from datetime import date
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import sessionmaker

from test_disease_enrollment_unique_races import _warm_pool  # 连接池热身，理由见 test_cost_allocation_races
from test_postgres_real import _race_on_pg  # Barrier 真并发的既有夹具，不另造一份

from app import visibility
from app.models import AccessLog, Consultation, FollowupTask, Organization, Patient, User
from app.routers.consultations import ConsultationAccept, ConsultationComplete, accept, complete, decline
from app.routers.followups import CompleteIn, cancel_followup, complete_followup

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
    if not all(sa_inspect(engine).has_table(t) for t in ("consultations", "followup_tasks")):
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
    """申请方 / 受邀方两家机构、患者、全域管理员（过归属校验）；跑完把自己造的行连同调阅留痕一并收拾。"""
    Session = _sessions(pg_engine)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        orgs = [Organization(name=f"PG会诊{name}{tag}", org_type=otype, level=level)
                for name, otype, level in (("申请院", "township", "township"), ("受邀院", "lead_hospital", "county"))]
        patient = Patient(name=f"会诊随访并发患者{tag}", id_card=f"3317{uuid.uuid4().int % 10**14:014d}",
                          ehc_no=f"PG-CF-{tag}")
        db.add_all([*orgs, patient])
        db.flush()
        admin = User(username=f"pg_cf_admin_{tag}", password_hash="x", full_name="全域管理员", role="admin")
        db.add(admin)
        db.commit()
        ids = {"from": orgs[0].id, "to": orgs[1].id, "patient": patient.id, "admin": admin.id,
               "consultations": [], "followups": []}

    yield ids

    with Session() as db:  # 共用库：自己造的行自己收拾（先子后父）
        db.query(AccessLog).filter(AccessLog.patient_id == ids["patient"]).delete(synchronize_session=False)
        db.query(Consultation).filter(Consultation.id.in_(ids["consultations"])).delete(synchronize_session=False)
        db.query(FollowupTask).filter(FollowupTask.id.in_(ids["followups"])).delete(synchronize_session=False)
        db.query(User).filter_by(id=ids["admin"]).delete()
        db.query(Patient).filter_by(id=ids["patient"]).delete()
        db.query(Organization).filter(Organization.id.in_([ids["from"], ids["to"]])).delete(synchronize_session=False)
        db.commit()


@pytest.fixture(autouse=True)
def _access_log_on_pg(pg_engine, monkeypatch):
    """会诊每一路都先过 `assert_patient_visible`，调阅留痕由 `visibility.SessionLocal` 另开会话落库——生产上那是同一个库。
    不改道的话留痕落进本进程的 SQLite 测试库、外键对不上，每路报一条「留痕丢失」，收尾也收不到；改道到 PG 与生产同构。"""
    monkeypatch.setattr(visibility, "SessionLocal", _sessions(pg_engine))


def _access_logs(Session, world):
    with Session() as db:
        return db.query(AccessLog).filter_by(patient_id=world["patient"], resource="consultation").count()


def _consultation(Session, world, status="applied"):
    with Session() as db:
        row = Consultation(patient_id=world["patient"], from_org_id=world["from"], to_org_id=world["to"],
                           question="并发取证", status=status, expert_name="专家零" if status == "accepted" else "",
                           created_by=world["admin"])
        db.add(row)
        db.commit()
        world["consultations"].append(row.id)
        return row.id


def _followup(Session, world):
    with Session() as db:
        row = FollowupTask(patient_id=world["patient"], org_id=world["from"], category="discharge",
                           title="并发取证随访", due_date=date.today().isoformat(), status="pending")
        db.add(row)
        db.commit()
        world["followups"].append(row.id)
        return row.id


def _race(Session, world, act):
    """八路同时对同一行走一步；每路回 ("ok", 序号) 或 (状态码, 说明)。"""
    def worker(index):
        with Session() as db:
            try:
                act(db, db.get(User, world["admin"]), index)
                return ("ok", index)
            except HTTPException as exc:
                return (exc.status_code, exc.detail)

    results, errors = _race_on_pg(worker, RACERS)
    assert not errors, errors
    assert len(results) == RACERS, results
    winners = [r[1] for r in results if r[0] == "ok"]
    losers = [r for r in results if r[0] != "ok"]
    assert len(winners) == 1, results   # 修前：八路都 200
    assert all(code == 409 for code, _ in losers), losers
    return winners[0]


def test_八位专家同时出具意见_恰一路成功_留下的就是它写的(pg_engine, world):
    Session = _sessions(pg_engine)
    cid = _consultation(Session, world, "accepted")
    logged = _access_logs(Session, world)
    winner = _race(Session, world, lambda db, user, i: complete(
        cid, ConsultationComplete(opinion=f"意见{i}"), db=db, user=user))
    with Session() as db:
        row = db.get(Consultation, cid)
        assert (row.status, row.opinion) == ("completed", f"意见{winner}")   # 修前：留下最后提交的那一份
    assert _access_logs(Session, world) - logged == RACERS   # 输了的几路也调阅过档案，留痕一条不少


def test_八路同时受理_受理专家就是成功那一路(pg_engine, world):
    Session = _sessions(pg_engine)
    cid = _consultation(Session, world)
    winner = _race(Session, world, lambda db, user, i: accept(
        cid, ConsultationAccept(expert_name=f"专家{i}"), db=db, user=user))
    with Session() as db:
        row = db.get(Consultation, cid)
        assert (row.status, row.expert_name) == ("accepted", f"专家{winner}")


def test_受理与拒绝交错_恰一路成功_状态跟着它走(pg_engine, world):
    Session = _sessions(pg_engine)
    cid = _consultation(Session, world)

    def act(db, user, i):
        if i % 2:
            decline(cid, db=db, user=user)
        else:
            accept(cid, ConsultationAccept(expert_name=f"专家{i}"), db=db, user=user)

    winner = _race(Session, world, act)
    with Session() as db:
        row = db.get(Consultation, cid)
        expected = ("declined", "") if winner % 2 else ("accepted", f"专家{winner}")
        assert (row.status, row.expert_name) == expected   # 修前：已受理的被改成已拒绝


def test_八路同时完成随访_恰一路成功_结果就是它记的(pg_engine, world):
    Session = _sessions(pg_engine)
    tid = _followup(Session, world)
    winner = _race(Session, world, lambda db, user, i: complete_followup(
        tid, CompleteIn(result=f"结果{i}"), db=db, user=user))
    with Session() as db:
        row = db.get(FollowupTask, tid)
        assert (row.status, row.result) == ("done", f"结果{winner}")   # 修前：先记的结果被盖掉


def test_随访完成与取消交错_恰一路成功_已完成的不被改成已取消(pg_engine, world):
    Session = _sessions(pg_engine)
    tid = _followup(Session, world)

    def act(db, user, i):
        if i % 2:
            cancel_followup(tid, db=db, user=user)
        else:
            complete_followup(tid, CompleteIn(result=f"结果{i}"), db=db, user=user)

    winner = _race(Session, world, act)
    with Session() as db:
        row = db.get(FollowupTask, tid)
        assert row.status == ("cancelled" if winner % 2 else "done"), (winner, row.status, row.result)
