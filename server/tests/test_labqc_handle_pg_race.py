"""失控点处理登记的真并发取证（P2-450，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_labqc_handle_pg_race.py -q

原先判「已处理」、赋值、commit，UPDATE 只有 `WHERE id = ?`：PG 的 READ COMMITTED 下并发的几路都读到「未处理」、都往下写，
全都 200，库里留下最后提交的那份原因与处理人。修后走 `concurrency.move_row`（`WHERE handled IS FALSE`），行锁让后到的
一路等前一路提交、再按新状态重判。SQLite 一侧用确定时序钉逻辑（`test_labqc_handle_race.py`）。

不变式：**一个失控点只登记一次处理**——恰一路 200、其余 409，库里的原因与处理人就是成的那一路。

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

from app.models import Organization, QcLot, QcMeasurement
from app.routers.labqc import HandleIn, handle_measurement

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
    if not sa_inspect(engine).has_table("qc_measurements"):
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
def lot(pg_engine):
    """一家检验科医院与一个质控批号；跑完把自己造的行全收拾掉（先子后父）。"""
    Session = sessionmaker(bind=pg_engine, autoflush=False)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG检验科{tag}", org_type="lead_hospital", level="county")
        db.add(org)
        db.flush()
        row = QcLot(org_id=org.id, item_code="K", item_name="血清钾", lot_no=f"PG-{tag}", target_value=5.0, sd=0.5)
        db.add(row)
        db.commit()
        ids = {"org": org.id, "lot": row.id}

    yield ids["lot"]

    with Session() as db:
        db.query(QcMeasurement).filter_by(lot_id=ids["lot"]).delete()
        db.query(QcLot).filter_by(id=ids["lot"]).delete()
        db.query(Organization).filter_by(id=ids["org"]).delete()
        db.commit()


def test_几位技师同时登记同一失控点_只登记一次(pg_engine, lot):
    Session = sessionmaker(bind=pg_engine, autoflush=False)
    for _ in range(ROUNDS):
        with Session() as db:
            point = QcMeasurement(lot_id=lot, value=7.0, out_of_control=True, violated_rules="1-3s")
            db.add(point)
            db.commit()
            mid = point.id

        def worker(index, mid=mid):
            # 全域角色过机构归属校验（只读 role / org_id）；处理人记的是 full_name
            user = SimpleNamespace(role="admin", org_id=None, full_name=f"技师{index}", username=f"lab{index}")
            with Session() as db:
                try:
                    handle_measurement(mid, HandleIn(reason=f"原因{index}", corrective_action="重新定标"), db=db, user=user)
                    return ("ok", index)
                except HTTPException as exc:
                    return (exc.status_code, index)

        results, errors = _race_on_pg(worker, RACERS)
        assert not errors, errors
        assert len(results) == RACERS, results
        winners = [index for code, index in results if code == "ok"]
        assert len(winners) == 1, results   # 修前：好几路 200，库里是最后提交的那份
        assert sorted(code for code, _ in results if code != "ok") == [409] * (RACERS - 1), results
        with Session() as db:
            row = db.get(QcMeasurement, mid)
            assert (row.handled, row.handle_reason, row.handled_by) == (True, f"原因{winners[0]}", f"技师{winners[0]}")
