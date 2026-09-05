"""专病入组"在管唯一"在**真 PostgreSQL** 下的一赢七输（P1-30）。

为什么必须上真 PG：SQLite 的库级写锁把整个"预检 → 插入"的窗口一并锁掉了，
八路并发在它上面几乎排成串行，`insert_or_conflict` 的兜底路径**一次也走不到**——
SQLite 档（`test_disease_enrollment_concurrency.py`）能证明索引在、能拦住直插，
却证明不了"抢输的那一路拿到的是 409 而不是 500"。PG 逐语句取快照、并发事务
彼此不可见，八路真的同时读到"没有在管记录"，窗口是真实打开的：输家的 INSERT
阻塞在赢家未提交的索引项上，赢家一提交就抛 unique_violation。

不变量（两轮都要成立）：
- 恰一路建组成功，其余七路拿到 409 且**文案与顺序请求逐字相同**；
- 没有任何一路漏出裸 `IntegrityError`（那就是 500，记录还丢了）；
- 库里该键上的**在管**记录恰一行。

第二轮把赢家改成出组再跑一遍：证明部分索引锁的是 `status = 'enrolled'` 而不是
整张表——复发再入组仍然放行，那一键上最终是"一条已出组 + 一条在管"两行。

对照（同一段并发、同一张表、只差一条索引，在一次性临时库上实测）：
建了部分唯一索引 → 1 路建组成功 / 6 路撞索引 / 1 路撞预检，库里**一条**在管记录；
不建 → 7 路全部建组成功、只有 1 路被预检拦下，库里**七条**在管记录。
那七条正是本轮要堵的洞：program_stats 双计、出组只翻掉一条、复发再入组永久被挡。

前置：本档跑在**共享**的 PG 测试库上，故一律用带随机后缀的自建数据，
既不 DROP SCHEMA 也不碰别人的行。开启方式见文件头的 pytestmark。
"""
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from test_postgres_real import _race_on_pg

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"
    ),
    # 共享库上的前置（推迁移、建自己的数据）允许撞锁重试，最坏要等几分钟；
    # 看门狗默认 120 秒是按单元用例定的尺子，按它量这一档必然误判（conftest
    # 的 `_resolve_timeout` 就是为这种情况留的口子）。
    pytest.mark.timeout(600),
]

#: 撞锁/撞并行任务时的重试节奏（共享库，别人可能正在建表或升级）
RETRIES, RETRY_WAIT = 5, 60

SERVER_DIR = Path(__file__).resolve().parents[1]
INDEX_NAME = "uq_disease_enrollment_program_patient_enrolled"
# 接口层预检与并发兜底必须是同一句话（app/routers/disease_programs.py::enroll）
ALREADY_ENROLLED = "该患者已在本专病在管中"
RACERS = 8


def _index_present(engine) -> bool:
    """索引在不在。**空库要答"不在"而不是抛异常**——CI 的集成档跑在全新库上，
    `get_indexes` 对不存在的表直接抛 NoSuchTableError，会让下面那段"缺了就升级"
    的自举逻辑根本走不到，这一档在 CI 上就是红而不是把库补齐。
    """
    from sqlalchemy import inspect

    inspector = inspect(engine)
    if "disease_enrollments" not in set(inspector.get_table_names()):
        return False
    return INDEX_NAME in {i["name"] for i in inspector.get_indexes("disease_enrollments")}


@pytest.fixture(scope="module")
def pg_engine():
    """把迁移链推到 heads，但**不清库**——这个库是与其他并行任务共用的。

    只在索引还没建上时才跑 alembic；升级本身可能与别处的升级/建表撞锁，
    撞了就等一会儿重来，而不是把用例跳过——跳过的并发用例等于没有并发用例。
    """
    from sqlalchemy import create_engine

    # 池子按参赛路数开：默认 pool_size=5，热身建好的八条连接有三条会被当场关掉，
    # 下一轮那三路又要重新握手，窗口就没完全打开（实测撞索引的从 6~7 路掉到 4~5 路）。
    engine = create_engine(PG_URL, pool_size=RACERS, max_overflow=RACERS)
    last = ""
    for attempt in range(RETRIES):
        if _index_present(engine):
            break
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_DIR,
            env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True,
            text=True,
        )
        last = result.stderr[-2000:]
        if result.returncode == 0:
            # 升级跑通了索引还是没有，只能是迁移探到存量重复主动跳过——重试无用，
            # 让下面那句断言把话说明白（处置 SQL 在迁移 docstring 里）。
            break
        if attempt < RETRIES - 1:
            time.sleep(RETRY_WAIT)  # 多半是别处正在升级/建表，等它让开
    assert _index_present(engine), (
        f"PG 上没有 {INDEX_NAME}：迁移没跑通，或迁移探到存量重复把它跳过了。\n{last}"
    )
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def fixture_key(pg_engine):
    """一组只属于本次运行的机构 / 专病目录 / 患者（随机后缀，避免与并行任务撞唯一列）。

    共享库上别的任务可能正好在重建表，撞上就等一会儿重来——**不跳过**：
    跳过的并发用例等于没有并发用例。
    """
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.orm import sessionmaker

    from app.models import DiseaseProgram, Organization, Patient

    Session = sessionmaker(bind=pg_engine)
    for attempt in range(RETRIES):
        tag = uuid.uuid4().hex[:10]
        try:
            with Session() as db:
                org = Organization(name=f"专病并发院-{tag}", org_type="township",
                                   level="township")
                db.add(org)
                db.flush()
                program = DiseaseProgram(
                    code=f"PGRACE-{tag}", name="并发入组验证专病", org_id=org.id,
                    path_nodes=[], active=True,
                )
                # 建档直连不过接口层，健康卡号与证件号都要自带；两列都唯一，故带后缀
                patient = Patient(
                    name=f"并发入组患者-{tag}", id_card=f"PGRACE{tag}0001", gender="男",
                    birth_date="1980-01-01", ehc_no=f"PG-EHC-{tag}",
                )
                db.add_all([program, patient])
                db.commit()
                return {"org_id": org.id, "program_id": program.id,
                        "patient_id": patient.id}
        except DBAPIError:
            if attempt == RETRIES - 1:
                raise
            time.sleep(RETRY_WAIT)
    raise AssertionError("unreachable")  # pragma: no cover


def _enroll_race(pg_engine, key):
    """八路并发跑一遍 `enroll` 的写入路径本体（预检 + insert_or_conflict）。

    刻意不走 HTTP：本进程的 app 引擎早按 SQLite 定型（conftest 固定了
    MEDPLAT_DATABASE_URL），换库要另起进程。这里直接把那两句绑到 pg_engine 上，
    竞争窗口与线上一模一样。

    每一路都说清自己是**被谁**拦下的：`precheck` 是接口层那句预检，`db` 是索引。
    只数 409 的个数不够——冷连接池下八路里只有第一路手里有连接，它建完组提交时
    其余七路还卡在 TCP 握手上，于是七个 409 全是预检给的，**把索引拆掉这条用例
    照样绿**。所以先把连接池热起来（见 `_warm_pool`），再要求至少有一路是撞在
    索引上回来的。
    """
    from fastapi import HTTPException
    from sqlalchemy.orm import sessionmaker

    from app.concurrency import insert_or_conflict
    from app.models import DiseaseEnrollment

    Session = sessionmaker(bind=pg_engine)

    def worker(_index):
        with Session() as db:
            existing = (
                db.query(DiseaseEnrollment)
                .filter(
                    DiseaseEnrollment.program_id == key["program_id"],
                    DiseaseEnrollment.patient_id == key["patient_id"],
                    DiseaseEnrollment.status == "enrolled",
                )
                .first()
            )
            if existing is not None:
                # 预检命中：与并发输家给出的是同一句 409（顺序请求走的就是这条）
                return ("precheck", 409, ALREADY_ENROLLED)
            row = DiseaseEnrollment(
                program_id=key["program_id"], patient_id=key["patient_id"],
                org_id=key["org_id"], enrolled_at="2026-09-05", created_by=None,
            )
            try:
                insert_or_conflict(db, row, ALREADY_ENROLLED)
            except HTTPException as exc:
                return ("db", exc.status_code, exc.detail)
            return ("created", row.id)

    _warm_pool(pg_engine)
    return _race_on_pg(worker, RACERS)


def _warm_pool(pg_engine):
    """先让 RACERS 条连接同时建好并回到池里，竞争窗口才真的打开。

    不热身的话第一路手握唯一一条连接，它提交完了别人才刚连上——测出来的是
    "排队"而不是"并发"。热身本身也走 Barrier：串行热身只会建出一条连接。
    """
    from sqlalchemy import text
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=pg_engine)

    def warm(_index):
        with Session() as db:
            db.execute(text("SELECT 1"))
        return "warm"

    _, errors = _race_on_pg(warm, RACERS)
    assert errors == [], f"连接池热身就失败了，PG 侧有问题：{errors}"


def _split(results):
    """(建组成功的, 被拦下的, 其中撞在索引上的)。"""
    created = [r for r in results if r[0] == "created"]
    losers = [r for r in results if r[0] != "created"]
    by_index = [r for r in losers if r[0] == "db"]
    return created, losers, by_index


def _counts(pg_engine, key):
    from sqlalchemy.orm import sessionmaker

    from app.models import DiseaseEnrollment

    Session = sessionmaker(bind=pg_engine)
    with Session() as db:
        rows = (
            db.query(DiseaseEnrollment)
            .filter(
                DiseaseEnrollment.program_id == key["program_id"],
                DiseaseEnrollment.patient_id == key["patient_id"],
            )
            .all()
        )
    return len(rows), [r for r in rows if r.status == "enrolled"]


def test_八路并发入组恰一路成功其余拿到同一句409(pg_engine, fixture_key):
    """静默双写的洞被堵上，且堵法对调用方无感：输家看到的与"本来就重复"一模一样。"""
    results, errors = _enroll_race(pg_engine, fixture_key)

    assert errors == [], f"有并发路径漏出了异常（裸 IntegrityError 就是 500）：{errors}"
    assert len(results) == RACERS, f"{RACERS} 路只回来了 {len(results)} 路"
    created, losers, by_index = _split(results)
    assert len(created) == 1, f"应恰有一路建组成功，实际 {len(created)} 路：{results}"
    assert {(r[1], r[2]) for r in losers} == {(409, ALREADY_ENROLLED)}, (
        f"抢输者拿到的状态码/文案与顺序请求不一致：{losers}"
    )
    assert by_index, (
        "没有任何一路是撞在索引上回来的——七个 409 全由预检给出，"
        "说明窗口没打开，这一轮证明不了兜底还在"
    )

    total, enrolled = _counts(pg_engine, fixture_key)
    assert len(enrolled) == 1, f"该患者在本专病下应只有一条在管记录，实际 {len(enrolled)} 条"
    assert total == 1, f"该键上应只落库一行，实际 {total} 行"


def test_出组后再并发入组仍是恰一路成功(pg_engine, fixture_key):
    """部分索引锁的是 `status = 'enrolled'`，不是整张表。

    把上一轮的赢家改成出组，同样八路再抢一次：复发再入组必须仍然放得进去
    （全量唯一在这里会八路全拒），而在管记录依旧恰一条。
    """
    from sqlalchemy.orm import sessionmaker

    from app.models import DiseaseEnrollment

    _, enrolled = _counts(pg_engine, fixture_key)
    if not enrolled:
        # 单跑本条（`-k 出组后`）时自己把第一轮补上，不靠用例执行顺序
        _enroll_race(pg_engine, fixture_key)
        _, enrolled = _counts(pg_engine, fixture_key)
    assert len(enrolled) == 1, f"前置：该键上应恰有一条在管记录，实际 {len(enrolled)} 条"

    Session = sessionmaker(bind=pg_engine)
    with Session() as db:
        row = db.get(DiseaseEnrollment, enrolled[0].id)
        row.status = "exited"
        row.exit_reason = "转上级医院继续治疗"
        row.exited_at = "2026-09-05"
        db.commit()

    results, errors = _enroll_race(pg_engine, fixture_key)

    assert errors == [], f"有并发路径漏出了异常：{errors}"
    created, losers, by_index = _split(results)
    assert len(created) == 1, f"出组后应恰有一路复发再入组成功，实际 {len(created)} 路：{results}"
    assert {(r[1], r[2]) for r in losers} == {(409, ALREADY_ENROLLED)}, losers
    assert by_index, "第二轮同样要有撞在索引上的一路，否则证明不了它在出组之后仍然生效"

    total, enrolled = _counts(pg_engine, fixture_key)
    assert len(enrolled) == 1, f"在管记录应仍恰一条，实际 {len(enrolled)} 条"
    assert total == 2, f"该键上应是一条已出组 + 一条在管，共两行，实际 {total} 行"
