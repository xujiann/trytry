"""高值耗材使用登记的真并发取证（P2-113，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_consumable_use_transition_races.py -q

使用登记原先「内存里判 in_stock → 改 used、记患者」。PG 的 READ COMMITTED 下同一条码八路并发登记给八位患者，
八路都读到在库、都 200，库里只留最后提交的那位。修法把状态翻转与使用信息压进一条 `WHERE status = 'in_stock'`
的 UPDATE。不变量：**恰一路成功，库里记的患者就是成功那一路登记的患者**。

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

from app.models import HighValueConsumable, Organization, Patient, User
from app.routers.materials import UseIn, use_consumable

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
    if not sa_inspect(engine).has_table("high_value_consumables"):
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
    """一家机构、一名本机构经办、八位患者、一枚在库的高值耗材（名字与条码带随机后缀）。"""
    Session = sessionmaker(bind=pg_engine)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG耗材并发院{tag}", org_type="lead_hospital", level="county")
        db.add(org)
        db.flush()
        user = User(username=f"pg_hvc_{tag}", password_hash="x", full_name=f"耗材经办{tag}",
                    role="doctor", org_id=org.id)
        patients = [Patient(name=f"耗材并发患者{tag}-{i}", id_card=f"3315{uuid.uuid4().int % 10**14:014d}",
                            gender="男", birth_date="1958-08-08", ehc_no=f"PG-HVC-{tag}-{i}") for i in range(RACERS)]
        item = HighValueConsumable(barcode=f"PG-HVC-{tag}", name="冠脉支架", org_id=org.id, expire_date="2099-12-31")
        db.add_all([user, item, *patients])
        db.commit()
        ids = {"org_id": org.id, "user_id": user.id, "patient_ids": [p.id for p in patients],
               "item_id": item.id, "barcode": item.barcode}

    yield ids

    with Session() as db:  # 共用库：自己造的行自己收拾（先子后父）
        db.query(HighValueConsumable).filter_by(id=ids["item_id"]).delete()
        db.query(Patient).filter(Patient.id.in_(ids["patient_ids"])).delete(synchronize_session=False)
        db.query(User).filter_by(id=ids["user_id"]).delete()
        db.query(Organization).filter_by(id=ids["org_id"]).delete()
        db.commit()


def test_同一条码八路并发登记给八位患者_恰一路成功_追溯链记的就是它(pg_engine, world):
    Session = sessionmaker(bind=pg_engine)

    def worker(i):
        patient_id = world["patient_ids"][i]
        with Session() as db:
            try:
                use_consumable(world["barcode"], UseIn(patient_id=patient_id), db=db,
                               user=db.get(User, world["user_id"]))
            except HTTPException as exc:
                return ("rejected", exc.status_code, exc.detail)
            return ("ok", patient_id)

    _warm_pool(pg_engine, RACERS)
    results, errors = _race_on_pg(worker, times=RACERS)
    assert not errors, f"异常不该漏给调用方：{errors}"
    ok = [r for r in results if r[0] == "ok"]
    rejected = [r for r in results if r[0] == "rejected"]
    assert len(ok) == 1, f"一枚耗材只能登记一次，实际 {results}"   # 修前：八路都 200
    assert all(r[1] == 409 for r in rejected), rejected
    with Session() as db:
        item = db.get(HighValueConsumable, world["item_id"])
        assert (item.status, item.used_patient_id) == ("used", ok[0][1]), "追溯链记的必须是登记成功的那位"
