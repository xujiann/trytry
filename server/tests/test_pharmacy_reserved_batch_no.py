"""保留批号「未标批号」设防（P2-1341，第四十批扫描 AD1-2）。

`pharmacy.UNSPECIFIED_BATCH_NO` / `UNSPECIFIED_EXPIRE_DATE` 的注释写明：没有批号可报的入库（直接入库、采购验收、
盘点盘盈）落到批号「未标批号」、效期哨兵 9999-12-31（含义「未登记效期」）的兜底批次，`_receive_unspecified` 就靠
这个前提往里累加。修前按批次入库不拦这个保留批号：批次台账上印着「未标批号」，药师照着填、再写上包装效期，201 落成
「未标批号 / 真效期」一行；此后直接入库、采购验收、盘盈全部累加进这一行、继承这个效期——扫描实测按批次入库 5 盒
（10 天后到期）再采购验收 300 盒，台账「未标批号 2026-10-14 305」；11 天后整行过期、可发 0，30 天的方 409
「可发批次库存不足」，采购建议 dispensable_stock 0。调拨按源批次的批号与效期建调入行，又能把这样一行原样搬到
调入机构去。

修后：按批次入库报保留批号 422；兜底行效期不是哨兵（修前已经落库的存量）时，三条无批号入库都 409、不累加，调拨
也不把这一行调出——存量要人工处置。效期是哨兵的兜底行照旧累加、照旧能调拨。
"""
from datetime import timedelta

import pytest

from conftest import business_today, login

from app.database import SessionLocal
from app.models import DrugBatch, DrugStock

B = "/api/pharmacy"
UNSPECIFIED = "未标批号"
SENTINEL = "9999-12-31"


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = [client.post("/api/organizations", headers=admin, json={
        "name": f"P21341 {name}", "org_type": "township", "level": "township"}).json()["id"] for name in ("甲院", "乙院")]
    supplier = client.post(f"{B}/suppliers", headers=admin, json={"name": "P21341 药业"}).json()["id"]
    resp = client.post("/api/users", headers=admin, json={
        "username": "p21341_director", "password": "passw0rd1", "full_name": "P21341 审批人", "role": "director",
        "org_id": orgs[0]})
    assert resp.status_code in (200, 201), resp.text
    # 采购单由 admin 提出，申请人不得自批（P2-759），审批另开一位本机构的管理层账号
    return {"org": orgs[0], "dest": orgs[1], "supplier": supplier,
            "director": login(client, "p21341_director", "passw0rd1")}


def _in_days(n: int) -> str:
    return (business_today() + timedelta(days=n)).isoformat()


def _stock(org, code):
    with SessionLocal() as db:
        row = db.query(DrugStock).filter(DrugStock.org_id == org, DrugStock.drug_code == code).first()
        return row.quantity if row else None


def _batches(org, code):
    with SessionLocal() as db:
        rows = (db.query(DrugBatch).filter(DrugBatch.org_id == org, DrugBatch.drug_code == code)
                .order_by(DrugBatch.id).all())
        return [(b.batch_no, b.expire_date, b.quantity, b.used_quantity) for b in rows]


def _purchase(client, admin, world, code, quantity):
    order = client.post(f"{B}/purchase-orders", headers=admin, json={
        "org_id": world["org"], "supplier_id": world["supplier"], "item_type": "drug",
        "item_code": code, "item_name": f"{code} 片", "quantity": quantity}).json()
    assert client.post(f"{B}/purchase-orders/{order['id']}/approve", headers=world["director"]).status_code == 200
    return order["id"]


@pytest.mark.parametrize("expire_in_days", [10, None])
def test_按批次入库报保留批号_422_库存不变(client, admin, world, expire_in_days):
    """包装效期照填也好、照抄哨兵也好，按批次入库都不收保留批号：它只给没有批号可报的入库兜底。"""
    code = f"P21341R{expire_in_days or 0}"
    resp = client.post(f"{B}/batches", headers=admin, json={
        "org_id": world["org"], "drug_code": code, "drug_name": f"{code} 片", "batch_no": UNSPECIFIED,
        "expire_date": _in_days(expire_in_days) if expire_in_days else SENTINEL, "quantity": 5})
    assert resp.status_code == 422, resp.text      # 修前 201：落成「未标批号 / 真效期」一行
    detail = resp.json()["detail"]
    assert "保留批号" in detail and "真实批号" in detail, detail
    assert _stock(world["org"], code) is None
    assert _batches(world["org"], code) == []


@pytest.fixture(scope="module")
def tainted(world):
    """修前按批次入库落下的存量：「未标批号」一行带着真效期（10 天后到期）、5 盒，汇总同为 5（对账不变式成立）。

    修后接口造不出这样的行，只能直接落库构造——测的是三条无批号入库与调拨碰上存量时的判定。"""
    code = "P21341T"
    expire = _in_days(10)
    with SessionLocal() as db:
        db.add(DrugStock(org_id=world["org"], drug_code=code, drug_name=f"{code} 片", quantity=5, threshold=0))
        db.add(DrugBatch(org_id=world["org"], drug_code=code, batch_no=UNSPECIFIED, expire_date=expire, quantity=5))
        db.commit()
    before = (_stock(world["org"], code), _batches(world["org"], code))
    assert before == (5, [(UNSPECIFIED, expire, 5, 0)])
    return {"code": code, "expire": expire, "before": before}


def _unchanged(world, tainted):
    assert (_stock(world["org"], tainted["code"]), _batches(world["org"], tainted["code"])) == tainted["before"]


def test_存量兜底行效期不是哨兵_直接入库409_库存不变(client, admin, world, tainted):
    resp = client.post(f"{B}/stocks", headers=admin, json={
        "org_id": world["org"], "drug_code": tainted["code"], "drug_name": f"{tainted['code']} 片", "quantity": 300})
    assert resp.status_code == 409, resp.text      # 修前 200：300 盒累加进这一行、继承 10 天后的效期
    detail = resp.json()["detail"]
    assert tainted["expire"] in detail and "人工" in detail, detail
    _unchanged(world, tainted)


def test_存量兜底行效期不是哨兵_采购验收409_采购单仍待验收(client, admin, world, tainted):
    order = _purchase(client, admin, world, tainted["code"], 300)
    resp = client.post(f"{B}/purchase-orders/{order}/receive", headers=admin)
    assert resp.status_code == 409, resp.text      # 修前 200：stock_quantity 305，台账「未标批号 <10 天后> 305」
    assert tainted["expire"] in resp.json()["detail"]
    _unchanged(world, tainted)
    orders = client.get(f"{B}/purchase-orders", headers=admin, params={"org_id": world["org"]}).json()
    assert {o["id"]: o["status"] for o in orders}[order] == "approved"   # 验收整笔回滚


def test_存量兜底行效期不是哨兵_盘盈409_库存不变(client, admin, world, tainted):
    surplus = _stock(world["org"], tainted["code"]) + 3   # 实盘比账面多 3 盒：盘盈
    resp = client.post(f"{B}/stock-takes", headers=admin, json={
        "org_id": world["org"], "drug_code": tainted["code"], "actual_qty": surplus})
    assert resp.status_code == 409, resp.text      # 修前 201：盘盈 3 盒落进这一行
    assert tainted["expire"] in resp.json()["detail"]
    _unchanged(world, tainted)


def test_存量兜底行效期不是哨兵_不调出_不在调入机构造出同样一行(client, admin, world, tainted):
    resp = client.post(f"{B}/transfers", headers=admin, json={
        "drug_code": tainted["code"], "from_org_id": world["org"], "to_org_id": world["dest"], "quantity": 3})
    assert resp.status_code == 409, resp.text      # 修前 201：调入机构多出「未标批号 / <10 天后>」一行
    assert tainted["expire"] in resp.json()["detail"]
    _unchanged(world, tainted)
    assert _stock(world["dest"], tainted["code"]) is None
    assert _batches(world["dest"], tainted["code"]) == []


def test_效期是哨兵的兜底行照旧累加_照旧能调拨(client, admin, world):
    code = "P21341N"
    resp = client.post(f"{B}/stocks", headers=admin, json={
        "org_id": world["org"], "drug_code": code, "drug_name": f"{code} 片", "quantity": 30})
    assert resp.status_code == 200, resp.text
    order = _purchase(client, admin, world, code, 20)
    assert client.post(f"{B}/purchase-orders/{order}/receive", headers=admin).status_code == 200
    take = client.post(f"{B}/stock-takes", headers=admin, json={"org_id": world["org"], "drug_code": code, "actual_qty": 60})
    assert take.status_code == 201 and take.json()["diff"] == 10, take.text
    assert (_stock(world["org"], code), _batches(world["org"], code)) == (60, [(UNSPECIFIED, SENTINEL, 60, 0)])
    moved = client.post(f"{B}/transfers", headers=admin, json={
        "drug_code": code, "from_org_id": world["org"], "to_org_id": world["dest"], "quantity": 15})
    assert moved.status_code == 201, moved.text
    assert (_stock(world["dest"], code), _batches(world["dest"], code)) == (15, [(UNSPECIFIED, SENTINEL, 15, 0)])
    assert (_stock(world["org"], code), _batches(world["org"], code)) == (45, [(UNSPECIFIED, SENTINEL, 60, 15)])
