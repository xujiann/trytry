"""P1-30 billing 组的**真 PostgreSQL** 竞态直测：日终对账唯一 + 调价条件更新。

默认跳过（CI/开发机不一定有 PG）。开启方式同 `test_postgres_real.py`：

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_billing_reconciliation_unique_races.py -q

**为什么非得在 PG 上再跑一遍**：SQLite 的库级写锁把"读 → 判定 → 写"整段串行化，
八路并发在 SQLite 上大多各自变成一次顺序请求——抢输那条路根本走不到（同档
SQLite 用例只好用引擎事件把窗口撑开，那是模拟，不是真并发）。PG 是 READ COMMITTED、
逐语句取快照，两件真机制只在这里成立：

* 唯一索引：抢输者的 INSERT 阻塞在赢家未提交的索引项上，赢家一提交就抬手
  （unique_violation → IntegrityError），这正是 `run_reconciliation` 的 409 来源；
* 条件 UPDATE：抢输者的 `UPDATE ... WHERE price = :old` 拿到行锁后由 EvalPlanQual
  **重新求值** WHERE（此时现价已是赢家写的值）→ rowcount 0，这正是
  `_change_price` 判"我抢输了"的依据。

本库可能与其他任务共用，因此**不 DROP、不清表**：自己造带唯一后缀的数据，跑完删掉自己
造的那些。但**空库要能自举**——CI 的集成档跑在全新库上，"库里缺表就断言失败"会让这一档
在 CI 上直接红（而不是 skip），所以缺表/缺索引时自己跑一次幂等的 `alembic upgrade heads`。
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
_RETRY_TIMES = 5
_RETRY_WAIT = 60


def _has_unique_index(engine) -> bool:
    """库里已经有 uq_reconciliation_batch_date 才算就绪（表在不等于索引在）。"""
    from sqlalchemy import inspect

    inspector = inspect(engine)
    if "reconciliation_batches" not in set(inspector.get_table_names()):
        return False
    return "uq_reconciliation_batch_date" in {
        i["name"] for i in inspector.get_indexes("reconciliation_batches")
    }


@pytest.fixture(scope="module")
def pg_sessionmaker():
    """绑到真 PG 的 sessionmaker（与 app 自己那套 SQLite 引擎无关）。

    **只在缺索引时**把迁移升到 heads：CI 的集成档跑在全新库上，"缺表就断言失败"
    会让这一档在 CI 上直接变红；而共用的开发库上 `alembic upgrade heads` 是幂等的
    空操作。刻意不 `DROP SCHEMA`（那会把同时在用这个库的其他用例连锅端掉），
    与 `test_postgres_real.py` 那条"证明迁移链能白手起家"的用例分工不同。
    共用库上可能撞别人的锁，故失败重试几次再判死。
    """
    from sqlalchemy import create_engine, inspect
    from sqlalchemy.orm import sessionmaker

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
            f"在 {PG_URL} 上跑迁移 {_RETRY_TIMES} 次都没成功：\n{result.stderr[-2000:]}"
        )
        time.sleep(_RETRY_WAIT)
    assert _has_unique_index(engine), (
        "PG 库上没有 uq_reconciliation_batch_date：迁移没跑到 b9c8d7e6f5a4，"
        "或迁移探到存量重复日期而跳过了建索引（见迁移 docstring 的人工处置 SQL）"
    )
    missing = {"charge_items", "charge_price_changes"} - set(inspect(engine).get_table_names())
    assert not missing, f"PG 库里缺表 {sorted(missing)}：迁移没跑全"
    yield sessionmaker(bind=engine, autoflush=False)
    engine.dispose()


@pytest.fixture(scope="module")
def actor(pg_sessionmaker):
    """留痕用的操作者：共用库里没有可用账号就自己建一个（用户名带随机后缀）。"""
    from app.models import User

    db = pg_sessionmaker()
    try:
        existing = db.query(User.id).order_by(User.id).first()
        if existing:
            return existing[0]
        user = User(
            username=f"p130_billing_race_{uuid.uuid4().hex[:8]}",
            password_hash="x", role="operator", full_name="并发用例",
        )
        db.add(user)
        db.commit()
        return user.id
    finally:
        db.close()


def _race_on_pg(worker, times=8):
    """Barrier 真并发（写法同 `test_postgres_real._race_on_pg`）。

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


class _Actor:
    """`run_reconciliation` / `reprice_charge_item` 只用到 user.id。"""

    def __init__(self, uid):
        self.id = uid


def _free_date(pg_sessionmaker):
    """挑一个远期且当前没有对账单的日期：共用库，不能跟别的任务/别的用例撞同一天
    （`reconciliation_batches.date` 是全局键，撞了这条用例的红绿就没有意义）。"""
    import random
    from datetime import date as date_cls
    from datetime import timedelta

    from app.models import ReconciliationBatch

    db = pg_sessionmaker()
    try:
        for _ in range(20):
            candidate = (date_cls(2100, 1, 1) + timedelta(days=random.randrange(30000))).isoformat()
            taken = (
                db.query(ReconciliationBatch.id)
                .filter(ReconciliationBatch.date == candidate)
                .first()
            )
            if taken is None:
                return candidate
        raise AssertionError("连挑 20 个远期日期都已被占用，共用库里像是有存量脏数据")
    finally:
        db.close()


# ================================================================ 日终对账：一天一张单


def test_八路同日对账在真PG上恰得一张单(pg_sessionmaker, actor):
    """并发重跑：赢家提交前抢输者阻塞在索引项上，提交后拿 unique_violation → 409。

    断言用宽松形状（同 `test_并发同证件号建档_唯一约束兜底后恰得一行`）：读在赢家提交
    **之后**的那一路会如实看到旧批次、删掉它再建自己的——那是文档写明的"重跑覆盖"，
    不是缺陷。不变量只有两条：**没有未捕获异常**、**这一天最后只剩一张单**。

    修复前在同一个库上实测：8 路里 5 路抛出未捕获的
    `UniqueViolation ... uq_reconciliation_batch_date`（生产上就是 500 加一段栈，
    前端只看得到"服务器错误"）；索引落库之前更早的那一版则是静默八张单。
    """
    from fastapi import HTTPException

    from app.models import ReconciliationBatch, ReconciliationDiff
    from app.routers.billing import run_reconciliation

    date = _free_date(pg_sessionmaker)
    user = _Actor(actor)

    def worker(_index):
        db = pg_sessionmaker()
        try:
            try:
                run_reconciliation(date=date, db=db, user=user)
            except HTTPException as exc:
                db.rollback()
                return ("http", exc.status_code, exc.detail)
            return ("ok", 201, None)
        finally:
            db.close()

    results, errors = _race_on_pg(worker)
    assert not errors, f"并发下出现未捕获异常（生产上就是 500）：{errors}"
    ok = [r for r in results if r[0] == "ok"]
    conflicts = [r for r in results if r[0] == "http"]
    assert len(ok) >= 1, f"一路都没跑成：{results}"
    assert len(ok) + len(conflicts) == 8, results
    assert {(c[1], c[2]) for c in conflicts} <= {
        (409, "该日期的对账刚由另一请求完成，请刷新查看最新对账单")
    }, f"抢输者的状态码/文案不对：{conflicts}"

    db = pg_sessionmaker()
    try:
        rows = db.query(ReconciliationBatch).filter(ReconciliationBatch.date == date).all()
        assert len(rows) == 1, f"同一天在 PG 上跑出了 {len(rows)} 张对账单"
        ids = [b.id for b in rows]
        db.query(ReconciliationDiff).filter(ReconciliationDiff.batch_id.in_(ids)).delete(
            synchronize_session=False
        )
        db.query(ReconciliationBatch).filter(ReconciliationBatch.id.in_(ids)).delete(
            synchronize_session=False
        )
        db.commit()  # 共用库：自己造的数据自己收走
    finally:
        db.close()


# ================================================================ 调价：一次跃迁一行历史


def _charge_item(pg_sessionmaker, price):
    from app.models import ChargeItem

    db = pg_sessionmaker()
    try:
        item = ChargeItem(
            code=f"P130-RACE-{uuid.uuid4().hex[:10]}", name="并发调价项目",
            category="exam", price=price,
        )
        db.add(item)
        db.commit()
        return item.id
    finally:
        db.close()


def _cleanup_item(pg_sessionmaker, item_id):
    from app.models import ChargeItem, ChargePriceChange

    db = pg_sessionmaker()
    try:
        db.query(ChargePriceChange).filter(ChargePriceChange.item_id == item_id).delete(
            synchronize_session=False
        )
        db.query(ChargeItem).filter(ChargeItem.id == item_id).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_八路同价调价在真PG上只留一行历史(pg_sessionmaker, actor):
    """八路同时把 10 改成 12：EvalPlanQual 让抢输者的 WHERE 重新对上赢家写的 12，
    rowcount 0 → 刷新后同价 → 与顺序请求同一句 409。价格只跳一次，历史只留一行
    （修复前实测八路八行 `10→12`）。"""
    from fastapi import HTTPException

    from app.models import ChargePriceChange
    from app.routers.billing import RepriceIn, reprice_charge_item

    item_id = _charge_item(pg_sessionmaker, 10)
    user = _Actor(actor)
    try:
        def worker(_index):
            db = pg_sessionmaker()
            try:
                try:
                    reprice_charge_item(
                        item_id=item_id,
                        body=RepriceIn(new_price=12, reason="并发", effective_date="2031-01-01"),
                        db=db, user=user,
                    )
                except HTTPException as exc:
                    db.rollback()
                    return ("http", exc.status_code, exc.detail)
                return ("ok", 200, None)
            finally:
                db.close()

        results, errors = _race_on_pg(worker)
        assert not errors, f"并发下出现未捕获异常（生产上就是 500）：{errors}"
        assert len([r for r in results if r[0] == "ok"]) == 1, f"同价并发应恰一路调成：{results}"
        assert {(r[1], r[2]) for r in results if r[0] == "http"} == {
            (409, "新价格与现价相同，无需调价")
        }, results

        db = pg_sessionmaker()
        try:
            rows = (
                db.query(ChargePriceChange)
                .filter(ChargePriceChange.item_id == item_id)
                .order_by(ChargePriceChange.id)
                .all()
            )
            assert [(r.old_price, r.new_price) for r in rows] == [(10, 12)], (
                "一次价格跃迁只该留一行历史"
            )
        finally:
            db.close()
    finally:
        _cleanup_item(pg_sessionmaker, item_id)


def test_八路异价调价在真PG上历史不断链(pg_sessionmaker, actor):
    """八路各要一个不同的新价：抢输者拿 409「现价已被其他操作修改，请刷新后重试」，
    不替他从一个他没见过的价往下接链。

    不变量不是"只成功一路"（读在赢家提交之后的那一路是**合法的**顺序调价），
    而是历史首尾相接成一条链、链头是初始价、链尾等于现价。修复前八路会写出
    八行同 `old_price=20` 的并列幽灵链，价格轨迹当场断掉。
    """
    from fastapi import HTTPException

    from app.models import ChargeItem, ChargePriceChange
    from app.routers.billing import RepriceIn, reprice_charge_item

    item_id = _charge_item(pg_sessionmaker, 20)
    user = _Actor(actor)
    try:
        def worker(index):
            db = pg_sessionmaker()
            try:
                try:
                    reprice_charge_item(
                        item_id=item_id, body=RepriceIn(new_price=21 + index), db=db, user=user
                    )
                except HTTPException as exc:
                    db.rollback()
                    return ("http", exc.status_code, exc.detail)
                return ("ok", 200, None)
            finally:
                db.close()

        results, errors = _race_on_pg(worker)
        assert not errors, f"并发下出现未捕获异常（生产上就是 500）：{errors}"
        ok = [r for r in results if r[0] == "ok"]
        assert len(ok) >= 1, f"一路都没调成：{results}"
        assert len(ok) + len([r for r in results if r[0] == "http"]) == 8, results
        assert {(r[1], r[2]) for r in results if r[0] == "http"} <= {
            (409, "现价已被其他操作修改，请刷新后重试"),
            (409, "新价格与现价相同，无需调价"),
        }, results

        db = pg_sessionmaker()
        try:
            chain = [
                (r.old_price, r.new_price)
                for r in db.query(ChargePriceChange)
                .filter(ChargePriceChange.item_id == item_id)
                .order_by(ChargePriceChange.id)
                .all()
            ]
            assert len(chain) == len(ok), "成功几次就该留几行历史"
            assert chain[0][0] == 20, f"链头必须是初始价：{chain}"
            for prev, nxt in zip(chain, chain[1:]):
                assert prev[1] == nxt[0], f"历史链断了（并列的幽灵链）：{chain}"
            assert chain[-1][1] == db.get(ChargeItem, item_id).price, "链尾必须等于现价"
        finally:
            db.close()
    finally:
        _cleanup_item(pg_sessionmaker, item_id)
