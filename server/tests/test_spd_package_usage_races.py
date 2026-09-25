"""慢专病服务包扣减登记的真并发取证（P2-112，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_spd_package_usage_races.py -q

扣减登记原先「读 binding.items → 判剩余 → 已用 +qty → 整列写回」，没有锁。PG 的 READ COMMITTED 下八笔并发扣减
都读到同一个已用次数、都判定还有剩余、都写回 已用+1——八条扣减流水，已用次数只加了一两次，5 次的项目用了 8 次，
账上还显示有剩余。修法把整段圈进这条绑定的 `serialized_on`（PG 上 `SELECT … FOR UPDATE`），锁里重读再判再写。

不变量：**扣成功的笔数不超过项目次数，JSON 里的已用次数 = 扣减流水的合计**。

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

from app.models import Organization, Patient, User
from app.spd.models import SpdEnrollment, SpdPackageBinding, SpdPackageUsage, SpdServicePackage
from app.spd.routers.population import UsageIn, add_usage

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"),
]

RACERS = 8
TIMES = 5
SERVER_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pg_engine():
    """连既有测试库；空库就地补跑迁移，任何情况下都不清 schema（理由同 test_insurance_apply_unique_races）。
    池子按参赛路数开并先热身，否则冷池下测出来的是排队而不是并发（见 test_cost_allocation_races）。"""
    engine = create_engine(PG_URL, pool_size=RACERS, max_overflow=RACERS)
    if not all(sa_inspect(engine).has_table(t) for t in ("spd_package_bindings", "spd_package_usages")):
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
    """机构、本机构医生、患者、在管档案、一个「血压测量 5 次」的服务包与它的一条在绑（名字全带随机后缀）。"""
    Session = sessionmaker(bind=pg_engine)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG服务包扣减院{tag}", org_type="township", level="township")
        patient = Patient(name=f"服务包扣减患者{tag}", id_card=f"3314{uuid.uuid4().int % 10**14:014d}",
                          gender="女", birth_date="1962-06-06", ehc_no=f"PG-USAGE-{tag}")
        package = SpdServicePackage(code=f"pkguse_{tag}", name=f"扣减服务包-{tag}", program_code="",
                                    price=100, period_days=30,
                                    items=[{"code": "bp_check", "name": "血压测量", "times": TIMES, "price": 5}])
        db.add_all([org, patient, package])
        db.flush()
        user = User(username=f"pg_usage_{tag}", password_hash="x", full_name=f"扣减医生{tag}",
                    role="doctor", org_id=org.id)
        enrollment = SpdEnrollment(patient_id=patient.id, program_code=f"pkguse_dm_{tag}", org_id=org.id,
                                   status="active")
        db.add_all([user, enrollment])
        db.flush()
        binding = SpdPackageBinding(enrollment_id=enrollment.id, package_id=package.id, status="bound", period_end="",
                                    items=[{"code": "bp_check", "name": "血压测量", "total": TIMES, "used": 0, "price": 5}])
        db.add(binding)
        db.commit()
        ids = {"org_id": org.id, "patient_id": patient.id, "package_id": package.id, "user_id": user.id,
               "enrollment_id": enrollment.id, "binding_id": binding.id}

    yield ids

    with Session() as db:  # 共用库：自己造的行自己收拾（先子后父）
        db.query(SpdPackageUsage).filter_by(binding_id=ids["binding_id"]).delete()
        db.query(SpdPackageBinding).filter_by(id=ids["binding_id"]).delete()
        db.query(SpdEnrollment).filter_by(id=ids["enrollment_id"]).delete()
        db.query(SpdServicePackage).filter_by(id=ids["package_id"]).delete()
        db.query(User).filter_by(id=ids["user_id"]).delete()
        db.query(Patient).filter_by(id=ids["patient_id"]).delete()
        db.query(Organization).filter_by(id=ids["org_id"]).delete()
        db.commit()


def test_八笔并发扣减同一项目_不超次数_已用与流水对得上(pg_engine, world):
    Session = sessionmaker(bind=pg_engine)

    def worker(_i):
        with Session() as db:
            try:
                add_usage(world["binding_id"], UsageIn(item_code="bp_check"), db=db,
                          user=db.get(User, world["user_id"]))
            except HTTPException as exc:
                return ("rejected", exc.status_code, exc.detail)
            return ("ok",)

    _warm_pool(pg_engine, RACERS)
    results, errors = _race_on_pg(worker, times=RACERS)
    assert not errors, f"异常不该漏给调用方：{errors}"
    ok = [r for r in results if r[0] == "ok"]
    rejected = [r for r in results if r[0] == "rejected"]
    assert len(ok) == TIMES, f"5 次的项目应恰好扣成 5 笔，实际 {results}"   # 修前：八笔都成功
    assert all(r[1:] == (409, "该项目剩余次数不足") for r in rejected), rejected
    with Session() as db:
        used = db.get(SpdPackageBinding, world["binding_id"]).items[0]["used"]
        logged = db.query(SpdPackageUsage).filter_by(binding_id=world["binding_id"]).count()
    assert used == logged == TIMES, (used, logged)   # 修前：流水 8 条、已用只记了一两次
