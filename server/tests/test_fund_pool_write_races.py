"""基金池年终清算与重跑预结、追加预付、改池子同时到：清算单按旧数结出结余，而且不能重做（P2-1185，第三十四批扫描 L1-6）。

`settle` 原先锁外判「执行中」、按当时各期预结之和算发生额，再置已清算、插清算单；`close_period` / `add_prepayment` /
`update_pool` 也都是锁外判完就写。重跑 12 月预结（90 万 → 130 万）夹在清算算完发生额与写入之间提交：清算单结余
120 万、池子账面结余 80 万，分配按清算单多分 40 万——清算单一个池子只有一张，改不了；顺序发生时清算之后再预结、再预付、
再改池子都是 409。同形的还有预付（一笔预付记在已清算的池子上）与改池子（清算按旧总额结完，总额随后被改掉）。

修法：四个写入口都进池子这一行的临界区（`serialized_on(FundPool)`），锁里刷新之后再判状态、在锁里提交；清算置已清算
改成带状态条件的 UPDATE。时序同 `test_emergency_milestone_order_race.py`：A 路发第一条写语句之前（引擎的
before_cursor_execute）起 B 路、等它最多两秒，再另开一个会话读池子此刻的状态——修前 B 很快提交，A 随后照着锁外读到的
旧数写；修后 B 卡在临界区外，A 提交出块后 B 才进去，读到的是 A 写下的结果。另一半时序（B 赶在 A 拿到锁之前提交）
替换 `serialized_on` 插进去：A 锁到手后读到的是 B 写下的数，与先 B 后 A 的顺序结果一样。
"""
import contextlib
import threading

import pytest
from fastapi import HTTPException
from sqlalchemy import event

from app.database import SessionLocal, engine
from app.models import FundPool, User
from app.routers.fund import (
    PeriodIn,
    PoolUpdate,
    PrepaymentIn,
    SettleIn,
    add_prepayment,
    close_period,
    settle,
    update_pool,
)

F = "/api/fund"
_years = iter(range(2041, 2060))


@pytest.fixture()
def pool(client, admin):
    """筹资 1200 万、1–12 月各预结 90 万（人工核定）的执行中池子，每条用例一个年度。"""
    year = next(_years)
    created = client.post(f"{F}/pools", headers=admin, json={"year": year, "total_amount": 12000000, "prepay_ratio_pct": 50})
    assert created.status_code == 201, created.text
    pool_id = created.json()["id"]
    for month in range(1, 13):
        closed = client.post(f"{F}/pools/{pool_id}/periods", headers=admin, json={
            "period": f"{year}-{month:02d}", "actual_amount": 900000, "note": "人工核定"})
        assert closed.status_code == 201, closed.text
    return {"id": pool_id, "year": year}


def _run(action):
    """直接调端点函数（自己一个会话、以 admin 身份），返回（状态码, 文案）；没抛即记 200。"""
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == "admin").one()
        try:
            action(db, user)
        except HTTPException as exc:
            return exc.status_code, exc.detail
        return 200, ""


def _status(pool_id):
    with SessionLocal() as db:
        return db.get(FundPool, pool_id).status


def _race(pool_id, first_write, mine, other):
    """A 路（mine）发第一条以 `first_write` 开头的写语句之前起 B 路（other）、等它最多两秒，记下那一刻池子的状态。"""
    result: dict = {}
    fired: list = []

    def run_other():
        result["other"] = _run(other)

    def listener(conn, cursor, statement, parameters, context, executemany):
        if not fired and statement.lstrip().upper().startswith(first_write):
            fired.append(True)   # 先记上：B 路与下面读状态的语句也会经过这里
            thread = threading.Thread(target=run_other)
            thread.start()
            thread.join(timeout=2)   # 修前 B 很快跑完；修后 B 卡在池子的临界区外，等满两秒放 A 往下写
            fired.append(_status(pool_id))   # A 这一笔写下去的那一刻，池子是什么状态
            fired.append(thread)

    event.listen(engine, "before_cursor_execute", listener)
    try:
        result["mine"] = _run(mine)
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert fired, "插桩没有触发：A 路不再发这条写语句了，换一个判定之后、写入之前的插点"
    fired[2].join(timeout=30)
    return result["mine"], result["other"], fired[1]


def _book(client, admin, pool):
    """池子的账面（筹资、已归集发生额、账面结余）与清算单。"""
    rows = client.get(f"{F}/pools", headers=admin, params={"year": pool["year"]}).json()
    me = next(row for row in rows if row["id"] == pool["id"])
    settlement = client.get(f"{F}/pools/{pool['id']}/settlement", headers=admin)
    assert settlement.status_code == 200, settlement.text
    return me, settlement.json()


def _december(client, admin, pool):
    rows = client.get(f"{F}/pools/{pool['id']}/periods", headers=admin).json()
    return next((row["actual_amount"], row["note"]) for row in rows if row["period"] == f"{pool['year']}-12")


def test_清算算完发生额还没写时重跑12月预结_预结409_清算单结余等于账面结余(client, admin, pool):
    mine, other, _ = _race(
        pool["id"], ("UPDATE FUND_POOLS", "INSERT INTO FUND_SETTLEMENTS"),
        lambda db, user: settle(pool["id"], SettleIn(overrun_action="none"), db=db, user=user),
        lambda db, user: close_period(pool["id"], PeriodIn(period=f"{pool['year']}-12", actual_amount=1300000,
                                                           note="12月补核定"), db=db),
    )
    assert mine == (200, "")
    # 修前 (200, '')：清算按 1080 万结出结余 120 万，这一期随后改成 130 万，账面结余只剩 80 万
    assert other == (409, "基金池状态为 已清算，不可再预结"), other   # 与清算之后再预结同一句
    me, settlement = _book(client, admin, pool)
    assert _december(client, admin, pool) == (900000, "人工核定")
    assert (settlement["total_expense"], settlement["balance"]) == (10800000, 1200000)
    assert settlement["balance"] == me["book_balance"] and settlement["total_expense"] == me["accrued_expense"]


def test_清算锁外判完_进锁之前重跑12月预结先提交_清算按新发生额结(client, admin, pool, monkeypatch):
    """另一半时序：重跑预结赶在清算拿到锁之前提交——清算锁到手后读到的是 130 万，与先预结后清算的顺序结果一样。

    钉的是「发生额在锁里取」：谁把这笔 SUM 挪回锁外，这里就按 1080 万结出结余 120 万、账面却只剩 80 万。
    """
    from app.routers import fund

    real, fired = fund.serialized_on, []

    @contextlib.contextmanager
    def racing(db, model, row_id):
        if not fired:   # 先记上：插进来的预结自己也要进这把锁
            fired.append("预结")
            fired.append(_run(lambda db2, user: close_period(pool["id"], PeriodIn(
                period=f"{pool['year']}-12", actual_amount=1300000, note="12月补核定"), db=db2)))
        with real(db, model, row_id):
            yield

    monkeypatch.setattr(fund, "serialized_on", racing)
    mine = _run(lambda db, user: settle(pool["id"], SettleIn(overrun_action="none"), db=db, user=user))
    monkeypatch.undo()
    assert fired, "插桩没有触发：清算不再进池子这把锁"
    assert fired[1] == (200, "") and mine == (200, "")
    me, settlement = _book(client, admin, pool)
    assert (settlement["total_expense"], settlement["balance"]) == (11200000, 800000)
    assert settlement["balance"] == me["book_balance"]


def test_预付写下去之前清算先到_预付不记在已清算的池子上(client, admin, pool):
    mine, other, moment = _race(
        pool["id"], ("INSERT INTO FUND_PREPAYMENTS",),
        lambda db, user: add_prepayment(pool["id"], PrepaymentIn(amount=100000, batch_no="P21185"), db=db, user=user),
        lambda db, user: settle(pool["id"], SettleIn(overrun_action="none"), db=db, user=user),
    )
    assert moment == "active", "修前：清算在预付判完「执行中」之后提交，这笔预付记在已清算的池子上"
    assert mine == (200, "") and other == (200, "")   # 预付先落、清算随后照常结
    assert _status(pool["id"]) == "settled"


def test_改筹资总额写下去之前清算先到_清算按改后的总额结_账面对得上(client, admin, pool):
    mine, other, moment = _race(
        pool["id"], ("UPDATE FUND_POOLS",),
        lambda db, user: update_pool(pool["id"], PoolUpdate(total_amount=13000000), db=db),
        lambda db, user: settle(pool["id"], SettleIn(overrun_action="none"), db=db, user=user),
    )
    assert moment == "active", "修前：清算按 1200 万结完之后，总额才被改成 1300 万"
    assert mine == (200, "") and other == (200, "")
    me, settlement = _book(client, admin, pool)
    assert (settlement["total_income"], settlement["balance"]) == (13000000, 2200000)   # 修前 1200 万 / 120 万
    assert settlement["total_income"] == me["total_amount"] and settlement["balance"] == me["book_balance"]


def test_不并发时_先重跑预结再清算按新发生额_清算之后四个写入口照旧409(client, admin, pool):
    rerun = client.post(f"{F}/pools/{pool['id']}/periods", headers=admin, json={
        "period": f"{pool['year']}-12", "actual_amount": 1300000, "note": "12月补核定"})
    assert rerun.status_code == 201, rerun.text
    assert (rerun.json()["actual_amount"], rerun.json()["source"]) == (1300000, "manual")
    periods = client.get(f"{F}/pools/{pool['id']}/periods", headers=admin).json()
    assert len(periods) == 12   # 重跑是覆盖，不是再插一期
    assert _december(client, admin, pool) == (1300000, "12月补核定")

    settled = client.post(f"{F}/pools/{pool['id']}/settle", headers=admin, json={})
    assert settled.status_code == 201, settled.text
    assert (settled.json()["total_expense"], settled.json()["balance"]) == (11200000, 800000)
    me, _ = _book(client, admin, pool)
    assert me["book_balance"] == 800000

    again = [
        client.post(f"{F}/pools/{pool['id']}/periods", headers=admin, json={"period": f"{pool['year']}-12"}),
        client.post(f"{F}/pools/{pool['id']}/prepayments", headers=admin, json={"amount": 1}),
        client.patch(f"{F}/pools/{pool['id']}", headers=admin, json={"total_amount": 1}),
        client.post(f"{F}/pools/{pool['id']}/settle", headers=admin, json={}),
    ]
    assert [(r.status_code, r.json()["detail"]) for r in again] == [
        (409, "基金池状态为 已清算，不可再预结"),
        (409, "基金池状态为 已清算，不可再预付"),
        (409, "已清算的基金池不可修改"),
        (409, "基金池状态为 已清算，不可清算"),
    ]
