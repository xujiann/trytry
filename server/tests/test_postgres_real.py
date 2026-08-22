"""真 PostgreSQL 验证（P3-4）：迁移链与方言敏感点。

默认跳过——CI/开发机不一定有 PG。开启方式：

    export MEDPLAT_PG_TEST_URL=postgresql://postgres@127.0.0.1:5432/medplat_pg_test
    python -m pytest tests/test_postgres_real.py -q

这套用例守的是 **SQLite 上测不出来的东西**：

- 双分支迁移（平台 + spd）在 PG 上能从零跑到 heads——SQLite 的 batch_alter
  与类型宽容会把很多 PG 才报的错吞掉（已实测抓到过一个：boolean 列配了
  整数默认值，PG 直接 DatatypeMismatch）；
- 部分唯一索引（仅 status='active'）真的只锁在管档案；
- 金额并发闸门（押金退费 / 出院结算 / 缴费收款）——这三条**只在 PG 上现形**，
  SQLite 的库级写锁把判定与写入之间的窗口一并锁掉了。用例本体在
  `test_billing_money_concurrency.py`（默认跟 test-unit 跑 SQLite），
  本文件用子进程把它换到 PG 上再跑一遍；
- 其余业务测试套在 PG 上跑通由 conftest 的 MEDPLAT_DATABASE_URL 支持，
  不在本文件重复。
"""
import os
import subprocess
import sys
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


@pytest.fixture(scope="module")
def pg_engine():
    from sqlalchemy import create_engine, text

    engine = create_engine(PG_URL)
    # 从零开始：清空 public schema，让迁移链证明自己能白手起家
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
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


def test_migrations_reach_both_heads(pg_engine):
    from sqlalchemy import text

    with pg_engine.connect() as conn:
        versions = {v for (v,) in conn.execute(text("SELECT version_num FROM alembic_version"))}
    assert len(versions) == 2, f"应有平台 + spd 两个分支头，实际 {versions}"


def test_spd_tables_created(pg_engine):
    from sqlalchemy import inspect

    tables = set(inspect(pg_engine).get_table_names())
    assert {"spd_enrollments", "spd_tasks", "spd_measurements",
            "spd_report_instances"} <= tables
    assert "patients" in tables and "attachments" in tables


def test_partial_unique_index_only_locks_active(pg_engine):
    """部分唯一索引：同患者同病种两条 active 撞索引；migrated + active 共存。

    用 ORM 建前置数据而不是手写 INSERT：表的非空列会随版本增加，
    手写列清单迟早跟不上模型。
    """
    import sqlalchemy.exc
    from sqlalchemy.orm import sessionmaker

    from app.models import Organization, Patient
    from app.spd.models import SpdEnrollment

    Session = sessionmaker(bind=pg_engine)
    with Session() as db:
        org = Organization(name="PG索引测试院", org_type="township", level="township")
        patient = Patient(name="PG索引患者", id_card="330900199001011234",
                          gender="男", birth_date="1990-01-01",
                          ehc_no="PG-EHC-0001")  # 健康卡号由接口层发号，直连建档要自带
        db.add_all([org, patient])
        db.flush()

        def enrollment(status):
            return SpdEnrollment(
                patient_id=patient.id, program_code="hypertension",
                org_id=org.id, status=status,
            )

        db.add(enrollment("migrated"))
        db.add(enrollment("active"))  # 与 migrated 共存：迁走后目标机构可重建
        db.commit()

        db.add(enrollment("active"))  # 第二条在管档案必须撞索引
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            db.commit()


def test_boolean_defaults_are_boolean(pg_engine):
    """child_records.high_risk 的默认值必须是布尔字面量（曾配成整数 0，PG 拒收）。"""
    from sqlalchemy import text

    with pg_engine.connect() as conn:
        default = conn.execute(text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'child_records' AND column_name = 'high_risk'"
        )).scalar()
    assert default and "false" in default.lower()


def test_迁移与模型的列集合零漂移(pg_engine):
    """**列级** parity：真 PG 上跑完 `upgrade heads`，每张表的列必须与模型一致。

    `test_schema_governance.test_模型表零漂移_每张表都被迁移建过` 守的是**表**级——
    有表就算过。但 ADR-0002 停用 `create_all` 之后，生产库完全由迁移建出，
    **漏写一列**同样会上线才炸：模型上有、迁移里没有的列，在开发 SQLite 上被
    `create_all` 悄悄补齐（开发环境仍开着它），到 PG 上就是 UndefinedColumn。

    这正是 ADR-0002 记的残余缺口，这里补上。
    """
    from sqlalchemy import inspect

    from app.database import Base
    import app.models  # noqa: F401 - 导入即注册平台模型
    import app.spd.models  # noqa: F401 - 以及子系统模型

    inspector = inspect(pg_engine)
    actual_tables = set(inspector.get_table_names())

    missing_columns: dict[str, set[str]] = {}
    extra_columns: dict[str, set[str]] = {}
    for table_name, table in sorted(Base.metadata.tables.items()):
        if table_name not in actual_tables:
            continue  # 表级缺失由 test_模型表零漂移 负责报，这里只管列
        in_db = {c["name"] for c in inspector.get_columns(table_name)}
        in_model = {c.name for c in table.columns}
        if in_model - in_db:
            missing_columns[table_name] = in_model - in_db
        if in_db - in_model:
            extra_columns[table_name] = in_db - in_model

    assert not missing_columns, (
        "模型上有、迁移没建的列（生产会 UndefinedColumn）：\n"
        + "\n".join(f"  {t}: {sorted(cols)}" for t, cols in missing_columns.items())
    )
    assert not extra_columns, (
        "迁移建了、模型上没有的列（多半是模型删列忘了写迁移，或迁移写错列名）：\n"
        + "\n".join(f"  {t}: {sorted(cols)}" for t, cols in extra_columns.items())
    )


def test_脏库上迁移只报告不删数据_处置脚本才归并(pg_engine):
    """P0-1 的真数据验证：**在有重复行的存量库上**跑这条迁移会发生什么。

    上面几条只能证明"新库建出来的索引是唯一的"——真正危险的是**已经有脏数据的
    生产库**：唯一索引建不上去，迁移要么中途失败，要么（更糟）有人一怒之下加个
    `DELETE` 把重复行直接删掉。平台通则是后者绝对不许（CLAUDE.md §4），
    所以这条用例把两段都跑一遍：

    1. 退到修复前那版（`d8e9f1a2b3c8`），此时唯一索引还是普通索引；
    2. 造两条同 user_id 的积分账户，各带一条流水；
    3. 升到 heads——断言**两行都还在**、索引没建成唯一、冲突进了台账（pending）；
    4. 再跑 `scripts/spd_dedup.py --apply`——断言归并成一条、余额相加、流水改指、
       台账落定为 merge 且存着整行 JSON，此后再插同一 user_id 被 DB 拦下。

    第 3 步是这条用例真正的价值：迁移**没**替人做决定。

    用子进程跑 alembic 与脚本：本进程的引擎早已按 `PG_URL` 定型，这两者要的是命令行口径。
    """
    import json

    from sqlalchemy import text

    def run(*args, what="", expect=0):
        result = subprocess.run(
            [sys.executable, *args],
            cwd=SERVER_DIR, capture_output=True, text=True,
            env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
        )
        assert result.returncode == expect, (
            f"{what or args} 退出码应为 {expect}，实际 {result.returncode}：\n"
            f"{result.stdout[-800:]}\n{result.stderr[-1500:]}"
        )
        return result

    def alembic(*args):
        return run("-m", "alembic", *args, what=f"alembic {args}")

    def unique_index_exists() -> bool:
        with pg_engine.connect() as conn:
            return bool(conn.execute(text(
                "SELECT indexdef ~ 'UNIQUE' FROM pg_indexes "
                "WHERE indexname = 'ix_spd_point_accounts_user_id'"
            )).scalar())

    alembic("downgrade", "d8e9f1a2b3c8")
    assert not unique_index_exists(), "降级后该索引应回到非唯一（这正是修复前生产库的样子）"

    with pg_engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO users (username, password_hash, full_name, role, created_at) "
            "VALUES ('pg_dedup_vd', 'x', '去重验证村医', 'doctor', now())"
        ))
        uid = conn.execute(text(
            "SELECT id FROM users WHERE username = 'pg_dedup_vd'")).scalar()
        for balance, earned, used in ((10, 30, 20), (5, 7, 2)):
            conn.execute(text(
                "INSERT INTO spd_point_accounts (user_id, balance, earned, used, created_at, updated_at)"
                " VALUES (:u, :b, :e, :s, now(), now())"
            ), {"u": uid, "b": balance, "e": earned, "s": used})
        account_ids = conn.execute(text(
            "SELECT id FROM spd_point_accounts WHERE user_id = :u ORDER BY id"), {"u": uid}
        ).scalars().all()
        assert len(account_ids) == 2, "修复前的库允许一个人两个积分账户——这就是缺陷本身"
        for account_id in account_ids:
            conn.execute(text(
                "INSERT INTO spd_point_records (account_id, rule_code, direction, points,"
                " balance_after, ref_type, note, created_at)"
                " VALUES (:a, 'sign', 'in', 1, 1, '', '去重验证流水', now())"
            ), {"a": account_id})

    # ---- 第一段：升级只报告 ----
    alembic("upgrade", "heads")

    with pg_engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id FROM spd_point_accounts WHERE user_id = :u ORDER BY id"), {"u": uid}
        ).scalars().all()
        assert rows == account_ids, (
            f"迁移不得替人删数据：两行都应原封不动，实际 {rows}"
        )
        pending = conn.execute(text(
            "SELECT strategy, kept_id, removed_id FROM spd_dedup_reports "
            "WHERE table_name = 'spd_point_accounts' AND key_value = :k"
        ), {"k": str(uid)}).mappings().all()
        assert len(pending) == 1, f"冲突应进台账，实际 {pending}"
        assert pending[0]["strategy"] == "pending", "迁移记下的冲突必须是待处置态"
        assert (pending[0]["kept_id"], pending[0]["removed_id"]) == tuple(account_ids)
    assert not unique_index_exists(), "有重复时不该建成唯一索引（那会让升级失败）"

    # ---- 第二段：人执行处置脚本 ----
    # 报告模式对"有冲突待处置"以退出码 1 收场——运维巡检据此告警，别改成 0
    report = run("scripts/spd_dedup.py", what="报告模式", expect=1)
    assert "未改动任何数据" in report.stdout
    run("scripts/spd_dedup.py", "--apply", what="处置")

    with pg_engine.connect() as conn:
        merged = conn.execute(text(
            "SELECT id, balance, earned, used FROM spd_point_accounts WHERE user_id = :u"
        ), {"u": uid}).mappings().all()
        assert len(merged) == 1, f"应归并成一条，实际 {len(merged)} 条"
        row = merged[0]
        assert (row["balance"], row["earned"], row["used"]) == (15, 37, 22), \
            f"余额与累计应相加，实际 {dict(row)}"
        assert row["id"] == account_ids[0], "应保留最早建的那条"

        holders = conn.execute(text(
            "SELECT DISTINCT account_id FROM spd_point_records WHERE note = '去重验证流水'"
        )).scalars().all()
        assert holders == [account_ids[0]], f"流水应全部改指到保留账户，实际 {holders}"

        done = conn.execute(text(
            "SELECT strategy, removed_id, removed_row FROM spd_dedup_reports "
            "WHERE table_name = 'spd_point_accounts' AND key_value = :k"
        ), {"k": str(uid)}).mappings().all()
        assert len(done) == 1 and done[0]["strategy"] == "merge", f"台账未落定：{done}"
        assert done[0]["removed_id"] == account_ids[1]
        archived = done[0]["removed_row"]
        archived = json.loads(archived) if isinstance(archived, str) else archived
        assert archived["balance"] == 5 and archived["earned"] == 7, \
            "留痕必须是被删那一行的**整行**，否则事后还原不了"

    assert unique_index_exists(), "处置完脚本必须把迁移跳过的唯一索引补建上"

    import sqlalchemy.exc

    with pg_engine.connect() as conn:
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            with conn.begin():
                conn.execute(text(
                    "INSERT INTO spd_point_accounts (user_id, balance, earned, used,"
                    " created_at, updated_at) VALUES (:u, 0, 0, 0, now(), now())"
                ), {"u": uid})

def test_金额并发闸门在真PG上成立(pg_engine):
    """把 `test_billing_money_concurrency.py` 换到 PG 上再跑一遍。

    起子进程而不是在本进程内切库：`app.database` 的引擎是模块级的，本进程早已
    按 SQLite 导入定型，改环境变量已经晚了。子进程里 `MEDPLAT_BILLING_PG_URL`
    会在导入 app 之前把连接串顶掉（那个模块头部有断言兜底）。

    **本条要放在文件末尾**：子进程用 `reset_database()`（drop_all + create_all）
    重建表，会把上面几条用例依赖的"迁移建出来的库"换成模型建出来的库。
    往后加 PG 用例请加在这一条之前。
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_billing_money_concurrency.py", "-q"],
        cwd=SERVER_DIR,
        env={**os.environ, "MEDPLAT_BILLING_PG_URL": PG_URL},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        "金额并发用例在真 PG 上没过：\n"
        + result.stdout[-4000:]
        + "\n"
        + result.stderr[-2000:]
    )
