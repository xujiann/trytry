"""转诊环节轨迹的竞态在**真 PostgreSQL** 上的实证（P1-30 · spd_referral_steps）。

默认跳过（同 `tests/test_postgres_real.py`）：

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_spd_referral_step_unique_races.py -q

为什么要有这一档：SQLite 的**库级写锁**把「读到旧态」与「按旧态写」之间的窗口
一并锁掉了，test-unit 里的线程探针因此对「拆掉条件 UPDATE」并不敏感（那边的确定性
来自窗口重放与静态钉，见 `tests/test_spd_referral_step_race.py`）。PG 是
READ COMMITTED、逐语句取快照，八个事务能真的同时读到 `status='accepted'`——旧写法
（`db.get` → Python 判 → ORM 赋值 → commit，flush 出的 UPDATE 只有 `WHERE id=?`）
在这里是八路全成、八行「到院」、八笔有效上转积分。

把判定压进 `UPDATE … WHERE id=:id AND status='accepted'` 之后：第一路拿到行锁，
其余七路阻塞在同一行上；赢家提交后它们按新行版本重算 WHERE（EvalPlanQual），
`status` 已是 `arrived`，rowcount=0——轨迹行与副作用一个都不落。

**这个库是共享的**：不 DROP SCHEMA、不清表，每条用例自造带随机后缀的机构/账号/患者，
只读写自己造出来的行。
"""
import os
import random
import subprocess
import sys
import threading
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

#: 与其他代理共用同一个库：拿不到锁时**重试**而不是跳过（跳过等于这一档没跑）。
_RETRIES = 5
_RETRY_WAIT_SECONDS = 10


SERVER_DIR = Path(__file__).resolve().parents[1]
_WANT_TABLES = {
    "organizations", "users", "patients", "spd_referral_cases", "spd_referral_steps",
}


def _tables_ready(engine) -> bool:
    from sqlalchemy import inspect

    return not (_WANT_TABLES - set(inspect(engine).get_table_names()))


@pytest.fixture(scope="module")
def pg_engine():
    """连到 PG 测试库，**只在缺表时**跑一次幂等的 `alembic upgrade heads`。

    **不重建 schema**：`test_postgres_real.py` 的 `pg_engine` 会
    `DROP SCHEMA public CASCADE`，那是它独占库时的做法；本文件可能跑在共享库上，
    清库会把别人连锅端掉。但也不能只校验"表得在"——CI 的集成档跑在**全新库**上，
    那样这一档会直接红而不是把库补齐；升级本身幂等，已升过就是空操作。
    """
    from sqlalchemy import create_engine

    engine = create_engine(PG_URL)
    for attempt in range(_RETRIES):
        if _tables_ready(engine):
            break
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_DIR,
            env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 or _tables_ready(engine):
            break
        assert attempt < _RETRIES - 1, (
            f"在 {PG_URL} 上跑迁移 {_RETRIES} 次都没成功：\n{result.stderr[-2000:]}"
        )
        time.sleep(_RETRY_WAIT_SECONDS)
    if not _tables_ready(engine):
        from sqlalchemy import inspect

        missing = sorted(_WANT_TABLES - set(inspect(engine).get_table_names()))
        raise AssertionError(f"目标库缺表 {missing}：`alembic upgrade heads`（双 head）没把库建起来")
    yield engine
    engine.dispose()


def _race(worker, times):
    """Barrier 真并发（写法同 test_postgres_real._race_on_pg）。

    只起线程不够：线程创建有先后，前一个常常已提交完后一个才开始读，窗口根本
    没打开。等待点一律带 timeout——会挂起的回归测试挡不住任何东西。
    """
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
        t.join(timeout=120)
    return results, errors


def _with_retry(action):
    """共享库上偶发的锁等待/串行化冲突：退避重试，不吞掉真正的断言失败。"""
    from sqlalchemy.exc import DBAPIError, OperationalError

    for attempt in range(_RETRIES):
        try:
            return action()
        except (OperationalError, DBAPIError) as exc:  # 锁超时 / 串行化冲突
            if attempt == _RETRIES - 1:
                raise
            print(f"[PG 共享库] 第 {attempt + 1} 次遇到 {type(exc).__name__}，{_RETRY_WAIT_SECONDS}s 后重试")
            time.sleep(_RETRY_WAIT_SECONDS)
    raise AssertionError("unreachable")


def _seed_accepted_case(Session):
    """造一张 status='accepted' 的转诊单（机构/账号/患者都带随机后缀，互不串味）。"""
    from app.models import Organization, Patient, User
    from app.spd.models import SpdReferralCase

    tag = uuid.uuid4().hex[:8]

    def _create():
        with Session() as db:
            org = Organization(name=f"PG转诊县医院-{tag}", org_type="lead_hospital", level="county")
            user = User(username=f"pg_ref_doc_{tag}", password_hash="x", full_name="县医院医生")
            patient = Patient(
                name=f"PG转诊患者-{tag}",
                id_card=f"3309{random.randrange(10 ** 13, 10 ** 14)}",
                gender="男", birth_date="1993-03-03", ehc_no=f"PG-EHC-REF-{tag}",
            )
            db.add_all([org, user, patient])
            db.flush()
            user.org_id = org.id
            case = SpdReferralCase(
                patient_id=patient.id, program_code="", direction="up",
                initiator_org_id=org.id, initiator_id=user.id,
                current_org_id=org.id, current_level="county",
                status="accepted", reason="PG竞态",
            )
            db.add(case)
            db.commit()
            return case.id, user.id, org.id

    return _with_retry(_create)


@pytest.mark.timeout(240)
def test_转诊单条件推进_八路并发恰一路到院(pg_engine):
    """八路同时给同一张 accepted 单登记到院：恰一路推进到，「到院」轨迹恰一行。

    修前（ORM 赋值 + `WHERE id=?`）在 PG 上是八路全成、八行轨迹；判定进了 WHERE
    之后，后到的七路按赢家提交后的行版本重算条件，rowcount=0，各自回滚。
    """
    from sqlalchemy.orm import sessionmaker

    from app.spd.models import SpdReferralCase, SpdReferralStep
    from app.spd.routers.referral import _advance_case

    Session = sessionmaker(bind=pg_engine)
    case_id, user_id, org_id = _seed_accepted_case(Session)

    def worker(i):
        with Session() as db:
            ok = _advance_case(db, case_id, "accepted", status="arrived", effective_visit=True)
            if ok:
                db.add(SpdReferralStep(
                    case_id=case_id, step="到院", action="arrive",
                    actor_id=user_id, org_id=org_id, opinion=f"第{i}路",
                ))
                db.commit()
            else:
                db.rollback()
            return ok

    results, errors = _race(worker, times=8)
    assert not errors, f"条件更新并发下不该抛错：{errors}"
    assert len(results) == 8
    assert results.count(True) == 1, f"一格只能推进一次，实际 {results.count(True)} 路推到"
    with Session() as db:
        case = db.get(SpdReferralCase, case_id)
        assert case is not None and case.status == "arrived"
        rows = (
            db.query(SpdReferralStep)
            .filter(SpdReferralStep.case_id == case_id, SpdReferralStep.step == "到院")
            .all()
        )
    assert len(rows) == 1, f"「到院」轨迹只能有一行，实际 {len(rows)} 行：{[r.opinion for r in rows]}"


@pytest.mark.timeout(240)
def test_到院与下转并发_不会写出下转之后又到院的轨迹(pg_engine):
    """跨环节竞态：`(case_id, step)` 唯一索引对它完全无效——环节名根本不同。

    四路到院 + 四路下转打同一张 accepted 单。合法结局有两种：到院先赢（下转随后
    仍可命中，accepted→arrived→down_referred 是合法顺序路径），或下转先赢（此后
    到院一路不得命中）。不变式：两个环节各至多一行，且**下转赢了之后不许再落到院**。
    """
    from sqlalchemy.orm import sessionmaker

    from app.spd.models import SpdReferralCase, SpdReferralStep
    from app.spd.routers.referral import _advance_case

    Session = sessionmaker(bind=pg_engine)
    case_id, user_id, org_id = _seed_accepted_case(Session)

    def worker(i):
        arrive = i % 2 == 0
        with Session() as db:
            if arrive:
                ok = _advance_case(db, case_id, "accepted", status="arrived", effective_visit=True)
            else:
                ok = _advance_case(
                    db, case_id, ("accepted", "arrived"),
                    status="down_referred", stable_for_down=True,
                )
            if ok:
                db.add(SpdReferralStep(
                    case_id=case_id, step="到院" if arrive else "下转",
                    action="arrive" if arrive else "down",
                    actor_id=user_id, org_id=org_id, opinion=f"第{i}路",
                ))
                db.commit()
            else:
                db.rollback()
            return ("到院" if arrive else "下转") if ok else None

    results, errors = _race(worker, times=8)
    assert not errors, f"条件更新并发下不该抛错：{errors}"
    assert results.count("到院") <= 1 and results.count("下转") <= 1, results
    assert results.count("下转") == 1, f"下转在两种结局下都应命中一次，实际：{results}"
    with Session() as db:
        case = db.get(SpdReferralCase, case_id)
        steps = (
            db.query(SpdReferralStep)
            .filter(SpdReferralStep.case_id == case_id)
            .order_by(SpdReferralStep.id)
            .all()
        )
    assert case is not None and case.status == "down_referred"
    names = [s.step for s in steps]
    assert names in (["下转"], ["到院", "下转"]), f"轨迹顺序自相矛盾：{names}"
