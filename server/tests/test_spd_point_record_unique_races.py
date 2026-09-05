"""真 PostgreSQL：转诊计分的八路并发只许入账一笔（P1-30 · spd_point_records）。

为什么必须在 PG 上跑：`spd_point_records` 上**没有**唯一索引，"同一次业务事件只发
一次分"这条不变式守在**父行**上——`spd_referral_cases.status` 的条件跃迁命中了才
`award_points`。SQLite 的库级写锁把判定与写入之间的窗口一并锁掉，同一段代码在
SQLite 上怎么并发都很难撞出第二笔；PG 逐语句取快照、READ COMMITTED 下八路都读到
`accepted`，旧写法（Python 判状态 → 无条件 `UPDATE … WHERE id=?`）就是八路全过、
八条"到院"轨迹、八笔"有效上转"，账户余额与累计获得各虚高 70 分。
`tests/test_spd_point_record_ledger.py` 的顺序用例只能证明"重复请求被拦住"，
证不了"并发下也只入一笔"，这一档补的就是后者。

守法的形状（与 `prescriptions._apply_review` / `tasks._finish_task` 同范式）：

    UPDATE spd_referral_cases SET status='arrived', … WHERE id=:id AND status='accepted'

行锁 + EvalPlanQual 让后到的七路按赢家提交后的状态重算 WHERE，rowcount=0 →
回滚 + 409，轨迹行与那笔分一个都不落。

**帮手的归属**：`app/spd/routers/referral.py` 归同一工程包的 spd_referral_steps 组
所有（本档所在的 spd_assess 组不改它）。本档因此**优先取路由里的条件推进帮手**
（`_advance_case` / `_flip_status`）：它一落地，本用例就从"证明这条 SQL 在真 PG 上
只让一路入账"升级成"证明路由用的就是这条 SQL"；在它落地之前，用与规范逐字一致的
同一条 UPDATE 驱动，先把不变式与机制钉住。

与 `tests/test_postgres_real.py` 的约定一致（integration 标记 + 无
`MEDPLAT_PG_TEST_URL` 即整档跳过），但有一处**刻意的不同**：本档**不 DROP SCHEMA**。
这个库是多方共用的，建库交给 `alembic upgrade heads`，本档只自带唯一命名的数据、
跑完清掉自己那几行。

    cd server && MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:55432/medplat_test \\
        python -m pytest tests/test_spd_point_record_unique_races.py -q
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
RACERS = 8


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


def _schema_ready(engine) -> bool:
    """本档要用的表在不在。空库要答"不在"而不是抛错——CI 的集成档跑在**全新库**上。"""
    from sqlalchemy import inspect

    return {"spd_referral_cases", "spd_point_records", "spd_point_accounts"} <= set(
        inspect(engine).get_table_names()
    )


@pytest.fixture(scope="module")
def pg_engine():
    """连库并**只在表缺失时**跑一次幂等的 `alembic upgrade heads`。

    刻意不 `DROP SCHEMA`（库与别的作业共用，清库会把别人连锅端掉）。
    """
    import subprocess
    import sys
    from pathlib import Path

    from sqlalchemy import create_engine

    server_dir = Path(__file__).resolve().parents[1]
    engine = create_engine(PG_URL)
    for attempt in range(5):
        if _schema_ready(engine):
            break
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=server_dir,
            env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 or _schema_ready(engine):
            break
        assert attempt < 4, f"在 {PG_URL} 上跑迁移五次都没成功：\n{result.stderr[-2000:]}"
        time.sleep(60)
    yield engine
    engine.dispose()


def _advance(db, case_id, expected, **values) -> bool:
    """父行条件推进：优先用转诊路由里的帮手，没有就用规范里那条同形状的 UPDATE。

    `referral.py` 不归本组所有（见模块 docstring）。帮手一旦落地，本档自动改用它，
    这条用例随之从"这条 SQL 只让一路过"变成"路由用的就是这条 SQL"。
    """
    from typing import cast

    from sqlalchemy import update
    from sqlalchemy.engine import CursorResult

    from app.spd.models import SpdReferralCase
    from app.spd.routers import referral as referral_mod

    helper = getattr(referral_mod, "_advance_case", None) or getattr(
        referral_mod, "_flip_status", None)
    if helper is not None:
        return bool(helper(db, case_id, expected, **values))
    moved = cast(CursorResult, db.execute(
        update(SpdReferralCase)
        .where(SpdReferralCase.id == case_id, SpdReferralCase.status == expected)
        .values(**values)
    ))
    return bool(moved.rowcount)


def _race(worker, times):
    """Barrier 真并发（写法同 `test_postgres_real._race_on_pg`）。

    只起线程不够——线程创建有先后，前一个常常已提交完了后一个才开始读，窗口根本
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


def _ensure_rule(db, event, code, name, points, daily_limit):
    """保证该事件至少有一条启用的规则；已有就用现成的（共享库里别再塞一条）。

    `award_points` 按 `event` + `active` 取 `.first()`，多塞一条会让"哪条规则生效"
    变成运气。断言一律按"这次入账落了几行"算，因此不依赖具体是哪条规则。
    """
    from app.spd.models import SpdPointRule

    existing = (
        db.query(SpdPointRule)
        .filter(SpdPointRule.event == event, SpdPointRule.active.is_(True))
        .first()
    )
    if existing is not None:
        return existing.id, False
    rule = SpdPointRule(code=code, name=name, event=event, points=points,
                        daily_limit=daily_limit, active=True)
    db.add(rule)
    db.flush()
    return rule.id, True


def _make_world(Session, suffix, status, event, rule_spec):
    """机构 + 医生 + 患者 + 积分账户 + 停在指定状态的转诊单。

    账户预先建好：`award_points` 的"没有账户就建一个"本身也是 check-then-act
    （守在 `spd_point_accounts.user_id` 的唯一约束上），预置它才能让本档的断言
    干干净净地只谈"这次事件入了几笔分"。
    """
    from app.models import Organization, Patient, User
    from app.spd.models import SpdPointAccount, SpdReferralCase

    digits = "".join(str(int(c, 16) % 10) for c in uuid.uuid4().hex[:14])
    with Session() as db:
        org = Organization(name=f"PG积分县医院{suffix}", org_type="lead_hospital",
                           level="county")
        doctor = User(username=f"pg_pt_doc_{suffix}", password_hash="x", full_name="计分村医")
        patient = Patient(name="PG积分患者", id_card=f"3309{digits}", gender="男",
                          birth_date="1993-03-03", ehc_no=f"PG-EHC-PT-{suffix}")
        db.add_all([org, doctor, patient])
        db.flush()
        rule_id, rule_is_ours = _ensure_rule(db, event, f"pt_{event}_{suffix}", *rule_spec)
        account = SpdPointAccount(user_id=doctor.id, org_id=org.id, balance=0, earned=0, used=0)
        case = SpdReferralCase(patient_id=patient.id, program_code="", direction="up",
                               initiator_org_id=org.id, initiator_id=doctor.id,
                               current_org_id=org.id, current_level="county",
                               status=status, reason="PG计分竞态")
        db.add_all([account, case])
        db.commit()
        return {"org_id": org.id, "doctor_id": doctor.id, "patient_id": patient.id,
                "account_id": account.id, "case_id": case.id,
                "rule_id": rule_id, "rule_is_ours": rule_is_ours}


def _cleanup(Session, world):
    """共享库：只清自己建的那几行，绝不 DROP SCHEMA。"""
    from app.models import Organization, Patient, User
    from app.spd.models import (
        SpdPointAccount,
        SpdPointRecord,
        SpdPointRule,
        SpdReferralCase,
        SpdReferralStep,
    )

    try:
        with Session() as db:
            for model, column, value in (
                (SpdPointRecord, "account_id", world["account_id"]),
                (SpdReferralStep, "case_id", world["case_id"]),
                (SpdReferralCase, "id", world["case_id"]),
                (SpdPointAccount, "id", world["account_id"]),
                (Patient, "id", world["patient_id"]),
                (User, "id", world["doctor_id"]),
                (Organization, "id", world["org_id"]),
            ):
                db.query(model).filter(getattr(model, column) == value).delete(
                    synchronize_session=False)
            if world["rule_is_ours"]:
                db.query(SpdPointRule).filter(SpdPointRule.id == world["rule_id"]).delete(
                    synchronize_session=False)
            db.commit()
    except Exception as exc:  # noqa: BLE001 - 清理失败不该盖掉用例本身的结论
        print(f"[清理失败] 共享库里残留 {world} 的数据，需人工清：{exc!r}")


def _ledger_rows(Session, case_id):
    from app.spd.models import SpdPointRecord

    with Session() as db:
        return (
            db.query(SpdPointRecord)
            .filter(SpdPointRecord.ref_type == "referral",
                    SpdPointRecord.ref_id == case_id,
                    SpdPointRecord.direction == "in")
            .order_by(SpdPointRecord.id)
            .all()
        )


def _run_award_race(pg_engine, *, status, next_values, event, step, action, rule_spec):
    """八路同时推进同一张单：只有推进到的那一路写轨迹并入账。"""
    from sqlalchemy.orm import sessionmaker

    from app.spd.models import SpdPointAccount, SpdReferralCase, SpdReferralStep
    from app.spd.service import award_points

    Session = sessionmaker(bind=pg_engine)
    suffix = uuid.uuid4().hex[:8]  # 共享库：数据自带唯一名字，不跟别的作业抢
    world = _retry_on_busy(
        lambda: _make_world(Session, suffix, status, event, rule_spec),
        "建并发用例的前置数据",
    )

    def worker(index):
        with Session() as db:
            if not _advance(db, world["case_id"], status, **next_values):
                db.rollback()  # 抢输的一路握着写事务，必须显式回滚
                return False
            db.add(SpdReferralStep(case_id=world["case_id"], step=step, action=action,
                                   actor_id=world["doctor_id"], org_id=world["org_id"],
                                   opinion=f"第{index}路"))
            award_points(db, world["doctor_id"], event, ref_type="referral",
                         ref_id=world["case_id"], note=step, org_id=world["org_id"])
            db.commit()
            return True

    try:
        results, errors = _race(worker, times=RACERS)
        assert not errors, f"条件推进并发下不该抛错（那是 500）：{errors}"
        assert len(results) == RACERS
        assert results.count(True) == 1, (
            f"一次业务事件只能推进一路，实际 {results.count(True)} 路推进到——"
            f"每多一路就多一笔分，余额与累计获得随之虚高"
        )
        assert results.count(False) == RACERS - 1

        rows = _ledger_rows(Session, world["case_id"])
        assert len(rows) == 1, (
            f"同一次事件在 spd_point_records 里留了 {len(rows)} 笔入账——"
            "重复计分只能人工冲销，而流水上看不出哪一笔是多的"
        )
        with Session() as db:
            case = db.get(SpdReferralCase, world["case_id"])
            assert case.status == next_values["status"]
            steps = (
                db.query(SpdReferralStep)
                .filter(SpdReferralStep.case_id == world["case_id"],
                        SpdReferralStep.action == action)
                .all()
            )
            assert len(steps) == 1, f"轨迹也只该留一行，实际 {len(steps)} 行"
            account = db.get(SpdPointAccount, world["account_id"])
            assert account.earned == rows[0].points, (
                f"累计获得 {account.earned} 与那一笔 {rows[0].points} 对不上：有分入了账却没留流水"
            )
            assert account.balance == rows[0].points
            assert rows[0].balance_after == rows[0].points
        return world["case_id"]
    finally:
        _cleanup(Session, world)


@pytest.mark.timeout(600)  # 共享库繁忙时最多退避重试 5 轮，默认 120 秒不够用
def test_八路并发登记到院_恰一路推进且有效上转只入一笔(pg_engine):
    """到院登记（`referral.arrive_referral` 的内核）：accepted → arrived。

    修之前：八路都在 Python 里读到 `accepted`，各自 `UPDATE … WHERE id=?`，
    八条"到院"轨迹、八笔"有效上转"（每笔 10 分）；`award_points` 的每日上限也救不了
    ——它同样是读-判-写，八路读到的当日已得都是 0。
    修之后：期望态进了 WHERE，行锁 + EvalPlanQual 让后到的七路 rowcount=0。
    """
    _run_award_race(
        pg_engine,
        status="accepted",
        next_values={"status": "arrived", "effective_visit": True},
        event="referral_up", step="到院", action="arrive",
        rule_spec=("有效上转", 10, 50),
    )


@pytest.mark.timeout(600)
def test_八路并发接收随访_恰一路闭环且下转承接只入一笔(pg_engine):
    """闭环的最后一格（`referral.receive_followup` 的内核）：down_referred → closed。

    与到院同形状：并发下旧写法会把 `closed_at` 反复盖写，并给承接方记满八笔
    "下转承接"。这一格还决定闭环率统计，重复计数会连指标一起做实。
    """
    from app.clock import now_naive

    _run_award_race(
        pg_engine,
        status="down_referred",
        next_values={"status": "closed", "closed_at": now_naive()},
        event="referral_down", step="随访接收", action="receive",
        rule_spec=("下转承接", 8, 40),
    )
