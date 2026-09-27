"""知情同意书签署与拒签的真并发取证（P2-449，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_outpatient_consent_conclude_pg_races.py -q

原先判完「待签署」、赋值、commit，UPDATE 只有 `WHERE id = ?`：PG 的 READ COMMITTED 下并发的几路都读到「待签署」、都往下
写——签署与拒签交错，两样都 200，告知书上的结论与签署人是最后提交的那一路。修后走 `concurrency.move_row`
（`WHERE status = 'pending'`），行锁让后到的一路等前一路提交、再按新状态重判。SQLite 一侧用确定时序钉逻辑
（`test_outpatient_consent_conclude_races.py`）。

不变式：**一份告知书只有一个结论**——恰一路 200、其余 409，库里的结论与签署人就是成的那一路。

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

from app.models import InformedConsent, Organization, Patient, User
from app.routers.outpatient_docs import RefuseIn, SignIn, refuse_consent, sign_consent

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"),
]

RACERS = 8
ROUNDS = 3
SERVER_DIR = Path(__file__).resolve().parents[1]
#: 全域角色过机构归属校验（`assert_org_writable` 只读 role / org_id）；直调路由函数，不经依赖注入
ADMIN = SimpleNamespace(role="admin", org_id=None)


@pytest.fixture(scope="module")
def pg_engine():
    """连既有测试库；空库就地补跑迁移，任何情况下都不清 schema（理由同 test_insurance_apply_unique_races）。
    池子按参赛路数开并先热身，否则冷池下测出来的是排队而不是并发（见 test_cost_allocation_races）。"""
    engine = create_engine(PG_URL, pool_size=RACERS, max_overflow=RACERS)
    if not sa_inspect(engine).has_table("informed_consents"):
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
    """一家门诊部、开告知书的医生（`created_by` 必填）与一位患者；跑完把自己造的行全收拾掉（先子后父）。"""
    Session = sessionmaker(bind=pg_engine, autoflush=False)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG门诊部{tag}", org_type="township", level="township")
        patient = Patient(name=f"签署并发患者{tag}", id_card=f"3324{uuid.uuid4().int % 10**14:014d}",
                          ehc_no=f"PG-IC-{tag}")
        db.add_all([org, patient])
        db.flush()
        doctor = User(username=f"pg_ic_doc_{tag}", password_hash="x", full_name="告知医生", role="doctor",
                      org_id=org.id)
        db.add(doctor)
        db.commit()
        ids = {"org": org.id, "patient": patient.id, "doctor": doctor.id}

    yield ids

    with Session() as db:
        db.query(InformedConsent).filter_by(patient_id=ids["patient"]).delete()
        db.query(User).filter_by(id=ids["doctor"]).delete()
        db.query(Patient).filter_by(id=ids["patient"]).delete()
        db.query(Organization).filter_by(id=ids["org"]).delete()
        db.commit()


def test_签署与拒签同时点_只留一个结论(pg_engine, world):
    Session = sessionmaker(bind=pg_engine, autoflush=False)
    for _ in range(ROUNDS):
        with Session() as db:
            consent = InformedConsent(patient_id=world["patient"], org_id=world["org"], consent_type="surgery",
                                      title="PG 手术知情同意书", content="正文", created_by=world["doctor"])
            db.add(consent)
            db.commit()
            cid = consent.id

        def worker(index, cid=cid):
            signer = f"签署人{index}"
            with Session() as db:
                try:
                    if index % 2:
                        sign_consent(cid, SignIn(signer_name=signer), db=db, user=ADMIN)
                    else:
                        refuse_consent(cid, RefuseIn(signer_name=signer, refuse_reason="不同意"), db=db, user=ADMIN)
                    return ("ok", signer)
                except HTTPException as exc:
                    return (exc.status_code, signer)

        results, errors = _race_on_pg(worker, RACERS)
        assert not errors, errors
        assert len(results) == RACERS, results
        winners = [signer for code, signer in results if code == "ok"]
        assert len(winners) == 1, results   # 修前：好几路 200，结论与签署人是最后提交的那一路
        assert sorted(code for code, _ in results if code != "ok") == [409] * (RACERS - 1), results
        with Session() as db:
            row = db.get(InformedConsent, cid)
            index = int(winners[0].removeprefix("签署人"))
            assert (row.status, row.signer_name) == ("signed" if index % 2 else "refused", winners[0])
