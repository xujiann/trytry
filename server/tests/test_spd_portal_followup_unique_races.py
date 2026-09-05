"""真 PostgreSQL 上的并发证明：慢专病居民端 + 智能随访端的五条不变式（P1-30）。

默认跳过（`MEDPLAT_PG_TEST_URL` 未设时），开启方式与 `test_postgres_real.py` 一致：

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:55432/medplat_test
    python -m pytest tests/test_spd_portal_followup_unique_races.py -q

**为什么非要真 PG**：SQLite 的库级写锁把"判定"与"写入"之间的窗口一并锁掉了，
八路线程在 SQLite 上大概率自己就串行了——把索引拆掉、把条件 UPDATE 改回
check-then-act，SQLite 档照样绿。PG 逐语句取快照、并发事务互不可见，窗口是
真实打开的：修复前八路各自查不到就各自插，八条待受理申请 / 八条开放会话 /
八条待呼叫任务 / 八条异常处置任务全部落库，且**没有一个请求报错**。
同伴用例（`test_spd_portal_followup_unique.py`）守顺序行为与防拆卸静态钉，
本档守的是"真并发下不变量仍然成立"。

本档**不 DROP SCHEMA**：这个库与别的用例、别的同事共用，每条用例只往里加自己
tag 前缀的数据；撞上别人的锁就退避重试（重试前先把本轮自己的数据清干净），
而不是跳过——跳过等于没测。
"""
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError, OperationalError

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"
    ),
]

#: 呼叫任务抢输时的 409 文案，与 `followup.create_call_task` 里的字面量必须一字不差
CALL_TASK_CONFLICT = "该患者对同一对象已有待呼叫任务，请先回写其结果（未接通/取消）后再发起"

REQUIRED_TABLES = (
    "patients", "resident_accounts", "spd_service_applies", "spd_consults",
    "spd_consult_messages", "spd_call_tasks", "spd_followup_records",
    "spd_qc_samples", "spd_tasks",
)
REQUIRED_INDEXES = (
    ("spd_service_applies", "uq_spd_apply_pending_patient_program"),
    ("spd_consults", "uq_spd_consult_open_patient_program"),
    ("spd_call_tasks", "uq_spd_call_task_pending_ref"),
    ("spd_qc_samples", "uq_spd_qc_sample_record_batch"),
)


SERVER_DIR = Path(__file__).resolve().parents[1]


def _schema_ready(engine) -> bool:
    """表和索引都在才算就绪。**空库要答"不在"而不是抛 NoSuchTableError**——
    CI 的集成档跑在全新库上，那样这一档会在读索引那步直接红而不是把库补齐。"""
    from sqlalchemy import inspect

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if any(t not in tables for t in REQUIRED_TABLES):
        return False
    for table, index_name in REQUIRED_INDEXES:
        if index_name not in {i["name"] for i in inspector.get_indexes(table)}:
            return False
    return True


@pytest.fixture(scope="module")
def pg_engine():
    """连到测试库，**只在表/索引不齐时**跑一次幂等的 `alembic upgrade heads`。

    刻意不 `DROP SCHEMA`：这个库可能与别的用例共用，清库会把别人连锅端掉
    （与 `test_postgres_real.py` 那条"证明迁移链能白手起家"的用例分工不同）。
    最终仍不齐就明确失败——静默跳过会让"并发证明"变成一句空话。
    """
    from sqlalchemy import create_engine

    engine = create_engine(PG_URL)
    for attempt in range(5):
        if _schema_ready(engine):
            break
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_DIR,
            env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 or _schema_ready(engine):
            break
        assert attempt < 4, f"在 {PG_URL} 上跑迁移五次都没成功：\n{result.stderr[-2000:]}"
        time.sleep(60)
    assert _schema_ready(engine), (
        f"测试库缺表或缺索引（要 {list(REQUIRED_TABLES)} 与 "
        f"{[n for _t, n in REQUIRED_INDEXES]}）：迁移没跑到 b9c8d7e6f5a4/f4e3d2c1b0a9，"
        "或探到存量重复而跳过了建索引（见迁移 docstring 的人工处置 SQL）"
    )
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def session_factory(pg_engine):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=pg_engine)


@pytest.fixture(scope="module")
def tag():
    """本次运行的数据前缀：库是共用的，谁都不许占用固定名字。"""
    return uuid4().hex[:8]


def _transient(exc: BaseException) -> bool:
    """是不是"别人的锁/连接抖了"这类可重试的错误。

    与本档要判红的东西必须分开：`IntegrityError` 漏成 500、409 文案不对、
    多写出一行——那些重试多少次都还在，重试只会把缺陷藏起来。
    """
    if not isinstance(exc, (OperationalError, DBAPIError)):
        return False
    message = str(exc).lower()
    return any(word in message for word in ("lock", "deadlock", "timeout", "connection"))


def _retry_on_lock(action, attempts: int = 5, wait_seconds: int = 60):
    """撞锁就退避重试——共用库上别人的事务可能正握着锁，跳过等于不测。"""
    for attempt in range(attempts):
        try:
            return action()
        except (OperationalError, DBAPIError) as exc:
            if not _transient(exc) or attempt == attempts - 1:
                raise
            time.sleep(wait_seconds)
    raise AssertionError("unreachable")  # pragma: no cover


def _race(worker, times: int):
    """Barrier 真并发（形状同 `test_postgres_real._race_on_pg`）。

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
        t.join(timeout=60)
    return results, errors


def _race_with_retry(worker, times: int, reset, attempts: int = 5, wait_seconds: int = 60):
    """跑一轮真并发；只有撞锁类异常才重试，且重试前先把本轮写进去的行清干净
    （不清的话第二轮的"恰一路成功"会被上一轮的赢家顶掉，红得莫名其妙）。"""
    for attempt in range(attempts):
        results, errors = _race(worker, times)
        blocked = [exc for exc in errors if _transient(exc)]
        if not blocked:
            return results, errors
        if attempt == attempts - 1:
            raise blocked[0]
        reset()
        time.sleep(wait_seconds)
    raise AssertionError("unreachable")  # pragma: no cover


def _make_patient(session_factory, tag, seq: int) -> int:
    """建一名本档专用的患者（证件号/电子健康卡号带 tag，绝不与他人撞）。"""
    from app.models import Patient

    def create():
        with session_factory() as db:
            patient = Patient(
                name=f"PG并发{tag}{seq}",
                id_card=f"3302{uuid4().int % 10**14:014d}",
                gender="男", birth_date="1990-01-01",
                ehc_no=f"PG-EHC-{tag}-{seq}",
            )
            db.add(patient)
            db.commit()
            return patient.id

    return _retry_on_lock(create)


def _resident_account(session_factory, patient_id: int) -> int:
    from app.models import ResidentAccount

    def create():
        with session_factory() as db:
            account = ResidentAccount(patient_id=patient_id, status="active")
            db.add(account)
            db.commit()
            return account.id

    return _retry_on_lock(create)


def _delete_where(session_factory, model, *criteria) -> None:
    with session_factory() as db:
        db.query(model).filter(*criteria).delete(synchronize_session=False)
        db.commit()


# ================================================================ 服务申请：抢输者 409


@pytest.mark.timeout(600)
def test_并发提交服务申请_恰一条待受理其余同一句409(session_factory, tag):
    """`portal.apply_service` 的 PG 直测：八路同时提交同病种申请。

    修复前：八路的"有没有待受理"预检都在对方提交前跑完，八条 pending 全部落库，
    团队收件箱里多出七条要人手工拒绝的申请。修复后：唯一索引让后到的七路的
    INSERT 抛 unique_violation，`insert_or_conflict` 回滚并给出与顺序请求
    **一字不差**的 409——对调用方来说"并发撞车"与"本来就重复"没有区别。
    """
    from app.models import ResidentAccount
    import app.spd.routers.portal as portal_mod
    from app.spd.models import SpdServiceApply

    patient_id = _make_patient(session_factory, tag, 1)
    account_id = _resident_account(session_factory, patient_id)
    program_code = f"pg_{tag}"

    def worker(_i):
        with session_factory() as db:
            account = db.get(ResidentAccount, account_id)
            try:
                portal_mod.apply_service(
                    portal_mod.ApplyIn(program_code=program_code), account=account, db=db
                )
                return 201, ""
            except HTTPException as exc:
                return exc.status_code, exc.detail

    def reset():
        _delete_where(session_factory, SpdServiceApply,
                      SpdServiceApply.patient_id == patient_id)

    results, errors = _race_with_retry(worker, 8, reset)
    assert not errors, f"并发提交不该把 IntegrityError 漏成 500：{errors}"
    assert results.count((201, "")) == 1, f"恰一条待受理，实际 {results}"
    assert results.count((409, "该病种已有待受理的服务申请")) == 7, results

    with session_factory() as db:
        rows = db.query(SpdServiceApply).filter(
            SpdServiceApply.patient_id == patient_id,
            SpdServiceApply.program_code == program_code,
        ).all()
        assert len(rows) == 1, f"抢输的七路一行都不许留下，实际 {len(rows)} 行"
        assert rows[0].status == "pending"
        rows[0].status = "rejected"  # 团队拒绝后再申请是合法的
        db.commit()

    results, errors = _race_with_retry(worker, 8, reset)
    assert not errors
    assert results.count((201, "")) == 1, "被拒后重来一轮仍是恰一条"
    with session_factory() as db:
        total = db.query(SpdServiceApply).filter(
            SpdServiceApply.patient_id == patient_id,
            SpdServiceApply.program_code == program_code,
        ).count()
    assert total == 2, "部分索引只锁 pending：一条已拒 + 一条待受理"


# ================================================================ 在线咨询：抢输者复用


@pytest.mark.timeout(600)
def test_并发发起在线咨询_八路复用同一会话且八条消息都在(session_factory, tag):
    """`portal.start_consult` 的 PG 直测：八路同时发起同病种咨询。

    这条的正确语义与别的表相反——顺序第二次请求本来就是"复用那条开放会话"，
    抢输者必须同样拿到 201 与**同一个 consult_id**，消息照样落进去；改成 409
    就等于把居民那条消息丢掉。修复前八路各建一条 open，消息分叉在八条线程里，
    医生列表与工作台各显八条。
    """
    from app.models import ResidentAccount
    import app.spd.routers.portal as portal_mod
    from app.spd.models import SpdConsult, SpdConsultMessage

    patient_id = _make_patient(session_factory, tag, 2)
    account_id = _resident_account(session_factory, patient_id)
    program_code = f"pg_{tag}"

    def worker(i):
        with session_factory() as db:
            account = db.get(ResidentAccount, account_id)
            out = portal_mod.start_consult(
                portal_mod.ConsultIn(program_code=program_code, content=f"第{i}条"),
                account=account, db=db,
            )
            return out["consult_id"], out["status"]

    def reset():
        with session_factory() as db:
            ids = [
                c.id for c in db.query(SpdConsult).filter(
                    SpdConsult.patient_id == patient_id
                )
            ]
            db.query(SpdConsultMessage).filter(
                SpdConsultMessage.consult_id.in_(ids or [0])
            ).delete(synchronize_session=False)
            db.query(SpdConsult).filter(SpdConsult.patient_id == patient_id).delete(
                synchronize_session=False
            )
            db.commit()

    results, errors = _race_with_retry(worker, 8, reset)
    assert not errors, f"抢输的一路必须复用而不是报错：{errors}"
    assert len(results) == 8
    assert len({cid for cid, _ in results}) == 1, f"八路必须落在同一条会话上：{results}"
    assert {status for _, status in results} == {"open"}

    consult_id = results[0][0]
    with session_factory() as db:
        open_rows = db.query(SpdConsult).filter(
            SpdConsult.patient_id == patient_id,
            SpdConsult.program_code == program_code,
            SpdConsult.status == "open",
        ).count()
        messages = db.query(SpdConsultMessage).filter(
            SpdConsultMessage.consult_id == consult_id
        ).count()
    assert open_rows == 1, f"同病种开放会话恰一条，实际 {open_rows} 条"
    assert messages == 8, f"八条消息一条都不许丢，实际 {messages} 条"


# ================================================================ 呼叫任务：抢输者 409


@pytest.mark.timeout(600)
def test_并发发起呼叫任务_恰一条待呼叫其余同一句409(session_factory, tag):
    """`followup.create_call_task` 落库那一步（`insert_or_conflict`）的 PG 直测。

    修复前双击「转呼叫」或两名坐席同时发起会建出两条 pending：网关被推两次
    （患者被拨两遍）、人工队列同一条随访出现两行，先接通后另一条永远挂着等人
    手工取消。这里绕开 `assert_patient_visible`（调阅留痕走独立会话，与本档要证
    的东西无关），直接压防线本体；文案由同伴用例的 HTTP 断言与静态钉钉住。
    """
    from app.concurrency import insert_or_conflict
    from app.spd.models import SpdCallTask, SpdFollowupRecord

    patient_id = _make_patient(session_factory, tag, 3)

    def seed_record():
        with session_factory() as db:
            record = SpdFollowupRecord(
                patient_id=patient_id, program_code=f"pg_{tag}",
                planned_at="2026-09-01", status="planned",
            )
            db.add(record)
            db.commit()
            return record.id

    record_id = _retry_on_lock(seed_record)

    def worker(_i):
        with session_factory() as db:
            task = SpdCallTask(
                patient_id=patient_id, phone="13900000000", ref_type="followup",
                ref_id=record_id, status="pending",
            )
            try:
                insert_or_conflict(db, task, CALL_TASK_CONFLICT)
                return 201, ""
            except HTTPException as exc:
                return exc.status_code, exc.detail

    def reset():
        _delete_where(session_factory, SpdCallTask, SpdCallTask.patient_id == patient_id)

    results, errors = _race_with_retry(worker, 8, reset)
    assert not errors, f"并发发起不该漏出 IntegrityError：{errors}"
    assert results.count((201, "")) == 1, f"恰一条待呼叫，实际 {results}"
    assert results.count((409, CALL_TASK_CONFLICT)) == 7, results

    with session_factory() as db:
        rows = db.query(SpdCallTask).filter(
            SpdCallTask.patient_id == patient_id,
            SpdCallTask.ref_type == "followup",
            SpdCallTask.ref_id == record_id,
        ).all()
        assert len(rows) == 1, f"抢输者不许留下半行，实际 {len(rows)} 行"
        rows[0].status = "failed"  # 未接通后重新发起是合法的
        db.commit()

    results, errors = _race_with_retry(worker, 8, reset)
    assert not errors
    assert results.count((201, "")) == 1, "回写未接通后重来一轮仍是恰一条"
    with session_factory() as db:
        total = db.query(SpdCallTask).filter(
            SpdCallTask.patient_id == patient_id,
            SpdCallTask.ref_type == "followup",
            SpdCallTask.ref_id == record_id,
        ).count()
    assert total == 2, "部分索引只锁 pending：一条未接通 + 一条待呼叫"


# ================================================================ 抽查计划：同批次只抽一次


@pytest.mark.timeout(600)
def test_并发生成抽查计划_同一批次一条随访只抽一次(session_factory, tag):
    """`followup.plan_qc` 的抽样循环（`insert_if_absent`）的 PG 直测。

    修复前两个质控员同时点"生成抽查计划"，两路都读到空集、都插一遍，同一条随访
    在同一批次里被抽两次，质控合格率的分母直接翻倍。修复后抢输的行被静默跳过
    （不计进 `created`），与顺序重跑一次的语义完全一致：八路 `created` 之和恰等于
    池子大小，且整批不因一行撞车而回滚。
    """
    from app.concurrency import insert_if_absent
    from app.spd.models import SpdFollowupRecord, SpdQcSample

    patient_id = _make_patient(session_factory, tag, 4)
    batch = f"QC{tag}"

    def seed_records():
        with session_factory() as db:
            records = [
                SpdFollowupRecord(
                    patient_id=patient_id, program_code=f"pg_{tag}",
                    planned_at="2026-09-01", status="done", dept="内科",
                )
                for _ in range(5)
            ]
            db.add_all(records)
            db.commit()
            return sorted((r.id for r in records), reverse=True)

    record_ids = _retry_on_lock(seed_records)

    def worker(_i):
        # 与 plan_qc 同一形状：先读已抽过的（快路径），再逐行 insert_if_absent。
        # 顺序必须与 plan_qc 一致（id 降序）：八路按同一顺序取键锁才不会互等成死锁。
        with session_factory() as db:
            existing = {
                rid for (rid,) in db.query(SpdQcSample.record_id)
                .filter(SpdQcSample.batch == batch).all()
            }
            created = 0
            for record_id in record_ids:
                if record_id in existing:
                    continue
                if insert_if_absent(
                    db, SpdQcSample(record_id=record_id, batch=batch, dept="内科")
                ):
                    created += 1
            db.commit()
            return created

    def reset():
        _delete_where(session_factory, SpdQcSample, SpdQcSample.batch == batch)

    results, errors = _race_with_retry(worker, 8, reset)
    assert not errors, f"整批抽样不该因一行撞车而全批回滚：{errors}"
    assert sum(results) == len(record_ids), (
        f"八路合计只应抽出 {len(record_ids)} 条，实际 {sum(results)}（{results}）"
    )
    with session_factory() as db:
        rows = db.query(SpdQcSample).filter(SpdQcSample.batch == batch).all()
    assert len(rows) == len(record_ids)
    assert len({r.record_id for r in rows}) == len(rows), "同批次同一条随访被抽了两次"


# ================================================================ 随访办结：父行条件 UPDATE


@pytest.mark.timeout(600)
@pytest.mark.parametrize("seed_status", ["planned", "unreachable"])
def test_并发办结随访_恰一路办到且只派一条处置任务(session_factory, tag, seed_status):
    """`service.close_followup_record` 的 PG 直测（`spd_tasks` 的不变式长在父行上）。

    修复前八路都读到 planned、都把 status 改成 done、都派一条"随访异常处置"，
    多出来的七条待办随后被超期扫描翻成超期，一路挂进督办与考核，要人手工作废。
    判定压进 UPDATE 的 WHERE 之后，行锁 + EvalPlanQual 让后到的七路按赢家提交后的
    状态重算条件，rowcount=0 → 接口层 409『该随访已结束』。

    `unreachable` 这一档钉的是 `allowed_from` 的宽度：失访后补录答案仍要能办结
    （照抄居民端的 `(planned, overdue)` 会把这条合法路径变成 409）。
    """
    from app.spd.models import SpdFollowupRecord, SpdTask
    from app.spd.service import close_followup_record

    patient_id = _make_patient(session_factory, tag, 5 if seed_status == "planned" else 6)
    program_code = f"pg_{tag}"

    def seed_record():
        with session_factory() as db:
            record = SpdFollowupRecord(
                patient_id=patient_id, program_code=program_code,
                planned_at="2026-09-01", status=seed_status,
            )
            db.add(record)
            db.commit()
            return record.id

    record_id = _retry_on_lock(seed_record)

    def worker(_i):
        with session_factory() as db:
            won = close_followup_record(
                db, record_id, "done",
                allowed_from=("planned", "overdue", "unreachable"),
            )
            if won:
                db.add(SpdTask(
                    program_code=program_code, patient_id=patient_id, task_type="report",
                    title="随访异常处置：立即上转评估", status="pending", priority=3,
                    due_date="2026-09-02", source="followup",
                ))
                db.commit()
            else:
                db.rollback()
            return won

    def reset():
        _delete_where(session_factory, SpdTask, SpdTask.patient_id == patient_id)
        with session_factory() as db:
            record = db.get(SpdFollowupRecord, record_id)
            if record is not None:
                record.status = seed_status
                db.commit()

    results, errors = _race_with_retry(worker, 8, reset)
    assert not errors, f"条件更新并发下不该抛错：{errors}"
    assert results.count(True) == 1, f"一条随访只能办结一次，实际 {results.count(True)} 路办到"

    with session_factory() as db:
        record = db.get(SpdFollowupRecord, record_id)
        assert record is not None and record.status == "done"
        tasks = db.query(SpdTask).filter(
            SpdTask.patient_id == patient_id, SpdTask.source == "followup"
        ).count()
    assert tasks == 1, f"一次执行只许派一条处置任务，实际 {tasks} 条"
