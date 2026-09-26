"""慢专病任务批量分配的真 PG 取证（P2-347，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_spd_task_batch_assign_pg_races.py -q

批量分配原先是「载入整批 → 内存里判过 → 往对象上赋值」，会话 autoflush 关着，UPDATE 要到提交时才发、只有 `WHERE id = ?`：
载入之后别人刚办结的任务照样被改了责任人（计分记在原责任人名下，任务却显示归新人），待接收的分配完也还是待接收。修后与
单条分配同一个状态闸门（`move_task`，`SET status = CASE WHEN status = 'pending' THEN 'claimed' ELSE status END` 与责任人
同一条 SQL，`WHERE status IN (未结束)`）。SQLite 一侧已有同一时序（`test_spd_task_batch_assign_gate.py`）；这里在 PG 上
再走一遍：按状态改值的 CASE 表达式与「提交之后按库里的新状态重判」都是方言相关的，SQLite 绿了不等于 PG 也对（§6）。

时序：整批那一路载入任务、校验完责任人之后，另一个连接当场把任务办结并提交（整批那一路此时还没写任何行，不会互等），
再放它往下写——与生产上「载入整批」到「逐条写」之间别人办结的窗口同形。

**这套库是多人共用的**：只用带随机后缀的自建数据，跑完自己收拾，从不 DROP SCHEMA；空库就地补跑迁移（只做加法）。
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import sessionmaker

from app.models import Organization, Patient, User
from app.spd.models import SpdTask
from app.spd.routers import tasks
from app.spd.routers.tasks import BatchTaskIn, SubmitIn, batch_tasks, complete_task

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"),
]

SERVER_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pg_engine():
    """连既有测试库；空库就地补跑迁移，任何情况下都不清 schema（理由同 test_insurance_apply_unique_races）。"""
    engine = create_engine(PG_URL, pool_size=4, max_overflow=4)
    if not sa_inspect(engine).has_table("spd_tasks"):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_DIR, env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"迁移在 PG 上失败：\n{result.stderr[-2000:]}"
    yield engine
    engine.dispose()


def _sessions(pg_engine):
    """与 `app.database.SessionLocal` 同一个配置（autoflush 关）：这个缺陷的窗口正是「改了内存、要到提交才发 UPDATE」。"""
    return sessionmaker(bind=pg_engine, autoflush=False)


@pytest.fixture(scope="module")
def world(pg_engine):
    """机构、患者、两位本机构医生（原责任人 / 新责任人，都过机构写权限）；跑完把自己造的行全收拾掉。"""
    Session = _sessions(pg_engine)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG批量分配院{tag}", org_type="township", level="township")
        patient = Patient(name=f"批量分配患者{tag}", id_card=f"3319{uuid.uuid4().int % 10**14:014d}",
                          ehc_no=f"PG-BA-{tag}")
        db.add_all([org, patient])
        db.flush()
        doctors = [User(username=f"pg_ba_{who}_{tag}", password_hash="x", full_name=f"医生{who}", role="doctor",
                        org_id=org.id) for who in ("a", "b")]
        db.add_all(doctors)
        db.commit()
        ids = {"org": org.id, "patient": patient.id, "a": doctors[0].id, "b": doctors[1].id, "tasks": []}

    yield ids

    with Session() as db:  # 共用库：自己造的行自己收拾（先子后父）
        db.query(SpdTask).filter(SpdTask.id.in_(ids["tasks"] or [0])).delete(synchronize_session=False)
        db.query(User).filter(User.id.in_([ids["a"], ids["b"]])).delete(synchronize_session=False)
        db.query(Patient).filter_by(id=ids["patient"]).delete()
        db.query(Organization).filter_by(id=ids["org"]).delete()
        db.commit()


def _task(Session, world, status, assignee=None):
    with Session() as db:
        row = SpdTask(patient_id=world["patient"], org_id=world["org"], task_type="followup", title="批量分配取证",
                      status=status, assignee_id=assignee)
        db.add(row)
        db.commit()
        world["tasks"].append(row.id)
        return row.id


def _row(Session, task_id):
    with Session() as db:
        row = db.get(SpdTask, task_id)
        return row.status, row.assignee_id, row.transferred_from


def _assign(Session, world, task_id, to):
    with Session() as db:
        return batch_tasks(BatchTaskIn(task_ids=[task_id], action="assign", assignee_id=world[to]),
                           db=db, user=db.get(User, world["a"]))


def test_批量分配待接收的任务_在PG上同样转成已接收(pg_engine, world):
    Session = _sessions(pg_engine)
    tid = _task(Session, world, "pending")
    assert _assign(Session, world, tid, "b") == {"processed": 1, "skipped": []}
    assert _row(Session, tid) == ("claimed", world["b"], None)   # 修前：分配完还是待接收（单条分配是已接收）


def test_载入整批之后任务被办结_批量分配跳过_责任人不被改(pg_engine, world, monkeypatch):
    Session = _sessions(pg_engine)
    tid = _task(Session, world, "claimed", assignee=world["a"])
    real, fired = tasks.unusable_user, []

    def racing(db, user_id):
        result = real(db, user_id)
        if not fired:   # 整批那一路已载入任务、还没写任何行：另一个连接当场办结并提交
            fired.append(True)
            with Session() as other:
                complete_task(tid, SubmitIn(), db=other, user=other.get(User, world["a"]))
        return result

    monkeypatch.setattr(tasks, "unusable_user", racing)
    out = _assign(Session, world, tid, "b")
    monkeypatch.undo()
    assert fired
    assert out == {"processed": 0, "skipped": [{"id": tid, "reason": "任务已结束"}]}   # 修前 processed 1
    assert _row(Session, tid) == ("done", world["a"], None)   # 修前：已办结的任务责任人被改成 b、转派来源记成 a
