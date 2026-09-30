"""收款 422「超出未付余额」把待支付的网关单也写成「已付」（P2-1124，第三十二批扫描 B1-3 的文案那一半）。

下单时额度按「到账 + 待支付」占（`_collected_amount(include_pending=True)`：受理不等于到账，可不占住额度同一张账单
能扫三次码收三次钱），说明却把这个数整个写成「已付」。修前（b3069f0 实测）：自付 400 的结算单，网关下单受理
（pending，患者还没扫码）之后再下网关单、改收现金，都得到 422「支付金额超出结算单未付余额（总额 400，个人自付 400，
已付 400）」——实际一分没付，收银员照着说明只能以为钱已经收了。

修后：说明把已付（到账的）与待支付分开写，额度口径不变；没有待支付单时说明与原先一字不差。付款码落库可取回、
收银端能否作废待支付的网关单待裁定，不在本条。
"""
import itertools
import re

import pytest

from app.routers import billing

ITEM = "P21124-FEE"
_SEQ = itertools.count(1)


class _PendingGateway:
    """与 `payments.HttpGatewayPaymentGateway.pay` 同形的应答：受理即 pending，带付款链接与二维码串。"""

    name = "gateway"

    def pay(self, order_id, amount, channel):
        return {"success": True, "pending": True, "trade_no": f"P21124-GW{order_id}",
                "pay_url": f"https://pay.example.com/o/{order_id}", "qr_code": f"weixin://p21124/{order_id}"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21124 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    client.post("/api/dictionaries/charge/entries", headers=admin, json={"code": ITEM, "name": "胸部CT(P21124)"})
    item = client.post("/api/billing/charge-items", headers=admin,
                       json={"code": ITEM, "name": "胸部CT(P21124)", "category": "exam", "price": 400})
    assert item.status_code in (201, 409), item.text
    return {"org": org}


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setitem(billing._GATEWAYS, "gateway", _PendingGateway())


def _settled(client, admin, world):
    """门诊：就诊 → 记 400 元 → 结算（无医保分担），返回结算单编号。"""
    n = next(_SEQ)
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P21124 患者{n}", "id_card": f"33010619800101{n:04d}"}).json()["id"]
    encounter = client.post("/api/encounters", headers=admin, json={
        "patient_id": patient, "org_id": world["org"], "diagnosis_name": "咳嗽"}).json()["id"]
    assert client.post("/api/billing/details", headers=admin, json={
        "patient_id": patient, "encounter_id": encounter, "item_code": ITEM}).status_code == 201
    settled = client.post("/api/billing/settlements", headers=admin, json={
        "bill_type": "outpatient", "encounter_id": encounter})
    assert settled.status_code == 201 and settled.json()["self_pay"] == 400, settled.text
    return settled.json()["id"]


def _pay(client, admin, settlement_id, channel, amount=None):
    body = {"settlement_id": settlement_id, "channel": channel}
    if amount is not None:
        body["amount"] = amount
    return client.post("/api/billing/payments", headers=admin, json=body)


def _covered(resp) -> tuple[float, float | None]:
    """说明尾巴上的（已付, 待支付）；没写待支付为 None。金额的写法随库不同（开发库整数回 int，P2-70），按数比。"""
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail.startswith("支付金额超出结算单未付余额（总额 400，个人自付 400，"), detail
    got = re.search(r"，已付 ([0-9.]+)(?:，待支付 ([0-9.]+))?）$", detail)
    assert got, detail
    return float(got.group(1)), (float(got.group(2)) if got.group(2) else None)


def test_有一张待支付网关单_再下单与改收现金_说明里已付0待支付400(client, admin, world, gateway):
    settlement = _settled(client, admin, world)
    order = _pay(client, admin, settlement, "gateway")
    assert order.status_code == 201 and order.json()["status"] == "pending", order.text
    assert _covered(_pay(client, admin, settlement, "gateway")) == (0, 400)   # 修前 (400, None)：「已付 400」
    assert _covered(_pay(client, admin, settlement, "cash")) == (0, 400)      # 额度照旧占着，只是说明写对


def test_到账一部分又挂着一张待支付_两样分开写(client, admin, world, gateway):
    settlement = _settled(client, admin, world)
    assert _pay(client, admin, settlement, "cash", amount=100).json()["status"] == "paid"
    pending = _pay(client, admin, settlement, "gateway")   # 留空收剩下的 300（P2-630）
    assert pending.status_code == 201 and (pending.json()["status"], pending.json()["amount"]) == ("pending", 300)
    assert _covered(_pay(client, admin, settlement, "cash", amount=1)) == (100, 300)   # 修前 (400, None)


def test_没有待支付单_说明与原先一致(client, admin, world):
    settlement = _settled(client, admin, world)
    assert _pay(client, admin, settlement, "cash").json()["status"] == "paid"
    over = _pay(client, admin, settlement, "cash", amount=1)
    assert _covered(over) == (400, None) and "待支付" not in over.json()["detail"]
