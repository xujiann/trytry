"""召回了的「未标批号」兜底批次不得再入库（P1-147）。

召回的规矩写在 `recall_batch` 上：「召回后不得再发药、不得再入库」；按批次入库（「该批次已召回，不得再入库」）与
调入（「已召回，不得调入」）早就这样拦。没有批号字段的三条入库路径——直接入库、采购验收、盘点盘盈——却落进
同一个兜底批次时不看它召回没有：汇总照加（缺药预警、采购建议都当有货），发药只取正常批次，于是加进去的量
一片也发不出（每张处方 409「可发批次库存不足」），召回又不能再做一次——模块开头警告过的幽灵库存。
"""
import pytest

UNSPECIFIED = "未标批号"
CODE = "C09AA01"


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P1147 乡卫生院", "org_type": "township", "level": "township"}).json()["id"]


@pytest.fixture(scope="module")
def supplier(client, admin):
    return client.post("/api/pharmacy/suppliers", headers=admin, json={"name": "P1147 药业"}).json()["id"]


def _fallback(client, admin, org):
    rows = client.get("/api/pharmacy/batches", headers=admin, params={"org_id": org, "drug_code": CODE}).json()
    return next(b for b in rows if b["batch_no"] == UNSPECIFIED)


def _summary(client, admin, org):
    rows = client.get("/api/pharmacy/stocks", headers=admin, params={"org_id": org}).json()
    return next(r["quantity"] for r in rows if r["drug_code"] == CODE)


@pytest.fixture(scope="module")
def recalled(client, admin, org):
    """直接入库 30 落兜底批次，再把兜底批次整批召回：汇总 0，批次收 30 / 退回不可发 30。"""
    resp = client.post("/api/pharmacy/stocks", headers=admin, json={
        "org_id": org, "drug_code": CODE, "drug_name": "卡托普利片", "quantity": 30})
    assert resp.status_code in (200, 201), resp.text
    batch = _fallback(client, admin, org)
    resp = client.post(f"/api/pharmacy/batches/{batch['id']}/recall", headers=admin,
                       json={"reason": "厂家召回，未标批号的一并封存"})
    assert resp.status_code == 200, resp.text
    assert _summary(client, admin, org) == 0
    return batch["id"]


def test_直接入库_409_汇总与批次都不动(client, admin, org, recalled):
    resp = client.post("/api/pharmacy/stocks", headers=admin, json={
        "org_id": org, "drug_code": CODE, "drug_name": "卡托普利片", "quantity": 500})
    assert resp.status_code == 409, resp.text      # 修前 200：汇总 500，批次收 530，一片也发不出
    assert "已召回" in resp.json()["detail"] and "按批次入库" in resp.json()["detail"]
    assert _summary(client, admin, org) == 0
    assert _fallback(client, admin, org)["quantity"] == 30


def test_采购验收_409_采购单仍待验收(client, admin, org, supplier, recalled):
    order = client.post("/api/pharmacy/purchase-orders", headers=admin, json={
        "org_id": org, "supplier_id": supplier, "item_type": "drug",
        "item_code": CODE, "item_name": "卡托普利片", "quantity": 200}).json()
    assert client.post(f"/api/pharmacy/purchase-orders/{order['id']}/approve", headers=admin).status_code == 200
    resp = client.post(f"/api/pharmacy/purchase-orders/{order['id']}/receive", headers=admin)
    assert resp.status_code == 409, resp.text      # 修前 200
    assert _summary(client, admin, org) == 0
    orders = client.get("/api/pharmacy/purchase-orders", headers=admin, params={"org_id": org}).json()
    assert [(o["id"], o["status"]) for o in orders] == [(order["id"], "approved")]   # 验收整笔回滚


def test_盘盈_409_召回封存的实物盘不回可发(client, admin, org, recalled):
    resp = client.post("/api/pharmacy/stock-takes", headers=admin, json={
        "org_id": org, "drug_code": CODE, "actual_qty": 30})
    assert resp.status_code == 409, resp.text      # 修前 201：封存的 30 盒成了盘盈、回到可发
    assert _summary(client, admin, org) == 0


def test_按批次入库照常(client, admin, org, recalled):
    before = _summary(client, admin, org)
    resp = client.post("/api/pharmacy/batches", headers=admin, json={
        "org_id": org, "drug_code": CODE, "drug_name": "卡托普利片",
        "batch_no": "KT-2409", "expire_date": "2028-09-30", "quantity": 100})
    assert resp.status_code == 201, resp.text
    assert _summary(client, admin, org) == before + 100
