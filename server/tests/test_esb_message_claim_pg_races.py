"""集成平台同一条消息多路同时消费的真并发取证（P2-405，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_esb_message_claim_pg_races.py -q

原先三条消费路径都是锁外读状态、再无条件写「处理中」（UPDATE 只有 `WHERE id = ?`）：PG 的 READ COMMITTED 下并发的
几路都读到「待处理」，第一路的写拿着行锁投递，后面几路等它提交后照样把状态改回「处理中」、各投一次——八路手工消费
同一条出站消息，对端收到八次。修后「转处理中」与判定压进同一条 UPDATE，后到的一路等到行锁后按新状态重判、改到 0 行
即抢输（409）。SQLite 一侧用确定时序钉逻辑（`test_esb_message_claim.py`）。

投递与处理都放慢一点：修前的缺陷要「几路都在第一路提交之前读完」才显形，处理太快时后几路读到的已是「成功」、被预检
拦下，测出来的是排队而不是并发。

**这套库是多人共用的**：只用带随机后缀的自建数据，跑完自己收拾，从不 DROP SCHEMA；空库就地补跑迁移（只做加法）。
不调定时出站（`consume_pending_outbound` 会挑库里所有出站端点的待投消息，别人的也在内）。
"""
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import sessionmaker

from test_disease_enrollment_unique_races import _warm_pool  # 连接池热身，理由见 test_cost_allocation_races
from test_esb_outbound import FakeHttpx
from test_postgres_real import _race_on_pg  # Barrier 真并发的既有夹具，不另造一份

from app.models import EsbEndpoint, EsbFlow, EsbFlowRun, EsbMessage, ExchangeLog, Organization, User
from app.routers import esb as esb_module

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"),
]

RACERS = 8
SLOW = 0.3
SERVER_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pg_engine():
    """连既有测试库；空库就地补跑迁移，任何情况下都不清 schema（理由同 test_insurance_apply_unique_races）。
    池子按参赛路数开并先热身，否则冷池下测出来的是排队而不是并发（见 test_cost_allocation_races）。"""
    engine = create_engine(PG_URL, pool_size=RACERS, max_overflow=RACERS)
    if not all(sa_inspect(engine).has_table(t) for t in ("esb_messages", "esb_flows")):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_DIR, env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"迁移在 PG 上失败：\n{result.stderr[-2000:]}"
    _warm_pool(engine, RACERS)
    yield engine
    engine.dispose()


def _sessions(pg_engine):
    """与 `app.database.SessionLocal` 同一个配置（autoflush 关）。"""
    return sessionmaker(bind=pg_engine, autoflush=False)


class SlowHttpx(FakeHttpx):
    """投递慢一点的替身：让几路都在第一路提交之前读完（见模块说明）。"""

    def post(self, url, content=b"", headers=None, timeout=None):
        time.sleep(SLOW)
        return super().post(url, content=content, headers=headers, timeout=timeout)


@pytest.fixture(scope="module")
def world(pg_engine):
    """一家机构与一位经办、出站 / 入站各一个接入方、一个只做校验的编排；跑完把自己造的行全收拾掉。"""
    Session = _sessions(pg_engine)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG集成院{tag}", org_type="lead_hospital", level="county")
        outbound = EsbEndpoint(code=f"PG_OUT_{tag}", name="并发出站", system_type="provincial", direction="outbound",
                               endpoint_url=f"https://province.example/pg-{tag}")
        inbound = EsbEndpoint(code=f"PG_IN_{tag}", name="并发入站", system_type="his", direction="inbound")
        flow = EsbFlow(code=f"PG_FLOW_{tag}", name="并发校验编排",
                       steps=[{"type": "validate", "config": {"required": ["k"]}}])
        db.add_all([org, outbound, inbound, flow])
        db.flush()
        operator = User(username=f"pg_esb_op_{tag}", password_hash="x", full_name="集成经办", role="operator",
                        org_id=org.id)
        db.add(operator)
        db.commit()
        ids = {"org": org.id, "operator": operator.id, "outbound": outbound.id, "inbound": inbound.id,
               "flow": flow.id, "flow_code": flow.code, "url": outbound.endpoint_url,
               "codes": [outbound.code, inbound.code]}

    yield ids

    with Session() as db:  # 共用库：自己造的行自己收拾（先子后父）
        db.query(EsbFlowRun).filter_by(flow_id=ids["flow"]).delete()
        db.query(EsbMessage).filter(EsbMessage.endpoint_id.in_([ids["outbound"], ids["inbound"]])).delete(
            synchronize_session=False)
        db.query(ExchangeLog).filter(ExchangeLog.source_system.in_(ids["codes"])).delete(synchronize_session=False)
        db.query(EsbFlow).filter_by(id=ids["flow"]).delete()
        db.query(EsbEndpoint).filter(EsbEndpoint.id.in_([ids["outbound"], ids["inbound"]])).delete(
            synchronize_session=False)
        db.query(User).filter_by(id=ids["operator"]).delete()
        db.query(Organization).filter_by(id=ids["org"]).delete()
        db.commit()


def _message(Session, endpoint_id, payload):
    with Session() as db:
        row = EsbMessage(endpoint_id=endpoint_id, msg_type="notice", payload=payload)
        db.add(row)
        db.commit()
        return row.id


def _race(Session, world, act):
    """八路同时消费同一条消息；每路回 ("ok", 序号) 或 (状态码, 说明)。"""
    def worker(index):
        with Session() as db:
            try:
                act(db, db.get(User, world["operator"]), index)
                return ("ok", index)
            except HTTPException as exc:
                return (exc.status_code, exc.detail)

    results, errors = _race_on_pg(worker, RACERS)
    assert not errors, errors
    assert len(results) == RACERS, results
    winners = [r[1] for r in results if r[0] == "ok"]
    assert len(winners) == 1, results   # 修前：几路都读到「待处理」、都往下走
    assert all(code == 409 for code, _ in results if code != "ok"), results
    return winners[0]


def test_八路手工消费同一条出站消息_对端只收到一次(pg_engine, world, monkeypatch):
    fake = SlowHttpx()
    monkeypatch.setattr(esb_module, "httpx", fake)
    Session = _sessions(pg_engine)
    mid = _message(Session, world["outbound"], {"text": "只该投一次"})
    _race(Session, world, lambda db, user, i: esb_module.process_message(mid, db=db, user=user))
    assert len([c for c in fake.calls if c["url"] == world["url"]]) == 1   # 修前：八路各投一次
    with Session() as db:
        row = db.get(EsbMessage, mid)
        assert (row.status, row.retry_count) == ("succeeded", 0)


def test_手工消费与编排执行撞在一起_这条消息只处理一次(pg_engine, world, monkeypatch):
    real_process, real_step = esb_module._process_message, esb_module._run_step

    def slow_process(*args, **kwargs):
        time.sleep(SLOW)
        return real_process(*args, **kwargs)

    def slow_step(*args, **kwargs):
        time.sleep(SLOW)
        return real_step(*args, **kwargs)

    monkeypatch.setattr(esb_module, "_process_message", slow_process)
    monkeypatch.setattr(esb_module, "_run_step", slow_step)
    Session = _sessions(pg_engine)
    mid = _message(Session, world["inbound"], {"k": "v"})

    def act(db, user, i):
        if i % 2:
            esb_module.run_flow(world["flow_code"], mid, db=db)
        else:
            esb_module.process_message(mid, db=db, user=user)

    winner = _race(Session, world, act)
    with Session() as db:
        runs = db.query(EsbFlowRun).filter_by(message_id=mid).count()
        row = db.get(EsbMessage, mid)
    assert runs == (1 if winner % 2 else 0)   # 修前：几路编排各记一条执行记录
    assert (row.status, row.retry_count) == ("succeeded", 0)
