"""物资采购审批、签合同、特病申报审核的真并发取证（P2-403，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_approval_transition_pg_races.py -q

三处原先都是「内存里判状态 → 赋值 → commit」，UPDATE 只有 `WHERE id = ?`：PG 的 READ COMMITTED 下并发的几路都读到同一个
待审状态、都往下走——一个批准、一个驳回同时到，两路都 200，库里留下后提交的那个结论；两路同时签合同，后签的供应商、
合同号、金额把先签的整份盖掉。修后走 `concurrency.move_row`（「状态还是判过的那个」与改值同一条 UPDATE），行锁让后到的
一路等前一路提交、再按新状态重判。SQLite 一侧用确定时序钉逻辑（`test_approval_transition_races.py`）。

这里钉 PG 上真并发的不变量：**每一轮恰一路成功、其余 409，库里留下的就是成功那一路写的**。

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

from app import visibility
from app.models import AccessLog, MaterialPurchase, Organization, Patient, SpecialDiseaseApp, Supplier, User
from app.routers.insurance import review_special_disease
from app.routers.materials import ApproveIn, ContractIn, approve_purchase, sign_contract

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
    if not all(sa_inspect(engine).has_table(t) for t in ("material_purchases", "special_disease_apps")):
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


@pytest.fixture(autouse=True)
def _access_log_on_pg(pg_engine, monkeypatch):
    """特病审核先过 `assert_patient_visible`，调阅留痕由 `visibility.SessionLocal` 另开会话落库——生产上那是同一个库；
    不改道的话留痕落进本进程的 SQLite 测试库、外键对不上，每路报一条「留痕丢失」。"""
    monkeypatch.setattr(visibility, "SessionLocal", _sessions(pg_engine))


@pytest.fixture(scope="module")
def world(pg_engine):
    """一家机构、申请人（经办）与审批人（管理层，全域角色）、两家供应商、一位患者；跑完把自己造的行全收拾掉。"""
    Session = _sessions(pg_engine)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG审批院{tag}", org_type="lead_hospital", level="county")
        patient = Patient(name=f"审批并发患者{tag}", id_card=f"3321{uuid.uuid4().int % 10**14:014d}",
                          ehc_no=f"PG-AP-{tag}")
        suppliers = [Supplier(name=f"PG审批供应商{n}{tag}") for n in ("甲", "乙")]
        db.add_all([org, patient, *suppliers])
        db.flush()
        operator = User(username=f"pg_ap_op_{tag}", password_hash="x", full_name="申请人", role="operator",
                        org_id=org.id)
        director = User(username=f"pg_ap_dir_{tag}", password_hash="x", full_name="审批人", role="director",
                        org_id=org.id)
        db.add_all([operator, director])
        db.commit()
        ids = {"org": org.id, "patient": patient.id, "suppliers": [s.id for s in suppliers],
               "operator": operator.id, "director": director.id}

    yield ids

    with Session() as db:  # 共用库：自己造的行自己收拾（先子后父）
        db.query(AccessLog).filter(AccessLog.patient_id == ids["patient"]).delete(synchronize_session=False)
        db.query(SpecialDiseaseApp).filter_by(patient_id=ids["patient"]).delete()
        db.query(MaterialPurchase).filter_by(org_id=ids["org"]).delete()
        db.query(User).filter(User.id.in_([ids["operator"], ids["director"]])).delete(synchronize_session=False)
        db.query(Supplier).filter(Supplier.id.in_(ids["suppliers"])).delete(synchronize_session=False)
        db.query(Patient).filter_by(id=ids["patient"]).delete()
        db.query(Organization).filter_by(id=ids["org"]).delete()
        db.commit()


def _purchase(Session, world, status="requested"):
    with Session() as db:
        row = MaterialPurchase(org_id=world["org"], item_name="并发审批输液泵", quantity=2, estimated_price=100,
                               requested_by=world["operator"], status=status)
        db.add(row)
        db.commit()
        return row.id


def _race(Session, world, act, as_user="director"):
    """八路同时对同一行走一步；每路回 ("ok", 序号) 或 (状态码, 说明)。"""
    def worker(index):
        with Session() as db:
            try:
                act(db, db.get(User, world[as_user]), index)
                return ("ok", index)
            except HTTPException as exc:
                return (exc.status_code, exc.detail)

    results, errors = _race_on_pg(worker, RACERS)
    assert not errors, errors
    assert len(results) == RACERS, results
    winners = [r[1] for r in results if r[0] == "ok"]
    assert len(winners) == 1, results   # 修前：几路都读到待审、都 200
    assert all(code == 409 for code, _ in results if code != "ok"), results
    return winners[0]


def test_八路采购审批批准与驳回交错_恰一路成功_结论跟着它走(pg_engine, world):
    Session = _sessions(pg_engine)
    pid = _purchase(Session, world)
    winner = _race(Session, world, lambda db, user, i: approve_purchase(
        pid, ApproveIn(approved=bool(i % 2)), db=db, user=user))
    with Session() as db:
        row = db.get(MaterialPurchase, pid)
        assert row.status == ("approved" if winner % 2 else "cancelled")   # 修前：留下最后提交的那个结论


def test_八路同时签合同_恰一路成功_合同就是它签的(pg_engine, world):
    Session = _sessions(pg_engine)
    pid = _purchase(Session, world, status="approved")
    winner = _race(Session, world, lambda db, user, i: sign_contract(
        pid, ContractIn(supplier_id=world["suppliers"][i % 2], contract_no=f"HT-PG-{i}", contract_amount=100 + i),
        db=db, user=user), as_user="operator")
    with Session() as db:
        row = db.get(MaterialPurchase, pid)
        assert (row.status, row.contract_no, row.supplier_id, row.contract_amount) == (
            "contracted", f"HT-PG-{winner}", world["suppliers"][winner % 2], 100 + winner)   # 修前：后签的整份盖掉先签的


def test_八路特病审核批准与驳回交错_恰一路成功_结论跟着它走(pg_engine, world):
    Session = _sessions(pg_engine)
    with Session() as db:
        app_ = SpecialDiseaseApp(patient_id=world["patient"], disease_name="并发审核尿毒症透析",
                                 created_by=world["operator"])
        db.add(app_)
        db.commit()
        app_id = app_.id
    winner = _race(Session, world, lambda db, user, i: review_special_disease(app_id, bool(i % 2), db=db, user=user))
    with Session() as db:
        row = db.get(SpecialDiseaseApp, app_id)
        assert (row.status, row.reviewed_by) == ("approved" if winner % 2 else "rejected", world["director"])
