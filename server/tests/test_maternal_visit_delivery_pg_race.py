"""孕产妇访视与分娩日期互相核对在真并发下的取证（P2-1020 跟进，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_maternal_visit_delivery_pg_race.py -q

`add_visit` 按这一胎的分娩日判访视（产后访视不得早于分娩），`add_delivery` 按已记的访视判分娩日——两边读的都是
**别的**行、写的都是 INSERT，INSERT 不给任何既有行加锁。一次分娩登记与几条日期早于它的产后访视同时落下，PG 的
READ COMMITTED 下各路都读到「还没有对方」、都放行，倒挂照样进库。修后两边的判定与写入圈在这份档案那一行的
`serialized_on`（PG 上 `SELECT … FOR UPDATE`）；SQLite 一侧的判定本身见 `test_maternal_visit_vs_delivery_date.py`。

不变式：**不论谁先谁后，登记了分娩的档案里没有日期早于分娩日的产后访视**。

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

from app.models import DeliveryRecord, MaternalRecord, MaternalVisit, Organization, Patient, User
from app.routers.maternal import DeliveryCreate, VisitCreate, add_delivery, add_visit

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"),
]

RACERS = 6   # 一路登记分娩，五路记日期早于分娩的产后访视
ROUNDS = 3
DELIVERY_DATE = "2026-09-20"
SERVER_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pg_engine():
    """连既有测试库；空库就地补跑迁移，任何情况下都不清 schema（理由同 test_insurance_apply_unique_races）。
    池子按参赛路数开并先热身，否则冷池下测出来的是排队而不是并发（见 test_cost_allocation_races）。"""
    engine = create_engine(PG_URL, pool_size=RACERS, max_overflow=RACERS)
    if not sa_inspect(engine).has_table("delivery_records"):
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
    """每轮一位孕妇一份档案，名字带随机后缀；跑完把自己造的行全收拾掉（先子后父）。"""
    Session = sessionmaker(bind=pg_engine, autoflush=False)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG孕产并发{tag}", org_type="township", level="township")
        patients = [Patient(ehc_no=f"PGM{tag}{i}", name=f"PG孕产并发{tag}-{i}", id_card=f"PGM{tag}{i}", gender="女")
                    for i in range(ROUNDS)]
        db.add(org)
        db.add_all(patients)
        db.flush()
        records = [MaternalRecord(patient_id=p.id) for p in patients]
        db.add_all(records)
        db.commit()
        ids = {"org": org.id, "patients": [p.id for p in patients], "records": [r.id for r in records]}

    yield ids

    with Session() as db:
        db.query(MaternalVisit).filter(MaternalVisit.record_id.in_(ids["records"])).delete(synchronize_session=False)
        db.query(DeliveryRecord).filter(DeliveryRecord.record_id.in_(ids["records"])).delete(synchronize_session=False)
        db.query(MaternalRecord).filter(MaternalRecord.id.in_(ids["records"])).delete(synchronize_session=False)
        db.query(Patient).filter(Patient.id.in_(ids["patients"])).delete(synchronize_session=False)
        db.query(Organization).filter(Organization.id == ids["org"]).delete(synchronize_session=False)
        db.commit()


def test_分娩登记与早于它的产后访视同时落下_库里不会倒挂(pg_engine, world):
    Session = sessionmaker(bind=pg_engine, autoflush=False)
    operator = User(username="pg-maternal", role="admin", org_id=None)   # 全域角色过机构写入守卫，不落库
    for record_id in world["records"]:
        def worker(index, record_id=record_id):
            with Session() as db:
                try:
                    if index == 0:
                        add_delivery(record_id, DeliveryCreate(org_id=world["org"], delivery_date=DELIVERY_DATE),
                                     db=db, user=operator)
                        return ("ok", "delivery")
                    add_visit(record_id, VisitCreate(visit_type="postpartum", visit_date=f"2026-09-1{index}"), db=db)
                    return ("ok", "visit")
                except HTTPException as exc:
                    return (exc.status_code, "delivery" if index == 0 else "visit")

        results, errors = _race_on_pg(worker, RACERS)
        assert not errors, errors
        assert len(results) == RACERS, results
        with Session() as db:
            delivered = [d for (d,) in db.query(DeliveryRecord.delivery_date).filter(DeliveryRecord.record_id == record_id)]
            visits = sorted(v for (v,) in db.query(MaternalVisit.visit_date).filter(
                MaternalVisit.record_id == record_id, MaternalVisit.visit_type == "postpartum"))
        if delivered:   # 分娩先落：五条早于它的产后访视都该 409
            assert visits == [], (delivered, visits, results)   # 修前：分娩与早于它的产后访视同在
            assert sorted(code for code, what in results if what == "visit") == [409] * (RACERS - 1), results
        else:           # 访视先落：分娩日晚于已记的产后访视，分娩 409
            assert ("ok", "delivery") not in results and (409, "delivery") in results, results
            assert visits, results
