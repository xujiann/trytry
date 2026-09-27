"""「已完成⇒进度 100%」在真并发下的取证（P2-416，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_project_progress_pg_races.py -q

原先结项校验按锁外读到的另一列判：四路只改状态结项（读到进度 100）、四路同时只把进度改成 60（读到「进行中」），
PG 的 READ COMMITTED 下各路都判过、都 200，库里留下「已完成但进度 60%」。修后只改其中一列时先发一条带条件、
值不变的 UPDATE 占住这一行，判过的那一列变了就 409。SQLite 一侧用确定时序钉逻辑（`test_project_progress_race.py`）。

这里钉的是不变式：**不论谁先谁后，库里不会是「已完成但进度不满 100」**。

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

from app.models import AdminProject, Organization, User
from app.routers.projects import ProjectUpdate, update_project

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"),
]

RACERS = 8
ROUNDS = 3
SERVER_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pg_engine():
    """连既有测试库；空库就地补跑迁移，任何情况下都不清 schema（理由同 test_insurance_apply_unique_races）。
    池子按参赛路数开并先热身，否则冷池下测出来的是排队而不是并发（见 test_cost_allocation_races）。"""
    engine = create_engine(PG_URL, pool_size=RACERS, max_overflow=RACERS)
    if not sa_inspect(engine).has_table("admin_projects"):
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
    """一家机构、一位管理层；跑完把自己造的行全收拾掉。"""
    Session = sessionmaker(bind=pg_engine, autoflush=False)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG项目院{tag}", org_type="township", level="township")
        db.add(org)
        db.flush()
        director = User(username=f"pg_proj_dir_{tag}", password_hash="x", full_name="项目主任", role="director",
                        org_id=org.id)
        db.add(director)
        db.commit()
        ids = {"org": org.id, "director": director.id}

    yield ids

    with Session() as db:  # 共用库：自己造的行自己收拾（先子后父）
        db.query(AdminProject).filter_by(org_id=ids["org"]).delete()
        db.query(User).filter_by(id=ids["director"]).delete()
        db.query(Organization).filter_by(id=ids["org"]).delete()
        db.commit()


def test_结项与改进度交错_库里不会是已完成但进度不满(pg_engine, world):
    Session = sessionmaker(bind=pg_engine, autoflush=False)
    for _ in range(ROUNDS):
        with Session() as db:
            project = AdminProject(org_id=world["org"], name="PG并发项目", status="ongoing", progress_pct=100)
            db.add(project)
            db.commit()
            pid = project.id

        def worker(index):
            with Session() as db:
                body = ProjectUpdate(status="done") if index % 2 == 0 else ProjectUpdate(progress_pct=60)
                try:
                    update_project(pid, body, db=db, user=db.get(User, world["director"]))
                    return ("ok", index)
                except HTTPException as exc:
                    return (exc.status_code, index)

        results, errors = _race_on_pg(worker, RACERS)
        assert not errors, errors
        assert len(results) == RACERS and all(code in ("ok", 409, 422) for code, _ in results), results
        with Session() as db:
            row = db.get(AdminProject, pid)
            assert not (row.status == "done" and row.progress_pct != 100), (row.status, row.progress_pct, results)
