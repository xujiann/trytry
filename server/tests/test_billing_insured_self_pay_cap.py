"""有医保分担的结算单，个人自付能收两遍：「收超了没有」拿全部渠道的已收额去比结算总额（P1-163）。

结算总额 = 医保支付（基金那一份）+ 个人自付。收款的默认额早按渠道分开取（医保渠道取医保支付、其余渠道取自付扣掉押金
冲抵，P1-142），居民端「已支付」也按渠道分开算（`self_pay_outstanding` 的说明写着与收款「同一套算术」）；唯独「收超了
没有」与网关回调入账复核拿全部渠道的已收额比总额——基金那一份成了个人自付渠道的额度。开发库实测（修前代码）：总额 1000
（医保 600、自付 400），收费不填金额点两次，两张 400 的现金单都 paid，自付收了两遍（第三次才被挡）；网关单超时作废后
改收现金，迟到的网关成功回调照样入账。既有用例的结算单一律 insurance_pay=0，走不到这一支。

修法：医保渠道与其余渠道的额度各算各的——医保渠道 ≤ 医保支付，其余渠道 ≤ 自付 − 押金冲抵；下单与回调复核同一个函数。
"""
import itertools
import json
import time

import httpx
import pytest

from app.config import settings
from app.egress import gateway_sign
from app.routers import billing

ITEM = "P1163-FEE"
GATEWAY_KEY = "p1163-gateway-key"
_SEQ = itertools.count(1)


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1163 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    client.post("/api/dictionaries/charge/entries", headers=admin, json={"code": ITEM, "name": "治疗费(P1163)"})
    item = client.post("/api/billing/charge-items", headers=admin,
                       json={"code": ITEM, "name": "治疗费(P1163)", "category": "treatment", "price": 500})
    assert item.status_code in (201, 409), item.text
    return {"org": org}


def _settled(client, admin, world, insurance_pay):
    """门诊：就诊 → 记 2 × 500 = 1000 元 → 结算（医保支付 insurance_pay），返回结算回执。"""
    n = next(_SEQ)
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P1163 患者{n}", "id_card": f"33010619790909{n:04d}"}).json()["id"]
    encounter = client.post("/api/encounters", headers=admin, json={
        "patient_id": patient, "org_id": world["org"], "diagnosis_name": "高血压"}).json()["id"]
    assert client.post("/api/billing/details", headers=admin, json={
        "patient_id": patient, "encounter_id": encounter, "item_code": ITEM, "quantity": 2}).status_code == 201
    settled = client.post("/api/billing/settlements", headers=admin, json={
        "bill_type": "outpatient", "encounter_id": encounter, "insurance_pay": insurance_pay})
    assert settled.status_code == 201, settled.text
    assert (settled.json()["total_amount"], settled.json()["self_pay"]) == (1000, 1000 - insurance_pay)
    return settled.json()


def _pay(client, admin, settlement_id, channel, amount=None):
    body = {"settlement_id": settlement_id, "channel": channel}
    if amount is not None:
        body["amount"] = amount
    return client.post("/api/billing/payments", headers=admin, json=body)


def test_不填金额点两次收费_自付只收一遍(client, admin, world):
    s = _settled(client, admin, world, insurance_pay=600)
    first = _pay(client, admin, s["id"], "cash")
    assert first.status_code == 201 and first.json()["amount"] == 400, first.text
    again = _pay(client, admin, s["id"], "cash")
    assert again.status_code == 422, again.text   # 修前 201 paid：400 + 400 ≤ 总额 1000
    assert again.json()["detail"].startswith("支付金额超出结算单未付余额（总额 1000")
    assert "个人自付 400" in again.json()["detail"]


def test_医保渠道收基金那一份_额度与自付分开(client, admin, world):
    s = _settled(client, admin, world, insurance_pay=600)
    fund = _pay(client, admin, s["id"], "insurance")
    assert fund.status_code == 201 and fund.json()["amount"] == 600, fund.text
    extra_fund = _pay(client, admin, s["id"], "insurance", amount=1)
    assert extra_fund.status_code == 422 and "医保支付 600" in extra_fund.json()["detail"], extra_fund.text
    # 基金那一份收足了不影响自付：自付照常按 400 收
    cash = _pay(client, admin, s["id"], "cash")
    assert cash.status_code == 201 and cash.json()["amount"] == 400, cash.text


def test_网关迟到的成功回调_自付已由现金收足的拒绝入账(client, admin, world, monkeypatch):
    from app.database import SessionLocal
    from app.models import PaymentOrder, User

    monkeypatch.setattr(settings, "payment_gateway_url", "http://8.8.8.8/gw")
    monkeypatch.setattr(settings, "payment_gateway_key", GATEWAY_KEY)
    assert billing.register_http_gateway() is True
    monkeypatch.setattr(httpx, "post", lambda *a, **k: type("R", (), {
        "status_code": 200, "text": "", "json": lambda self: {"accepted": True, "trade_no": "P1163-GW"}})())
    try:
        s = _settled(client, admin, world, insurance_pay=600)
        order = _pay(client, admin, s["id"], "gateway")
        assert order.status_code == 201 and order.json()["status"] == "pending", order.text
        with SessionLocal() as db:   # 网关单还挂着的时候，窗口上已经收了现金 400（比如扫码超时后改收现金）
            operator = db.query(User).filter_by(username="admin").one().id
            db.add(PaymentOrder(settlement_id=s["id"], channel="cash", amount=400, status="paid",
                                trade_no="P1163-CASH", created_by=operator))
            db.commit()
        payload = {"order_id": order.json()["id"], "status": "paid", "trade_no": "P1163-GW",
                   "amount_fen": 40000}
        body = json.dumps(payload).encode("utf-8")
        ts = str(int(time.time()))
        late = client.post("/api/billing/payments/callback", content=body, headers={
            "Content-Type": "application/json", "X-Timestamp": ts, "X-Signature": gateway_sign(GATEWAY_KEY, ts, body)})
        assert late.status_code == 409, late.text   # 修前 200 paid：400 + 400 ≤ 总额 1000，自付收了两遍
        assert late.json()["detail"].startswith("该结算单已收足（总额 1000，个人自付 400")
    finally:
        billing._GATEWAYS.pop("gateway", None)
