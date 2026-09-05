"""P1-30：收费项目调价——"每条历史行的 old_price 必须等于写入那一刻的现价"。

洞的形状：`update_charge_item` 与 `reprice_charge_item` 都是"`db.get` 读现价 →
Python 比一下 → `db.add` 一条历史行 → 无条件 `UPDATE charge_items.price`"，
读与写之间没有闸门。两个管理员同时把 10 元改成 12 元，两路都读到 10、都留一行
`10→12`：价格只跳了一次，历史里却有两笔；改成不同价（12 与 15）更坏——历史里
出现 `10→12` 与 `10→15` 两条**并列的幽灵链**，价格轨迹从此断掉，而这张表存在的
唯一理由就是对外公示与事后解释（"上个月不是这个价"要拿得出账）。

这条不变式**不能**写成 `(item_id, old_price)` 唯一索引：10→12→10→12 这种往返调价
里 `old_price=10` 是合法重复，加索引会拒掉正常业务。它长在父行 `charge_items.price`
上，所以守法是父行条件 UPDATE——`billing._change_price`：
`UPDATE charge_items SET price=:new WHERE id=:id AND price=:old`，`rowcount == 1`
才写历史行，与 `prescriptions._apply_review` / `maternal._mark_high_risk` 同一范式。

本档钉三件事：
1. **顺序语义一字不变**：同价 reprice 仍是 409「新价格与现价相同，无需调价」，
   同价 PATCH 仍是 200 且不留痕，改名不留痕，正常调价照旧成链；
2. **抢输者的两种去向**：刷新后与请求同价 → 走顺序请求那句 409 / PATCH 静默 200；
   刷新后是第三个价 → 409「现价已被其他操作修改，请刷新后重试」。两者都**不留痕**；
3. **防拆卸静态钉**：`_change_price` 必须还在、UPDATE 的 WHERE 里必须还带
   `ChargeItem.price` 这个条件、必须还看 `rowcount`，且**全文件的
   `ChargePriceChange(...)` 只许长在它里面**——留痕一旦挪回端点里，条件就又与写入分家了。
"""
import ast
import threading
from pathlib import Path

import pytest
from sqlalchemy import event

from conftest import reset_database

from app.database import engine

BILLING_PY = Path(__file__).resolve().parents[1] / "app" / "routers" / "billing.py"

_code_seq = [0]


@pytest.fixture(scope="module")
def client():
    """raise_server_exceptions=False：并发档要断言的正是"会不会出 500"。"""
    from fastapi.testclient import TestClient

    from app.main import app

    reset_database()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def _item(client, admin, price, name="并发调价项目"):
    """每条用例用各自的收费项目：调价改的是父行本身，共用一行会让红绿取决于执行顺序。"""
    _code_seq[0] += 1
    resp = client.post(
        "/api/billing/charge-items",
        json={"code": f"RP-{_code_seq[0]:03d}", "name": name, "category": "exam", "price": price},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _history(client, admin, item_id):
    resp = client.get(f"/api/billing/charge-items/{item_id}/price-history", headers=admin)
    assert resp.status_code == 200, resp.text
    return [(h["old_price"], h["new_price"]) for h in resp.json()]


def _price(client, admin, item_id):
    rows = client.get("/api/billing/charge-items", headers=admin).json()
    return next(r["price"] for r in rows if r["id"] == item_id)


class _Window:
    """把竞态窗口**确定性地**撑开：在被测请求的 `UPDATE charge_items` 发出之前，
    让"另一路"先把价格改掉并提交（`with _Window(item_id, 12):`，出块摘钩子）。

    SQLite 的库级写锁会把线程探针大段串行化（后到的那路读到的已经是新价，走的是
    顺序语义），抢输那条路在 SQLite 上**不保证**走到——而它正是这次要守的分支，
    靠不准的用例钉不住它。引擎事件复现的就是生产上真实发生的顺序：我们读到 10，
    写的时候现价已经不是 10 了。真并发下的同一条路见
    `test_billing_reconciliation_unique_races.py`（真 PG，默认跳过）。
    """

    def __init__(self, item_id, winner_price):
        self.item_id = item_id
        self.winner_price = winner_price
        self.state = {"fired": False}

    def __enter__(self):
        from app.database import SessionLocal
        from app.models import ChargeItem

        def hook(conn, cursor, statement, parameters, context, executemany):
            if self.state["fired"] or "UPDATE charge_items" not in statement:
                return
            self.state["fired"] = True
            other = SessionLocal()
            try:
                item = other.get(ChargeItem, self.item_id)
                item.price = self.winner_price
                other.commit()
            finally:
                other.close()

        self._hook = hook
        event.listen(engine, "before_cursor_execute", hook)
        return self.state

    def __exit__(self, *exc):
        event.remove(engine, "before_cursor_execute", self._hook)
        return False


# ================================================================ 顺序语义（一字不变）


def test_同价调价仍是409且文案不变(client, admin):
    item = _item(client, admin, 10)
    resp = client.post(
        f"/api/billing/charge-items/{item['id']}/reprice", json={"new_price": 10}, headers=admin
    )
    assert resp.status_code == 409
    assert resp.json() == {"detail": "新价格与现价相同，无需调价"}
    assert _history(client, admin, item["id"]) == []


def test_同价PATCH仍是200且不留痕_改名也不留痕(client, admin):
    item = _item(client, admin, 10)
    same = client.patch(
        f"/api/billing/charge-items/{item['id']}", json={"price": 10, "name": "改名了"},
        headers=admin,
    )
    assert same.status_code == 200 and same.json()["name"] == "改名了"
    assert _history(client, admin, item["id"]) == [], "同价 PATCH 不该留痕"

    rename = client.patch(
        f"/api/billing/charge-items/{item['id']}", json={"name": "又改名"}, headers=admin
    )
    assert rename.status_code == 200
    assert _history(client, admin, item["id"]) == []


def test_往返调价的重复old_price是合法的(client, admin):
    """10→12→10→12：`old_price=10` 出现两次，这正是**不能**建
    `(item_id, old_price)` 唯一索引的原因——真加了，第二次降回 10 就被拒。"""
    item = _item(client, admin, 10)
    for price in (12, 10, 12):
        resp = client.post(
            f"/api/billing/charge-items/{item['id']}/reprice",
            json={"new_price": price, "reason": "往返调价", "effective_date": "2031-01-01"},
            headers=admin,
        )
        assert resp.status_code == 200, resp.text
    # 历史按 id 倒序返回（既有契约）：最近一次在最前
    assert _history(client, admin, item["id"]) == [(10, 12), (12, 10), (10, 12)]
    assert _price(client, admin, item["id"]) == 12


# ================================================================ 抢输者的两种去向


def test_抢输者刷新后同价_reprice仍是顺序那句409且不留痕(client, admin):
    """两个人同时把 10 改成 12：只该跳一次价、只该留一行历史。

    抢输的一路刷新后看到的就是自己想要的价——对他来说与"本来就同价"没有区别，
    所以措辞也必须是同一句。
    """
    item = _item(client, admin, 10)
    with _Window(item["id"], 12) as state:
        resp = client.post(
            f"/api/billing/charge-items/{item['id']}/reprice",
            json={"new_price": 12, "reason": "并发", "effective_date": "2031-02-01"},
            headers=admin,
        )
    assert state["fired"], "窗口没撑开（UPDATE 语句形状变了？），这条用例没测到东西"
    assert resp.status_code == 409, f"应拿 409，实际 {resp.status_code}：{resp.text}"
    assert resp.json() == {"detail": "新价格与现价相同，无需调价"}
    assert _history(client, admin, item["id"]) == [], "抢输者不得留痕"
    assert _price(client, admin, item["id"]) == 12


def test_抢输者刷新后异价_409提示刷新且不接断链(client, admin):
    """别人把 10 改成了 15，我却是照着 10 决定要改成 12 的。

    此时替他写一行 `10→12` 就是伪造：现价从来没有从 10 跳到 12 过。让他刷新重判。
    """
    item = _item(client, admin, 10)
    with _Window(item["id"], 15) as state:
        resp = client.post(
            f"/api/billing/charge-items/{item['id']}/reprice",
            json={"new_price": 12}, headers=admin,
        )
    assert state["fired"]
    assert resp.status_code == 409, f"应拿 409，实际 {resp.status_code}：{resp.text}"
    assert resp.json() == {"detail": "现价已被其他操作修改，请刷新后重试"}
    assert _history(client, admin, item["id"]) == [], "断链的历史行一条都不许写"
    assert _price(client, admin, item["id"]) == 15


def test_抢输者刷新后同价_PATCH静默200且不留痕(client, admin):
    """PATCH 在顺序请求下同价就是"静默不留痕、200"，抢输后同价也该是同一件事。"""
    item = _item(client, admin, 10)
    with _Window(item["id"], 12) as state:
        resp = client.patch(
            f"/api/billing/charge-items/{item['id']}", json={"price": 12}, headers=admin
        )
    assert state["fired"]
    assert resp.status_code == 200, resp.text
    assert resp.json()["price"] == 12
    assert _history(client, admin, item["id"]) == []


def test_抢输者刷新后异价_PATCH拿409且其余字段不落库(client, admin):
    """异价抢输时整个 PATCH 都不生效——名称跟着价格一起回滚，
    调用方拿到 409 后重来一次即可，不会出现"价格没改成、名字倒是改了"的半截结果。"""
    item = _item(client, admin, 10, name="原名")
    with _Window(item["id"], 15) as state:
        resp = client.patch(
            f"/api/billing/charge-items/{item['id']}",
            json={"price": 12, "name": "半截改名"}, headers=admin,
        )
    assert state["fired"]
    assert resp.status_code == 409
    assert resp.json() == {"detail": "现价已被其他操作修改，请刷新后重试"}
    rows = client.get("/api/billing/charge-items", headers=admin).json()
    row = next(r for r in rows if r["id"] == item["id"])
    assert row["name"] == "原名" and row["price"] == 15
    assert _history(client, admin, item["id"]) == []


# ================================================================ 八路真并发


def _race(call, times=8):
    """Barrier 真并发：只起线程不够——线程创建有先后，前一个常常已经提交完了
    后一个才开始读，竞态窗口根本没打开。"""
    results: list = []
    lock = threading.Lock()
    barrier = threading.Barrier(times)

    def run(index):
        barrier.wait(timeout=30)
        outcome = call(index)
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(times)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return results


def test_八路同价调价只跳一次价也只留一行历史(client, admin):
    item = _item(client, admin, 10)

    def call(_index):
        resp = client.post(
            f"/api/billing/charge-items/{item['id']}/reprice",
            json={"new_price": 12}, headers=admin,
        )
        return resp.status_code, resp.json().get("detail")

    results = _race(call)
    codes = sorted(code for code, _ in results)
    assert codes == [200] + [409] * 7, f"同价并发应恰一路调成：{results}"
    assert {d for c, d in results if c == 409} == {"新价格与现价相同，无需调价"}, results
    assert _history(client, admin, item["id"]) == [(10, 12)]
    assert _price(client, admin, item["id"]) == 12


def test_八路异价调价的历史必须是一条不断的链(client, admin):
    """不变量不是"只成功一路"——SQLite 会把请求整段串行化，后到的那路读到的是
    新价，于是**合法地**接着往下调（那就是顺序请求）。真正的不变量是：
    历史必须首尾相接成一条链，且链尾等于现价。旧写法在这里会写出多条同 `old_price`
    的并列行，链当场断掉。"""
    item = _item(client, admin, 20)

    def call(index):
        resp = client.post(
            f"/api/billing/charge-items/{item['id']}/reprice",
            json={"new_price": 21 + index}, headers=admin,
        )
        return resp.status_code, resp.json().get("detail")

    results = _race(call)
    codes = sorted(code for code, _ in results)
    assert codes.count(200) >= 1, f"一路都没调成：{results}"
    assert codes.count(200) + codes.count(409) == 8, f"出现了 200/409 之外的状态码：{results}"
    assert {d for c, d in results if c == 409} <= {
        "新价格与现价相同，无需调价", "现价已被其他操作修改，请刷新后重试",
    }, results

    chain = _history(client, admin, item["id"])[::-1]  # 历史按 id 倒序，反过来即时间序
    assert len(chain) == codes.count(200), "成功几次就该留几行历史"
    assert chain[0][0] == 20, f"链头必须是初始价：{chain}"
    for prev, nxt in zip(chain, chain[1:]):
        assert prev[1] == nxt[0], f"历史链断了（并列的幽灵链）：{chain}"
    assert chain[-1][1] == _price(client, admin, item["id"]), "链尾必须等于现价"


def test_八路同价PATCH全部200且只留一行历史(client, admin):
    item = _item(client, admin, 10)

    def call(_index):
        resp = client.patch(
            f"/api/billing/charge-items/{item['id']}", json={"price": 12}, headers=admin
        )
        return resp.status_code

    results = _race(call)
    assert results == [200] * 8, f"同价 PATCH 在顺序请求下就是 200，并发下也该是：{results}"
    assert _history(client, admin, item["id"]) == [(10, 12)]


# ================================================================ 防拆卸静态钉


def _funcs(tree):
    return [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]


def _calls(node, name):
    return [
        n for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
    ]


def test_防拆卸_调价与留痕必须压在同一条带现价条件的UPDATE里():
    """守法拆掉的三种方式，每一种都让不变式当场失效：

    * `_change_price` 没了 → 判定与写入又分了家；
    * WHERE 里不再带 `ChargeItem.price` → 变回无条件 UPDATE，谁最后写谁算数；
    * `ChargePriceChange(...)` 长回端点里 → 历史行不再由 `rowcount` 把关，
      抢输者照样留痕（一次价格跃迁两行历史）。
    """
    tree = ast.parse(BILLING_PY.read_text(encoding="utf-8"))
    helper = next((f for f in _funcs(tree) if f.name == "_change_price"), None)
    assert helper is not None, "billing._change_price 没了——调价的条件 UPDATE 被拆了"

    conditions: list[str] = []
    for node in ast.walk(helper):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "where":
            continue
        inner = node.func.value
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Name)
            and inner.func.id == "update"
            and inner.args
            and isinstance(inner.args[0], ast.Name)
            and inner.args[0].id == "ChargeItem"
        ):
            conditions.extend(ast.unparse(a) for a in node.args)
    assert any("ChargeItem.id" in c for c in conditions), f"UPDATE 不再按 id 定位：{conditions}"
    assert any("ChargeItem.price" in c for c in conditions), (
        f"UPDATE 的 WHERE 里没有现价条件，等于变回无条件覆盖：{conditions}"
    )
    assert any(
        isinstance(n, ast.Attribute) and n.attr == "rowcount" for n in ast.walk(helper)
    ), "不看 rowcount，就没人知道这条 UPDATE 到底改没改到行"

    inside = len(_calls(helper, "ChargePriceChange"))
    total = len(_calls(tree, "ChargePriceChange"))
    assert inside == 1, f"_change_price 里应恰有一处留痕，实际 {inside}"
    assert total == inside, (
        f"billing.py 里还有 {total - inside} 处 ChargePriceChange(...) 长在 _change_price 之外"
        "——留痕必须由条件 UPDATE 的 rowcount 把关"
    )
