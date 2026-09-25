"""用血申请审批 / 发血的真并发取证（P2-110，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_blood_status_transition_races.py -q

审批（pending → approved / rejected）与发血（approved → issued，扣库存）原先都是「内存里判状态 → 改」。库存一侧的
条件 UPDATE 只保证不扣成负数，保证不了「一张申请只扣一次」：PG 的 READ COMMITTED 下并发的几路都读到同一个旧状态、
都往下走——

- 同一张申请八路同时发血：八路都成功，库存扣八次（发出去一袋，账上扣了八袋）；
- 同一张申请四路审批、四路驳回同时到：八路都成功，最后是谁看提交先后。

SQLite 的库级写锁把窗口压平，开发库上同一份代码永远绿（SQLite 一侧用「拿着旧对象接着转」的确定时序钉逻辑，
见 test_blood_status_transition.py）。这里钉 PG 上真并发的不变量：**每一轮恰一路成功、其余 409，库存只按成功的
那一路扣**。

**这套库是多人共用的**：只用带随机后缀的自建数据，跑完自己收拾，从不 DROP SCHEMA；空库就地补跑迁移（只做加法）。
血液库存按（血型, 成分）全局一行，这里借用 AB 型血浆那一行，跑完把数量还原。
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

from app.models import BloodStock, Organization, Patient, TransfusionRequest, User
from app.routers.blood import issue_blood, review_transfusion

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"),
]

RACERS = 8
SERVER_DIR = Path(__file__).resolve().parents[1]
STOCK_KEY = {"blood_type": "AB", "component": "plasma"}


@pytest.fixture(scope="module")
def pg_engine():
    """连既有测试库；空库就地补跑迁移，任何情况下都不清 schema（理由同 test_insurance_apply_unique_races）。
    池子按参赛路数开并先热身，否则冷池下测出来的是排队而不是并发（见 test_cost_allocation_races）。"""
    engine = create_engine(PG_URL, pool_size=RACERS, max_overflow=RACERS)
    if not all(sa_inspect(engine).has_table(t) for t in ("blood_stocks", "transfusion_requests")):
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
    """一家机构、一名本机构血库经办（发血要过机构写权限）、一位患者；AB 型血浆库存补足 5000 毫升，跑完还原。"""
    Session = sessionmaker(bind=pg_engine)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG用血并发院{tag}", org_type="lead_hospital", level="county")
        patient = Patient(name=f"用血并发患者{tag}", id_card=f"3313{uuid.uuid4().int % 10**14:014d}",
                          gender="男", birth_date="1965-05-05", ehc_no=f"PG-BLOOD-{tag}")
        db.add_all([org, patient])
        db.flush()
        user = User(username=f"pg_blood_{tag}", password_hash="x", full_name=f"血库经办{tag}",
                    role="operator", org_id=org.id)
        db.add(user)
        stock = db.query(BloodStock).filter_by(**STOCK_KEY).first()
        original = None if stock is None else stock.quantity_ml
        if stock is None:
            db.add(BloodStock(**STOCK_KEY, quantity_ml=5000))
        else:
            stock.quantity_ml = 5000
        db.commit()
        ids = {"tag": tag, "org_id": org.id, "user_id": user.id, "patient_id": patient.id,
               "original_ml": original, "request_ids": []}

    yield ids

    with Session() as db:  # 共用库：自己造的行自己收拾（先子后父），借用的库存行还原
        db.query(TransfusionRequest).filter(TransfusionRequest.id.in_(ids["request_ids"])).delete(
            synchronize_session=False)
        stock = db.query(BloodStock).filter_by(**STOCK_KEY).one()
        if ids["original_ml"] is None:
            db.delete(stock)
        else:
            stock.quantity_ml = ids["original_ml"]
        db.query(User).filter_by(id=ids["user_id"]).delete()
        db.query(Patient).filter_by(id=ids["patient_id"]).delete()
        db.query(Organization).filter_by(id=ids["org_id"]).delete()
        db.commit()


def _new_request(db, world, status):
    request = TransfusionRequest(patient_id=world["patient_id"], org_id=world["org_id"], quantity_ml=200,
                                 requested_by=world["user_id"], status=status, **STOCK_KEY)
    db.add(request)
    db.commit()
    world["request_ids"].append(request.id)
    return request.id


def _stock_ml(db):
    return db.query(BloodStock).filter_by(**STOCK_KEY).one().quantity_ml


def _outcomes(results):
    return [r for r in results if r[0] == "ok"], [r for r in results if r[0] == "rejected"]


def test_同一申请八路并发发血_库存只扣一次(pg_engine, world):
    Session = sessionmaker(bind=pg_engine)
    with Session() as db:
        request_id = _new_request(db, world, "approved")
        before = _stock_ml(db)

    def worker(_i):
        with Session() as db:
            try:
                issue_blood(request_id, db=db, user=db.get(User, world["user_id"]))
            except HTTPException as exc:
                return ("rejected", exc.status_code, exc.detail)
            return ("ok",)

    _warm_pool(pg_engine, RACERS)
    results, errors = _race_on_pg(worker, times=RACERS)
    assert not errors, f"异常不该漏给调用方：{errors}"
    ok, rejected = _outcomes(results)
    assert len(ok) == 1, f"应恰一路发血成功，实际 {results}"   # 修前：八路都成功
    assert all(r[1] == 409 for r in rejected), rejected
    with Session() as db:
        assert db.get(TransfusionRequest, request_id).status == "issued"
        assert _stock_ml(db) == before - 200, "库存只该按成功的那一路扣一次"   # 修前：扣了 1600


def test_审批与驳回同时到_只成一路(pg_engine, world):
    Session = sessionmaker(bind=pg_engine)
    with Session() as db:
        request_id = _new_request(db, world, "pending")
        director = User(username=f"pg_blood_dir_{uuid.uuid4().hex[:8]}", password_hash="x",
                        full_name="用血审批", role="director", org_id=world["org_id"])
        db.add(director)
        db.commit()
        director_id = director.id

    def worker(i):
        with Session() as db:
            try:
                review_transfusion(request_id, approve=bool(i % 2), db=db, user=db.get(User, director_id))
            except HTTPException as exc:
                return ("rejected", exc.status_code, exc.detail)
            return ("ok", "approved" if i % 2 else "rejected")

    try:
        _warm_pool(pg_engine, RACERS)
        results, errors = _race_on_pg(worker, times=RACERS)
        assert not errors, f"异常不该漏给调用方：{errors}"
        ok, rejected = _outcomes(results)
        assert len(ok) == 1, f"审批与驳回应只成一路，实际 {results}"   # 修前：八路都成功
        assert all(r[1] == 409 for r in rejected), rejected
        with Session() as db:
            assert db.get(TransfusionRequest, request_id).status == ok[0][1], "库里的结论要与成功那一路的回执一致"
    finally:
        with Session() as db:
            db.query(TransfusionRequest).filter_by(id=request_id).update({"approved_by": None})
            db.query(User).filter_by(id=director_id).delete()
            db.commit()
