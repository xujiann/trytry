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

    比对逻辑与 `test_migration_model_parity.py`（SQLite 空库、跑在 test-unit 里）
    **共用 `schema_parity.diff_schema`**，不是两份实现。本条守 SQLite 测不出来的
    方言问题，那条负责让改模型的人在本地 7 秒内就拿到反馈。
    """
    from sqlalchemy import inspect

    from schema_parity import diff_schema, format_columns

    from app.database import Base
    import app.models  # noqa: F401 - 导入即注册平台模型
    import app.spd.models  # noqa: F401 - 以及子系统模型

    drift = diff_schema(inspect(pg_engine), Base.metadata)
    # 表级缺失由 test_模型表零漂移 负责报，这里只管列
    assert not drift["missing_columns"], (
        "模型上有、迁移没建的列（生产会 UndefinedColumn）：\n"
        + format_columns(drift["missing_columns"])
    )
    assert not drift["extra_columns"], (
        "迁移建了、模型上没有的列（多半是模型删列忘了写迁移，或迁移写错列名）：\n"
        + format_columns(drift["extra_columns"])
    )
    # 防呆：PG 上没建出表时上面两条恒真
    assert drift["table_count"] >= 200, f"PG 上只有 {drift['table_count']} 张表，迁移没跑完"


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

# ---------------------------------------------------------------------------
# P1-18：READ COMMITTED 竞争窗口的直测（SQLite 的库级写锁证不了这些）
#
# 下面四条不走 HTTP：本进程的 app 引擎早按 SQLite 定型，HTTP 档已由文件末尾的
# 子进程用例（test_billing_money_concurrency.py 换库重跑）覆盖。这里把**最热的
# check-then-act 防线本体**——建档幂等、upsert_unique、押金临界区、批次占用——
# 直接绑到 pg_engine 上多线程真并发，PG 逐语句取快照、并发事务互不可见，
# 竞态窗口是真实打开的。每条的断言都是不变量：恰一行 / 恰 N 笔 / 不超余量。


def _race_on_pg(worker, times):
    """Barrier 真并发（写法同 test_billing_money_concurrency._race）。

    只起线程不够——线程创建有先后，前一个常常已提交完了后一个才开始读，
    窗口根本没打开。等待点全部带 timeout：会阻塞的回归测试不是回归测试
    （见 conftest 看门狗注释）。
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


def test_并发同证件号建档_唯一约束兜底后恰得一行(pg_engine):
    """EMPI 建档幂等（routers/patients.create_patient_idempotent）在 PG 下的形状：

    先查后插是 check-then-act，六路同时查不到就六路都去插；防线是唯一约束 +
    捕获 IntegrityError 后重查。PG 上 IntegrityError 要等赢家 **commit** 才在
    输家的 INSERT 上抛出来（SQLite 的写锁在语句层就把并发压平了），这条兜底
    路径只有真 PG 能走到。不变量：库里恰一行、六路拿到同一份档案、恰一路真建档。
    """
    from sqlalchemy.orm import sessionmaker

    from app.models import Patient
    from app.pii import pii_filter
    from app.routers.patients import create_patient_idempotent

    Session = sessionmaker(bind=pg_engine)
    id_card = "331000199303034567"

    def worker(_i):
        with Session() as db:
            patient, created = create_patient_idempotent(
                db, {"name": "PG并发建档患者", "id_card": id_card}
            )
            return patient.id, created

    results, errors = _race_on_pg(worker, times=6)
    assert not errors, f"并发建档不该把 IntegrityError 漏给调用方：{errors}"
    assert len(results) == 6
    assert len({pid for pid, _ in results}) == 1, f"六路必须拿到同一份档案：{results}"
    assert sum(1 for _, created in results if created) == 1, f"恰一路真建档：{results}"
    with Session() as db:
        rows = db.query(Patient).filter(
            pii_filter(Patient.id_card_idx, Patient.id_card, id_card)
        ).all()
        assert len(rows) == 1, f"同证件号建出 {len(rows)} 份档案——主索引分叉"


def test_upsert_unique并发写同一唯一键_收敛成一行不抛冲突(pg_engine):
    """`concurrency.upsert_unique` 的先插后改在 PG 下真并发验证。

    反着写（先查再插）就是 check-then-act；正写法下输家的 INSERT 在赢家 commit
    后抛约束冲突，回滚重查转为 UPDATE。不变量：一行、全部调用方成功返回、
    恰一路是"新建"，其余是"覆盖"。
    """
    from sqlalchemy.orm import sessionmaker

    from app.concurrency import upsert_unique
    from app.models import LiveFeedback, LiveSession, User

    Session = sessionmaker(bind=pg_engine)
    with Session() as db:
        user = User(username="pg_upsert_user", password_hash="x", full_name="并发反馈者")
        db.add(user)
        db.flush()
        session_row = LiveSession(title="PG并发直播", status="finished", requested_by=user.id)
        db.add(session_row)
        db.commit()
        user_id, session_id = user.id, session_row.id

    def worker(i):
        with Session() as db:
            obj, updated = upsert_unique(
                db,
                LiveFeedback,
                keys={"session_id": session_id, "user_id": user_id},
                values={"rating": i + 1, "comment": f"线程{i}"},
            )
            return obj.id, updated

    results, errors = _race_on_pg(worker, times=6)
    assert not errors, f"upsert_unique 并发下不该抛错（这正是它存在的理由）：{errors}"
    assert len(results) == 6
    assert len({row_id for row_id, _ in results}) == 1, f"六路必须落在同一行上：{results}"
    assert sum(1 for _, updated in results if not updated) == 1, f"恰一路新建：{results}"
    with Session() as db:
        rows = db.query(LiveFeedback).filter(
            LiveFeedback.session_id == session_id, LiveFeedback.user_id == user_id
        ).all()
        assert len(rows) == 1
        assert 1 <= rows[0].rating <= 6  # 终值是某个赢家的完整写入，不是撕裂值


def test_押金退费与结算冲抵混合并发_扣减合计不超余额(pg_engine):
    """`_serialized_on`(FOR UPDATE) + `_atomic_deposit_deduct` 的 PG 分支直测。

    HTTP 档已各自验证过退费、结算的并发；这里补两者**互抢同一笔余额**的组合：
    退费与冲抵抢的是同一把住院登记行锁，谁都不能按旧余额判定。押金余额是流水
    现算，INSERT..SELECT 不锁既有行、聚合读的是语句快照——修复前实测八路全过、
    余额 -600（文件头表格），防线全靠外层 FOR UPDATE。
    不变量：1000 元恰成 3 笔 300（无论退费还是冲抵），余额恰 100、绝不为负。
    """
    from sqlalchemy.orm import sessionmaker

    from app.models import Admission, Bed, Deposit, Organization, Patient, User, Ward
    from app.routers.billing import _atomic_deposit_deduct, _serialized_on, deposit_balance

    Session = sessionmaker(bind=pg_engine)
    with Session() as db:
        org = Organization(name="PG押金并发院", org_type="lead_hospital", level="county")
        operator = User(username="pg_deposit_op", password_hash="x", full_name="并发收退员")
        patient = Patient(name="PG押金患者", id_card="331000199404045678",
                          ehc_no="PG-EHC-DEP1")
        db.add_all([org, operator, patient])
        db.flush()
        ward = Ward(org_id=org.id, name="PG押金病区")
        db.add(ward)
        db.flush()
        bed = Bed(ward_id=ward.id, bed_no="PGD-1")
        db.add(bed)
        db.flush()
        admission = Admission(patient_id=patient.id, org_id=org.id, ward_id=ward.id,
                              bed_id=bed.id, created_by=operator.id)
        db.add(admission)
        db.flush()
        db.add(Deposit(admission_id=admission.id, amount=1000, deposit_type="prepay",
                       operator="并发收退员"))
        db.commit()
        admission_id = admission.id

    def worker(i):
        deposit_type = "refund" if i % 2 == 0 else "offset"
        with Session() as db:
            # commit 必须在临界区内：行锁随事务释放，锁一放下一路读到的
            # 就必须是本笔已提交后的余额（`_serialized_on` 文档约定）。
            with _serialized_on(db, Admission, admission_id):
                ok = _atomic_deposit_deduct(
                    db, admission_id, 300, deposit_type, "cash", "并发收退员"
                )
                if ok:
                    db.commit()
                else:
                    db.rollback()
            return deposit_type, ok

    results, errors = _race_on_pg(worker, times=8)
    assert not errors, f"混合并发扣减不该抛错：{errors}"
    succeeded = [r for r in results if r[1]]
    assert len(succeeded) == 3, f"1000 元只够扣 3 笔 300，实际成了 {len(succeeded)} 笔：{results}"
    with Session() as db:
        assert deposit_balance(db, admission_id) == 100.0, "余额必须恰为 100，绝不为负"
        deduct_rows = (
            db.query(Deposit)
            .filter(Deposit.admission_id == admission_id, Deposit.deposit_type != "prepay")
            .all()
        )
        assert len(deduct_rows) == 3, "台账行数必须与成功笔数一致（钱账相符）"
        assert all(float(d.amount) == 300.0 for d in deduct_rows)


def test_发药批次并发占用_合计不超过批次余量(pg_engine):
    """`dispense._claim_batch` 的原子占用在 PG 行锁 + EvalPlanQual 下的直测。

    判"够不够"与占用压在同一条 UPDATE 里才成立：UPDATE 对既有行取行锁、
    锁到手后重新求值 WHERE，输家的条件按赢家提交后的 used_quantity 重算。
    批次量 10、八路各占 3：恰 3 路占到（9≤10），第 4 路起 12>10 一律空手。
    """
    from sqlalchemy.orm import sessionmaker

    from app.models import DrugBatch, Organization
    from app.routers.dispense import _claim_batch

    Session = sessionmaker(bind=pg_engine)
    with Session() as db:
        org = Organization(name="PG批次并发院", org_type="township", level="township")
        db.add(org)
        db.flush()
        batch = DrugBatch(org_id=org.id, drug_code="PG-RACE", batch_no="PGB-1",
                          expire_date="2031-01-01", quantity=10)
        db.add(batch)
        db.commit()
        batch_id = batch.id

    def worker(_i):
        with Session() as db:
            ok = _claim_batch(db, batch_id, 3)
            if ok:
                db.commit()
            else:
                db.rollback()
            return ok

    results, errors = _race_on_pg(worker, times=8)
    assert not errors, f"批次占用并发下不该抛错：{errors}"
    assert results.count(True) == 3, f"余量 10 只够 3 路各占 3，实际 {results.count(True)} 路占到"
    with Session() as db:
        row = db.get(DrugBatch, batch_id)
        assert row is not None and row.used_quantity == 9, (
            f"已用量必须等于 3 路×3（={row.used_quantity if row else '?'}），超 10 即超发"
        )


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
