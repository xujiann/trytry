"""转诊接诊与退回的真并发取证（P2-448，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_referral_status_pg_races.py -q

原先「内存里判状态 → 赋值 → commit」，UPDATE 只有 `WHERE id = ?`：PG 的 READ COMMITTED 下并发的几路都读到「待接诊」、
都往下走——接诊与退回交错，两样都 200，库里是最后提交的那个。修后走 `concurrency.move_row`，行锁让后到的一路等前一路
提交、再按新状态重判。SQLite 一侧用确定时序钉逻辑（`test_referral_status_transition_races.py`）。

不变式：**同一张待接诊的单子，接诊与退回只能成一个**——恰一路 200、其余 409，库里的状态就是成的那一路要的。

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

from app.models import Organization, Patient, Referral, User
from app.routers.referrals import update_status
from app.schemas import ReferralStatusUpdate

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"),
]

RACERS = 8
ROUNDS = 3
SERVER_DIR = Path(__file__).resolve().parents[1]
#: 全域角色过接收方归属校验（`_assert_receiving_org` 只读 role / org_id）；直调路由函数，不经依赖注入
ADMIN = SimpleNamespace(role="admin", org_id=None)


@pytest.fixture(scope="module")
def pg_engine():
    """连既有测试库；空库就地补跑迁移，任何情况下都不清 schema（理由同 test_insurance_apply_unique_races）。
    池子按参赛路数开并先热身，否则冷池下测出来的是排队而不是并发（见 test_cost_allocation_races）。"""
    engine = create_engine(PG_URL, pool_size=RACERS, max_overflow=RACERS)
    if not sa_inspect(engine).has_table("referrals"):
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
    """转出、接收两家机构、转出方医生（`created_by` 必填）与一位患者；跑完把自己造的行全收拾掉（先子后父）。"""
    Session = sessionmaker(bind=pg_engine, autoflush=False)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        src = Organization(name=f"PG转出院{tag}", org_type="township", level="township")
        dst = Organization(name=f"PG接收院{tag}", org_type="lead_hospital", level="county")
        patient = Patient(name=f"转诊并发患者{tag}", id_card=f"3323{uuid.uuid4().int % 10**14:014d}",
                          ehc_no=f"PG-RF-{tag}")
        db.add_all([src, dst, patient])
        db.flush()
        doctor = User(username=f"pg_rf_doc_{tag}", password_hash="x", full_name="转出医生", role="doctor",
                      org_id=src.id)
        db.add(doctor)
        db.commit()
        ids = {"from": src.id, "to": dst.id, "patient": patient.id, "doctor": doctor.id}

    yield ids

    with Session() as db:
        db.query(Referral).filter_by(patient_id=ids["patient"]).delete()
        db.query(User).filter_by(id=ids["doctor"]).delete()
        db.query(Patient).filter_by(id=ids["patient"]).delete()
        db.query(Organization).filter(Organization.id.in_([ids["from"], ids["to"]])).delete(synchronize_session=False)
        db.commit()


def test_接诊与退回同时点_只成一个(pg_engine, world):
    Session = sessionmaker(bind=pg_engine, autoflush=False)
    for _ in range(ROUNDS):
        with Session() as db:
            referral = Referral(patient_id=world["patient"], from_org_id=world["from"], to_org_id=world["to"],
                                direction="up", reason="PG 并发", status="pending", created_by=world["doctor"])
            db.add(referral)
            db.commit()
            rid = referral.id

        def worker(index, rid=rid):
            target = "accepted" if index % 2 else "rejected"
            with Session() as db:
                try:
                    update_status(rid, ReferralStatusUpdate(status=target), db=db, user=ADMIN)
                    return ("ok", target)
                except HTTPException as exc:
                    return (exc.status_code, target)

        results, errors = _race_on_pg(worker, RACERS)
        assert not errors, errors
        assert len(results) == RACERS, results
        winners = [target for code, target in results if code == "ok"]
        assert len(winners) == 1, results   # 修前：好几路 200，库里是最后提交的那个
        assert sorted(code for code, _ in results if code != "ok") == [409] * (RACERS - 1), results
        with Session() as db:
            assert db.get(Referral, rid).status == winners[0]
