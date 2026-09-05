"""真 PostgreSQL：专病规则改版的八路并发只许一路改成（P1-30）。

为什么必须在 PG 上跑：`update_program` 的洞是 READ COMMITTED 下的读-改-写窗口
（读 `program.version` → 存该版本号的快照 → 升版 → 提交），SQLite 的**库级写锁**
把读与写之间的窗口一并锁掉了——同一段代码在 SQLite 上怎么并发都撞不出问题，
在 PG 上八路齐发就是八份同版快照、只升一格版、七个人的改动被最后一个覆盖掉。
`tests/test_spd_program_version_unique.py` 的顺序用例只能证明"重复被拦住"，
证不了"并发下也拦得住"，这一档补的就是后者。

与 `tests/test_postgres_real.py` 的约定一致（integration 标记 + 无
`MEDPLAT_PG_TEST_URL` 即整档跳过），但有一处**刻意的不同**：本档
**不 `DROP SCHEMA`**。这个库是多方共用的，建库交给 `alembic upgrade heads`，
本档只自带唯一命名的数据、跑完清掉自己那几行。

    cd server && MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:55432/medplat_test \\
        python -m pytest tests/test_spd_program_version_unique_races.py -q
"""
import os
import threading
import time
import uuid

import pytest

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"
    ),
]

#: 共享库上别的作业可能正持着锁或正在跑迁移。这类失败是"等一等就好"，
#: 跳过它等于把本档变成永远绿的摆设，所以是重试而不是 skip。
BUSY_RETRIES = 5
BUSY_WAIT_SECONDS = 60


def _retry_on_busy(action, what):
    """撞上共享库的锁/迁移就等一轮再来，最后一轮仍失败则如实报错。"""
    from sqlalchemy.exc import SQLAlchemyError

    last: BaseException | None = None
    for attempt in range(BUSY_RETRIES):
        try:
            return action()
        except SQLAlchemyError as exc:
            last = exc
            if attempt == BUSY_RETRIES - 1:
                break
            print(f"[共享库繁忙] {what} 第 {attempt + 1} 次失败（{type(exc).__name__}），"
                  f"{BUSY_WAIT_SECONDS} 秒后重试")
            time.sleep(BUSY_WAIT_SECONDS)
    raise AssertionError(f"{what} 连续 {BUSY_RETRIES} 次失败：{last!r}") from last


def _index_present(engine) -> bool:
    """索引在不在。空库要答"不在"而不是抛 NoSuchTableError——CI 的集成档跑在**全新库**上。"""
    from sqlalchemy import inspect

    inspector = inspect(engine)
    if "spd_program_versions" not in set(inspector.get_table_names()):
        return False
    return "uq_spd_program_version" in {
        i["name"] for i in inspector.get_indexes("spd_program_versions")
    }


@pytest.fixture(scope="module")
def pg_engine():
    """连库并**只在缺索引时**跑一次幂等的 `alembic upgrade heads`。

    刻意不 `DROP SCHEMA`（库可能与别的用例共用，清库会把别人连锅端掉）；
    但也不能假设库已经建好——CI 的集成档跑在全新库上，那样这一档会在
    "读索引"那步直接红，而且退避重试会把它拖成好几分钟的静默等待。
    """
    import subprocess
    import sys
    from pathlib import Path

    from sqlalchemy import create_engine

    server_dir = Path(__file__).resolve().parents[1]
    engine = create_engine(PG_URL)
    for attempt in range(5):
        if _index_present(engine):
            break
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=server_dir,
            env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 or _index_present(engine):
            break
        assert attempt < 4, f"在 {PG_URL} 上跑迁移五次都没成功：\n{result.stderr[-2000:]}"
        time.sleep(60)
    yield engine
    engine.dispose()


@pytest.mark.timeout(400)  # 共享库繁忙时最多退避重试 5 轮，默认 120 秒不够用
def test_唯一索引在PG上真的建过(pg_engine):
    """模型上有、PG 上没有（漏迁移）等于没有约束，而列比对类的用例看不见索引。"""
    from sqlalchemy import inspect

    names = _retry_on_busy(
        lambda: {i["name"] for i in inspect(pg_engine).get_indexes("spd_program_versions")},
        "读取 spd_program_versions 的索引",
    )
    assert "uq_spd_program_version" in names, (
        "PG 上没有 uq_spd_program_version——请先 `alembic upgrade heads`；"
        "本档不建库（库是共享的），模型有而库没有正是漏迁移的形状"
    )


def _race(prepare, act, times):
    """先各自把前置读做完，再用 Barrier 同时进入"改-写"。

    与 `test_postgres_real._race_on_pg` 的区别只在多一个 `prepare`：本例要复现的
    正是"八路都读到 v1"这一刻的交错——读也放在 Barrier 之后，谁先读谁赢，
    窗口开没开全凭调度运气（实测八路里会时不时冒出第二个赢家，因为它的 SELECT
    落在赢家提交之后，读到的已经是 v2 了）。等待点一律带 timeout
    （会阻塞的回归测试不是回归测试）。
    """
    results: list = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(times)

    def run(index: int):
        try:
            ctx = prepare(index)
            barrier.wait(timeout=30)
            outcome = act(index, ctx)
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


@pytest.mark.timeout(600)  # 共享库繁忙时最多退避重试 5 轮，默认 120 秒不够用
def test_八路并发改同一病种规则_恰一路改成其余409且只留一份同版快照(pg_engine):
    """洞的形状与兜底的形状，一条用例里同时钉住。

    修之前：八路都读到 `v1`，各写一份 `(program, 'v1')` 快照、都把版本设成 `v2`，
    八路全 200——库里八份一模一样的快照，七个人的规则改动被最后一次 UPDATE 盖掉，
    而"这批人按哪版规则纳的管"从此有八个自相矛盾的答案。

    修之后：快照与升版共享同一次提交，输家的 INSERT 撞 `uq_spd_program_version`，
    `insert_or_conflict` 把**整笔**（含它已经改好的字段与版本号）回滚成 409。
    不变量：`200 + 409 == 8`、库里只有一份 `v1` 快照、快照存的是改**之前**的规则、
    病种停在 `v2` 且只带赢家那一路的规则（没有被合并进来的输家字段）。
    """
    from fastapi import HTTPException
    from sqlalchemy.orm import sessionmaker

    from app.models import User
    from app.spd.models import SpdProgram, SpdProgramVersion
    from app.spd.routers.config.catalog import ProgramUpdate, update_program

    Session = sessionmaker(bind=pg_engine)
    suffix = uuid.uuid4().hex[:8]  # 共享库：数据自带唯一名字，不跟别的作业抢
    times = 8

    def setup():
        with Session() as db:
            user = User(username=f"pg_spv_{suffix}", password_hash="x",
                        full_name="并发配置员", role="director")
            program = SpdProgram(code=f"PG-SPV-{suffix}", name="并发改版病种",
                                 category="specialty", include_rules=[], version="v1")
            db.add_all([user, program])
            db.commit()
            return user.id, program.id

    user_id, program_id = _retry_on_busy(setup, "建并发用例的前置数据")

    def prepare(_index):
        """开会话并把病种读进身份映射——八路在 Barrier 之前都拿到了 `v1`。

        必须把实例**强引用**带出去：Session 的身份映射是弱引用，读完就丢会被
        当场回收，`update_program` 里的 `db.get` 就又去库里读一遍，八路读到什么
        版本号重新变成看调度脸色。留住它，交错才是钉死的"八路都读到 v1"。
        """
        db = Session()
        pinned = db.get(SpdProgram, program_id)
        assert pinned is not None and pinned.version == "v1", "前置读没读到 v1，交错没钉住"
        return db, pinned

    def act(index, ctx):
        db, _pinned = ctx
        try:
            out = update_program(
                program_id,
                ProgramUpdate(include_rules=[{"field": "age", "op": ">=", "value": 60 + index}],
                              note=f"第{index}路"),
                db,
                db.get(User, user_id),
            )
        except HTTPException as exc:
            return exc.status_code, None, None
        finally:
            db.close()
        return 200, out["version"], out["include_rules"]

    try:
        results, errors = _race(prepare, act, times=times)
        assert not errors, (
            f"并发改版该给出 409，不该把异常漏给调用方（那是 500）：{errors}"
        )
        assert len(results) == times

        statuses = [status for status, _v, _r in results]
        assert set(statuses) <= {200, 409}, f"只该是 200 或 409，实际 {sorted(set(statuses))}"
        winners = statuses.count(200)
        assert winners == 1, (
            f"八路都读到 v1，却有 {winners} 路改成——同一个版本标签被退役了不止一次，"
            f"快照从此对不上账（各路结果：{sorted(results, key=str)}）"
        )
        assert statuses.count(409) == times - 1

        winner_rules = next(rules for status, _v, rules in results if status == 200)
        winner_version = next(v for status, v, _r in results if status == 200)
        assert winner_version == "v2"

        with Session() as db:
            rows = (
                db.query(SpdProgramVersion)
                .filter(SpdProgramVersion.program_id == program_id)
                .order_by(SpdProgramVersion.id)
                .all()
            )
            assert len(rows) == 1, f"同一个版本标签只该留一份快照，实际 {len(rows)} 份"
            assert rows[0].version == "v1"
            assert rows[0].snapshot["include_rules"] == [], "快照存的必须是改之前那一版"

            program = db.get(SpdProgram, program_id)
            assert program.version == "v2", "输家的升版必须随它的快照一起回滚"
            assert program.include_rules == winner_rules, (
                "病种上只该有赢家那一路的规则——出现别路的字段就是丢更新（改动被合并）"
            )
    finally:
        # 共享库：只清自己建的那几行，绝不 DROP SCHEMA
        try:
            with Session() as db:
                db.query(SpdProgramVersion).filter(
                    SpdProgramVersion.program_id == program_id
                ).delete(synchronize_session=False)
                db.query(SpdProgram).filter(SpdProgram.id == program_id).delete(
                    synchronize_session=False)
                db.query(User).filter(User.id == user_id).delete(synchronize_session=False)
                db.commit()
        except Exception as exc:  # noqa: BLE001 - 清理失败不该盖掉用例本身的结论
            print(f"[清理失败] 共享库里残留 PG-SPV-{suffix} 的数据，需人工清：{exc!r}")
