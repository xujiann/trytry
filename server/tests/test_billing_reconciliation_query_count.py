"""日终对账只取当天的支付单：日期条件下推到 SQL，Mock 流水复用本地侧那一批（P2-1577，第四十六批扫描 AJ1-7）。

修前 `billing._orders_of_day` 在 SQL 里只按状态与流水号取、不带日期条件，整表读进内存后在 Python 里按
`paid_at.strftime("%Y-%m-%d") == date` 筛；Mock 通道出流水时又调一遍同一个函数。扫描实测：表里 3650 张已支付单，对其中
一天（10 张）对账，实例化 7290 个 PaymentOrder、2 条整表 SELECT——存量越多越慢，到百万级一次对账要实例化约两百万个对象。

修法：`paid_at` 落在 `[该日 00:00:00, 次日 00:00:00)`（naive UTC）下推到 SQL，日界与原判据逐字节一致（对账的日该切在
UTC 还是本地日历是 P2-35，待裁定，这里不动）；Mock 镜像本地侧已取出的那一批，不再重查。

照 `test_paged_list_query_count.py` 的写法数（这里数的是 ORM 实例化的支付单张数与 `SELECT … FROM payment_orders` 条数）：
1. 别的日子落几千张已支付单，对一天对账：加载的支付单只等于当天的单数、SELECT 只一条；再落几千张，两个数不变；
2. 日界：该日 00:00:00 与 23:59:59.999999 的单在，前一日 23:59:59.999999、次日 00:00:00 的不在（各归各的日，没丢）；
   9999-12-31 没有次日，不设上界，照收当天的单；
3. 改前改后同一天的对账结果（matched / unmatched / 各类差异）逐字段一致：把原实现（原样抄在下面）换回去再跑一遍当对照。
"""
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, insert

from app.database import SessionLocal, engine
from app.models import PaymentOrder, User
from app.routers import billing
from app.routers.billing import MOCK_GATEWAY

DAY = "2026-03-15"
PREV_DAY, NEXT_DAY, LAST_DAY = "2026-03-14", "2026-03-16", "9999-12-31"
START = datetime(2026, 3, 15)
MICRO = timedelta(microseconds=1)

#: 当天进对账的单，按落库先后（= id 先后）：(流水号, 渠道, 金额, 已退, 状态, paid_at)
IN_DAY = [
    ("P21577-START", "cash", 100, 0, "paid", START),                                   # 该日 00:00:00
    ("P21577-PART", "online", 200, 50, "paid", START + timedelta(hours=8)),            # 部分退款：净额 150
    ("P21577-FULL", "card", 80, 80, "refunded", START + timedelta(hours=11)),          # 全额退款：净额 0
    ("P21577-DROP", "cash", 60, 0, "paid", START + timedelta(hours=14)),               # 通道缺这笔
    ("P21577-OVER", "insurance", 120, 0, "paid", START + timedelta(hours=17)),         # 通道金额不同
    ("P21577-END", "online", 30, 0, "paid", START + timedelta(days=1) - MICRO),        # 该日 23:59:59.999999
]
#: 前后两日紧贴日界的两张
NEIGHBOURS = [
    ("P21577-PREV", "cash", 10, 0, "paid", START - MICRO),                # 前一日 23:59:59.999999
    ("P21577-NEXT", "cash", 10, 0, "paid", START + timedelta(days=1)),    # 次日 00:00:00
]
#: 当天落库、但不算对账口径的：待支付 / 失败的没有 paid_at，已支付却没有流水号的
NOT_COUNTED = [
    ("", "online", 10, 0, "pending", None),
    ("", "card", 10, 0, "failed", None),
    ("", "cash", 10, 0, "paid", START + timedelta(hours=9)),
]
FAR_END = ("P21577-9999", "cash", 10, 0, "paid", datetime(9999, 12, 31, 23, 59, 59, 999999))


def _elsewhere(tag: str, n: int) -> list[tuple]:
    """别的日子的已支付单 n 张：一半在该日之前、一半在之后，最近的贴着前后两日，都不落在该日。"""
    specs = []
    for k in range(n):
        at = START + timedelta(hours=k % 24, minutes=k % 60)   # 先取该日内某一刻，再整天整天地挪出去
        shift = timedelta(days=1 + k // 2 % 300)
        specs.append((f"P21577-{tag}{k:05d}", "cash", 10, 0, "paid", at - shift if k % 2 else at + shift))
    return specs


def _insert(world: dict, specs: list[tuple]) -> None:
    rows = [
        {"settlement_id": world["settlement_id"], "channel": channel, "amount": amount, "refunded_amount": refunded,
         "status": status, "trade_no": trade_no, "fail_reason": "", "paid_at": paid_at,
         "created_by": world["created_by"], "created_at": paid_at or START}
        for trade_no, channel, amount, refunded, status, paid_at in specs
    ]
    with SessionLocal() as db:
        db.execute(insert(PaymentOrder), rows)
        db.commit()


@pytest.fixture(autouse=True)
def clean_gateway():
    """每个用例前后复位 Mock 通道开关，避免用例间串扰。"""
    MOCK_GATEWAY.reset()
    yield
    MOCK_GATEWAY.reset()


@pytest.fixture(scope="module")
def world(client, admin):
    """一张门诊结算单下挂上面这些支付单，外加别的日子的三千张已支付单。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21577 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    client.post("/api/dictionaries/charge/entries", headers=admin, json={"code": "P21577-FEE", "name": "诊查费(P21577)"})
    assert client.post("/api/billing/charge-items", headers=admin, json={
        "code": "P21577-FEE", "name": "诊查费(P21577)", "category": "treatment", "price": 100}).status_code == 201
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21577 患者", "id_card": "330106198001011577"}).json()["id"]
    encounter = client.post("/api/encounters", headers=admin, json={
        "patient_id": patient, "org_id": org, "diagnosis_name": "复诊"}).json()["id"]
    client.post("/api/billing/details", headers=admin, json={
        "patient_id": patient, "encounter_id": encounter, "item_code": "P21577-FEE"})
    settled = client.post("/api/billing/settlements", headers=admin, json={
        "bill_type": "outpatient", "encounter_id": encounter, "insurance_pay": 0})
    assert settled.status_code == 201, settled.text
    with SessionLocal() as db:
        creator = db.query(User.id).filter(User.username == "admin").scalar()
    world = {"settlement_id": settled.json()["id"], "created_by": creator}
    _insert(world, IN_DAY + NEIGHBOURS + NOT_COUNTED + [FAR_END] + _elsewhere("A", 3000))
    return world


@contextmanager
def count_payment_orders():
    """数这段时间里 ORM 实例化了几张支付单、发了几条 `SELECT … FROM payment_orders`。"""
    seen = {"loaded": 0, "selects": 0}

    def _loaded(target, context):
        seen["loaded"] += 1

    def _statement(conn, cursor, statement, parameters, context, executemany):
        sql = statement.lstrip().upper()
        if sql.startswith("SELECT") and "FROM PAYMENT_ORDERS" in sql:
            seen["selects"] += 1

    event.listen(PaymentOrder, "load", _loaded)
    event.listen(engine, "before_cursor_execute", _statement)
    try:
        yield seen
    finally:
        event.remove(PaymentOrder, "load", _loaded)
        event.remove(engine, "before_cursor_execute", _statement)


def _run(client, admin, day: str = DAY) -> dict:
    resp = client.post(f"/api/billing/reconciliation/run?date={day}", headers=admin)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_对一天对账只加载当天的支付单_不随总行数涨(client, admin, world):
    with count_payment_orders() as first:
        assert _run(client, admin)["total_orders"] == len(IN_DAY)
    _insert(world, _elsewhere("B", 3000))   # 再落三千张别的日子的已支付单
    with count_payment_orders() as second:
        assert _run(client, admin)["total_orders"] == len(IN_DAY)
    counts = [(seen["loaded"], seen["selects"]) for seen in (first, second)]
    assert counts == [(len(IN_DAY), 1)] * 2, (
        f"对 {DAY}（{len(IN_DAY)} 张）对账：(实例化支付单张数, SELECT payment_orders 条数) 依次为 {counts}"
        "——应只取当天的单、Mock 流水复用这一批（修前整表读两遍，随总行数涨）"
    )


def test_日界_该日零点与最后一微秒在_前后两日紧贴日界的不在(client, admin, world):
    with SessionLocal() as db:
        day = [o.trade_no for o in billing._orders_of_day(db, DAY)]
        prev_day = {o.trade_no for o in billing._orders_of_day(db, PREV_DAY)}
        next_day = {o.trade_no for o in billing._orders_of_day(db, NEXT_DAY)}
        last_day = [o.trade_no for o in billing._orders_of_day(db, LAST_DAY)]
    assert day == [spec[0] for spec in IN_DAY]   # 两头紧贴日界的都在，排序照旧按 id
    assert "P21577-PREV" in prev_day and "P21577-NEXT" in next_day   # 各归各的日
    assert last_day == ["P21577-9999"]   # 9999-12-31 没有次日：不设上界

    # 接口走的是同一个口径：通道把这些流水全丢掉，进了对账的本地单逐张以「本地有通道无」点名
    MOCK_GATEWAY.drop_trade_nos = {spec[0] for spec in IN_DAY + NEIGHBOURS}
    assert [(d["diff_type"], d["trade_no"]) for d in _run(client, admin)["diffs"]] == [
        ("missing_remote", spec[0]) for spec in IN_DAY]
    assert _run(client, admin, LAST_DAY)["total_orders"] == 1


def _orders_of_day_before_p21577(db, date):
    """修前的 `billing._orders_of_day`，原样抄来当对照：整表读进内存，再在 Python 里按 UTC 日期筛。"""
    return [
        o
        for o in db.query(PaymentOrder)
        .filter(PaymentOrder.status.in_(["paid", "refunded"]), PaymentOrder.trade_no != "")
        .order_by(PaymentOrder.id)
        .all()
        if o.paid_at and o.paid_at.strftime("%Y-%m-%d") == date
    ]


def _comparable(batch: dict) -> dict:
    """去掉每跑一次都会变的批次号、生成时刻与差异行号，其余逐字段比。"""
    kept = {k: v for k, v in batch.items() if k not in ("id", "created_at", "diffs")}
    return {**kept, "diffs": [{k: v for k, v in d.items() if k != "id"} for d in batch["diffs"]]}


@pytest.mark.parametrize("day", [DAY, PREV_DAY, NEXT_DAY, LAST_DAY])
def test_改前改后同一天的对账结果逐字段一致(client, admin, world, monkeypatch, day):
    MOCK_GATEWAY.drop_trade_nos = {"P21577-DROP"}
    MOCK_GATEWAY.amount_overrides = {"P21577-OVER": 100.0}
    MOCK_GATEWAY.extra_transactions = [{"trade_no": "P21577-GHOST", "amount": 45.5}]
    after = _comparable(_run(client, admin, day))
    with monkeypatch.context() as m:
        m.setattr(billing, "_orders_of_day", _orders_of_day_before_p21577)
        before = _comparable(_run(client, admin, day))
    assert after == before


def test_当天对账的数_钉死(client, admin, world):
    """上一条只证明改前改后相同；这里把当天的数钉死，免得两边错得一样。"""
    MOCK_GATEWAY.drop_trade_nos = {"P21577-DROP"}
    MOCK_GATEWAY.amount_overrides = {"P21577-OVER": 100.0}
    MOCK_GATEWAY.extra_transactions = [{"trade_no": "P21577-GHOST", "amount": 45.5}]
    batch = _run(client, admin)
    assert (batch["total_orders"], batch["total_amount"], batch["matched"], batch["unmatched"],
            batch["diff_amount"]) == (6, 460, 4, 3, 125.5)
    assert [(d["diff_type"], d["trade_no"], d["local_amount"], d["remote_amount"]) for d in batch["diffs"]] == [
        ("missing_remote", "P21577-DROP", 60, 0),
        ("amount_mismatch", "P21577-OVER", 120, 100),
        ("missing_local", "P21577-GHOST", 0, 45.5),
    ]
