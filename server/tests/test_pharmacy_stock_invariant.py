"""药品可用汇总与批次台账的对账不变式（ADR-0013）。

一条口径贯穿全文件：

    DrugStock.quantity == Σ(批次 quantity - used_quantity - blocked_quantity)

`test_pharmacy_batches.py` 已经守住了批次入库/发药/冲销三条路径。本文件补的是
另外六个会改动汇总的入口——直接入库、采购验收、调拨、盘点、召回、冲销回补——
以及两条"不能发生的事"：

- **幽灵库存不可产生**：任何让汇总长出来的动作，长出来的量都必须真的发得出去。
  调拨曾经把 40 片搬到乡镇院、批次一行不落，那 40 片一张方也发不出来，
  而缺药预警看的正是汇总，于是那家院既发不出药也不会被提示缺药。
- **并发冲销恰一次成功**：状态闸门必须原子，否则一张发药单会被退好几次，
  把别的处方发出的药也一起退回来。
"""
import threading

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.database import SessionLocal
from app.main import app
from app.models import DrugBatch

#: 兜底批次：没有批号可报的入库（直接入库/采购验收/盘点盘盈）落在这里
UNSPECIFIED = "未标批号"


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "对账县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def township(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "对账卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def patient(client, admin):
    return client.post(
        "/api/patients",
        json={"name": "对账患者", "id_card": "340000199002026666"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def supplier(client, admin):
    return client.post(
        "/api/pharmacy/suppliers", json={"name": "对账药业"}, headers=admin
    ).json()


# ------------------------------------------------------------------ 小工具


def _stock(client, admin, org_id, code):
    rows = client.get("/api/pharmacy/stocks", params={"org_id": org_id}, headers=admin).json()
    mine = [r for r in rows if r["drug_code"] == code]
    return mine[0]["quantity"] if mine else None


def _batches(client, admin, org_id, code):
    return client.get(
        "/api/pharmacy/batches", params={"org_id": org_id, "drug_code": code}, headers=admin
    ).json()


def _assert_invariant(client, admin, org_id, code):
    """汇总 == Σ(批次量 - 已用 - 退回不可发)。两边只在同一事务里同改。"""
    stock = _stock(client, admin, org_id, code) or 0
    batches = _batches(client, admin, org_id, code)
    batch_sum = sum(b["available"] for b in batches)
    assert stock == batch_sum, (
        f"汇总 {stock} 与批次可发余量合计 {batch_sum} 对不上账：{batches}"
    )
    return stock


def _direct_in(client, admin, org_id, code, qty, name="对账药", threshold=0):
    return client.post(
        "/api/pharmacy/stocks",
        json={"org_id": org_id, "drug_code": code, "drug_name": name,
              "quantity": qty, "threshold": threshold},
        headers=admin,
    )


def _receive_batch(client, admin, org_id, code, batch_no, expire, qty, name="对账药"):
    return client.post(
        "/api/pharmacy/batches",
        json={"org_id": org_id, "drug_code": code, "drug_name": name,
              "batch_no": batch_no, "expire_date": expire, "quantity": qty},
        headers=admin,
    )


def _rx(client, admin, patient_id, org_id, code, name="对账药", daily_dose=1, days=1):
    resp = client.post(
        "/api/prescriptions",
        json={"patient_id": patient_id, "org_id": org_id, "diagnosis_name": "上呼吸道感染",
              "items": [{"drug_code": code, "drug_name": name,
                         "daily_dose": daily_dose, "days": days}]},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _dispense(client, admin, rx_id):
    return client.post("/api/dispense", json={"prescription_id": rx_id}, headers=admin)


# ============================================================ 六个入口各自的不变式


def test_直接入库落兜底批次_汇总长出来的量发得出去(client, admin, org, patient):
    """`POST /stocks` 没有批号字段，以前只加汇总——加进去的 50 片一张方发不出来。"""
    assert _direct_in(client, admin, org["id"], "DIRECT", 50).status_code == 200
    assert _stock(client, admin, org["id"], "DIRECT") == 50
    batches = _batches(client, admin, org["id"], "DIRECT")
    assert [b["batch_no"] for b in batches] == [UNSPECIFIED], "无批号入库应落兜底批次"
    _assert_invariant(client, admin, org["id"], "DIRECT")

    # 幽灵库存不可产生：账上的 50 片必须真的发得出去
    rx = _rx(client, admin, patient["id"], org["id"], "DIRECT", daily_dose=50, days=1)
    resp = _dispense(client, admin, rx["id"])
    assert resp.status_code == 201, f"汇总说有 50 片却发不出来（幽灵库存）：{resp.text}"
    assert sum(i["quantity"] for i in resp.json()["items"]) == 50
    assert _assert_invariant(client, admin, org["id"], "DIRECT") == 0


def test_再次直接入库累加到同一个兜底批次(client, admin, org):
    _direct_in(client, admin, org["id"], "DIRECT2", 20)
    _direct_in(client, admin, org["id"], "DIRECT2", 30)
    batches = _batches(client, admin, org["id"], "DIRECT2")
    assert len(batches) == 1 and batches[0]["quantity"] == 50, "兜底批次应累加而不是建两行"
    assert _assert_invariant(client, admin, org["id"], "DIRECT2") == 50


def test_采购验收同事务落批次(client, admin, org, supplier, patient):
    order = client.post(
        "/api/pharmacy/purchase-orders",
        json={"org_id": org["id"], "supplier_id": supplier["id"], "item_type": "drug",
              "item_code": "PURCH", "item_name": "验收药", "quantity": 40},
        headers=admin,
    ).json()
    assert client.post(
        f"/api/pharmacy/purchase-orders/{order['id']}/approve", headers=admin
    ).status_code == 200
    received = client.post(
        f"/api/pharmacy/purchase-orders/{order['id']}/receive", headers=admin
    )
    assert received.status_code == 200 and received.json()["stock_quantity"] == 40
    assert _assert_invariant(client, admin, org["id"], "PURCH") == 40
    # 验收进来的量必须发得出去
    rx = _rx(client, admin, patient["id"], org["id"], "PURCH", "验收药", daily_dose=40, days=1)
    assert _dispense(client, admin, rx["id"]).status_code == 201
    assert _assert_invariant(client, admin, org["id"], "PURCH") == 0


def test_采购单只能验收一次_并发闸门原子(client, admin, org, supplier):
    """验收是往库存里加数的路径：闸门先判后改，两笔并发就按同一张单加两次。"""
    order = client.post(
        "/api/pharmacy/purchase-orders",
        json={"org_id": org["id"], "supplier_id": supplier["id"], "item_type": "drug",
              "item_code": "PONCE", "item_name": "验收一次药", "quantity": 100},
        headers=admin,
    ).json()
    client.post(f"/api/pharmacy/purchase-orders/{order['id']}/approve", headers=admin)

    codes: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def run():
        barrier.wait(timeout=30)
        code = client.post(
            f"/api/pharmacy/purchase-orders/{order['id']}/receive", headers=admin
        ).status_code
        with lock:
            codes.append(code)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert codes.count(200) == 1, f"同一张采购单被验收 {codes.count(200)} 次：{sorted(codes)}"
    assert _assert_invariant(client, admin, org["id"], "PONCE") == 100


def test_调拨搬批次_调入方拿到的药发得出去(client, admin, org, township, patient):
    """最贵的那个幽灵库存：调 40 片过去、批次一行不落，那 40 片一张方发不出来。"""
    _receive_batch(client, admin, org["id"], "MOVE", "MV-1", "2030-06-01", 60, name="调拨药")
    resp = client.post(
        "/api/pharmacy/transfers",
        json={"drug_code": "MOVE", "from_org_id": org["id"],
              "to_org_id": township["id"], "quantity": 40},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["quantity"] == 40
    assert _assert_invariant(client, admin, org["id"], "MOVE") == 20
    assert _assert_invariant(client, admin, township["id"], "MOVE") == 40

    # 批号与效期跟着药走——召回时"这批发给了谁"才查得出来
    moved = _batches(client, admin, township["id"], "MOVE")
    assert [(b["batch_no"], b["expire_date"], b["quantity"]) for b in moved] == [
        ("MV-1", "2030-06-01", 40)
    ]

    rx = _rx(client, admin, patient["id"], township["id"], "MOVE", "调拨药",
             daily_dose=40, days=1)
    resp = _dispense(client, admin, rx["id"])
    assert resp.status_code == 201, f"调入方账上有 40 片却发不出来（幽灵库存）：{resp.text}"
    assert resp.json()["items"][0]["batch_no"] == "MV-1"
    assert _assert_invariant(client, admin, township["id"], "MOVE") == 0


def test_调拨只搬可发批次_过期与召回批次不出门(client, admin, org, township):
    """汇总够、可发批次不够时拒绝调拨——搬过去也是发不出的幽灵库存。"""
    _receive_batch(client, admin, org["id"], "STALE", "ST-OLD", "2020-01-01", 30, name="陈药")
    assert _stock(client, admin, org["id"], "STALE") == 30
    resp = client.post(
        "/api/pharmacy/transfers",
        json={"drug_code": "STALE", "from_org_id": org["id"],
              "to_org_id": township["id"], "quantity": 30},
        headers=admin,
    )
    assert resp.status_code == 409
    assert "可发批次" in resp.json()["detail"]
    # 拒绝后两边都不许留半截
    assert _assert_invariant(client, admin, org["id"], "STALE") == 30
    assert _batches(client, admin, township["id"], "STALE") == []
    assert _stock(client, admin, township["id"], "STALE") is None


def test_盘亏按批次核销_盘完的账下一次发药也认(client, admin, org, patient):
    """只改汇总的话，盘完账面 5 片、批次仍挂 100 片，下一次发药照发不误。"""
    _receive_batch(client, admin, org["id"], "COUNT", "CT-1", "2030-09-01", 100, name="盘点药")
    take = client.post(
        "/api/pharmacy/stock-takes",
        json={"org_id": org["id"], "drug_code": "COUNT", "actual_qty": 5, "note": "破损"},
        headers=admin,
    )
    assert take.status_code == 201 and take.json()["diff"] == -95
    assert _assert_invariant(client, admin, org["id"], "COUNT") == 5

    # 盘完只剩 5 片：再开一张 6 片的方必须发不出来（盘点不能白盘）
    rx = _rx(client, admin, patient["id"], org["id"], "COUNT", "盘点药", daily_dose=6, days=1)
    assert _dispense(client, admin, rx["id"]).status_code == 409
    rx2 = _rx(client, admin, patient["id"], org["id"], "COUNT", "盘点药", daily_dose=5, days=1)
    assert _dispense(client, admin, rx2["id"]).status_code == 201
    assert _assert_invariant(client, admin, org["id"], "COUNT") == 0


def test_盘盈落兜底批次(client, admin, org):
    _receive_batch(client, admin, org["id"], "GAIN", "GN-1", "2030-09-01", 10, name="盘盈药")
    take = client.post(
        "/api/pharmacy/stock-takes",
        json={"org_id": org["id"], "drug_code": "GAIN", "actual_qty": 18},
        headers=admin,
    )
    assert take.status_code == 201 and take.json()["diff"] == 8
    assert _assert_invariant(client, admin, org["id"], "GAIN") == 18
    by_no = {b["batch_no"]: b["quantity"] for b in _batches(client, admin, org["id"], "GAIN")}
    assert by_no == {"GN-1": 10, UNSPECIFIED: 8}, "盘盈没有批号可归，应落兜底批次"


# ============================================================ 召回与冲销回补


def test_召回把余量退出可用汇总(client, admin, org, patient):
    """只翻 status 不动汇总，召回的药就一直被算成有货，缺药预警长期少报。"""
    _receive_batch(client, admin, org["id"], "BACK", "BK-1", "2030-01-01", 100, name="召回药")
    _receive_batch(client, admin, org["id"], "BACK", "BK-2", "2031-01-01", 50, name="召回药")
    rx = _rx(client, admin, patient["id"], org["id"], "BACK", "召回药", daily_dose=20, days=1)
    record = _dispense(client, admin, rx["id"]).json()
    assert record["items"][0]["batch_no"] == "BK-1"  # FEFO
    assert _assert_invariant(client, admin, org["id"], "BACK") == 130

    bk1 = next(b for b in _batches(client, admin, org["id"], "BACK") if b["batch_no"] == "BK-1")
    assert client.post(
        f"/api/pharmacy/batches/{bk1['id']}/recall", json={"reason": "厂家召回"}, headers=admin
    ).status_code == 200
    # BK-1 余 80 片一片也发不出去了，汇总必须同步只剩 BK-2 的 50
    assert _assert_invariant(client, admin, org["id"], "BACK") == 50

    # 冲销这张单：药回到 BK-1，但 BK-1 已召回——只回批次不回可用汇总
    rev = client.post(
        f"/api/dispense/{record['id']}/reverse", json={"reason": "患者退药"}, headers=admin
    )
    assert rev.status_code == 200
    bk1_after = next(
        b for b in _batches(client, admin, org["id"], "BACK") if b["batch_no"] == "BK-1"
    )
    assert bk1_after["used_quantity"] == 0, "批次侧要回补，批号追溯才对得上"
    assert bk1_after["remaining"] == 100, "药确实回到了库房"
    assert bk1_after["blocked_quantity"] == 100, (
        "召回时退出汇总的 80 片 + 冲销退回来的 20 片，整批 100 片都发不出去"
    )
    assert bk1_after["available"] == 0
    assert _assert_invariant(client, admin, org["id"], "BACK") == 50, (
        "冲销把召回批次的量补回可用汇总——实测汇总 160→180 而可发仍是 50"
    )

    # 汇总说 50，就必须刚好能发 50、发不出 51
    rx_over = _rx(client, admin, patient["id"], org["id"], "BACK", "召回药",
                  daily_dose=51, days=1)
    assert _dispense(client, admin, rx_over["id"]).status_code == 409
    rx_ok = _rx(client, admin, patient["id"], org["id"], "BACK", "召回药",
                daily_dose=50, days=1)
    assert _dispense(client, admin, rx_ok["id"]).status_code == 201
    assert _assert_invariant(client, admin, org["id"], "BACK") == 0


def test_冲销回补到已过期批次不污染可用汇总(client, admin, org, patient):
    """发药时还没过期、冲销时已过期：药回库房，但一片也发不出去。

    这个状态只有时间能自然造出来（发药必然挑未过期批次），接口面上没有改效期的
    入口，所以直接改库里的效期来构造——测的是冲销那一侧的判定，不是入库。
    """
    _receive_batch(client, admin, org["id"], "GONE", "GO-1", "2031-01-01", 40, name="临期药")
    rx = _rx(client, admin, patient["id"], org["id"], "GONE", "临期药", daily_dose=10, days=1)
    record = _dispense(client, admin, rx["id"]).json()
    assert record["items"][0]["batch_no"] == "GO-1"
    assert _assert_invariant(client, admin, org["id"], "GONE") == 30

    # 时间往前推：这一批在发药之后过了效期
    db = SessionLocal()
    try:
        batch = (
            db.query(DrugBatch)
            .filter(DrugBatch.org_id == org["id"], DrugBatch.batch_no == "GO-1")
            .one()
        )
        batch.expire_date = "2020-01-01"
        db.commit()
    finally:
        db.close()

    rev = client.post(
        f"/api/dispense/{record['id']}/reverse", json={"reason": "退"}, headers=admin
    )
    assert rev.status_code == 200
    gone = _batches(client, admin, org["id"], "GONE")[0]
    assert gone["used_quantity"] == 0 and gone["remaining"] == 40, "批次侧照常回补"
    assert gone["blocked_quantity"] == 10, "退回时已过效期，这 10 片不再计入可用汇总"
    assert gone["available"] == 30, "另外 30 片当初入库时还没过期，仍挂在汇总上"
    # 汇总不许因为这次冲销而长回去（长回去缺药预警就会少报）
    assert _stock(client, admin, org["id"], "GONE") == 30
    _assert_invariant(client, admin, org["id"], "GONE")


# ============================================================ 并发冲销恰一次


def test_召回后采购建议不再把召回的药算成有货(client, admin, org, patient):
    """A-10 真正的危害：缺药预警与采购建议看的是汇总。

    召回不动汇总时，召回掉的 140 片继续被算成有货，采购建议就永远不提示补货
    ——缺药预警长期少报，而且少报的正是"刚出过质量问题、最该补货"的那个品种。
    """
    _receive_batch(client, admin, org["id"], "SUGGEST", "SG-1", "2031-04-01", 200,
                   name="建议药")
    rx = _rx(client, admin, patient["id"], org["id"], "SUGGEST", "建议药",
             daily_dose=60, days=1)
    assert _dispense(client, admin, rx["id"]).status_code == 201
    assert _assert_invariant(client, admin, org["id"], "SUGGEST") == 140

    # 近 30 天用量 60，账上 140 > 60：本来就不该提示采购
    before = client.get("/api/pharmacy/purchase-suggestions", headers=admin).json()
    assert [r for r in before if r["drug_code"] == "SUGGEST"] == []

    batch = _batches(client, admin, org["id"], "SUGGEST")[0]
    assert client.post(
        f"/api/pharmacy/batches/{batch['id']}/recall",
        json={"reason": "质量问题"}, headers=admin,
    ).status_code == 200
    assert _assert_invariant(client, admin, org["id"], "SUGGEST") == 0

    after = client.get("/api/pharmacy/purchase-suggestions", headers=admin).json()
    mine = [r for r in after if r["drug_code"] == "SUGGEST"]
    assert mine, "整批召回后一片也发不出来，采购建议必须提示补货"
    assert mine[0]["current_stock"] == 0, (
        f"召回的 140 片仍被算成有货：{mine[0]}"
    )
    assert mine[0]["suggested_quantity"] == 60


def _race_reverse(client, admin, dispense_id, threads=8):
    """8 路并发冲销同一张单，返回状态码清单。"""
    codes: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(threads)

    def run():
        barrier.wait(timeout=30)
        code = client.post(
            f"/api/dispense/{dispense_id}/reverse", json={"reason": "退"}, headers=admin
        ).status_code
        with lock:
            codes.append(code)

    workers = [threading.Thread(target=run) for _ in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    return codes


def test_并发冲销同一发药单恰一次成功(client, admin, org, patient):
    """8 路并发冲销同一张单，实测（修复前）3 笔同时通过：批次已用 60→30、
    可用汇总 440→470，把别的处方发出的药也一起退了回来，凭空多出 20 片。

    与 test_pharmacy_batches 的并发用例同法：Barrier 卡住所有线程一起放行——
    不真并发的并发用例测不出竞态（先判后改的旧写法照样全绿）。

    **跑三轮**是刻意的：先判后改能不能被撞出来取决于线程调度，单轮实测会漏
    （同一份有 bug 的代码，单跑必红、跟在别的用例后面跑却绿了一次）。
    每轮换一张新的发药单，三轮全要求恰一次成功——漏网概率降到可忽略，
    而正确的实现每轮都必然只成功一次，不会因此变flaky。
    """
    _receive_batch(client, admin, org["id"], "RACEREV", "RR-1", "2031-02-01", 500,
                   name="并发退药")
    # 先发 5 张方把已用垫高：闸门漏了就会把这些方的药一起退回来
    for _ in range(5):
        other = _rx(client, admin, patient["id"], org["id"], "RACEREV", "并发退药",
                    daily_dose=10, days=1)
        assert _dispense(client, admin, other["id"]).status_code == 201

    for round_no in range(3):
        target = _rx(client, admin, patient["id"], org["id"], "RACEREV", "并发退药",
                     daily_dose=10, days=1)
        record = _dispense(client, admin, target["id"]).json()
        stock_before = _assert_invariant(client, admin, org["id"], "RACEREV")
        batch_before = _batches(client, admin, org["id"], "RACEREV")[0]["used_quantity"]

        codes = _race_reverse(client, admin, record["id"])
        assert codes.count(200) == 1, (
            f"第 {round_no + 1} 轮：同一张发药单被冲销 {codes.count(200)} 次：{sorted(codes)}"
        )
        assert codes.count(409) == 7

        stock_after = _assert_invariant(client, admin, org["id"], "RACEREV")
        assert stock_after == stock_before + 10, (
            f"第 {round_no + 1} 轮：该单只发出 10 片，冲销后汇总应为 "
            f"{stock_before + 10}，实为 {stock_after}"
        )
        batch_after = _batches(client, admin, org["id"], "RACEREV")[0]["used_quantity"]
        assert batch_after == batch_before - 10, "批次已用只该减掉这一单的 10 片"
        # 冲销后不可再冲销
        assert client.post(
            f"/api/dispense/{record['id']}/reverse", json={"reason": "再退"}, headers=admin
        ).status_code == 409
