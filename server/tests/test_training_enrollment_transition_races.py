"""实训退报名 / 重报名的真并发取证（P2-111，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_training_enrollment_transition_races.py -q

退报名（enrolled → cancelled，名额 -1）与退过报名的人重报（cancelled → enrolled，名额 +1）原先都是「内存里判状态 → 改」。
名额计数一侧的条件 UPDATE 只保证不越界，保证不了「一次转换只动一次名额」：PG 的 READ COMMITTED 下同一个人的几路
并发请求都读到同一个旧状态、都往下走——

- 八路同时退同一条报名：八路都成功，名额计数扣八次（扣到 0 为止）——另两位学员还占着名额，之后能多报进两个人；
- 退过报名的人八路同时重报：剩几个名额就成功几路，一个人占了好几个名额。

SQLite 的库级写锁把窗口压平，开发库上同一份代码永远绿（SQLite 一侧用「拿着旧对象接着转」的确定时序钉逻辑，
见 test_training_enrollment_transition.py）。这里钉 PG 上真并发的不变量：**每一轮恰一路成功，名额计数 = 该计划
「已报名」的条数**。

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

from app.models import Organization, TrainingEnrollment, TrainingPlan, User
from app.routers.education import cancel_enroll, enroll_plan

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
    if not all(sa_inspect(engine).has_table(t) for t in ("training_plans", "training_enrollments")):
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
    """一家机构、三名本机构学员（报名要过机构写权限）；各用例自己开计划（名字带随机后缀）。"""
    Session = sessionmaker(bind=pg_engine)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG实训并发基地{tag}", org_type="lead_hospital", level="county")
        db.add(org)
        db.flush()
        users = [User(username=f"pg_enr_{tag}_{i}", password_hash="x", full_name=f"并发学员{i}",
                      role="doctor", org_id=org.id) for i in range(3)]
        db.add_all(users)
        db.commit()
        ids = {"tag": tag, "org_id": org.id, "user_ids": [u.id for u in users], "plan_ids": []}

    yield ids

    with Session() as db:  # 共用库：自己造的行自己收拾（先子后父）
        db.query(TrainingEnrollment).filter(TrainingEnrollment.plan_id.in_(ids["plan_ids"])).delete(
            synchronize_session=False)
        db.query(TrainingPlan).filter(TrainingPlan.id.in_(ids["plan_ids"])).delete(synchronize_session=False)
        db.query(User).filter(User.id.in_(ids["user_ids"])).delete(synchronize_session=False)
        db.query(Organization).filter_by(id=ids["org_id"]).delete()
        db.commit()


def _new_plan(db, world, target_status):
    """容量 5 的计划：两位旁人各报一个名额，第一位学员的报名按 target_status 落库，名额计数与之相符。

    旁人的名额是必须的：名额计数一侧的条件 UPDATE 不会扣成负数，只有一条报名时「多扣」会被 0 兜住、看不出来。"""
    uid, *others = world["user_ids"]
    plan = TrainingPlan(title=f"并发实训{uuid.uuid4().hex[:8]}", org_id=world["org_id"], plan_date="2026-10-08",
                        capacity=5, enrolled_count=2 if target_status == "cancelled" else 3,
                        created_by=uid, status="open")
    db.add(plan)
    db.flush()
    world["plan_ids"].append(plan.id)
    db.add_all([TrainingEnrollment(plan_id=plan.id, user_id=uid, status=target_status),
                *(TrainingEnrollment(plan_id=plan.id, user_id=o, status="enrolled") for o in others)])
    db.commit()
    return plan.id


def _ledger(db, plan_id):
    """(名额计数, 已报名条数, 第一位学员的报名状态)——前两个必须相等。"""
    plan = db.get(TrainingPlan, plan_id)
    rows = db.query(TrainingEnrollment).filter_by(plan_id=plan_id).all()
    enrolled = sum(1 for r in rows if r.status == "enrolled")
    first = next(r for r in rows if r.user_id == plan.created_by)
    return plan.enrolled_count, enrolled, first.status


def _race(pg_engine, world, fn, plan_id):
    Session = sessionmaker(bind=pg_engine)

    def worker(_i):
        with Session() as db:
            try:
                fn(plan_id, db=db, user=db.get(User, world["user_ids"][0]))
            except HTTPException as exc:
                return ("rejected", exc.status_code, exc.detail)
            return ("ok",)

    _warm_pool(pg_engine, RACERS)
    results, errors = _race_on_pg(worker, times=RACERS)
    assert not errors, f"异常不该漏给调用方：{errors}"
    return [r for r in results if r[0] == "ok"], [r for r in results if r[0] == "rejected"], results


def test_八路并发退同一条报名_名额只放一个(pg_engine, world):
    Session = sessionmaker(bind=pg_engine)
    with Session() as db:
        plan_id = _new_plan(db, world, "enrolled")
    ok, rejected, results = _race(pg_engine, world, cancel_enroll, plan_id)
    assert len(ok) == 1, f"应恰一路退成，实际 {results}"   # 修前：八路都成功
    assert all(r[1] == 404 for r in rejected), rejected   # 与顺序重复退报名同一句「未报名该实训计划」
    with Session() as db:
        count, enrolled, status = _ledger(db, plan_id)
    assert status == "cancelled"
    assert count == enrolled == 2, (count, enrolled)   # 修前：计数 0、两位旁人还占着名额


def test_退过报名的人八路并发重报_只占一个名额(pg_engine, world):
    Session = sessionmaker(bind=pg_engine)
    with Session() as db:
        plan_id = _new_plan(db, world, "cancelled")
    ok, rejected, results = _race(pg_engine, world, enroll_plan, plan_id)
    assert len(ok) == 1, f"应恰一路重报成功，实际 {results}"   # 修前：三路成功（剩三个名额，占满为止）
    assert all(r[1] == 409 for r in rejected), rejected
    with Session() as db:
        count, enrolled, status = _ledger(db, plan_id)
    assert status == "enrolled"
    assert count == enrolled == 3, (count, enrolled)   # 修前：计数 5（约满）——一个人占了三个名额
