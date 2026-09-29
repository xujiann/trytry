"""药品采购到货验收收实收数：按实收入库、记在采购单上（P2-852，第二十三批「部分之和 vs 整体」扫描 Y4-2）。

`receive_purchase` 原先没有请求体，一律按申请量整单入库：100 盒的单到了 60，请求体带 `{"received_quantity": 60}` 被悄悄
丢掉、照样 200，库存汇总与兜底批次各加 100——少到的 40 盒成了账上能发、实际不存在的库存，要等盘点以「盘亏」冒出来，
采购单上也看不出只到了 60。兄弟路径物资采购早就按实收记（不超过采购量）。修后可选的实收数不超过采购量、按它入库并记在
采购单上；不带请求体照旧按申请量。
"""
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import DrugBatch, DrugStock

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-public.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2852 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    supplier = client.post("/api/pharmacy/suppliers", headers=admin, json={"name": "P2852 医药公司"})
    assert supplier.status_code in (200, 201), supplier.text
    headers = {}
    for role in ("operator", "director"):   # 申请与审批不能是同一个人（采购单的职责分离）
        made = client.post("/api/users", headers=admin, json={
            "username": f"p2852_{role}", "password": "passw0rd1", "full_name": f"p2852_{role}", "role": role,
            "org_id": org})
        assert made.status_code in (200, 201), made.text
        token = client.post("/api/auth/login", json={"username": f"p2852_{role}", "password": "passw0rd1"})
        headers[role] = {"Authorization": f"Bearer {token.json()['access_token']}"}
    return {"org": org, "supplier": supplier.json()["id"], **headers}


def _approved(client, admin, world, code, quantity):
    made = client.post("/api/pharmacy/purchase-orders", headers=world["operator"], json={
        "org_id": world["org"], "supplier_id": world["supplier"], "item_type": "drug", "item_code": code,
        "item_name": f"{code} 药", "quantity": quantity})
    assert made.status_code == 201, made.text
    approved = client.post(f"/api/pharmacy/purchase-orders/{made.json()['id']}/approve", headers=world["director"])
    assert approved.status_code == 200, approved.text
    return made.json()["id"]


def _stock(world, code):
    with SessionLocal() as db:
        stock = db.query(DrugStock).filter(DrugStock.org_id == world["org"], DrugStock.drug_code == code).one()
        batches = [b.quantity for b in db.query(DrugBatch).filter(
            DrugBatch.org_id == world["org"], DrugBatch.drug_code == code)]
        return stock.quantity, batches


def _row(client, admin, order_id):
    return next(r for r in client.get("/api/pharmacy/purchase-orders", headers=admin, params={"limit": 500}).json()
                if r["id"] == order_id)


def test_少到按实收入库_记在采购单上(client, admin, world):
    order = _approved(client, admin, world, "P2852A", 100)
    got = client.post(f"/api/pharmacy/purchase-orders/{order}/receive", headers=world["operator"], json={"received_quantity": 60})
    assert got.status_code == 200, got.text
    assert (got.json()["status"], got.json()["stock_quantity"]) == ("received", 60)   # 修前 100
    assert _stock(world, "P2852A") == (60, [60])   # 修前 (100, [100])
    assert (_row(client, admin, order)["quantity"], _row(client, admin, order)["received_quantity"]) == (100, 60)


def test_实收超过采购量_422不入库(client, admin, world):
    order = _approved(client, admin, world, "P2852B", 10)
    got = client.post(f"/api/pharmacy/purchase-orders/{order}/receive", headers=world["operator"], json={"received_quantity": 11})
    assert got.status_code == 422, got.text
    assert _row(client, admin, order)["status"] == "approved"


def test_不带请求体_照旧按申请量(client, admin, world):
    order = _approved(client, admin, world, "P2852C", 30)
    got = client.post(f"/api/pharmacy/purchase-orders/{order}/receive", headers=world["operator"])
    assert got.status_code == 200 and got.json() == {"id": order, "status": "received", "stock_quantity": 30}
    assert _row(client, admin, order)["received_quantity"] == 30


def test_页面验收时填实收数():
    assert 'data-porec="${o.id}" data-qty="${esc(o.quantity)}"' in PAGE
    start = PAGE.index("if (d.porec) {")
    body = PAGE[start:start + 1000]
    assert 'spdModal("到货验收"' in body and "received_quantity: qty" in body
    assert "（实收 ${o.received_quantity}）" in PAGE
