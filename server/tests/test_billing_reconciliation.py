"""P1-30：日终对账"一天一张单"落成唯一索引之后的回归。

洞的形状：`run_reconciliation` 是"先查同日期批次 → 删掉它和它的差异明细 → 插一张新的"，
两步之间没有闸门。文档写的是"同一日期重跑覆盖上一批次（对账单以最后一次结果为准）"，
可并发下两路各自删完再各自插入——**不报错，静默留下两张口径不同的对账单**：
差异明细分别挂在两个 batch_id 下，`GET /api/billing/reconciliation?date=` 原样返回两张，
事后没人分得清哪张算数，而对账单正是拿去跟通道对钱、给财务签字的东西。

迁移 `b9c8d7e6f5a4` 把它下沉为**全量唯一索引** `uq_reconciliation_batch_date`
（`date` 列 NOT NULL，没有 NULL 洞，故不需要部分条件——写成部分索引反而会留缺口），
接口层在 `db.flush()` 上捕获 `IntegrityError` 后回滚并给 409。
之所以不是 `insert_or_conflict`：那个助手会当场 commit，把"批次 + 合计 + 全部差异明细"
一次提交拆成两个事务，中途崩了或被并发 GET 撞见就是一张零合计的半截对账单。

本档钉四件事：
1. **顺序语义不变**：同日期重跑仍是 201 且覆盖旧批次，不同日期各留一张；
2. **并发下恰得一张**：抢输者拿 409 且文案固定（前端 `#recon-msg` 直接显示这句）；
3. **防拆卸静态钉**：索引必须留在模型上、带 `unique=True`、键是 `(date)`、
   **且不是部分索引**（加了条件就等于给某些行留了后门）；
4. **绕开接口层直插**：库自己拦得住——接口层的 try/except 在顺序请求下永远不触发，
   行为用例分辨不出兜底是否真的生效，SQLite 的库级写锁又让线程探针对"拆掉索引"
   不敏感，直插才是确定性的网（同 test_logical_unique_races 的分工）。
"""
import contextlib
import threading

import pytest
from sqlalchemy import event
from sqlalchemy import inspect as sa_inspect

from conftest import login, reset_database

from app.database import engine
from app.models import Base

# 挑没有任何支付单的未来日期：对账跑出来是空单，竞态窗口只剩库往返，
# 也不会跟别的用例产生的当日流水互相干扰。
RACE_DATE = "2031-01-01"
OTHER_DATE = "2031-01-02"
DIRECT_DATE = "2031-01-03"
WINDOW_DATE = "2031-01-05"


@contextlib.contextmanager
def _winner_commits_before_our_insert(date):
    """把竞态窗口**确定性地**撑开：在被测请求的 INSERT 发出之前，
    让"另一路"先把同日期的对账单建好并提交。

    SQLite 的库级写锁会把线程探针大段串行化（实测常常八路全是 201，各自"删旧建新"），
    抢输那条路在 SQLite 上**不保证**走到——而它正是这次要守的分支，靠不准的用例
    钉不住它。这里用引擎事件在 `INSERT INTO reconciliation_batches` 执行前插入
    赢家的一行并提交，复现的就是生产上真实发生的顺序：我们 SELECT 时它还没提交，
    我们 INSERT 时它已经在了。真并发下的同一条路见
    `test_billing_reconciliation_unique_races.py`（真 PG，默认跳过）。
    """
    from app.database import SessionLocal
    from app.models import ReconciliationBatch

    state = {"fired": False}

    def hook(conn, cursor, statement, parameters, context, executemany):
        if state["fired"] or "INSERT INTO reconciliation_batches" not in statement:
            return
        state["fired"] = True
        other = SessionLocal()
        try:
            other.add(ReconciliationBatch(date=date, created_by=1))
            other.commit()
        finally:
            other.close()

    event.listen(engine, "before_cursor_execute", hook)
    try:
        yield state
    finally:
        event.remove(engine, "before_cursor_execute", hook)


@pytest.fixture(scope="module")
def client():
    """raise_server_exceptions=False：并发档要断言的正是"会不会出 500"，
    让异常抛进用例就看不到状态码了。"""
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routers.billing import MOCK_GATEWAY

    reset_database()
    MOCK_GATEWAY.reset()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def operator(client, admin):
    """日终对账限 operator/director（财务/经办）。"""
    org = client.post(
        "/api/organizations",
        json={"name": "对账并发县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    client.post(
        "/api/users",
        json={"username": "recon_op", "password": "pass123456", "role": "operator",
              "org_id": org["id"], "full_name": "对账员"},
        headers=admin,
    )
    return login(client, "recon_op", "pass123456")


def _batches(client, operator, date):
    resp = client.get(f"/api/billing/reconciliation?date={date}", headers=operator)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ================================================================ 顺序语义


def test_同日期重跑覆盖旧批次_不同日期各留一张(client, operator):
    """"以最后一次结果为准"是这个端点的既有设计，唯一索引不能把它拒掉。

    重跑时旧批次的 DELETE 与新批次的 INSERT 在**同一个事务里**，DELETE 先 flush，
    唯一索引看到的始终只有一行——顺序调用方感觉不到索引的存在。
    """
    first = client.post(f"/api/billing/reconciliation/run?date={RACE_DATE}", headers=operator)
    assert first.status_code == 201, first.text
    second = client.post(f"/api/billing/reconciliation/run?date={RACE_DATE}", headers=operator)
    assert second.status_code == 201, second.text

    rows = _batches(client, operator, RACE_DATE)
    assert [b["id"] for b in rows] == [second.json()["id"]], "重跑应覆盖旧批次而不是并列两张"

    # 合法的"多行"：另一个日期照样能建，唯一性只锁在同一自然日内
    other = client.post(f"/api/billing/reconciliation/run?date={OTHER_DATE}", headers=operator)
    assert other.status_code == 201, other.text
    assert len(_batches(client, operator, OTHER_DATE)) == 1


# ================================================================ 并发


def test_八路同日对账恰得一张单_抢输者拿409提示刷新(client, operator):
    """并发重跑：谁先插进去谁算数，其余的看赢家那张，而不是并列写出第二张。

    用 Barrier 卡住再一起放行——只起线程是不够的，线程创建本身有先后，
    前一个常常已经提交完了后一个才开始读，竞态窗口根本没打开。

    断言用宽松形状（同 `test_并发同证件号建档_唯一约束兜底后恰得一行`）：
    读在赢家提交**之后**的那一路会如实看到旧批次、删掉它再建自己的——那正是
    文档写明的"重跑覆盖"，不是缺陷。不变量只有一个：**这一天最后只剩一张单**。
    """
    codes: list[int] = []
    details: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def run():
        barrier.wait(timeout=30)
        resp = client.post(f"/api/billing/reconciliation/run?date={RACE_DATE}", headers=operator)
        with lock:
            codes.append(resp.status_code)
            if resp.status_code != 201:
                details.append(resp.json().get("detail", resp.text))

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert codes.count(201) >= 1, f"一路都没跑成：{codes}"
    assert codes.count(201) + codes.count(409) == 8, f"出现了 201/409 之外的状态码：{codes}"
    assert details == ["该日期的对账刚由另一请求完成，请刷新查看最新对账单"] * codes.count(409), (
        f"抢输者的文案不对：{details}"
    )
    rows = _batches(client, operator, RACE_DATE)
    assert len(rows) == 1, f"同一天并发跑出了 {len(rows)} 张对账单"


def test_抢输那一路拿409而不是500也不写出第二张单(client, operator):
    """确定性地走一遍抢输分支：`db.flush()` 撞 uq_reconciliation_batch_date。

    这一条钉的是"接得住"而不只是"拦得住"——没有 try/except 时同样只剩一行
    （库拦住了），但调用方拿到的是 500 加一段栈，前端 `#recon-msg` 显示不出任何人话，
    而且写事务不回滚会连累后面的审计留痕。
    """
    with _winner_commits_before_our_insert(WINDOW_DATE) as state:
        resp = client.post(
            f"/api/billing/reconciliation/run?date={WINDOW_DATE}", headers=operator
        )
    assert state["fired"], "窗口没撑开（INSERT 语句形状变了？），这条用例没测到东西"
    assert resp.status_code == 409, f"抢输者应拿 409，实际 {resp.status_code}：{resp.text}"
    assert resp.json() == {"detail": "该日期的对账刚由另一请求完成，请刷新查看最新对账单"}
    rows = _batches(client, operator, WINDOW_DATE)
    assert len(rows) == 1, f"抢输的那一路仍写出了第二张单：{rows}"

    # 回滚干净：拿到 409 之后同一日期还能正常重跑（写锁/事务没被握着不放）
    again = client.post(f"/api/billing/reconciliation/run?date={WINDOW_DATE}", headers=operator)
    assert again.status_code == 201, again.text
    assert len(_batches(client, operator, WINDOW_DATE)) == 1


def test_并发跑完不留孤儿差异明细(client, operator):
    """被删批次的差异明细必须跟着走：留下 batch_id 指向已删批次的明细行，
    对账页会把它算进别人的单子里（`_batch_out` 是按 batch_id 取明细的）。"""
    from app.database import SessionLocal
    from app.models import ReconciliationBatch, ReconciliationDiff

    db = SessionLocal()
    try:
        alive = {b for (b,) in db.query(ReconciliationBatch.id).all()}
        orphans = [
            d for (d,) in db.query(ReconciliationDiff.batch_id).distinct().all() if d not in alive
        ]
        assert orphans == [], f"这些 batch_id 已不存在却还挂着差异明细：{orphans}"
    finally:
        db.close()


# ================================================================ 防拆卸静态钉


def test_每日一张对账单的唯一索引不许消失():
    """模型侧的声明就是这条不变式的落点，删掉就等于把静默双写的洞放回去。

    同时钉住"**不是**部分索引"：`date` 列 NOT NULL，全量唯一恰好表达不变式；
    加上任何 WHERE 条件都等于给条件外的行留了一个可以并列写两张的后门。
    """
    index = next(
        (
            i
            for i in Base.metadata.tables["reconciliation_batches"].indexes
            if i.name == "uq_reconciliation_batch_date"
        ),
        None,
    )
    assert index is not None, "uq_reconciliation_batch_date 没了——一天两张对账单的洞回来了"
    assert index.unique, "uq_reconciliation_batch_date 不再是唯一索引，等于没有约束"
    assert [c.name for c in index.columns] == ["date"], "对账单的自然键就是日期，键变了"
    for dialect in ("sqlite", "postgresql"):
        where = index.dialect_options[dialect].get("where")
        assert where is None, (
            f"{dialect} 上被改成了部分索引（where={where}）："
            "date 是 NOT NULL 列，全量唯一才是这条不变式，加条件等于留后门"
        )


def test_对账单唯一索引真的建在库上():
    """模型声明了、库里没建过（漏迁移）同样等于没有约束——按真实表结构再钉一遍。"""
    names = {i["name"] for i in sa_inspect(engine).get_indexes("reconciliation_batches")}
    assert "uq_reconciliation_batch_date" in names, (
        "reconciliation_batches 上没有 uq_reconciliation_batch_date（库与模型对不上）"
    )


def test_绕开接口层直插时库里真的拦得住(client, operator):
    """索引"在不在"与"拦不拦得住"是两回事。

    接口层的 try/except 在顺序请求下**永远不触发**（重跑是先删后插），行为用例因此
    分辨不出兜底是否真的生效；SQLite 的库级写锁又让线程探针对"拆掉索引"不敏感。
    这里绕开接口层直接写库——那正是并发抢输者实际到达的位置——看数据库自己是否抬手。
    """
    from sqlalchemy.exc import IntegrityError

    from app.database import SessionLocal
    from app.models import ReconciliationBatch

    db = SessionLocal()
    try:
        db.add(ReconciliationBatch(date=DIRECT_DATE, created_by=1))
        db.commit()
        db.add(ReconciliationBatch(date=DIRECT_DATE, created_by=1))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        # 不同日期不受影响（全量唯一只锁同一天）
        db.add(ReconciliationBatch(date="2031-01-04", created_by=1))
        db.commit()
    finally:
        db.close()
