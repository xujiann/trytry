"""出院与开医嘱的真并发取证（P2-274，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_discharge_order_races.py -q

开医嘱原先只在锁外判「在院」：与出院并发时读到的还是在院，新医嘱在出院停完医嘱之后才提交，以「执行中」挂在已出院的
住院上。修法把判定圈进住院登记行的临界区（`SELECT ... FOR UPDATE`，锁到手后刷新再判）；出院的第一条写语句是这一行上的
条件 UPDATE，两者互斥——开医嘱先拿到锁，它的医嘱先提交、随后被出院停掉；出院先拿到锁，开医嘱等它提交后读到已出院、409。

出院一路只取与锁相关的写语句（条件 UPDATE 置出院 → 停在执行医嘱 → 提交），顺序与 `discharge_admission` 相同；随访、
通知、领域事件这些旁支在共用库里收拾不干净，且与这把锁无关。不变量：**每一轮出院成功之后，这次住院上没有执行中的医嘱**。

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

from app.clock import now_naive
from app.models import Admission, Bed, InpatientOrder, Organization, Patient, User, Ward
from app.routers.inpatient import OrderCreate, _mark_discharged, create_order

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"),
]

RACERS = 8          # 一路出院 + 七路开医嘱
ROUNDS = 6
SERVER_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pg_engine():
    """连既有测试库；空库就地补跑迁移，任何情况下都不清 schema（理由同 test_insurance_apply_unique_races）。"""
    engine = create_engine(PG_URL, pool_size=RACERS, max_overflow=RACERS)
    if not sa_inspect(engine).has_table("inpatient_orders"):
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
    """一家医院、一名本院医师、一个病区，每轮一张床一位患者一次在院（名字带随机后缀）。"""
    Session = sessionmaker(bind=pg_engine)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG出院并发院{tag}", org_type="lead_hospital", level="county")
        db.add(org)
        db.flush()
        user = User(username=f"pg_dis_{tag}", password_hash="x", full_name=f"出院医师{tag}", role="doctor",
                    org_id=org.id)
        ward = Ward(org_id=org.id, name=f"出院并发病区{tag}")
        db.add_all([user, ward])
        db.flush()
        beds = [Bed(ward_id=ward.id, bed_no=f"D{tag}-{i}") for i in range(ROUNDS)]
        patients = [Patient(name=f"出院并发患者{tag}-{i}", id_card=f"3316{uuid.uuid4().int % 10**14:014d}",
                            gender="男", birth_date="1960-06-06", ehc_no=f"PG-DIS-{tag}-{i}") for i in range(ROUNDS)]
        db.add_all([*beds, *patients])
        db.flush()
        admissions = [Admission(patient_id=p.id, org_id=org.id, ward_id=ward.id, bed_id=b.id, created_by=user.id,
                                diagnosis_name="肺炎") for p, b in zip(patients, beds)]
        db.add_all(admissions)
        db.commit()
        ids = {"org_id": org.id, "user_id": user.id, "ward_id": ward.id, "bed_ids": [b.id for b in beds],
               "patient_ids": [p.id for p in patients], "admission_ids": [a.id for a in admissions]}

    yield ids

    with Session() as db:  # 共用库：自己造的行自己收拾（先子后父）
        db.query(InpatientOrder).filter(InpatientOrder.admission_id.in_(ids["admission_ids"])).delete(
            synchronize_session=False)
        db.query(Admission).filter(Admission.id.in_(ids["admission_ids"])).delete(synchronize_session=False)
        db.query(Bed).filter(Bed.id.in_(ids["bed_ids"])).delete(synchronize_session=False)
        db.query(Patient).filter(Patient.id.in_(ids["patient_ids"])).delete(synchronize_session=False)
        db.query(Ward).filter_by(id=ids["ward_id"]).delete()
        db.query(User).filter_by(id=ids["user_id"]).delete()
        db.query(Organization).filter_by(id=ids["org_id"]).delete()
        db.commit()


def _discharge_core(db, admission_id):
    """出院与锁相关的写语句，顺序同 `discharge_admission`：条件 UPDATE 置出院是第一条写，随后停在执行医嘱、提交。"""
    now = now_naive()
    if not _mark_discharged(db, admission_id, now):
        db.rollback()
        return ("rejected", 409)
    db.query(InpatientOrder).filter(
        InpatientOrder.admission_id == admission_id, InpatientOrder.status == "active"
    ).update({InpatientOrder.status: "stopped", InpatientOrder.stopped_at: now}, synchronize_session=False)
    db.commit()
    return ("discharged",)


def test_出院与七路开医嘱同时到达_出院之后没有执行中的医嘱挂在这次住院上(pg_engine, world):
    Session = sessionmaker(bind=pg_engine)
    for round_no, admission_id in enumerate(world["admission_ids"]):
        def worker(i, admission_id=admission_id, round_no=round_no):
            with Session() as db:
                if i == 0:
                    return _discharge_core(db, admission_id)
                try:
                    create_order(OrderCreate(admission_id=admission_id, order_type="temp",
                                             content=f"第{round_no}轮临时医嘱{i}"),
                                 db=db, user=db.get(User, world["user_id"]))
                except HTTPException as exc:
                    return ("rejected", exc.status_code)
                return ("ordered",)

        _warm_pool(pg_engine, RACERS)
        results, errors = _race_on_pg(worker, times=RACERS)
        assert not errors, f"第 {round_no} 轮：异常不该漏给调用方：{errors}"
        assert ("discharged",) in results, results
        assert all(r[1] == 409 for r in results if r[0] == "rejected"), results
        with Session() as db:
            active = db.query(InpatientOrder).filter_by(admission_id=admission_id, status="active").count()
        assert active == 0, f"第 {round_no} 轮：出院之后仍有 {active} 条执行中的医嘱（修前：锁外判完在院的那几路）"
