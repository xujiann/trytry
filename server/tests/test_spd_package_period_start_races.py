"""真 PostgreSQL 上的「改服务起始日 → 在绑服务包有效期跟着重算」竞态（P2-576，默认跳过）。

`_follow_service_start` 先按读到的旧起始日条件写起始日这一列（`move_row`），再按快照天数重算在绑服务包的有效期。
不加这道条件：两人同时改同一份档案的起始日，各按自己的新起始日重算有效期、各自提交——档案上最终的起始日是后写
档案行的那一路的，服务包的有效期却可能是另一路算的，两者对不上，也没人知道。

这里让八路先都读到同一个旧起始日、再一起写：应当 1×200 + 7×409，库里的起始日与有效期出自同一路。SQLite 的库级
写锁测不出这个（写本身是串行的，条件照样成立），所以放在真 PG 档。

开启方式与 tests/test_postgres_real.py 同一约定（`MEDPLAT_PG_TEST_URL`）；不重建 schema，数据带随机后缀。
"""
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"),
]

SERVER_DIR = Path(__file__).resolve().parents[1]


def _retrying(fn, what: str, attempts: int = 5, wait: int = 60):
    """共用库上撞锁就等一会儿再来——跳过测试等于把红灯藏起来。"""
    from sqlalchemy.exc import DBAPIError, OperationalError

    for i in range(attempts):
        try:
            return fn()
        except (OperationalError, DBAPIError) as exc:  # 锁等待/串行化冲突
            if i == attempts - 1:
                raise
            print(f"[{what}] 第 {i + 1} 次撞锁（{type(exc).__name__}），{wait}s 后重试")
            time.sleep(wait)
    raise AssertionError("unreachable")


@pytest.fixture(scope="module")
def pg_engine():
    """连上共用的 PG 测试库；快照天数这一列还没有就跑一次迁移（幂等，不 DROP）。"""
    from sqlalchemy import create_engine, inspect

    engine = create_engine(PG_URL)
    inspector = inspect(engine)
    if "spd_package_bindings" not in inspector.get_table_names() or "period_days" not in {
            c["name"] for c in inspector.get_columns("spd_package_bindings")}:
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_DIR,
            env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"迁移在 PG 上失败：\n{result.stderr[-2000:]}"
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def world(pg_engine):
    """机构 + 患者 + 通用服务包（30 天）+ 一份起始日 2026-09-01 的档案，已绑包。"""
    from sqlalchemy.orm import sessionmaker

    from app.models import Organization, Patient
    from app.spd.models import SpdEnrollment, SpdServicePackage
    from app.spd.routers.population import _bind_package

    tag = uuid.uuid4().hex[:8]
    Session = sessionmaker(bind=pg_engine)

    def build():
        with Session() as db:
            org = Organization(name=f"起始日竞态院-{tag}", org_type="township", level="township")
            patient = Patient(name=f"起始日竞态-{tag}", id_card=f"3310{tag}0011", gender="男",
                              birth_date="1960-01-01", ehc_no=f"P2576-EHC-{tag}")
            package = SpdServicePackage(
                code=f"p2576_{tag}", name=f"起始日竞态包-{tag}", program_code="", price=200, period_days=30,
                items=[{"code": "bp_check", "name": "血压测量", "times": 2, "price": 5}],
            )
            db.add_all([org, patient, package])
            db.flush()
            enrollment = SpdEnrollment(patient_id=patient.id, program_code=f"p2576_dm_{tag}", org_id=org.id,
                                       status="active", service_start="2026-09-01")
            db.add(enrollment)
            db.flush()
            binding = _bind_package(db, enrollment, package.id)
            db.commit()
            assert binding.period_end == "2026-09-30"
            return {"Session": Session, "enrollment_id": enrollment.id, "binding_id": binding.id}

    return _retrying(build, "建场景")


def test_八路同时改起始日_只成一路_有效期与起始日出自同一路(world):
    from fastapi import HTTPException

    from app.spd.models import SpdEnrollment, SpdPackageBinding
    from app.spd.routers.population import _follow_service_start

    targets = [(date(2027, 1, 1) + timedelta(days=i)).isoformat() for i in range(8)]
    barrier = threading.Barrier(len(targets))
    results: list[tuple[int, str]] = []
    lock = threading.Lock()

    def run(target: str):
        with world["Session"]() as db:
            enrollment = db.get(SpdEnrollment, world["enrollment_id"])
            old_start = enrollment.service_start
            barrier.wait(timeout=60)   # 八路都读到同一个旧起始日之后再一起写
            enrollment.service_start = target
            try:
                _follow_service_start(db, enrollment, old_start)
                db.commit()
                code = 200
            except HTTPException as exc:
                code = exc.status_code
        with lock:
            results.append((code, target))

    threads = [threading.Thread(target=run, args=(t,)) for t in targets]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    codes = sorted(code for code, _ in results)
    assert codes == [200] + [409] * 7, codes
    winner = next(target for code, target in results if code == 200)
    with world["Session"]() as db:
        assert db.get(SpdEnrollment, world["enrollment_id"]).service_start == winner
        period_end = db.get(SpdPackageBinding, world["binding_id"]).period_end
    assert period_end == (date.fromisoformat(winner) + timedelta(days=29)).isoformat()
