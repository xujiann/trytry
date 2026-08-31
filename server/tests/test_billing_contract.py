"""费用结算 `/api/billing` 平台侧 15 个未治理端点的**特征化网 + 响应契约**。

套路同 `test_esb_contract.py` / `test_vaccine_supply_contract.py`：先钉住**当前**
响应的完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §7/§11）。已治理的 6 个端点（deposits×4 / balance / callback）不在此列。

本簇的建模判断（都以此处的精确断言为依据）：

- **Money 列及其 round/sum 派生值一律 `int | float`**：整数金额读回来是 `int`
  （SQLite NUMERIC 亲和无损转整、`round(int)` 仍 int），声明成 float 会把
  「15 元」印成「15.0 元」。种子里刻意同时铺了整数（床位费 100×3=300、
  诊查费 15）与小数（25.5、175.5）两种取值，`type(x) is int` 的断言才咬得住。
- **`avg_amount`/`insurance_ratio_pct` 恒 float**：真除法 / `*100.0` / 兜底
  字面量 `0.0` 三条产地全是浮点，声明 float 才是原样（Float 列同理）。
- **结算回执的条件键**：`deposit_offset`/`payable_after_offset`/`deposit_balance`
  仅住院结算出现，门诊回执**整个没有**这些键（不是 null）——
  `exclude_unset` 双向钉：住院一条断"键在且值对"，门诊一条断"键不在"。
- **支付回执的条件键**：`pay_url`/`qr_code` 仅异步网关（pending）分支出现，
  同步渠道（Mock 即付）回执 14 键封口——两分支各钉一遍。
- 新建/操作回执与列表行**同形**（settlements/payments 的额外尾键除外）；
  退款回执 = 支付单 14 键 + `refund_no`/`refund_amount` 两个恒在尾键。
- 对账差异行恒为 8 键固定形状；`remote_amount` 落库再读，整数值回 int
  （通道给 5.0、detail 文案写 5.0、字段却是 5——三处各钉各的）。
"""
import json
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.config import settings
from app.main import app
from app.routers import billing as billing_router
from app.routers.billing import MOCK_GATEWAY

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

CHARGE_ITEM_KEYS = ["id", "code", "name", "category", "price", "active"]
PRICE_CHANGE_KEYS = ["id", "old_price", "new_price", "reason", "effective_date", "changed_at"]
DETAIL_KEYS = [
    "id", "patient_id", "admission_id", "encounter_id", "item_code", "item_name",
    "unit_price", "quantity", "amount", "settled", "settlement_id",
]
SETTLEMENT_KEYS = [
    "id", "patient_id", "org_id", "bill_type", "admission_id", "encounter_id",
    "total_amount", "insurance_pay", "self_pay", "insurance_settlement_id", "created_at",
]
SETTLEMENT_INPATIENT_EXTRA = ["deposit_offset", "payable_after_offset", "deposit_balance"]
STAT_KEYS = ["bill_type", "count", "total_amount", "insurance_pay", "avg_amount", "insurance_ratio_pct"]
PAYMENT_KEYS = [
    "id", "settlement_id", "channel", "channel_name", "amount", "refunded_amount",
    "status", "status_name", "trade_no", "fail_reason",
    "paid_at", "refunded_at", "callback_at", "created_at",
]
BATCH_KEYS = [
    "id", "date", "total_orders", "total_amount", "matched", "unmatched",
    "diff_amount", "created_at", "diffs",
]
DIFF_KEYS = [
    "id", "order_id", "trade_no", "diff_type", "diff_type_name",
    "local_amount", "remote_amount", "detail",
]


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_gateway():
    """每个用例前复位 Mock 通道开关（对账用例会拨差异开关，别串扰）。"""
    MOCK_GATEWAY.reset()
    yield
    MOCK_GATEWAY.reset()


def login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def seed(client, admin):
    """一次种完全部场景，测试只做断言（esb 契约网同款布局）。

    金额取值刻意混铺整数与小数：BED 100×3=300（int）、LAB 25.5×1（float）、
    REG 15×1 / 15×2（int）；住院结算 total 325.5 / 医保 150 / 自付 175.5，
    押金 100 全额冲抵；门诊结算 total 15 全自付，现金收讫后退 5.5 再退 3。
    """
    data: dict = {}
    org = client.post(
        "/api/organizations",
        json={"name": "契约结算医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    data["org"] = org
    for username, role in [("blct_doc", "doctor"), ("blct_op", "operator")]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
    data["doctor"] = login(client, "blct_doc", "pass123456")
    data["operator"] = login(client, "blct_op", "pass123456")
    data["patient"] = client.post(
        "/api/patients",
        json={"name": "契约结算患者", "id_card": "330881199001017701"},
        headers=admin,
    ).json()

    # 收费目录：整数价 + 小数价 + 之后要调价的第三项
    def charge_item(code, name, category, price):
        resp = client.post(
            "/api/billing/charge-items",
            json={"code": code, "name": name, "category": category, "price": price},
            headers=admin,
        )
        assert resp.status_code == 201, resp.text
        return resp.json()

    data["item_bed"] = charge_item("CT-BED", "床位费(契约)", "bed", 100)
    data["item_lab"] = charge_item("CT-LAB", "血钾测定(契约)", "exam", 25.5)
    data["item_reg"] = charge_item("CT-REG", "门诊诊查费(契约)", "treatment", 15)

    # 住院场景：病区/床位/入院登记 + 押金预交 100
    ward = client.post(
        "/api/inpatient/wards", json={"org_id": org["id"], "name": "契约病区"}, headers=admin
    ).json()
    bed = client.post(
        "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": "CT-01"}, headers=admin
    ).json()
    data["admission"] = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": data["patient"]["id"], "ward_id": ward["id"], "bed_id": bed["id"],
              "diagnosis_name": "肺炎"},
        headers=data["operator"],
    ).json()
    assert client.post(
        "/api/billing/deposits",
        json={"admission_id": data["admission"]["id"], "amount": 100},
        headers=data["operator"],
    ).status_code == 201

    def bill_detail(payload):
        resp = client.post("/api/billing/details", json=payload, headers=data["operator"])
        assert resp.status_code == 201, resp.text
        return resp.json()

    adm_id = data["admission"]["id"]
    data["d1"] = bill_detail({"patient_id": data["patient"]["id"], "admission_id": adm_id,
                              "item_code": "CT-BED", "quantity": 3})
    data["d2"] = bill_detail({"patient_id": data["patient"]["id"], "admission_id": adm_id,
                              "item_code": "CT-LAB"})

    # 门诊场景
    data["encounter"] = client.post(
        "/api/encounters",
        json={"patient_id": data["patient"]["id"], "org_id": org["id"], "diagnosis_name": "上感"},
        headers=data["doctor"],
    ).json()
    enc_id = data["encounter"]["id"]
    data["d3"] = bill_detail({"patient_id": data["patient"]["id"], "encounter_id": enc_id,
                              "item_code": "CT-REG"})

    # 结算：住院（医保 150 + 押金全额冲抵）与门诊（全自付）
    resp = client.post(
        "/api/billing/settlements",
        json={"bill_type": "inpatient", "admission_id": adm_id, "insurance_pay": 150},
        headers=data["operator"],
    )
    assert resp.status_code == 201, resp.text
    data["settle_in"] = resp.json()
    resp = client.post(
        "/api/billing/settlements",
        json={"bill_type": "outpatient", "encounter_id": enc_id, "insurance_pay": 0},
        headers=data["operator"],
    )
    assert resp.status_code == 201, resp.text
    data["settle_out"] = resp.json()

    # 结算后的追加明细（未结清行，供 settled=false 过滤钉住）
    data["d4"] = bill_detail({"patient_id": data["patient"]["id"], "encounter_id": enc_id,
                              "item_code": "CT-REG", "quantity": 2})

    # 调价历史：PATCH 改价（无依据）→ reprice（带依据与生效日）
    data["item_reg_patched"] = client.patch(
        f"/api/billing/charge-items/{data['item_reg']['id']}",
        json={"name": "门诊诊查费(契约改)", "price": 18},
        headers=admin,
    ).json()
    data["item_reg_repriced"] = client.post(
        f"/api/billing/charge-items/{data['item_reg']['id']}/reprice",
        json={"new_price": 16.5, "reason": "契约调价演练", "effective_date": "2026-09-30"},
        headers=admin,
    ).json()

    # 支付：门诊单现金收讫（15，整数）→ 部分退款 5.5 → 再退 3；住院单银行卡收 175.5
    resp = client.post(
        "/api/billing/payments",
        json={"settlement_id": data["settle_out"]["id"], "channel": "cash"},
        headers=data["operator"],
    )
    assert resp.status_code == 201, resp.text
    data["pay1"] = resp.json()
    data["refund1"] = client.post(
        f"/api/billing/payments/{data['pay1']['id']}/refund",
        json={"amount": 5.5, "reason": "多收退回"},
        headers=data["operator"],
    ).json()
    data["refund2"] = client.post(
        f"/api/billing/payments/{data['pay1']['id']}/refund",
        json={"amount": 3},
        headers=data["operator"],
    ).json()
    resp = client.post(
        "/api/billing/payments",
        json={"settlement_id": data["settle_in"]["id"], "channel": "card"},
        headers=data["operator"],
    )
    assert resp.status_code == 201, resp.text
    data["pay2"] = resp.json()
    return data


# ---------------------------------------------------------------- 收费项目目录


def test_收费项目回执精确_键序与Money类型(seed):
    body = seed["item_bed"]
    assert list(body.keys()) == CHARGE_ITEM_KEYS
    assert body == {
        "id": body["id"], "code": "CT-BED", "name": "床位费(契约)",
        "category": "bed", "price": 100, "active": True,
    }
    # Money 列：整数价读回来是 int（声明成 float 会变 100.0，即改字节）
    assert type(body["price"]) is int
    assert seed["item_lab"]["price"] == 25.5 and isinstance(seed["item_lab"]["price"], float)


def test_收费项目维护回执与列表同形(client, admin, seed):
    patched = seed["item_reg_patched"]
    assert list(patched.keys()) == CHARGE_ITEM_KEYS
    assert patched == {**seed["item_reg"], "name": "门诊诊查费(契约改)", "price": 18}
    assert type(patched["price"]) is int
    repriced = seed["item_reg_repriced"]
    assert repriced == {**patched, "price": 16.5}

    rows = client.get("/api/billing/charge-items", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [CHARGE_ITEM_KEYS] * 3  # code 升序
    assert rows == [seed["item_bed"], seed["item_lab"], repriced]
    assert client.get("/api/billing/charge-items?category=exam", headers=admin).json() == [
        seed["item_lab"]
    ]
    assert client.get("/api/billing/charge-items?active=true", headers=admin).json() == rows


def test_调价历史精确_两条产地两种形状(client, admin, seed):
    rows = client.get(
        f"/api/billing/charge-items/{seed['item_reg']['id']}/price-history", headers=admin
    ).json()
    assert [list(r.keys()) for r in rows] == [PRICE_CHANGE_KEYS] * 2  # id 倒序
    # reprice：带依据与生效日；PATCH 改价：两者留空（模型默认值）
    assert rows[0] == {
        "id": rows[0]["id"], "old_price": 18, "new_price": 16.5,
        "reason": "契约调价演练", "effective_date": "2026-09-30",
        "changed_at": rows[0]["changed_at"],
    }
    assert rows[1] == {
        "id": rows[1]["id"], "old_price": 15, "new_price": 18,
        "reason": "", "effective_date": "", "changed_at": rows[1]["changed_at"],
    }
    assert type(rows[0]["old_price"]) is int and isinstance(rows[0]["new_price"], float)
    assert isinstance(rows[0]["changed_at"], str)


# ---------------------------------------------------------------- 费用明细


def test_计费回执精确_键序与Money类型(seed):
    body = seed["d1"]
    assert list(body.keys()) == DETAIL_KEYS
    assert body == {
        "id": body["id"],
        "patient_id": seed["patient"]["id"],
        "admission_id": seed["admission"]["id"],
        "encounter_id": None,
        "item_code": "CT-BED",
        "item_name": "床位费(契约)",
        "unit_price": 100,
        "quantity": 3,
        "amount": 300,
        "settled": False,
        "settlement_id": None,
    }
    assert type(body["unit_price"]) is int and type(body["amount"]) is int
    # 小数金额原样是 float；门诊行 admission_id 为 null
    d2 = seed["d2"]
    assert (d2["unit_price"], d2["amount"], d2["quantity"]) == (25.5, 25.5, 1)
    assert isinstance(d2["amount"], float)
    d3 = seed["d3"]
    assert d3["admission_id"] is None and d3["encounter_id"] == seed["encounter"]["id"]
    # 快照语义：调价发生在计费之后，明细里的 item_name/unit_price 不随目录变
    assert d3["item_name"] == "门诊诊查费(契约)" and d3["unit_price"] == 15


def test_费用明细列表与回执同形_过滤(client, admin, seed):
    settled_d1 = {**seed["d1"], "settled": True, "settlement_id": seed["settle_in"]["id"]}
    settled_d2 = {**seed["d2"], "settled": True, "settlement_id": seed["settle_in"]["id"]}
    settled_d3 = {**seed["d3"], "settled": True, "settlement_id": seed["settle_out"]["id"]}
    rows = client.get(
        f"/api/billing/details?admission_id={seed['admission']['id']}", headers=admin
    ).json()
    assert [list(r.keys()) for r in rows] == [DETAIL_KEYS] * 2
    assert rows == [settled_d2, settled_d1]  # id 倒序
    assert client.get(
        f"/api/billing/details?patient_id={seed['patient']['id']}", headers=admin
    ).json() == [seed["d4"], settled_d3, settled_d2, settled_d1]
    assert client.get(
        f"/api/billing/details?encounter_id={seed['encounter']['id']}&settled=false",
        headers=admin,
    ).json() == [seed["d4"]]
    assert client.get(
        f"/api/billing/details?encounter_id={seed['encounter']['id']}&settled=true",
        headers=admin,
    ).json() == [settled_d3]


# ---------------------------------------------------------------- 结算


def test_住院结算回执_条件键在且值对(seed):
    body = seed["settle_in"]
    assert list(body.keys()) == SETTLEMENT_KEYS + SETTLEMENT_INPATIENT_EXTRA
    assert body == {
        "id": body["id"],
        "patient_id": seed["patient"]["id"],
        "org_id": seed["org"]["id"],
        "bill_type": "inpatient",
        "admission_id": seed["admission"]["id"],
        "encounter_id": None,
        "total_amount": 325.5,
        "insurance_pay": 150,
        "self_pay": 175.5,
        "insurance_settlement_id": body["insurance_settlement_id"],
        "created_at": body["created_at"],
        "deposit_offset": 100,
        "payable_after_offset": 75.5,
        "deposit_balance": 0.0,
    }
    assert isinstance(body["insurance_settlement_id"], int)
    # Money 派生值的 int/float 之别：整数读回 int；押金全额冲抵后的余额 0
    # 走的是 `round(total or 0.0, 2)` 的兜底字面量分支——是 0.0 不是 0
    assert type(body["insurance_pay"]) is int and type(body["deposit_offset"]) is int
    assert isinstance(body["deposit_balance"], float)
    assert isinstance(body["total_amount"], float) and isinstance(body["payable_after_offset"], float)


def test_门诊结算回执_条件键整个不出现(seed):
    body = seed["settle_out"]
    assert list(body.keys()) == SETTLEMENT_KEYS  # 无 deposit_* 三键（不是 null）
    assert body == {
        "id": body["id"],
        "patient_id": seed["patient"]["id"],
        "org_id": seed["org"]["id"],
        "bill_type": "outpatient",
        "admission_id": None,
        "encounter_id": seed["encounter"]["id"],
        "total_amount": 15,
        "insurance_pay": 0,
        "self_pay": 15,
        "insurance_settlement_id": None,
        "created_at": body["created_at"],
    }
    assert type(body["total_amount"]) is int and type(body["self_pay"]) is int


def test_结算列表与回执同形_不带条件键(client, admin, seed):
    in_row = {k: v for k, v in seed["settle_in"].items() if k in SETTLEMENT_KEYS}
    rows = client.get("/api/billing/settlements", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [SETTLEMENT_KEYS] * 2
    assert rows == [seed["settle_out"], in_row]  # id 倒序
    assert client.get("/api/billing/settlements?bill_type=inpatient", headers=admin).json() == [
        in_row
    ]
    assert client.get(
        f"/api/billing/settlements?patient_id={seed['patient']['id']}", headers=admin
    ).json() == rows


def test_费用统计精确_int与float之别(client, admin, seed):
    rows = client.get("/api/billing/stats", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [STAT_KEYS] * 2  # bill_type 升序
    assert rows == [
        {
            "bill_type": "inpatient", "count": 1, "total_amount": 325.5,
            "insurance_pay": 150, "avg_amount": 325.5,
            "insurance_ratio_pct": round(150 * 100.0 / 325.5, 2),
        },
        {
            "bill_type": "outpatient", "count": 1, "total_amount": 15,
            "insurance_pay": 0, "avg_amount": 15.0, "insurance_ratio_pct": 0.0,
        },
    ]
    out_row = rows[1]
    # 总额/医保额是 Money 之和：整数读回 int；均次与占比是真除法/兜底 0.0：恒 float
    assert type(out_row["total_amount"]) is int and type(out_row["insurance_pay"]) is int
    assert isinstance(out_row["avg_amount"], float) and isinstance(out_row["insurance_ratio_pct"], float)
    assert isinstance(rows[0]["total_amount"], float)


# ---------------------------------------------------------------- 统一支付


def test_同步渠道支付回执_14键封口无支付参数(seed):
    body = seed["pay1"]
    assert list(body.keys()) == PAYMENT_KEYS  # 同步分支：无 pay_url/qr_code（不是 null）
    assert body == {
        "id": body["id"],
        "settlement_id": seed["settle_out"]["id"],
        "channel": "cash",
        "channel_name": "现金",
        "amount": 15,
        "refunded_amount": 0,
        "status": "paid",
        "status_name": "已支付",
        "trade_no": body["trade_no"],
        "fail_reason": "",
        "paid_at": body["paid_at"],
        "refunded_at": None,
        "callback_at": None,
        "created_at": body["created_at"],
    }
    assert body["trade_no"].startswith("MOCK") and isinstance(body["paid_at"], str)
    assert type(body["amount"]) is int and type(body["refunded_amount"]) is int
    assert seed["pay2"]["amount"] == 175.5 and isinstance(seed["pay2"]["amount"], float)


def test_退款回执_支付单14键加两个尾键(seed):
    body = seed["refund1"]
    assert list(body.keys()) == PAYMENT_KEYS + ["refund_no", "refund_amount"]
    assert body == {
        **{k: v for k, v in seed["pay1"].items()},
        "refunded_amount": 5.5,
        "refunded_at": body["refunded_at"],
        "refund_no": "RF" + seed["pay1"]["trade_no"][-10:],
        "refund_amount": 5.5,
    }
    assert isinstance(body["refunded_at"], str)
    # 入参退款额经 `amount: float` 字段解析：整数入参 3 也是 3.0（请求侧产地）；
    # 缺省全额退款分支的 refundable 则来自 DB 读回，整数值是 int——故建模 int|float
    body2 = seed["refund2"]
    assert body2["refund_amount"] == 3.0 and isinstance(body2["refund_amount"], float)
    assert body2["refunded_amount"] == 8.5 and body2["status"] == "paid"


def test_支付列表与回执同形_过滤(client, admin, seed):
    pay1_row = {k: v for k, v in seed["refund2"].items() if k in PAYMENT_KEYS}
    rows = client.get("/api/billing/payments", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [PAYMENT_KEYS] * 2
    assert rows == [seed["pay2"], pay1_row]  # id 倒序
    assert client.get(
        f"/api/billing/payments?settlement_id={seed['settle_out']['id']}", headers=admin
    ).json() == [pay1_row]
    assert client.get("/api/billing/payments?channel=card", headers=admin).json() == [seed["pay2"]]
    assert client.get("/api/billing/payments?status=paid", headers=admin).json() == rows


class _DummyResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def test_网关渠道下单回执_pending分支多两个尾键(client, admin, seed, monkeypatch):
    """条件键的另一分支：异步受理回执 = 支付单 14 键 + pay_url/qr_code。"""
    monkeypatch.setattr(settings, "payment_gateway_url", "http://8.8.8.8/gw")
    monkeypatch.setattr(settings, "payment_gateway_key", "contract-gw-key")
    monkeypatch.setattr(
        httpx, "post",
        lambda url, content=b"", headers=None, timeout=None, **kw: _DummyResp(
            {"accepted": True, "trade_no": "GWCT0001",
             "pay_url": "https://pay.example/h5", "qr_code": "weixin://wxpay/ct"}
        ),
    )
    assert billing_router.register_http_gateway() is True
    try:
        patient = client.post(
            "/api/patients",
            json={"name": "契约网关患者", "id_card": "330881199001017702"},
            headers=admin,
        ).json()
        encounter = client.post(
            "/api/encounters",
            json={"patient_id": patient["id"], "org_id": seed["org"]["id"], "diagnosis_name": "复诊"},
            headers=seed["doctor"],
        ).json()
        client.post(
            "/api/billing/details",
            json={"patient_id": patient["id"], "encounter_id": encounter["id"],
                  "item_code": "CT-REG", "quantity": 2},
            headers=seed["operator"],
        )
        settlement = client.post(
            "/api/billing/settlements",
            json={"bill_type": "outpatient", "encounter_id": encounter["id"], "insurance_pay": 0},
            headers=seed["operator"],
        ).json()
        resp = client.post(
            "/api/billing/payments",
            json={"settlement_id": settlement["id"], "channel": "gateway"},
            headers=seed["operator"],
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert list(body.keys()) == PAYMENT_KEYS + ["pay_url", "qr_code"]
        assert body == {
            "id": body["id"],
            "settlement_id": settlement["id"],
            "channel": "gateway",
            "channel_name": "网关支付",
            "amount": 33,
            "refunded_amount": 0,
            "status": "pending",
            "status_name": "待支付",
            "trade_no": "GWCT0001",
            "fail_reason": "",
            "paid_at": None,
            "refunded_at": None,
            "callback_at": None,
            "created_at": body["created_at"],
        # 16.5×2=33.0 落库读回 int：pending 分支的金额同样不许被印成 33.0
            "pay_url": "https://pay.example/h5",
            "qr_code": "weixin://wxpay/ct",
        }
        assert type(body["amount"]) is int
    finally:
        billing_router._GATEWAYS.pop("gateway", None)


# ---------------------------------------------------------------- 日终对账


def test_日终对账_三类差异精确形状(client, admin, seed):
    """通道侧拨三处差异：pay1 金额不一致、pay2 通道缺失、幽灵流水本地无单。"""
    MOCK_GATEWAY.drop_trade_nos.add(seed["pay2"]["trade_no"])
    MOCK_GATEWAY.amount_overrides[seed["pay1"]["trade_no"]] = 5.0
    MOCK_GATEWAY.extra_transactions.append({"trade_no": "GHOST01", "amount": 45.5})

    resp = client.post(
        f"/api/billing/reconciliation/run?date={TODAY}", headers=seed["operator"]
    )
    assert resp.status_code == 201, resp.text
    batch = resp.json()
    assert list(batch.keys()) == BATCH_KEYS
    assert [list(d.keys()) for d in batch["diffs"]] == [DIFF_KEYS] * 3
    assert batch == {
        "id": batch["id"],
        "date": TODAY,
        "total_orders": 2,          # pay1/pay2；pending 的网关单不进对账口径
        "total_amount": 182,        # 175.5 + (15-8.5)=6.5 → 182.0 落库读回 int
        "matched": 0,
        "unmatched": 3,
        "diff_amount": 222.5,       # 175.5 + 1.5 + 45.5
        "created_at": batch["created_at"],
        "diffs": [
            {
                "id": batch["diffs"][0]["id"],
                "order_id": seed["pay1"]["id"],
                "trade_no": seed["pay1"]["trade_no"],
                "diff_type": "amount_mismatch",
                "diff_type_name": "金额不一致",
                "local_amount": 6.5,
                "remote_amount": 5,   # 通道给 5.0，落库读回 int
                "detail": "本地 6.5 与通道 5.0 金额不一致",
            },
            {
                "id": batch["diffs"][1]["id"],
                "order_id": seed["pay2"]["id"],
                "trade_no": seed["pay2"]["trade_no"],
                "diff_type": "missing_remote",
                "diff_type_name": "本地有通道无",
                "local_amount": 175.5,
                "remote_amount": 0,
                "detail": f"本地支付单 {seed['pay2']['id']} 金额 175.5 在通道流水中不存在",
            },
            {
                "id": batch["diffs"][2]["id"],
                "order_id": None,
                "trade_no": "GHOST01",
                "diff_type": "missing_local",
                "diff_type_name": "通道有本地无",
                "local_amount": 0,
                "remote_amount": 45.5,
                "detail": "通道流水 GHOST01 金额 45.5 无对应本地支付单",
            },
        ],
    }
    assert type(batch["total_amount"]) is int and isinstance(batch["diff_amount"], float)
    assert type(batch["diffs"][0]["remote_amount"]) is int
    assert isinstance(batch["diffs"][0]["local_amount"], float)

    # 列表与回执同形；date 过滤；无批次的日期回空
    assert client.get("/api/billing/reconciliation", headers=admin).json() == [batch]
    assert client.get(f"/api/billing/reconciliation?date={TODAY}", headers=admin).json() == [batch]
    assert client.get("/api/billing/reconciliation?date=1999-01-01", headers=admin).json() == []


# ---------------------------------------------------------------- 错误体


def test_各类错误体都只有detail(client, admin, seed):
    cases = [
        client.post("/api/billing/charge-items",
                    json={"code": "CT-BED", "name": "重复", "price": 1}, headers=admin),  # 409
        client.patch("/api/billing/charge-items/999999", json={"price": 1}, headers=admin),  # 404
        client.post(f"/api/billing/charge-items/{seed['item_bed']['id']}/reprice",
                    json={"new_price": 100}, headers=admin),  # 与现价相同 409
        client.get("/api/billing/charge-items/999999/price-history", headers=admin),  # 404
        client.post("/api/billing/details",
                    json={"patient_id": 999999, "encounter_id": 1, "item_code": "CT-REG"},
                    headers=seed["operator"]),  # 404
        client.post("/api/billing/details",
                    json={"patient_id": seed["patient"]["id"],
                          "admission_id": seed["admission"]["id"],
                          "encounter_id": seed["encounter"]["id"], "item_code": "CT-REG"},
                    headers=seed["operator"]),  # 二选一 422
        client.post("/api/billing/settlements",
                    json={"bill_type": "inpatient", "admission_id": seed["admission"]["id"],
                          "insurance_pay": 0},
                    headers=seed["operator"]),  # 无未结明细 422
        client.post("/api/billing/settlements",
                    json={"bill_type": "outpatient", "insurance_pay": 0},
                    headers=seed["operator"]),  # 缺 encounter_id 422
        client.post("/api/billing/payments",
                    json={"settlement_id": 999999, "channel": "cash"},
                    headers=seed["operator"]),  # 404
        client.post("/api/billing/payments",
                    json={"settlement_id": seed["settle_out"]["id"], "channel": "gateway"},
                    headers=seed["operator"]),  # 网关未注册 503
        client.post("/api/billing/payments/999999/refund", json={},
                    headers=seed["operator"]),  # 404
        client.post(f"/api/billing/payments/{seed['pay2']['id']}/refund",
                    json={"amount": 99999}, headers=seed["operator"]),  # 超可退 422
        client.post("/api/billing/reconciliation/run?date=2026-13-01",
                    headers=seed["operator"]),  # 日期格式 422
    ]
    assert [r.status_code for r in cases] == [
        409, 404, 409, 404, 404, 422, 422, 422, 404, 503, 404, 422, 422
    ]
    for r in cases:
        assert set(r.json()) == {"detail"}
