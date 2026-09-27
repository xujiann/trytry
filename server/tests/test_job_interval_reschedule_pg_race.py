"""改定时任务间隔的重排与调度器收尾并发的真 PostgreSQL 取证（P2-467 补；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_job_interval_reschedule_pg_race.py -q

P2-467 让 `PATCH /api/jobs/{name}` 改间隔时顺带重排下次到期：`min(原定到期, 上次执行 + 新间隔)`。原先这两个值是
锁外读的——日跑任务到期、调度线程正在跑它，管理员此时改成每小时：调度器收尾把「上次执行 = 现在、下次到期 = 明天」
落库，改间隔这一路随后按读到的旧值写回「昨天 + 1 小时」，下次到期被拽回过去，下一轮调度就再跑一遍。

修后圈进这一行的临界区（PG 上 `SELECT … FOR UPDATE`）、锁到手后重读再算。时序：先开一个会话替调度器落收尾那条
UPDATE、不提交（持着行锁）；另起一路改间隔，等它被这把行锁挡住（修前挡在写回的 UPDATE 上，修后挡在 FOR UPDATE
上——两者都在读过旧值之后），再让调度器提交。

**这套库是多人共用的**：只用自建数据，跑完自己收拾，从不 DROP SCHEMA；空库就地补跑迁移（只做加法）。
"""
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import sessionmaker

from app.clock import now_naive
from app.models import ScheduledJob
from app.routers.jobs import JobUpdate, update_job

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
    if not sa_inspect(engine).has_table("scheduled_jobs"):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_DIR, env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"迁移在 PG 上失败：\n{result.stderr[-2000:]}"
    yield engine
    engine.dispose()


@pytest.fixture()
def due_job(pg_engine):
    """一个日跑、昨天这个点跑过、5 分钟前到期（调度线程正在跑它）的任务；跑完删掉。"""
    Session = sessionmaker(bind=pg_engine, autoflush=False)
    ran_at = now_naive().replace(microsecond=0)
    name = f"pg_job_{uuid.uuid4().hex[:8]}"
    with Session() as db:
        db.add(ScheduledJob(name=name, title="PG 重排取证", interval_seconds=86400,
                            last_run_at=ran_at - timedelta(days=1, minutes=5), next_run_at=ran_at - timedelta(minutes=5)))
        db.commit()

    yield name, ran_at

    with Session() as db:
        db.query(ScheduledJob).filter_by(name=name).delete()
        db.commit()


def _blocked_on_lock(pg_engine, deadline: float) -> bool:
    """等到有一路碰 scheduled_jobs 的语句在等锁（修前是写回的 UPDATE，修后是 FOR UPDATE）。"""
    while time.monotonic() < deadline:
        with pg_engine.connect() as conn:
            waiting = conn.execute(text(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                "AND datname = current_database() AND query ILIKE '%scheduled_jobs%'")).scalar()
        if waiting:
            return True
        time.sleep(0.05)
    return False


def test_调度器收尾时改间隔_按刚落库的执行重排_不把下次到期拽回过去(pg_engine, due_job):
    name, ran_at = due_job
    Session = sessionmaker(bind=pg_engine, autoflush=False)
    outcome: dict = {}

    def patch_interval():
        with Session() as db:
            try:
                outcome["body"] = update_job(name, JobUpdate(interval_seconds=3600), db=db)
            except Exception as exc:  # noqa: BLE001 - 带回主线程断言
                outcome["error"] = exc

    scheduler = Session()
    try:
        # 调度器跑完这个任务，收尾那条 UPDATE 已发出、未提交：持着这一行的行锁
        row = scheduler.query(ScheduledJob).filter_by(name=name).one()
        row.last_run_at, row.last_status = ran_at, "succeeded"
        row.next_run_at = ran_at + timedelta(days=1)
        scheduler.flush()

        thread = threading.Thread(target=patch_interval)
        thread.start()
        assert _blocked_on_lock(pg_engine, time.monotonic() + 10), "改间隔那一路没有排到这把行锁上"
        scheduler.commit()
    finally:
        scheduler.close()
    thread.join(timeout=30)

    assert "error" not in outcome, outcome
    assert outcome["body"]["interval_seconds"] == 3600
    with Session() as db:
        next_run_at = db.query(ScheduledJob).filter_by(name=name).one().next_run_at
    # 修前 = 昨天 + 55 分钟（按锁外读到的旧值重排）：已过点，下一轮调度再跑一遍
    assert next_run_at == ran_at + timedelta(hours=1), next_run_at
