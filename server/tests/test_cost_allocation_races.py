"""成本分摊比例合计 ≤ 100 的真并发取证（P1-116，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_cost_allocation_races.py -q

为什么非真 PG 不可：合计校验是「先把现有比例加起来、再插一条」——聚合读的是语句开始时的快照，INSERT 不给任何既有行
加锁，READ COMMITTED 下八路同时读到「已分出 0%」、各插一条 60%，合计 480%。SQLite 的库级写锁把判定与写入之间的
窗口一并锁掉，同一份代码在开发库上永远绿。修法把判定与写入圈进来源科室这一行的 `serialized_on`
（PG 上 `SELECT … FOR UPDATE`），本档钉的就是这一点。

不变量：同一来源科室八路并发各建一条 60%（目标科室各不相同，撞不上唯一约束）——**恰一路成功，其余七路都是
「合计超过 100%」的 422**，库里该来源科室的比例合计 ≤ 100；异常一次都不许漏给调用方。把 `serialized_on` 拿掉
（2026-09-25 在本机 PG 16 上变异实测 5 次、次次红）：八路里 5～7 路成功，合计 300%～420%。

**这套库是多人共用的**：只用带随机后缀的自建数据，跑完自己收拾，从不 DROP SCHEMA；空库就地补跑迁移（只做加法）。
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import sessionmaker

from test_disease_enrollment_unique_races import _warm_pool  # 连接池热身，同上
from test_postgres_real import _race_on_pg  # Barrier 真并发的既有夹具，不另造一份

from app.models import CostAllocationRule, Department, Organization
from app.routers.cost import AllocationIn, create_allocation_rule

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

    池子按参赛路数开并先热身（`_warm_pool`）：冷池下第一路建完规则提交时其余七路还在握手，七个 422 全是
    「看见了已提交的 60%」给的，**把临界区拿掉这条用例照样绿**（实测如此，才补的热身）。
    """
    engine = create_engine(PG_URL, pool_size=RACERS, max_overflow=RACERS)
    if not all(sa_inspect(engine).has_table(t) for t in ("cost_allocation_rules", "departments")):
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
    """一家机构、一个来源科室（后勤）、八个目标科室，名字带随机后缀。"""
    Session = sessionmaker(bind=pg_engine)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG分摊并发医院{tag}", org_type="lead_hospital", level="county")
        db.add(org)
        db.flush()
        source = Department(org_id=org.id, code=f"HQ{tag}", name=f"后勤{tag}", category="admin")
        targets = [Department(org_id=org.id, code=f"T{i}{tag}", name=f"临床{i}{tag}") for i in range(RACERS)]
        db.add_all([source, *targets])
        db.commit()
        ids = {"org_id": org.id, "source_id": source.id, "target_ids": [t.id for t in targets]}

    yield ids

    with Session() as db:  # 共用库：自己造的行自己收拾（先子后父）
        db.query(CostAllocationRule).filter_by(from_dept_id=ids["source_id"]).delete()
        db.query(Department).filter_by(org_id=ids["org_id"]).delete()
        db.query(Organization).filter_by(id=ids["org_id"]).delete()
        db.commit()


def test_并发各建一条60比例_恰一路成功_合计不超过100(pg_engine, world):
    Session = sessionmaker(bind=pg_engine)

    def worker(i):
        with Session() as db:
            try:
                receipt = create_allocation_rule(
                    AllocationIn(from_dept_id=world["source_id"], to_dept_id=world["target_ids"][i], ratio_pct=60),
                    db=db,
                )
            except HTTPException as exc:
                return ("rejected", exc.status_code, exc.detail)
            return ("ok", receipt["id"], receipt["ratio_pct"])

    _warm_pool(pg_engine, RACERS)
    results, errors = _race_on_pg(worker, times=RACERS)
    assert not errors, f"异常不该漏给调用方：{errors}"
    ok = [r for r in results if r[0] == "ok"]
    rejected = [r for r in results if r[0] == "rejected"]
    assert len(ok) == 1, f"应恰一路成功，实际 {results}"   # 拿掉临界区：实测 5～7 路成功
    assert len(rejected) == RACERS - 1
    assert all(code == 422 and "合计超过 100%" in detail for _, code, detail in rejected), rejected
    with Session() as db:
        total = db.query(func.sum(CostAllocationRule.ratio_pct)).filter(
            CostAllocationRule.from_dept_id == world["source_id"]).scalar()
    assert total == 60, total
