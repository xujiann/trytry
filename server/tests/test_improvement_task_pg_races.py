"""绩效整改任务确认 / 登记进展的真并发取证（P2-463，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_improvement_task_pg_races.py -q

原先判状态、赋值、commit，UPDATE 只有 `WHERE id = ?`：PG 的 READ COMMITTED 下并发的几路都读到同一个旧状态、都往下写，
全都 200——确认关闭与退回拼进同一行，登记进展把刚提交完成的任务改回整改中。修后走 `concurrency.move_row`，行锁让
后到的一路等前一路提交、再按新状态重判。SQLite 一侧用确定时序钉逻辑（`test_improvement_task_races.py`）。

不变式：
- **一条待确认的任务只确认一次**——几位管理者同时点「确认关闭 / 退回」，恰一路 200、其余 409，库里是成的那一路的
  完整结果（关闭：带确认时间与完成时间；退回：整改中、完成时间清空、没有确认时间）；
- **提交完成之后不会被登记进展改回整改中**——一路「提交完成」与几路「登记进展」同时落在一条待整改的任务上，
  最终一定停在「已提交完成、待确认」，抢输的登记进展 409。

**这套库是多人共用的**：只用带随机后缀的自建数据，跑完自己收拾，从不 DROP SCHEMA；空库就地补跑迁移（只做加法）。
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import sessionmaker

from test_disease_enrollment_unique_races import _warm_pool  # 连接池热身，理由见 test_cost_allocation_races
from test_postgres_real import _race_on_pg  # Barrier 真并发的既有夹具，不另造一份

from app.clock import now_naive
from app.models import ImprovementTask, Organization, User
from app.routers.performance import TaskProgress, TaskVerify, progress_task, verify_task

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"),
]

RACERS = 8
ROUNDS = 3
SERVER_DIR = Path(__file__).resolve().parents[1]


def _user(index):
    """全域角色过机构归属校验（只读 role / org_id）；确认人记的是 full_name。直调路由函数，不经依赖注入。"""
    return SimpleNamespace(role="admin", org_id=None, full_name=f"管理者{index}", username=f"mgr{index}")


@pytest.fixture(scope="module")
def pg_engine():
    """连既有测试库；空库就地补跑迁移，任何情况下都不清 schema（理由同 test_insurance_apply_unique_races）。
    池子按参赛路数开并先热身，否则冷池下测出来的是排队而不是并发（见 test_cost_allocation_races）。"""
    engine = create_engine(PG_URL, pool_size=RACERS, max_overflow=RACERS)
    if not sa_inspect(engine).has_table("improvement_tasks"):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_DIR, env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"迁移在 PG 上失败：\n{result.stderr[-2000:]}"
    _warm_pool(engine, RACERS)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def world(pg_engine):
    """一家机构与下达整改的管理者（`created_by` 必填）；跑完把自己造的行全收拾掉（先子后父）。"""
    Session = sessionmaker(bind=pg_engine, autoflush=False)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG整改院{tag}", org_type="township", level="township")
        db.add(org)
        db.flush()
        director = User(username=f"pg_imp_dir_{tag}", password_hash="x", full_name="下达人", role="director",
                        org_id=None)
        db.add(director)
        db.commit()
        ids = {"org": org.id, "user": director.id}

    yield ids

    with Session() as db:
        db.query(ImprovementTask).filter_by(org_id=ids["org"]).delete()
        db.query(User).filter_by(id=ids["user"]).delete()
        db.query(Organization).filter_by(id=ids["org"]).delete()
        db.commit()


def _new_task(Session, world, **values):
    with Session() as db:
        task = ImprovementTask(org_id=world["org"], problem="PG 慢病随访率偏低", owner_name="张三",
                               due_date="2030-01-01", created_by=world["user"], **values)
        db.add(task)
        db.commit()
        return task.id


def test_几位管理者同时确认关闭或退回_只确认一次(pg_engine, world):
    Session = sessionmaker(bind=pg_engine, autoflush=False)
    for _ in range(ROUNDS):
        tid = _new_task(Session, world, status="completed", completion_note="已补做随访", completed_at=now_naive())

        def worker(index, tid=tid):
            with Session() as db:
                try:
                    verify_task(tid, TaskVerify(approve=index % 2 == 0, comment=f"意见{index}"), db=db,
                                user=_user(index))
                    return ("ok", index)
                except HTTPException as exc:
                    return (exc.status_code, index)

        results, errors = _race_on_pg(worker, RACERS)
        assert not errors, errors
        assert len(results) == RACERS, results
        winners = [index for code, index in results if code == "ok"]
        assert len(winners) == 1, results   # 修前：好几路 200，确认与退回拼进同一行
        assert sorted(code for code, _ in results if code != "ok") == [409] * (RACERS - 1), results
        with Session() as db:
            row = db.get(ImprovementTask, tid)
            assert (row.verified_by, row.verify_comment) == (f"管理者{winners[0]}", f"意见{winners[0]}")
            if winners[0] % 2 == 0:
                assert row.status == "verified" and row.verified_at is not None and row.completed_at is not None
            else:
                assert row.status == "in_progress" and row.completed_at is None and row.verified_at is None


def test_提交完成与几路登记进展同时落下_最终停在待确认(pg_engine, world):
    Session = sessionmaker(bind=pg_engine, autoflush=False)
    for _ in range(ROUNDS):
        tid = _new_task(Session, world, status="open")

        def worker(index, tid=tid):
            body = (TaskProgress(complete=True, completion_note="已补做随访 30 人") if index == 0
                    else TaskProgress(measures=f"措施{index}"))
            with Session() as db:
                try:
                    progress_task(tid, body, db=db, user=_user(index))
                    return ("ok", index)
                except HTTPException as exc:
                    return (exc.status_code, exc.detail, index)

        results, errors = _race_on_pg(worker, RACERS)
        assert not errors, errors
        assert len(results) == RACERS, results
        assert ("ok", 0) in results, results   # 提交完成那一路总能成：别的几路都不会把任务推出待整改 / 整改中
        lost = [r for r in results if r[0] != "ok"]
        assert all(r[:2] == (409, "已提交完成、待确认——确认不通过退回后再登记进展") for r in lost), results
        with Session() as db:
            row = db.get(ImprovementTask, tid)
            # 修前：提交完成之后才写下去的登记进展把它改回 in_progress，完成时间还留着
            assert (row.status, row.completion_note) == ("completed", "已补做随访 30 人"), (row.status, results)
            assert row.completed_at is not None
