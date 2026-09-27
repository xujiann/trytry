"""绿道节点「不得先救治后发病」在真并发下的取证（P2-419，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_emergency_milestone_pg_races.py -q

时序判定读的是本例**别的**节点、写的是一条 INSERT——INSERT 不给任何既有行加锁，唯一约束 `(case_id, milestone)`
只挡同一节点记两次。六路同时各记一个节点、时间与固定序列两两倒挂（发病 10:05、呼救 10:04 … 开始救治 10:00），
PG 的 READ COMMITTED 下各路都读到「还没别的节点」、都判不矛盾，修前一例里落下好几条互相矛盾的节点。修后判定与
写入圈在这例急救事件那一行的 `serialized_on`（PG 上 `SELECT … FOR UPDATE`）。SQLite 一侧见
`test_emergency_milestone_order_race.py`。

不变式：**不论谁先谁后，库里这一例已记的节点按固定序列时间单调不减**；两两倒挂，所以恰一路记上、其余 422。

**这套库是多人共用的**：只用带随机后缀的自建数据，跑完自己收拾，从不 DROP SCHEMA；空库就地补跑迁移（只做加法）。
"""
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import sessionmaker

from test_disease_enrollment_unique_races import _warm_pool  # 连接池热身，理由见 test_cost_allocation_races
from test_postgres_real import _race_on_pg  # Barrier 真并发的既有夹具，不另造一份

from app.models import EmergencyCase, EmergencyMilestone
from app.routers.emergency import MILESTONE_SEQUENCE, MilestoneCreate, record_milestone

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"),
]

RACERS = len(MILESTONE_SEQUENCE)   # 六个节点各一路
ROUNDS = 3
SERVER_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pg_engine():
    """连既有测试库；空库就地补跑迁移，任何情况下都不清 schema（理由同 test_insurance_apply_unique_races）。
    池子按参赛路数开并先热身，否则冷池下测出来的是排队而不是并发（见 test_cost_allocation_races）。"""
    engine = create_engine(PG_URL, pool_size=RACERS, max_overflow=RACERS)
    if not sa_inspect(engine).has_table("emergency_milestones"):
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
def cases(pg_engine):
    """每轮一例急救事件，事发地带随机后缀；跑完把自己造的行全收拾掉（先子后父）。"""
    Session = sessionmaker(bind=pg_engine, autoflush=False)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        rows = [EmergencyCase(location=f"PG绿道并发{tag}-{i}", channel_type="chest_pain") for i in range(ROUNDS)]
        db.add_all(rows)
        db.commit()
        ids = [row.id for row in rows]

    yield ids

    with Session() as db:
        db.query(EmergencyMilestone).filter(EmergencyMilestone.case_id.in_(ids)).delete(synchronize_session=False)
        db.query(EmergencyCase).filter(EmergencyCase.id.in_(ids)).delete(synchronize_session=False)
        db.commit()


def test_六路同时记两两倒挂的节点_库里不会先救治后发病(pg_engine, cases):
    Session = sessionmaker(bind=pg_engine, autoflush=False)
    for case_id in cases:
        def worker(index, case_id=case_id):
            milestone = MILESTONE_SEQUENCE[index]
            # 序列越靠后、时间越早：任意两个节点都互相倒挂
            body = MilestoneCreate(milestone=milestone, occurred_at=f"2026-09-27 10:{RACERS - 1 - index:02d}")
            with Session() as db:
                try:
                    record_milestone(case_id, body, db=db)
                    return ("ok", milestone)
                except HTTPException as exc:
                    return (exc.status_code, milestone)

        results, errors = _race_on_pg(worker, RACERS)
        assert not errors, errors
        assert len(results) == RACERS, results
        with Session() as db:
            recorded = {
                row.milestone: datetime.fromisoformat(row.occurred_at)
                for row in db.query(EmergencyMilestone).filter(EmergencyMilestone.case_id == case_id)
            }
        times = [recorded[m] for m in MILESTONE_SEQUENCE if m in recorded]
        assert times == sorted(times), (recorded, results)   # 修前：好几条互相倒挂的节点
        assert sorted(code for code, _ in results if code != "ok") == [422] * (RACERS - 1), results
        assert len(recorded) == 1, (recorded, results)
