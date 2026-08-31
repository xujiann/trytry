"""中心药房 `/api/pharmacy` 平台侧 9 个未治理端点的**特征化网 + 响应契约**。

套路同 `test_billing_contract.py` / `test_inpatient_contract.py`：先钉住**当前**
响应的完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §7/§11）。已治理的 9 个端点（stocks×2 / transfers / alerts /
batches×5）不在此列；ADR-0013「改汇总必同事务改批次」的语义一行未动——
本批只加出参声明。

本簇的建模判断（都以此处的精确断言为依据）：

- **本簇没有 Money 列**：库存量/采购量/盘点数全是 Integer 列（`DrugStock.quantity`
  / `PurchaseOrder.quantity` / `StockTake.book_qty` 等），声明 `int` 并用
  `type(x) is int` 显式钉——写成 float 会把「50 片」印成「50.0 片」，
  写成 int|float 则是没有依据的宽化。
- **`usage_30d` 恒 float**：唯一产地是 `float(row.usage or 0)`，整数用量也以
  30.0 出参——种子里专门造了一味「日剂量 10×3 天」的整数用量钉住这一位。
- **`stock_quantity` 是"键恒在值可空"**：药品验收回入库后的最新汇总（int）；
  非药品（material）验收回 null——键都在，故声明 `int | None`，
  不是条件键，不用 exclude_unset。
- 新建与审批回执同形（id+status）共用一个模型；验收回执多一个尾键，分开建模。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

SUPPLIER_CREATED_KEYS = ["id", "name", "active"]
SUPPLIER_KEYS = ["id", "name", "contact", "license_no", "active"]
ORDER_ACTION_KEYS = ["id", "status"]
ORDER_RECEIVE_KEYS = ["id", "status", "stock_quantity"]
ORDER_KEYS = ["id", "org_id", "supplier_id", "item_type", "item_code", "item_name", "quantity", "status"]
STOCK_TAKE_CREATED_KEYS = ["id", "book_qty", "actual_qty", "diff"]
STOCK_TAKE_KEYS = ["id", "org_id", "drug_code", "book_qty", "actual_qty", "diff", "note"]
SUGGESTION_KEYS = ["drug_code", "drug_name", "usage_30d", "current_stock", "suggested_quantity"]


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def seed(client, admin):
    """一次种完全部场景，测试只做断言（billing 契约网同款布局）。

    采购链：药品单（50 片，验收自动入库）→ 盘点 45（亏 5）→ 再盘 47（盈 2）；
    物资单（7 件，验收不入药房库存）；另留一张待审批单与一张驳回单给过滤用。
    采购建议：PHCT-MET 用量 30.5×3=91.5（float）对库存 45；
    PHCT-INTZ 用量 10×3=30.0（整数值的 float）对零库存。
    """
    data: dict = {}
    org = client.post(
        "/api/organizations",
        json={"name": "契约中心药房医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    data["org"] = org
    for username, role in [
        ("phct_doc", "doctor"),
        ("phct_pha", "pharmacist"),
        ("phct_dir", "director"),
        ("phct_op", "operator"),
    ]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
        data[role] = login(client, username, "pass123456")
    data["patient"] = client.post(
        "/api/patients",
        json={"name": "契约药房患者", "id_card": "330881199001017801"},
        headers=admin,
    ).json()

    resp = client.post(
        "/api/pharmacy/suppliers",
        json={"name": "契约县医药公司", "contact": "王经理", "license_no": "浙AA0001"},
        headers=data["director"],
    )
    assert resp.status_code == 201, resp.text
    data["supplier"] = resp.json()
    # 第二家走 operator + 全默认值，钉住 contact/license_no 的空串默认
    data["supplier2"] = client.post(
        "/api/pharmacy/suppliers", json={"name": "契约二号药商"}, headers=data["operator"]
    ).json()

    def purchase(payload, headers=None):
        resp = client.post(
            "/api/pharmacy/purchase-orders", json=payload, headers=headers or data["operator"]
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    sid = data["supplier"]["id"]
    data["po_drug"] = purchase(
        {"org_id": org["id"], "supplier_id": sid, "item_type": "drug",
         "item_code": "PHCT-MET", "item_name": "契约二甲双胍", "quantity": 50}
    )
    oid = data["po_drug"]["id"]
    data["po_drug_approved"] = client.post(
        f"/api/pharmacy/purchase-orders/{oid}/approve", headers=data["director"]
    ).json()
    resp = client.post(f"/api/pharmacy/purchase-orders/{oid}/receive", headers=data["operator"])
    assert resp.status_code == 200, resp.text
    data["po_drug_received"] = resp.json()

    data["po_material"] = purchase(
        {"org_id": org["id"], "supplier_id": sid, "item_type": "material",
         "item_code": "PHCT-GZ", "item_name": "契约纱布", "quantity": 7}
    )
    mid = data["po_material"]["id"]
    client.post(f"/api/pharmacy/purchase-orders/{mid}/approve", headers=data["director"])
    data["po_material_received"] = client.post(
        f"/api/pharmacy/purchase-orders/{mid}/receive", headers=data["operator"]
    ).json()

    data["po_pending"] = purchase(
        {"org_id": org["id"], "supplier_id": sid, "item_type": "drug",
         "item_code": "PHCT-AMX", "item_name": "契约阿莫西林", "quantity": 30}
    )
    data["po_rejected"] = purchase(
        {"org_id": org["id"], "supplier_id": sid, "item_type": "drug",
         "item_code": "PHCT-IBU", "item_name": "契约布洛芬", "quantity": 20}
    )
    data["po_rejected_receipt"] = client.post(
        f"/api/pharmacy/purchase-orders/{data['po_rejected']['id']}/approve?reject=true",
        headers=data["director"],
    ).json()

    resp = client.post(
        "/api/pharmacy/stock-takes",
        json={"org_id": org["id"], "drug_code": "PHCT-MET", "actual_qty": 45, "note": "破损5片"},
        headers=data["pharmacist"],
    )
    assert resp.status_code == 201, resp.text
    data["take_loss"] = resp.json()
    data["take_gain"] = client.post(
        "/api/pharmacy/stock-takes",
        json={"org_id": org["id"], "drug_code": "PHCT-MET", "actual_qty": 47},
        headers=data["pharmacist"],
    ).json()

    # 近 30 天用量：一味小数用量（30.5×3=91.5）、一味整数用量（10×3=30.0，无库存）
    for items in (
        [{"drug_code": "PHCT-MET", "drug_name": "契约二甲双胍", "daily_dose": 30.5, "days": 3}],
        [{"drug_code": "PHCT-INTZ", "drug_name": "契约整数用量药", "daily_dose": 10, "days": 3}],
    ):
        resp = client.post(
            "/api/prescriptions",
            json={"patient_id": data["patient"]["id"], "org_id": org["id"],
                  "diagnosis_name": "2型糖尿病", "items": items},
            headers=data["doctor"],
        )
        assert resp.status_code == 201, resp.text
    return data


# ---------------------------------------------------------------- 供应商


def test_供应商建档回执精确_键序(seed):
    body = seed["supplier"]
    assert list(body.keys()) == SUPPLIER_CREATED_KEYS
    assert body == {"id": body["id"], "name": "契约县医药公司", "active": True}
    assert type(body["id"]) is int


def test_供应商列表精确_默认值空串(client, admin, seed):
    rows = client.get("/api/pharmacy/suppliers", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [SUPPLIER_KEYS] * 2  # id 升序
    assert rows == [
        {"id": seed["supplier"]["id"], "name": "契约县医药公司", "contact": "王经理",
         "license_no": "浙AA0001", "active": True},
        {"id": seed["supplier2"]["id"], "name": "契约二号药商", "contact": "",
         "license_no": "", "active": True},
    ]


# ---------------------------------------------------------------- 采购单


def test_采购单新建与审批回执同形_两键封口(seed):
    created = seed["po_drug"]
    assert list(created.keys()) == ORDER_ACTION_KEYS
    assert created == {"id": created["id"], "status": "pending"}
    approved = seed["po_drug_approved"]
    assert approved == {"id": created["id"], "status": "approved"}
    assert list(approved.keys()) == ORDER_ACTION_KEYS
    rejected = seed["po_rejected_receipt"]
    assert rejected == {"id": seed["po_rejected"]["id"], "status": "rejected"}


def test_验收回执_药品回汇总int_物资回null(seed):
    body = seed["po_drug_received"]
    assert list(body.keys()) == ORDER_RECEIVE_KEYS
    assert body == {"id": seed["po_drug"]["id"], "status": "received", "stock_quantity": 50}
    # Integer 列：声明成 float 会把 50 印成 50.0
    assert type(body["stock_quantity"]) is int
    material = seed["po_material_received"]
    assert list(material.keys()) == ORDER_RECEIVE_KEYS  # 键恒在，值为 null（不是键消失）
    assert material == {"id": seed["po_material"]["id"], "status": "received", "stock_quantity": None}


def test_采购单列表精确_过滤(client, admin, seed):
    org_id, sid = seed["org"]["id"], seed["supplier"]["id"]
    expected = [
        {"id": seed["po_rejected"]["id"], "org_id": org_id, "supplier_id": sid,
         "item_type": "drug", "item_code": "PHCT-IBU", "item_name": "契约布洛芬",
         "quantity": 20, "status": "rejected"},
        {"id": seed["po_pending"]["id"], "org_id": org_id, "supplier_id": sid,
         "item_type": "drug", "item_code": "PHCT-AMX", "item_name": "契约阿莫西林",
         "quantity": 30, "status": "pending"},
        {"id": seed["po_material"]["id"], "org_id": org_id, "supplier_id": sid,
         "item_type": "material", "item_code": "PHCT-GZ", "item_name": "契约纱布",
         "quantity": 7, "status": "received"},
        {"id": seed["po_drug"]["id"], "org_id": org_id, "supplier_id": sid,
         "item_type": "drug", "item_code": "PHCT-MET", "item_name": "契约二甲双胍",
         "quantity": 50, "status": "received"},
    ]
    rows = client.get("/api/pharmacy/purchase-orders", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [ORDER_KEYS] * 4  # id 倒序
    assert rows == expected
    assert type(rows[0]["quantity"]) is int
    assert client.get("/api/pharmacy/purchase-orders?status=pending", headers=admin).json() == [
        expected[1]
    ]
    assert client.get(
        f"/api/pharmacy/purchase-orders?org_id={org_id}&status=received", headers=admin
    ).json() == [expected[2], expected[3]]


# ---------------------------------------------------------------- 盘点


def test_盘点回执精确_盈亏两分支(seed):
    loss = seed["take_loss"]
    assert list(loss.keys()) == STOCK_TAKE_CREATED_KEYS
    assert loss == {"id": loss["id"], "book_qty": 50, "actual_qty": 45, "diff": -5}
    assert type(loss["diff"]) is int and type(loss["book_qty"]) is int
    gain = seed["take_gain"]
    assert gain == {"id": gain["id"], "book_qty": 45, "actual_qty": 47, "diff": 2}


def test_盘点列表精确_note默认空串(client, admin, seed):
    rows = client.get("/api/pharmacy/stock-takes", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [STOCK_TAKE_KEYS] * 2  # id 倒序
    assert rows == [
        {"id": seed["take_gain"]["id"], "org_id": seed["org"]["id"], "drug_code": "PHCT-MET",
         "book_qty": 45, "actual_qty": 47, "diff": 2, "note": ""},
        {"id": seed["take_loss"]["id"], "org_id": seed["org"]["id"], "drug_code": "PHCT-MET",
         "book_qty": 50, "actual_qty": 45, "diff": -5, "note": "破损5片"},
    ]


# ---------------------------------------------------------------- 采购建议


def test_采购建议精确_usage恒float_数量恒int(client, admin, seed):
    rows = client.get("/api/pharmacy/purchase-suggestions", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [SUGGESTION_KEYS] * 2  # 建议量倒序
    assert rows == [
        {"drug_code": "PHCT-MET", "drug_name": "契约二甲双胍", "usage_30d": 91.5,
         "current_stock": 47, "suggested_quantity": 45},  # int(91.5-47+0.999)
        {"drug_code": "PHCT-INTZ", "drug_name": "契约整数用量药", "usage_30d": 30.0,
         "current_stock": 0, "suggested_quantity": 30},
    ]
    # usage 唯一产地是 float(...)：整数用量也是 30.0，不是 30
    assert isinstance(rows[0]["usage_30d"], float) and isinstance(rows[1]["usage_30d"], float)
    assert type(rows[1]["current_stock"]) is int and type(rows[1]["suggested_quantity"]) is int


# ---------------------------------------------------------------- 错误体


def test_各类错误体都只有detail(client, admin, seed):
    op = seed["operator"]
    ok_order = {"org_id": seed["org"]["id"], "supplier_id": seed["supplier"]["id"],
                "item_type": "drug", "item_code": "X", "item_name": "X", "quantity": 1}
    cases = [
        client.post("/api/pharmacy/suppliers", json={"name": "契约县医药公司"},
                    headers=seed["director"]),  # 重名 409
        client.post("/api/pharmacy/purchase-orders",
                    json={**ok_order, "supplier_id": 999999}, headers=op),  # 404
        client.post(f"/api/pharmacy/purchase-orders/{seed['po_drug']['id']}/approve",
                    headers=seed["director"]),  # 已验收再审批 409
        client.post("/api/pharmacy/purchase-orders/999999/approve",
                    headers=seed["director"]),  # 404
        client.post(f"/api/pharmacy/purchase-orders/{seed['po_pending']['id']}/receive",
                    headers=op),  # 未审批不可验收 409
        client.post("/api/pharmacy/stock-takes",
                    json={"org_id": seed["org"]["id"], "drug_code": "NO-SUCH", "actual_qty": 1},
                    headers=op),  # 无库存记录 404
    ]
    assert [r.status_code for r in cases] == [409, 404, 409, 404, 409, 404]
    for r in cases:
        assert set(r.json()) == {"detail"}
