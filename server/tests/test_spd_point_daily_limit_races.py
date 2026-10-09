"""积分每日上限的真并发取证（P2-1581，第四十六批扫描 AJ4-5，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_spd_point_daily_limit_races.py -q

`service.award_points` 原先先 `SELECT SUM` 判当天该规则已入账分值到没到上限，到 `add_amount` 才第一次锁账户行。PG 的
READ COMMITTED 下同一村医几笔入账同时到，各路的求和都看不到别路未提交的流水、都判「没到上限」，后到的在 `add_amount` 上
排队、前一路提交后照样入账——上限形同虚设。修法：判上限之前 `SELECT … FOR UPDATE` 锁住账户行，同一账户的判定与入账排队，
后到的一路锁到手时前一路已提交，求和读得到它。

不变量：**上限设为单次分的 3 倍，八路并发只入账 3 笔；流水合计 = 账户累计获得 = 上限**。开发库 SQLite 的库级写锁把窗口
一并锁掉，这一档只有真 PG 打得开（SQLite 档见 test_spd_point_daily_limit_lock.py）。

**这套库是多人共用的**：只用带随机后缀的自建数据（规则的事件名也带后缀，不与种子规则抢），跑完自己收拾，从不 DROP SCHEMA；
空库就地补跑迁移（只做加法）。
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

from test_disease_enrollment_unique_races import _warm_pool  # 连接池热身，理由见 test_cost_allocation_races
from test_postgres_real import _race_on_pg  # Barrier 真并发的既有夹具，不另造一份

from app.models import Organization, User
from app.spd.models import SpdPointAccount, SpdPointRecord, SpdPointRule
from app.spd.service import award_points

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"),
]

RACERS = 8
POINTS = 3
LIMIT_TIMES = 3
SERVER_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pg_engine():
    """连既有测试库；空库就地补跑迁移，任何情况下都不清 schema（理由同 test_insurance_apply_unique_races）。
    池子按参赛路数开并先热身，否则冷池下测出来的是排队而不是并发（见 test_cost_allocation_races）。"""
    engine = create_engine(PG_URL, pool_size=RACERS, max_overflow=RACERS)
    if not all(sa_inspect(engine).has_table(t) for t in ("spd_point_accounts", "spd_point_records", "spd_point_rules")):
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
    """村卫生室、一位村医（账户已建、余额 0）、一条只此一家的入账规则：单次 3 分、每日上限 9 分（名字全带随机后缀）。"""
    Session = sessionmaker(bind=pg_engine)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG积分上限村卫生室{tag}", org_type="village", level="village")
        rule = SpdPointRule(code=f"p21581_{tag}", name=f"并发入账-{tag}", event=f"p21581_{tag}", points=POINTS,
                            daily_limit=POINTS * LIMIT_TIMES, active=True)
        db.add_all([org, rule])
        db.flush()
        user = User(username=f"pg_points_{tag}", password_hash="x", full_name=f"积分村医{tag}",
                    role="doctor", org_id=org.id)
        db.add(user)
        db.flush()
        account = SpdPointAccount(user_id=user.id, org_id=org.id, balance=0, earned=0, used=0)
        db.add(account)
        db.commit()
        ids = {"org_id": org.id, "rule_id": rule.id, "user_id": user.id, "account_id": account.id,
               "event": rule.event}

    yield ids

    with Session() as db:  # 共用库：自己造的行自己收拾（先子后父）
        db.query(SpdPointRecord).filter_by(account_id=ids["account_id"]).delete()
        db.query(SpdPointAccount).filter_by(id=ids["account_id"]).delete()
        db.query(SpdPointRule).filter_by(id=ids["rule_id"]).delete()
        db.query(User).filter_by(id=ids["user_id"]).delete()
        db.query(Organization).filter_by(id=ids["org_id"]).delete()
        db.commit()


def test_八路并发入账_上限是单次分的3倍_只入账3笔(pg_engine, world):
    Session = sessionmaker(bind=pg_engine)

    def worker(index):
        with Session() as db:
            record = award_points(db, world["user_id"], world["event"], ref_type="p21581", ref_id=index,
                                  org_id=world["org_id"])
            db.commit()
            return "credited" if record is not None else "capped"

    _warm_pool(pg_engine, RACERS)
    results, errors = _race_on_pg(worker, times=RACERS)
    assert not errors, f"异常不该漏给调用方：{errors}"
    assert sorted(results) == ["capped"] * (RACERS - LIMIT_TIMES) + ["credited"] * LIMIT_TIMES, results   # 修前多入
    with Session() as db:
        logged = [r.points for r in db.query(SpdPointRecord).filter_by(account_id=world["account_id"])]
        account = db.get(SpdPointAccount, world["account_id"])
        assert sum(logged) == account.earned == account.balance == POINTS * LIMIT_TIMES, (logged, account.earned)
