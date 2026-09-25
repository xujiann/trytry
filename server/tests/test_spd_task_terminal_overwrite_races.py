"""慢专病任务终态被整批写入盖掉的真并发取证（P2-114，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_spd_task_terminal_overwrite_races.py -q

超期扫描（`sweep_overdue`）与结案收尾（`close_open_work`）都是「查一批未结束的任务 → 逐条 `task.status = …` → 由调用方
提交」，会话 autoflush 关着，UPDATE 要到提交时才发，而且只有 `WHERE id = ?`。窗口是从查询到提交的整段：

- 扫描期间医护办结了一条（计了分）：提交时被改回「超期」，重新进待办，再办一次随访计分再记一笔；
- 结案期间医护办结了一条：提交时被改成「已取消」，分记了、任务却是取消的。

SQLite 上这两处读的是查询、摆不出确定时序，只有真 PG 看得到。这里用两条线程把窗口钉住：整批那一路做完（不提交）→
另一路办结 → 放行整批那一路提交。修后整批的每一条都是条件 UPDATE（`service.move_task`），行锁让办结那一路等它提交后
按新状态重判：扫描之后办结照常成（超期也是未结束），结案之后办结 409、不计分。

**这套库是多人共用的**：只用带随机后缀的自建数据，跑完自己收拾，从不 DROP SCHEMA；扫描只传一个远古的「今天」，扫得到的
只有这里造的那条任务。
"""
import os
import subprocess
import sys
import threading
import uuid
from datetime import date
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import sessionmaker

from app.models import Organization, Patient, User
from app.spd.models import SpdEnrollment, SpdPointAccount, SpdPointRecord, SpdPointRule, SpdTask
from app.spd.routers.tasks import SubmitIn, complete_task
from app.spd.service import close_open_work, sweep_overdue

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"),
]

SERVER_DIR = Path(__file__).resolve().parents[1]
ANCIENT_TODAY = date(2000, 1, 2)   # 扫描的「今天」：只有 due_date 早于它的任务会被扫到，即这里造的那条


@pytest.fixture(scope="module")
def pg_engine():
    """连既有测试库；空库就地补跑迁移，任何情况下都不清 schema（理由同 test_insurance_apply_unique_races）。"""
    engine = create_engine(PG_URL, pool_size=4, max_overflow=4)
    if not all(sa_inspect(engine).has_table(t) for t in ("spd_tasks", "spd_point_records")):
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
    """机构、本机构医生（办结要过机构写权限）、村医（计分对象）、患者；随访计分规则没有就补一条（跑完删掉补的）。"""
    Session = _sessions(pg_engine)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG任务终态院{tag}", org_type="township", level="township")
        patient = Patient(name=f"任务终态患者{tag}", id_card=f"3316{uuid.uuid4().int % 10**14:014d}",
                          gender="男", birth_date="1955-05-05", ehc_no=f"PG-TASK-{tag}")
        db.add_all([org, patient])
        db.flush()
        doctor = User(username=f"pg_task_dr_{tag}", password_hash="x", full_name="办结医生", role="doctor", org_id=org.id)
        village = User(username=f"pg_task_vd_{tag}", password_hash="x", full_name="村医", role="doctor", org_id=org.id)
        db.add_all([doctor, village])
        rule_added = None
        if db.query(SpdPointRule).filter_by(event="followup", active=True).first() is None:
            rule = SpdPointRule(code=f"pt_fu_{tag}", name="随访完成", event="followup", points=3, daily_limit=30)
            db.add(rule)
            db.flush()
            rule_added = rule.id
        db.commit()
        ids = {"tag": tag, "org_id": org.id, "patient_id": patient.id, "doctor_id": doctor.id, "village_id": village.id,
               "rule_added": rule_added, "enrollment_ids": [], "task_ids": []}

    yield ids

    with Session() as db:  # 共用库：自己造的行自己收拾（先子后父）
        account_ids = [a.id for a in db.query(SpdPointAccount).filter_by(user_id=ids["village_id"])]
        db.query(SpdPointRecord).filter(SpdPointRecord.account_id.in_(account_ids)).delete(synchronize_session=False)
        db.query(SpdPointAccount).filter(SpdPointAccount.id.in_(account_ids)).delete(synchronize_session=False)
        db.query(SpdTask).filter(SpdTask.id.in_(ids["task_ids"])).delete(synchronize_session=False)
        db.query(SpdEnrollment).filter(SpdEnrollment.id.in_(ids["enrollment_ids"])).delete(synchronize_session=False)
        if ids["rule_added"] is not None:
            db.query(SpdPointRule).filter_by(id=ids["rule_added"]).delete()
        db.query(User).filter(User.id.in_([ids["doctor_id"], ids["village_id"]])).delete(synchronize_session=False)
        db.query(Patient).filter_by(id=ids["patient_id"]).delete()
        db.query(Organization).filter_by(id=ids["org_id"]).delete()
        db.commit()


def _sessions(pg_engine):
    """与 `app.database.SessionLocal` 同一个配置（autoflush 关）：这个缺陷的窗口正是「改了内存、要到提交才发 UPDATE」，
    开着 autoflush 的会话在扫描后面的查询里就把 UPDATE 发出去、先拿了行锁，修前的代码也测不出来。"""
    return sessionmaker(bind=pg_engine, autoflush=False)


def _new_task(Session, world, program_suffix):
    """每条用例一份新档案（病种各不相同，免得撞在管唯一）+ 一条随访任务，到期日远古（只有它会被扫到）。"""
    with Session() as db:
        enrollment = SpdEnrollment(patient_id=world["patient_id"], program_code=f"PGT_{world['tag']}_{program_suffix}",
                                   org_id=world["org_id"], status="active", village_doctor_id=world["village_id"])
        db.add(enrollment)
        db.flush()
        task = SpdTask(patient_id=world["patient_id"], enrollment_id=enrollment.id, org_id=world["org_id"],
                       program_code=enrollment.program_code, task_type="followup", title="终态竞态随访",
                       status="pending", due_date="2000-01-01")
        db.add(task)
        db.commit()
        world["enrollment_ids"].append(enrollment.id)
        world["task_ids"].append(task.id)
        return enrollment.id, task.id


def _points(Session, task_id):
    with Session() as db:
        return db.query(SpdPointRecord).filter_by(ref_type="task", ref_id=task_id, direction="in").count()


def _race(Session, world, task_id, batch_write):
    """整批那一路做完（不提交）→ 另一路办结 → 放行整批那一路提交。返回办结那一路的结果。"""
    swept, release = threading.Event(), threading.Event()
    outcome: dict = {}

    def batch():
        try:
            with Session() as db:
                batch_write(db)
                swept.set()
                assert release.wait(timeout=30), "放行信号没等到"
                db.commit()
        except BaseException as exc:  # noqa: BLE001 - 收集断言用
            outcome["batch_error"] = exc
            swept.set()

    def finish():
        try:
            with Session() as db:
                complete_task(task_id, SubmitIn(), db=db, user=db.get(User, world["doctor_id"]))
            outcome["finish"] = "ok"
        except HTTPException as exc:
            outcome["finish"] = (exc.status_code, exc.detail)
        except BaseException as exc:  # noqa: BLE001
            outcome["finish_error"] = exc

    batch_thread = threading.Thread(target=batch)
    batch_thread.start()
    assert swept.wait(timeout=30), "整批那一路没走到提交前"
    finish_thread = threading.Thread(target=finish)
    finish_thread.start()
    finish_thread.join(timeout=1)   # 修前：没人持行锁，办结当场提交；修后：它等在整批那一路的行锁上
    release.set()
    batch_thread.join(timeout=30)
    finish_thread.join(timeout=30)
    assert not batch_thread.is_alive() and not finish_thread.is_alive(), "线程没收尾（行锁互等？）"
    assert "batch_error" not in outcome and "finish_error" not in outcome, outcome
    return outcome["finish"]


def test_超期扫描期间办结的任务_提交后不会被改回超期(pg_engine, world):
    Session = _sessions(pg_engine)
    _, task_id = _new_task(Session, world, "sweep")
    finish = _race(Session, world, task_id, lambda db: sweep_overdue(db, ANCIENT_TODAY))
    assert finish == "ok"   # 扫描之后办结照常成（「超期」也是未结束）
    with Session() as db:
        assert db.get(SpdTask, task_id).status == "done"   # 修前 overdue：重新进待办，再办一次再记一笔分
    assert _points(Session, task_id) == 1


def test_结案收尾期间办结的任务_要么取消要么办结_不会分记了任务却是取消的(pg_engine, world):
    Session = _sessions(pg_engine)
    enrollment_id, task_id = _new_task(Session, world, "close")

    def close(db):
        close_open_work(db, db.get(SpdEnrollment, enrollment_id), "death:终态竞态")

    finish = _race(Session, world, task_id, close)
    with Session() as db:
        status = db.get(SpdTask, task_id).status
    points = _points(Session, task_id)
    # 结案先拿到行：办结那一路按「已取消」重判、409、不计分（修前：办结 200、计了分，提交时又被结案改成「已取消」）
    assert (finish, status, points) == ((409, "该任务已结束"), "cancelled", 0), (finish, status, points)
