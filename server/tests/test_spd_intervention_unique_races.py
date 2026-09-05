"""真 PostgreSQL 上的高危自动干预竞态（P1-30 · spd_interventions 档）。

默认跳过——CI/开发机不一定有 PG。开启方式（与 test_postgres_real.py 一致）：

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_spd_intervention_unique_races.py -q

守的是 **SQLite 上只能靠调度碰运气的东西**：`care._auto_intervene` 的两段
check-then-act（在途干预按 (纳管档案, 干预模板)、高危复诊按 (患者, 病种)）在
READ COMMITTED 下窗口是**确定**打开的——会话 `autoflush=False`，调用方提交之前
一条语句都没到库，Barrier 让八路的存在性 SELECT 全跑在任何一路提交之前，
于是各查各的空、各插各的行，八条。SQLite 侧同名的行为探针
（tests/test_spd_intervention_auto_race.py）红绿取决于线程调度，只有这条是必然的。

修法是以档案行为界的临界区（`concurrency.serialized_on`）：赢家拿到
`SELECT id FROM spd_enrollments WHERE id=:id FOR UPDATE`，插完在块内提交、锁随
之释放；后到的七路被这把锁挡在门外，放行之后每条 SELECT 都取新快照（PG 逐语句
取快照），看见赢家已提交的行就跳过。**没有 409**：这条路径是幂等而不是互斥，
八路全部正常返回，只是自动干预与高危复诊各只落一条。

本库与其他并行任务共用：本文件**不 DROP SCHEMA、不跑迁移**，只用带随机后缀的
自造数据，跑完把自己的行删掉。
"""
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"
    ),
]

#: 锁等待/瞬时冲突的重试（共享库，别人的用例可能正握着锁）。
RETRIES = 5
RETRY_WAIT_S = 60


SERVER_DIR = Path(__file__).resolve().parents[1]
_WANT_TABLES = {
    "spd_enrollments", "spd_interventions", "spd_revisits",
    "spd_intervention_templates", "patients", "organizations",
}


def _missing_tables(engine) -> set[str]:
    from sqlalchemy import inspect

    return _WANT_TABLES - set(inspect(engine).get_table_names())


@pytest.fixture(scope="module")
def pg_engine():
    """连库；**不** DROP SCHEMA（库可能是多任务共用的），但**缺表就自己升一次**。

    只校验"表得在"是不够的：CI 的集成档跑在**全新库**上，那样这一档会直接红，
    而不是把库补齐——`alembic upgrade heads` 本身幂等，已升过就是空操作。
    """
    from sqlalchemy import create_engine

    engine = create_engine(PG_URL)
    for attempt in range(RETRIES):
        if not _missing_tables(engine):
            break
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_DIR,
            env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 or not _missing_tables(engine):
            break
        assert attempt < RETRIES - 1, (
            f"在 {PG_URL} 上跑迁移 {RETRIES} 次都没成功：\n{result.stderr[-2000:]}"
        )
        time.sleep(RETRY_WAIT_S)
    if _missing_tables(engine):
        raise AssertionError(
            f"目标库缺表 {sorted(_missing_tables(engine))}："
            "`alembic upgrade heads`（双 head）没把库建起来"
        )
    yield engine
    engine.dispose()


def _retrying(fn):
    """共享库上撞锁/瞬时冲突就等一会儿重来，不许因此跳过用例。"""
    from sqlalchemy.exc import DBAPIError, OperationalError

    last: BaseException | None = None
    for attempt in range(RETRIES):
        try:
            return fn()
        except (OperationalError, DBAPIError) as exc:  # 锁超时 / 死锁 / 连接抖动
            last = exc
            if attempt == RETRIES - 1:
                break
            time.sleep(RETRY_WAIT_S)
    raise AssertionError(f"共享库上重试 {RETRIES} 次仍失败：{last}") from last


def _race_on_pg(worker, times):
    """Barrier 真并发（写法同 test_postgres_real._race_on_pg）。

    只起线程不够——线程创建有先后，前一个常常已提交完了后一个才开始读，
    窗口根本没打开。等待点全部带 timeout：会阻塞的回归测试不是回归测试。
    """
    import threading

    results: list = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(times)

    def run(index: int):
        try:
            barrier.wait(timeout=30)
            outcome = worker(index)
            with lock:
                results.append(outcome)
        except BaseException as exc:  # noqa: BLE001 - 收集断言用
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(times)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results, errors


def test_高危自动干预临界区_八路并发恰一条干预一条复诊(pg_engine):
    """`care._auto_intervene` 的 PG 直测：同一档案八路并发极高危，恰一条自动干预 + 一条高危复诊。

    修之前：Barrier 让八路的存在性 SELECT 全部跑在任何一路提交之前，八路都判"没有"、
    八路都插——档案上挂八条一模一样的"very_high风险自动干预"和八条高危复诊。
    修之后：档案行的 FOR UPDATE 把八路排成队，第二位起重查时快照里已有赢家的行。

    断言里没有 409：这条路径幂等不互斥，八路都该正常返回。
    """
    from sqlalchemy.orm import sessionmaker

    from app.models import Organization, Patient
    from app.spd.models import (
        SpdEnrollment,
        SpdIntervention,
        SpdInterventionTemplate,
        SpdRevisit,
    )
    from app.spd.routers.care import _auto_intervene

    Session = sessionmaker(bind=pg_engine)
    # 共享库：一切标识都带随机后缀，既不撞并行任务，也让本文件可重复跑。
    tag = uuid.uuid4().hex[:8]
    program_code = f"pgai_{tag}"

    def _setup():
        with Session() as db:
            org = Organization(name=f"PG高危联动院{tag}", org_type="township", level="township")
            patient = Patient(
                name=f"PG高危联动患者{tag}", id_card=f"3309001996{uuid.uuid4().int % 10**8:08d}",
                gender="男", birth_date="1996-06-06", ehc_no=f"PG-EHC-AI-{tag}",
            )
            db.add_all([org, patient])
            db.flush()
            enrollment = SpdEnrollment(
                patient_id=patient.id, program_code=program_code, org_id=org.id,
                risk_level="low", status="active",
            )
            template = SpdInterventionTemplate(
                code=f"pg_auto_vh_{tag}", name="PG极高危干预包", program_code=program_code,
                category="drug", content="药物调整", auto_risk_level="very_high", active=True,
            )
            db.add_all([enrollment, template])
            db.commit()
            return enrollment.id, template.id, patient.id, org.id

    eid, tid, pid, oid = _retrying(_setup)

    try:
        def worker(_i):
            with Session() as db:
                # `_auto_intervene` 在自己的临界区里提交，调用方不必再 commit。
                _auto_intervene(db, db.get(SpdEnrollment, eid), "very_high")
                return True

        results, errors = _race_on_pg(worker, times=8)
        assert not errors, f"自动干预临界区并发下不该抛错：{errors}"
        assert len(results) == 8, f"八路都该正常返回（没有 409、没有互斥），实际 {len(results)}"

        with Session() as db:
            interventions = (
                db.query(SpdIntervention)
                .filter(
                    SpdIntervention.enrollment_id == eid,
                    SpdIntervention.template_id == tid,
                    SpdIntervention.status.in_(["planned", "doing"]),
                )
                .all()
            )
            revisits = (
                db.query(SpdRevisit)
                .filter(
                    SpdRevisit.patient_id == pid,
                    SpdRevisit.program_code == program_code,
                    SpdRevisit.source == "high_risk",
                    SpdRevisit.status == "planned",
                )
                .all()
            )
        assert len(interventions) == 1, (
            f"同一档案同一模板的在途自动干预只该一条，实际 {len(interventions)} 条"
            "——临界区没圈住，两路都读到空快照就都插了"
        )
        assert interventions[0].goal == "very_high风险自动干预", interventions[0].goal
        assert len(revisits) == 1, f"高危自动复诊只该一条，实际 {len(revisits)} 条"
    finally:
        # 共享库：只删自己造的行（都带 tag / 自建主键），失败也不影响别人。
        def _cleanup():
            with Session() as db:
                db.query(SpdIntervention).filter(SpdIntervention.enrollment_id == eid).delete()
                db.query(SpdRevisit).filter(SpdRevisit.patient_id == pid).delete()
                db.query(SpdEnrollment).filter(SpdEnrollment.id == eid).delete()
                db.query(SpdInterventionTemplate).filter(
                    SpdInterventionTemplate.id == tid
                ).delete()
                db.query(Patient).filter(Patient.id == pid).delete()
                db.query(Organization).filter(Organization.id == oid).delete()
                db.commit()

        try:
            _retrying(_cleanup)
        except AssertionError:  # 清不掉不该把用例判红，留给库主人收尾
            pass
