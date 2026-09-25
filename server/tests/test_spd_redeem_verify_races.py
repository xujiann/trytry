"""积分兑换线下核销的真并发取证（P2-115，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_spd_redeem_verify_races.py -q

核销原先「按码查一张待核销的 → 改 verified → commit」，判定在查询里、写入是只有 `WHERE id = ?` 的 UPDATE。
PG 的 READ COMMITTED 下同一个码在两个点位同时出示，两路都查到这张待核销单、都 200——奖品发了两份，
兑换时只扣了一件库存、一份积分。修法：核销是一条 `WHERE id = :id AND status = 'pending'` 的条件 UPDATE，
抢输的一路与顺序重复核销同一句 404「核销码无效或已核销」。

SQLite 上这一处摆不出确定时序：输家的查询带着 `status = 'pending'`，赢家一提交它就查不到了（修前修后一样）；
窗口只在两路都查完、都还没写的那一刻，只有真并发打得开。

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

from app.models import Organization, User
from app.spd.models import SpdGoods, SpdPointAccount, SpdRedeem
from app.spd.routers.assess import VerifyIn, verify_redeem

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
    池子按参赛路数开并先热身，否则冷池下测出来的是排队而不是并发（见 test_cost_allocation_races）。"""
    engine = create_engine(PG_URL, pool_size=RACERS, max_overflow=RACERS)
    if not sa_inspect(engine).has_table("spd_redeems"):
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
    """一家机构、一名兑换的村医、一名核销的经办、一件奖品、一张待核销的兑换单（码带随机后缀，免得撞上别人的待核销单）。"""
    Session = sessionmaker(bind=pg_engine, autoflush=False)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG核销并发院{tag}", org_type="township", level="township")
        db.add(org)
        db.flush()
        doctor = User(username=f"pg_rdm_dr_{tag}", password_hash="x", full_name="兑换村医", role="doctor", org_id=org.id)
        operator = User(username=f"pg_rdm_op_{tag}", password_hash="x", full_name="核销经办", role="operator",
                        org_id=org.id)
        goods = SpdGoods(code=f"pg_rdm_{tag}", name=f"核销并发毛巾{tag}", points=1, stock=0)
        db.add_all([doctor, operator, goods])
        db.flush()
        account = SpdPointAccount(user_id=doctor.id, org_id=org.id, balance=0, earned=1, used=1)
        db.add(account)
        db.flush()
        redeem = SpdRedeem(account_id=account.id, goods_id=goods.id, points=1, verify_code=f"R{tag[:7]}",
                           status="pending")
        db.add(redeem)
        db.commit()
        ids = {"org_id": org.id, "user_ids": [doctor.id, operator.id], "operator_id": operator.id,
               "goods_id": goods.id, "account_id": account.id, "redeem_id": redeem.id, "code": redeem.verify_code}

    yield ids

    with Session() as db:  # 共用库：自己造的行自己收拾（先子后父）
        db.query(SpdRedeem).filter_by(id=ids["redeem_id"]).delete()
        db.query(SpdPointAccount).filter_by(id=ids["account_id"]).delete()
        db.query(SpdGoods).filter_by(id=ids["goods_id"]).delete()
        db.query(User).filter(User.id.in_(ids["user_ids"])).delete(synchronize_session=False)
        db.query(Organization).filter_by(id=ids["org_id"]).delete()
        db.commit()


def test_同一核销码八个点位同时出示_只核销一次(pg_engine, world):
    Session = sessionmaker(bind=pg_engine, autoflush=False)   # 与 app.database.SessionLocal 同配置

    def worker(_i):
        with Session() as db:
            try:
                verify_redeem(VerifyIn(verify_code=world["code"]), db=db, user=db.get(User, world["operator_id"]))
            except HTTPException as exc:
                return ("rejected", exc.status_code, exc.detail)
            return ("ok",)

    _warm_pool(pg_engine, RACERS)
    results, errors = _race_on_pg(worker, times=RACERS)
    assert not errors, f"异常不该漏给调用方：{errors}"
    ok = [r for r in results if r[0] == "ok"]
    rejected = [r for r in results if r[0] == "rejected"]
    assert len(ok) == 1, f"一张兑换单只能核销一次（奖品只发一份），实际 {results}"   # 修前：几路都 200
    assert all(r[1:] == (404, "核销码无效或已核销") for r in rejected), rejected
    with Session() as db:
        assert db.get(SpdRedeem, world["redeem_id"]).status == "verified"
