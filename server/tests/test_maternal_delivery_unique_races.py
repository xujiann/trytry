"""真 PostgreSQL 上的分娩登记并发（P1-30 · `uq_delivery_record`）。

为什么必须在 PG 上跑：SQLite 的**库级写锁**把"两路同时插同一个 record_id"这件事
在语句层就压平了——赢家没提交完，输家根本开始不了，`IntegrityError` 这条兜底路径
在 SQLite 上走不到。PG 逐语句取快照、并发事务互不可见，输家的 INSERT 要等赢家
**commit** 之后才在唯一索引上撞出 unique_violation，那正是 `insert_or_conflict`
存在的理由，也正是接口层"先查有没有分娩记录"这一步守不住的窗口。

跑法（与 `test_postgres_real.py` 同一开关）：

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:55432/medplat_test
    python -m pytest tests/test_maternal_delivery_unique_races.py -q

不变量：八路并发**恰一路 201**、其余七路拿到与顺序重复请求逐字相同的 409、
没有任何 `IntegrityError` 漏给调用方、库里 `record_id` 那一档**恰一行**分娩记录，
且档案状态被赢家置成 `delivered` 一次（输家回滚时连它一起退掉）。

本档**不清库**（这个测试库与他人共用）：只按需把迁移升到 heads，前置数据一律带
随机后缀自建，断言只看自己那本册子。
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
]

SERVER_DIR = Path(__file__).resolve().parents[1]

DUPLICATE_DETAIL = "该档案已有分娩记录"

#: 撞上别的进程正在升级/写同一张表时的等待与重试（测试库共用，不独占）。
_RETRY_TIMES = 5
_RETRY_WAIT_SECONDS = 60


def _has_unique_index(engine) -> bool:
    from sqlalchemy import inspect

    inspector = inspect(engine)
    if "delivery_records" not in inspector.get_table_names():
        return False
    return "uq_delivery_record" in {i["name"] for i in inspector.get_indexes("delivery_records")}


@pytest.fixture(scope="module")
def pg_engine():
    """连上测试库，并**只在缺索引时**把迁移升到 heads。

    与 `test_postgres_real.py` 的 `pg_engine` 刻意不同：那条要证明迁移链能白手起家，
    所以先 `DROP SCHEMA`；这里要证明的是并发行为，清库只会把同时在用这个库的其他
    用例（和其他人）一起掀翻。`alembic upgrade heads` 本身幂等，已升过就是空操作。
    """
    from sqlalchemy import create_engine

    engine = create_engine(PG_URL)
    for attempt in range(_RETRY_TIMES):
        if _has_unique_index(engine):
            break
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_DIR,
            env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 or _has_unique_index(engine):
            break
        assert attempt < _RETRY_TIMES - 1, (
            f"迁移在 PG 上失败，且 uq_delivery_record 仍未建上：\n{result.stderr[-2000:]}"
        )
        time.sleep(_RETRY_WAIT_SECONDS)  # 多半是别的进程正在升同一个库，等它升完
    assert _has_unique_index(engine), (
        "测试库上没有 uq_delivery_record——迁移 b9c8d7e6f5a4 没跑到，并发闸门等于没有"
    )
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def maternal_record(pg_engine):
    """一家机构 + 一位女性患者 + 一本还没登记分娩的册子（名字全带随机后缀，不占共用键）。"""
    from sqlalchemy.orm import sessionmaker

    from app.models import MaternalRecord, Organization, Patient

    tag = uuid.uuid4().hex[:8]
    Session = sessionmaker(bind=pg_engine)
    with Session() as db:
        org = Organization(name=f"PG分娩并发妇幼院-{tag}", org_type="township", level="township")
        patient = Patient(
            name=f"PG分娩并发孕产妇-{tag}",
            # 证件号/健康卡号都是唯一列：共用测试库里必须各建各的
            id_card=f"3302811995{uuid.uuid4().int % 10**8:08d}",
            gender="女", birth_date="1995-02-02", ehc_no=f"PG-EHC-{tag}",
        )
        db.add_all([org, patient])
        db.flush()
        record = MaternalRecord(patient_id=patient.id, lmp="2026-01-01", edc="2026-10-08")
        db.add(record)
        db.commit()
        return {"org_id": org.id, "record_id": record.id}


def _race(worker, times):
    """Barrier 真并发（写法同 `test_postgres_real._race_on_pg`）。

    只起线程不够——线程创建有先后，前一个常常已提交完了后一个才开始，窗口根本
    没打开。等待点全部带 timeout：会阻塞的回归测试不是回归测试。
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
        t.join(timeout=60)
    return results, errors


def _register_delivery(Session, record_id: int, org_id: int, delivery_date: str):
    """复刻 `routers/maternal.add_delivery` 的写序：先翻档案状态，再插分娩记录。

    返回 (status_code, detail)——与接口层给调用方的东西一一对应。
    """
    from fastapi import HTTPException

    from app.concurrency import insert_or_conflict
    from app.models import DeliveryRecord, MaternalRecord

    with Session() as db:
        record = db.get(MaternalRecord, record_id)
        record.status = "delivered"
        delivery = DeliveryRecord(
            record_id=record_id, org_id=org_id, delivery_date=delivery_date,
            delivery_mode="natural", newborn_count=1, outcome="",
        )
        try:
            insert_or_conflict(db, delivery, DUPLICATE_DETAIL)
        except HTTPException as exc:
            return exc.status_code, exc.detail
        return 201, None


# 迁移可能要现升（首次跑），加上重试等待，给足预算；看门狗默认 120 秒会误判。
@pytest.mark.timeout(600)
def test_八路并发登记同一档案分娩_恰一路成功其余拿到同一句409(pg_engine, maternal_record):
    """接口层"先查有没有分娩记录"是 check-then-act：八路同时查不到就八路都去插。

    闸门是 `uq_delivery_record`。输家在 PG 上要等赢家 commit 才撞上它，
    `insert_or_conflict` 回滚后给出的 409 与顺序重复请求**逐字相同**——对调用方而言
    "并发撞车"与"本来就重复"没有区别。回滚同时退掉输家那句 `status='delivered'`，
    所以档案状态是赢家写的那一次。
    """
    from sqlalchemy.orm import sessionmaker

    from app.models import DeliveryRecord, MaternalRecord

    Session = sessionmaker(bind=pg_engine)
    rid, org_id = maternal_record["record_id"], maternal_record["org_id"]

    results, errors = _race(
        lambda i: _register_delivery(Session, rid, org_id, "2026-10-05"), times=8
    )
    assert not errors, f"并发分娩登记不该把 IntegrityError 漏给调用方：{errors}"
    assert len(results) == 8, f"八路都要有结论：{results}"

    codes = sorted(code for code, _ in results)
    assert codes == [201] + [409] * 7, f"必须恰一路登记成功：{results}"
    assert {detail for code, detail in results if code == 409} == {DUPLICATE_DETAIL}, (
        f"抢输的一路必须拿到与顺序重复请求逐字相同的 409 文案：{results}"
    )

    with Session() as db:
        rows = db.query(DeliveryRecord).filter(DeliveryRecord.record_id == rid).all()
        assert len(rows) == 1, f"一本册子写出了 {len(rows)} 条分娩记录——查询侧 .first() 无序，此后取到哪条全看运气"
        assert db.get(MaternalRecord, rid).status == "delivered"

    # 尘埃落定后再来一次（接口层的预检在这里被刻意跳过，直接撞索引）仍是同一句 409
    assert _register_delivery(Session, rid, org_id, "2026-10-06") == (409, DUPLICATE_DETAIL)


@pytest.mark.timeout(600)
def test_另一本册子并发登记不受影响(pg_engine):
    """唯一性只按"一本册子"划界：键写宽了（比如误按机构或分娩日期）这条会红。

    与上一条同机构、同分娩日期，只换一本册子——八路里仍然恰一路成功。
    """
    from sqlalchemy.orm import sessionmaker

    from app.models import DeliveryRecord, MaternalRecord, Organization, Patient

    tag = uuid.uuid4().hex[:8]
    Session = sessionmaker(bind=pg_engine)
    with Session() as db:
        org = Organization(name=f"PG分娩并发妇幼院乙-{tag}", org_type="township", level="township")
        patient = Patient(
            name=f"PG分娩并发孕产妇乙-{tag}",
            id_card=f"3302811995{uuid.uuid4().int % 10**8:08d}",
            gender="女", birth_date="1995-03-03", ehc_no=f"PG-EHC-乙{tag}",
        )
        db.add_all([org, patient])
        db.flush()
        record = MaternalRecord(patient_id=patient.id, lmp="2026-02-01", edc="2026-11-08")
        db.add(record)
        db.commit()
        rid, org_id = record.id, org.id

    results, errors = _race(
        lambda i: _register_delivery(Session, rid, org_id, "2026-10-05"), times=8
    )
    assert not errors, f"并发分娩登记不该把 IntegrityError 漏给调用方：{errors}"
    assert sorted(code for code, _ in results) == [201] + [409] * 7, results
    with Session() as db:
        assert db.query(DeliveryRecord).filter(DeliveryRecord.record_id == rid).count() == 1
        assert db.get(MaternalRecord, rid).status == "delivered"
