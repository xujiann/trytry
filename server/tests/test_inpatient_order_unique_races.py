"""住院三条不变式在**真 PostgreSQL** 上的八路并发直测（P1-30）。

默认跳过——CI/开发机不一定有 PG。开启方式（约定同 `tests/test_postgres_real.py`）：

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:55432/medplat_test
    python -m pytest tests/test_inpatient_order_unique_races.py -q

**为什么非得在 PG 上跑**：SQLite 的库级写锁把"判定与写入之间"那段窗口整个锁掉了，
八路并发在它上面自动排成八次顺序请求——预检就把重复挡了，抢输者根本到不了兜底。
PG 是逐语句取快照的 READ COMMITTED：八路都读得到"还没有"，窗口是真开着的。
所以单元档（`test_inpatient_order_unique.py` / `test_inpatient_order_admission_gate.py`）
只能钉静态形状与侧信道复现，"恰一路成功"这条只有这里能证。

三条不变式各一条主用例 + 对照：

1. `inpatient_orders`：同一次住院内容相同的**执行中长期医嘱**恰一条
   （部分唯一索引 `uq_inpatient_order_active_long` + `insert_or_conflict` 兜底）；
   对照是临时医嘱八路全落库、停用后重开又能落一条；
2. `admissions` 出院：一条住院记录只能从 admitted 迁出一次
   （`inpatient._mark_discharged` 的条件 UPDATE），且出院随访恰一条；
3. `admissions` 转床：只能从"我读到的那张床"上换走（比较交换），
   抢输者不占床——修复前两张目标床都会被占上，另一张永远漏着。

**这个库是共享的**：不 DROP SCHEMA、不清表，每条用例自带随机后缀的机构/病区/床/
患者/账号，只按自己造的 id 断言；撞锁就等一会儿重试，而不是跳过。
"""
import os
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
    # 共享库上撞锁要按约定等 60 秒再试（最多 5 次），比 conftest 默认的 120 秒宽得多
    pytest.mark.timeout(900),
]

LONG_CONTENT = "头孢曲松 2g qd ivgtt"
DUPLICATE_DETAIL = "该住院已有内容相同的执行中长期医嘱，请先停止原医嘱再开立"


SERVER_DIR = Path(__file__).resolve().parents[1]

_WANT_INDEXES = (
    ("inpatient_orders", "uq_inpatient_order_active_long"),
    ("admissions", "uq_admission_patient_admitted"),
)


def _indexes_ready(engine) -> bool:
    """两条索引都在才算就绪（空库上表都还没有，先当作没就绪）。"""
    from sqlalchemy import inspect as sa_inspect

    inspector = sa_inspect(engine)
    tables = set(inspector.get_table_names())
    for table, index_name in _WANT_INDEXES:
        if table not in tables:
            return False
        if index_name not in {i["name"] for i in inspector.get_indexes(table)}:
            return False
    return True


@pytest.fixture(scope="module")
def pg_engine():
    """连上库并**只在缺索引时**跑一次幂等的 `alembic upgrade heads`。

    刻意不 `DROP SCHEMA`：这个库可能与别的用例共用，清库会把别人连锅端掉
    （与 `test_postgres_real.py` 那条"证明迁移链能白手起家"的用例分工不同）。
    但也不能只断言"索引得在"——CI 的集成档跑在**全新库**上，那样这一档会直接变红
    而不是把库补齐。共用库上可能撞别人的锁，故失败重试几次再判死。
    """
    from sqlalchemy import create_engine

    engine = create_engine(PG_URL)
    for attempt in range(5):
        if _indexes_ready(engine):
            break
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_DIR,
            env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 or _indexes_ready(engine):
            break
        assert attempt < 4, f"在 {PG_URL} 上跑迁移五次都没成功：\n{result.stderr[-2000:]}"
        time.sleep(60)
    assert _indexes_ready(engine), (
        "库上缺 uq_inpatient_order_active_long / uq_admission_patient_admitted："
        "迁移没跑到 b9c8d7e6f5a4，或探到存量重复而跳过了建索引"
        "（见迁移 docstring 的人工处置 SQL）；并发用例没有约束就是假绿"
    )
    yield engine
    engine.dispose()


def _retrying(fn, attempts: int = 5, wait: float = 60.0):
    """共享库上撞到锁/连接抖动就等一会儿重来，而不是把用例跳过。

    **只重试 `OperationalError`**（锁等待超时、死锁、连接被掐这类"重来可能成功"的）。
    别把范围放宽到 `DBAPIError`：写超长的 bed_no 抛的 `DataError` 也是它的子类，
    那种错重试五次只是把一个一秒就能看见的 bug 拖成五分钟的静默等待——
    这一条是本档自己踩出来的。
    """
    from sqlalchemy.exc import OperationalError

    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except OperationalError as exc:  # noqa: PERF203 - 重试语义即在此
            last = exc
            if attempt == attempts - 1:
                break
            time.sleep(wait)
    raise AssertionError(f"共享库连续 {attempts} 次不可用，放弃（最后一次：{last!r}）")


def _race_on_pg(worker, times: int):
    """Barrier 真并发（写法同 tests/test_postgres_real.py::_race_on_pg）。

    只起线程不够——线程创建有先后，前一个常常已提交完了后一个才开始读，
    窗口根本没打开。等待点全部带 timeout：会阻塞的回归测试不是回归测试。
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


def _seed(Session, tag: str, extra_beds: int = 0):
    """一套自带随机后缀的最小场景：机构 / 病区 / 床 / 患者 / 医师 / 在院记录。

    共享库里别的用例也在写，所有名字都带 tag，断言只认自己造出来的 id。
    `tag` 取 8 位十六进制：`beds.bed_no` 只有 `String(16)`，再长就是 DataError。
    """
    from app.models import Admission, Bed, Organization, Patient, User, Ward

    with Session() as db:
        org = Organization(name=f"PG住院并发院-{tag}", org_type="lead_hospital", level="county")
        doctor = User(username=f"pg_ip_doc_{tag}", password_hash="x", full_name="并发医生")
        patient = Patient(name=f"PG住院患者-{tag}", id_card=f"3302811990{tag}",
                          gender="男", birth_date="1981-01-01", ehc_no=f"PG-EHC-IP-{tag}")
        db.add_all([org, doctor, patient])
        db.flush()
        ward = Ward(org_id=org.id, name=f"PG并发病区-{tag}")
        db.add(ward)
        db.flush()
        beds = [Bed(ward_id=ward.id, bed_no=f"B{tag}{i}", status="free")
                for i in range(1 + extra_beds)]
        db.add_all(beds)
        db.flush()
        beds[0].status = "occupied"
        admission = Admission(patient_id=patient.id, org_id=org.id, ward_id=ward.id,
                              bed_id=beds[0].id, created_by=doctor.id, status="admitted")
        db.add(admission)
        db.commit()
        return {
            "org_id": org.id, "ward_id": ward.id, "patient_id": patient.id,
            "admission_id": admission.id, "bed_ids": [b.id for b in beds],
        }


# ================================================================ 长期医嘱唯一


def test_长期医嘱八路并发恰一条201其余全是同一句409(pg_engine):
    """`inpatient.create_order` 的 PG 直测：八路同时开同一条长期医嘱。

    修复前没有任何查重，八路八条一模一样的在执行长期医嘱——两条 MAR 行、
    两次给药，最后要主管医师回头人工仲裁停掉一条。这里连**预检 + 兜底**一起跑：
    读到赢家已提交的那几路被预检挡下，卡在窗口里的那几路撞索引，
    两种抢输者拿到的 409 必须一模一样。
    """
    from fastapi import HTTPException
    from sqlalchemy.orm import sessionmaker

    from app.concurrency import insert_or_conflict
    from app.models import InpatientOrder

    Session = sessionmaker(bind=pg_engine)
    tag = uuid.uuid4().hex[:8]
    seeded = _retrying(lambda: _seed(Session, tag))
    aid = seeded["admission_id"]

    def worker(_i):
        with Session() as db:
            duplicate = (
                db.query(InpatientOrder.id)
                .filter(
                    InpatientOrder.admission_id == aid,
                    InpatientOrder.order_type == "long",
                    InpatientOrder.status == "active",
                    InpatientOrder.content == LONG_CONTENT,
                )
                .first()
            )
            if duplicate:
                return 409, DUPLICATE_DETAIL
            order = InpatientOrder(admission_id=aid, order_type="long", content=LONG_CONTENT,
                                   created_by_name="并发医生")
            try:
                insert_or_conflict(db, order, DUPLICATE_DETAIL)
            except HTTPException as exc:
                return exc.status_code, exc.detail
            return 201, None

    results, errors = _race_on_pg(worker, times=8)
    assert not errors, f"并发开医嘱不该抛错（500 就是没兜住）：{errors}"
    assert results.count((201, None)) == 1, f"只该有一路开成，实际：{sorted(map(str, results))}"
    assert [r for r in results if r != (201, None)] == [(409, DUPLICATE_DETAIL)] * 7, (
        f"抢输的七路必须与顺序重复拿到同一句 409：{sorted(map(str, results))}"
    )

    with Session() as db:
        assert db.query(InpatientOrder).filter(InpatientOrder.admission_id == aid).count() == 1


def test_临时医嘱八路并发全部落库(pg_engine):
    """对照：索引范围写错成"不分 order_type"时，这条会红——临时医嘱按次开立。"""
    from sqlalchemy.orm import sessionmaker

    from app.models import InpatientOrder

    Session = sessionmaker(bind=pg_engine)
    tag = uuid.uuid4().hex[:8]
    aid = _retrying(lambda: _seed(Session, tag))["admission_id"]

    def worker(_i):
        with Session() as db:
            db.add(InpatientOrder(admission_id=aid, order_type="temp", content="换药一次",
                                  created_by_name="并发医生"))
            db.commit()
            return True

    results, errors = _race_on_pg(worker, times=8)
    assert not errors, f"临时医嘱不该被索引误伤：{errors}"
    assert results == [True] * 8
    with Session() as db:
        assert db.query(InpatientOrder).filter(InpatientOrder.admission_id == aid).count() == 8


def test_停用后再八路并发只再落一条(pg_engine):
    """对照：唯一性只锁"执行中"这一态——停用后重开合法，但仍只许重开一条。"""
    from fastapi import HTTPException
    from sqlalchemy import update
    from sqlalchemy.orm import sessionmaker

    from app.concurrency import insert_or_conflict
    from app.models import InpatientOrder, utcnow

    Session = sessionmaker(bind=pg_engine)
    tag = uuid.uuid4().hex[:8]
    aid = _retrying(lambda: _seed(Session, tag))["admission_id"]

    with Session() as db:
        db.add(InpatientOrder(admission_id=aid, order_type="long", content=LONG_CONTENT,
                              created_by_name="并发医生"))
        db.commit()
        db.execute(
            update(InpatientOrder)
            .where(InpatientOrder.admission_id == aid)
            .values(status="stopped", stopped_at=utcnow())
        )
        db.commit()

    def worker(_i):
        with Session() as db:
            order = InpatientOrder(admission_id=aid, order_type="long", content=LONG_CONTENT,
                                   created_by_name="并发医生")
            try:
                insert_or_conflict(db, order, DUPLICATE_DETAIL)
            except HTTPException as exc:
                return exc.status_code, exc.detail
            return 201, None

    results, errors = _race_on_pg(worker, times=8)
    assert not errors, f"停用后重开不该抛错：{errors}"
    assert results.count((201, None)) == 1, f"重开也只许一条：{sorted(map(str, results))}"
    with Session() as db:
        rows = db.query(InpatientOrder).filter(InpatientOrder.admission_id == aid).all()
        assert len(rows) == 2, "一条停用 + 一条新开"
        assert sorted(o.status for o in rows) == ["active", "stopped"]


# ================================================================ 出院闸门


def test_出院条件更新八路并发恰一路出院且随访恰一条(pg_engine):
    """`inpatient._mark_discharged` 的 PG 直测（连派生随访一起跑，形状同处理器）。

    修复前八路都读到 admitted、八路都赋值提交：床释放八次、出院随访八条、
    ADMISSION_DISCHARGED 发布八次、`discharged_at` 以最后提交的为准。
    条件压进 WHERE 之后，行锁 + EvalPlanQual 让后到的七路按赢家提交后的状态
    重算条件，rowcount=0 → 回滚 → 与顺序重复完全一致的 409。
    """
    from sqlalchemy.orm import sessionmaker

    from app.models import Admission, FollowupTask, utcnow
    from app.routers.followups import DISCHARGE_FOLLOWUP_DAYS, create_task
    from app.routers.inpatient import _mark_discharged

    Session = sessionmaker(bind=pg_engine)
    tag = uuid.uuid4().hex[:8]
    seeded = _retrying(lambda: _seed(Session, tag))
    aid, pid, oid = seeded["admission_id"], seeded["patient_id"], seeded["org_id"]

    def worker(_i):
        with Session() as db:
            now = utcnow()
            if not _mark_discharged(db, aid, now):
                db.rollback()
                return 409, "该患者已出院"
            create_task(db, patient_id=pid, org_id=oid, category="discharge", source_id=aid,
                        title="出院随访：PG 并发", due_days=DISCHARGE_FOLLOWUP_DAYS)
            db.commit()
            return 200, None

    results, errors = _race_on_pg(worker, times=8)
    assert not errors, f"出院闸门并发下不该抛错：{errors}"
    assert results.count((200, None)) == 1, f"一条住院只能出院一次：{sorted(map(str, results))}"
    assert [r for r in results if r != (200, None)] == [(409, "该患者已出院")] * 7

    with Session() as db:
        row = db.get(Admission, aid)
        assert row is not None and row.status == "discharged" and row.discharged_at is not None
        assert db.query(FollowupTask).filter(
            FollowupTask.category == "discharge", FollowupTask.source_id == aid
        ).count() == 1, "出院随访恰一条——修复前这里是 8 条"


# ================================================================ 转床比较交换


def test_转床比较交换八路并发恰一路换成且不漏占床(pg_engine):
    """八路同时把同一位患者转到八张**不同**的空床上。

    修复前八张目标床全被占上、`bed_id` 只留最后写的那个，另外七张永远占着
    且没有任何住院记录挂在上面——只能人工改库。比较交换（`WHERE status='admitted'
    AND bed_id = 我读到的那张`）之后，抢输的七路 rowcount=0，占床根本不会执行。
    不变量：全病区恰一张占用床，且它就是住院记录当前那张。
    """
    from typing import cast

    from sqlalchemy import update
    from sqlalchemy.engine import CursorResult
    from sqlalchemy.orm import sessionmaker

    from app.models import Admission, Bed

    Session = sessionmaker(bind=pg_engine)
    tag = uuid.uuid4().hex[:8]
    seeded = _retrying(lambda: _seed(Session, tag, extra_beds=8))
    aid, wid = seeded["admission_id"], seeded["ward_id"]
    from_bed, targets = seeded["bed_ids"][0], seeded["bed_ids"][1:]

    def worker(i):
        with Session() as db:
            moved = cast(CursorResult, db.execute(
                update(Admission)
                .where(Admission.id == aid, Admission.status == "admitted",
                       Admission.bed_id == from_bed)
                .values(ward_id=wid, bed_id=targets[i])
                .execution_options(synchronize_session=False)
            ))
            if not moved.rowcount:
                db.rollback()
                return False
            db.execute(update(Bed).where(Bed.id == targets[i]).values(status="occupied"))
            db.execute(update(Bed).where(Bed.id == from_bed).values(status="free"))
            db.commit()
            return True

    results, errors = _race_on_pg(worker, times=8)
    assert not errors, f"比较交换并发下不该抛错（PG 上死锁会以 500 现形）：{errors}"
    assert results.count(True) == 1, f"只该有一路换成，实际 {results.count(True)} 路"

    with Session() as db:
        admission = db.get(Admission, aid)
        assert admission is not None and admission.bed_id in targets
        occupied = [b.id for b in db.query(Bed).filter(Bed.ward_id == wid).all()
                    if b.status == "occupied"]
        assert occupied == [admission.bed_id], (
            f"全病区该恰一张占用床且等于住院记录当前那张，实际占着 {occupied}"
        )
