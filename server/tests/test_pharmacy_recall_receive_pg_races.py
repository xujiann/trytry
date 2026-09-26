"""入库与召回的真 PG 取证（P2-350，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_pharmacy_recall_receive_pg_races.py -q

「召回后不得再入库」（`recall_batch`、P1-147）原先是锁外判的：按批次入库先判 `batch.status != "normal"`，再 `add_amount`
无条件累加。读到「正常」之后召回提交，这一笔照样加进已召回的批次、汇总跟着涨——一片也发不出（发药只取正常批次），缺药
预警与采购建议却当有货。修后累加与「还没召回」同一条 UPDATE（`_add_to_normal_batch`），加不上即 409。SQLite 一侧用确定
时序钉逻辑（`test_pharmacy_recall_receive_race.py`）。

这里两条：

- **确定时序**（修前红）：入库那一路读到「正常」之后、写入之前，另一个连接把批次召回并提交（入库那一路此时只读过、没持
  行锁，不会互等），再放它往下写——正是缺陷的窗口，在 PG 的 READ COMMITTED 下按库里的新状态重判；
- **真并发**（七路入库与一路召回同时到）：钉修后的不变量——召回的批次上不留可发余量、汇总不当有货、入成的每一笔都在、
  拒掉的一笔没进，且不互等、不 500。**这一条修前也常绿**，别拿它当回归：入库先对批次的唯一键做
  `INSERT … ON CONFLICT DO NOTHING`，撞上召回那一路改了还没提交的行会等它提交，读批次就落在召回之后了；缺陷只剩「插完
  这一下之后、读批次之前召回开始改行」这一个来回宽的窗口，要靠上面那条确定时序钉住。

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

from app.models import (DispenseItem, DispenseRecord, DrugBatch, DrugStock, Organization, Patient, Prescription,
                        PrescriptionItem, User)
from app.routers import dispense, pharmacy
from app.routers.dispense import DispenseCreate, ReverseIn, dispense_prescription, reverse_dispense
from app.routers.pharmacy import BatchRecallIn, BatchReceiveIn, recall_batch, receive_batch

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"),
]

RACERS = 8
LOT = 10   # 每一笔入库的量
SERVER_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pg_engine():
    """连既有测试库；空库就地补跑迁移，任何情况下都不清 schema（理由同 test_insurance_apply_unique_races）。
    池子按参赛路数开并先热身，否则冷池下测出来的是排队而不是并发（见 test_cost_allocation_races）。"""
    engine = create_engine(PG_URL, pool_size=RACERS, max_overflow=RACERS)
    if not all(sa_inspect(engine).has_table(t) for t in ("drug_batches", "drug_stocks")):
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


@pytest.fixture(scope="module")
def world(pg_engine):
    """一家机构、一个全域管理员（过机构写权限）；跑完把这家机构名下的批次与汇总连同机构、账号一并收拾。"""
    Session = _sessions(pg_engine)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG召回入库院{tag}", org_type="township", level="township")
        db.add(org)
        db.flush()
        admin = User(username=f"pg_rc_admin_{tag}", password_hash="x", full_name="全域管理员", role="admin")
        db.add(admin)
        db.commit()
        ids = {"tag": tag, "org": org.id, "admin": admin.id}

    yield ids

    with Session() as db:  # 共用库：自己造的行自己收拾（先子后父）
        records = [r.id for r in db.query(DispenseRecord).filter_by(org_id=ids["org"])]
        db.query(DispenseItem).filter(DispenseItem.dispense_id.in_(records or [0])).delete(synchronize_session=False)
        db.query(DispenseRecord).filter_by(org_id=ids["org"]).delete()
        rxs = [r.id for r in db.query(Prescription).filter_by(org_id=ids["org"])]
        db.query(PrescriptionItem).filter(PrescriptionItem.prescription_id.in_(rxs or [0])).delete(synchronize_session=False)
        db.query(Prescription).filter_by(org_id=ids["org"]).delete()
        db.query(Patient).filter(Patient.ehc_no.like(f"PG-RC-{ids['tag']}%")).delete(synchronize_session=False)
        db.query(DrugBatch).filter_by(org_id=ids["org"]).delete()
        db.query(DrugStock).filter_by(org_id=ids["org"]).delete()
        db.query(User).filter_by(id=ids["admin"]).delete()
        db.query(Organization).filter_by(id=ids["org"]).delete()
        db.commit()


def _receipt(world, drug):
    return BatchReceiveIn(org_id=world["org"], drug_code=drug, drug_name=f"{drug} 片", batch_no="LOT-1",
                          expire_date="2027-12-31", quantity=LOT)


def _first_lot(Session, world, drug):
    with Session() as db:
        return receive_batch(_receipt(world, drug), db=db, user=db.get(User, world["admin"]))["id"]


def _batch_and_stock(Session, world, drug, batch_id):
    with Session() as db:
        batch = db.get(DrugBatch, batch_id)
        stock = db.query(DrugStock).filter_by(org_id=world["org"], drug_code=drug).one()
        return batch.status, batch.quantity - batch.used_quantity - batch.blocked_quantity, stock.quantity


def test_入库读到正常之后批次被召回_这一笔409_不加进已召回的批次(pg_engine, world, monkeypatch):
    Session = _sessions(pg_engine)
    drug = f"PGRC{uuid.uuid4().hex[:6]}"
    batch_id = _first_lot(Session, world, drug)
    real, fired = pharmacy.ensure_present, []

    def recalled_meanwhile(obj, *args, **kwargs):
        result = real(obj, *args, **kwargs)
        if isinstance(result, DrugBatch) and result.id == batch_id and not fired:
            fired.append(True)   # 入库那一路已读到「正常」、还没写：另一个连接当场召回并提交
            with Session() as other:
                recall_batch(batch_id, BatchRecallIn(reason="并发取证"), db=other, user=other.get(User, world["admin"]))
        return result

    monkeypatch.setattr(pharmacy, "ensure_present", recalled_meanwhile)
    with Session() as db, pytest.raises(HTTPException) as refused:
        receive_batch(_receipt(world, drug), db=db, user=db.get(User, world["admin"]))
    monkeypatch.undo()
    assert fired
    assert (refused.value.status_code, refused.value.detail) == (409, "该批次已召回，不得再入库")   # 修前 201
    # 修前：这一笔加进已召回的批次（发不出却算可发余量），汇总跟着涨
    assert _batch_and_stock(Session, world, drug, batch_id) == ("recalled", 0, 0)


def test_七路入库与一路召回并发_召回的批次上不留可发余量_汇总不当有货(pg_engine, world):
    Session = _sessions(pg_engine)
    drug = f"PGRC{uuid.uuid4().hex[:6]}"
    batch_id = _first_lot(Session, world, drug)

    def worker(index):
        with Session() as db:
            user = db.get(User, world["admin"])
            try:
                if index == 0:
                    recall_batch(batch_id, BatchRecallIn(reason="并发取证"), db=db, user=user)
                    return (index, "recalled")
                receive_batch(_receipt(world, drug), db=db, user=user)
                return (index, "received")
            except HTTPException as exc:
                return (index, exc.status_code, exc.detail)

    results, errors = _race_on_pg(worker, RACERS)
    assert not errors, errors
    assert (0, "recalled") in results, results
    received = [r for r in results if r[1] == "received"]
    refused = [r for r in results if r[1] != "received" and r[0] != 0]
    assert len(received) + len(refused) == RACERS - 1, results
    assert all(r[1:] == (409, "该批次已召回，不得再入库") for r in refused), results
    with Session() as db:
        batch = db.get(DrugBatch, batch_id)
        stock = db.query(DrugStock).filter_by(org_id=world["org"], drug_code=drug).one()
        assert batch.status == "recalled"
        assert batch.quantity == LOT * (1 + len(received)), results   # 入成的每一笔都在，拒掉的一笔没进
        # 修前：排在召回后面的那几笔照样加进已召回的批次——发不出，却算可发余量
        assert batch.quantity - batch.used_quantity - batch.blocked_quantity == 0, (results, batch.quantity,
                                                                                    batch.blocked_quantity)
        assert stock.quantity == 0, results   # 修前：汇总跟着涨，缺药预警与采购建议当有货


def test_冲销读到正常之后批次被召回_退回的量不回可用汇总(pg_engine, world, monkeypatch):
    """P2-402：退药冲销取批次是锁外读的；读到「正常」之后另一个连接召回并提交（召回已把余量整笔转进不可发），修前这一笔
    再按「正常」加回可用汇总，召回的批次上又长出可发余量。修后回补批次已用拿到行锁之后重读批次、按召回之后的状态定去向。"""
    Session = _sessions(pg_engine)
    drug = f"PGRV{uuid.uuid4().hex[:6]}"
    batch_id = _first_lot(Session, world, drug)   # 10 片
    with Session() as db:
        patient = Patient(name="冲销并发患者", id_card=f"3320{uuid.uuid4().int % 10**14:014d}",
                          ehc_no=f"PG-RC-{world['tag']}-{drug}")
        db.add(patient)
        db.flush()
        rx = Prescription(patient_id=patient.id, org_id=world["org"], diagnosis_name="上呼吸道感染",
                          status="auto_passed", created_by=world["admin"])
        db.add(rx)
        db.flush()
        db.add(PrescriptionItem(prescription_id=rx.id, drug_code=drug, drug_name=f"{drug} 片", daily_dose=6, days=1))
        db.commit()
        dispense_id = dispense_prescription(DispenseCreate(prescription_id=rx.id), db=db,
                                            user=db.get(User, world["admin"]))["id"]
    real, fired = dispense.ensure_present, []

    def recalled_meanwhile(obj, *args, **kwargs):
        result = real(obj, *args, **kwargs)
        if isinstance(result, DrugBatch) and result.id == batch_id and not fired:
            fired.append(True)   # 冲销那一路已读到「正常」、还没回补：另一个连接当场召回并提交
            with Session() as other:
                recall_batch(batch_id, BatchRecallIn(reason="并发取证"), db=other, user=other.get(User, world["admin"]))
        return result

    monkeypatch.setattr(dispense, "ensure_present", recalled_meanwhile)
    with Session() as db:
        reverse_dispense(dispense_id, ReverseIn(reason="退药"), db=db, user=db.get(User, world["admin"]))
    monkeypatch.undo()
    assert fired
    # 修前 ("recalled", 6, 6)：退回的 6 片进了可用汇总，召回的批次上又有 6 片「可发」
    assert _batch_and_stock(Session, world, drug, batch_id) == ("recalled", 0, 0)
