"""支付回调：同一笔「失败」回调重放、退款后网关重投原成功回调，都回 409、不认作重放（P2-1246，第三十六批「接口的 HTTP 语义」
扫描 U3-9）。

`billing._settled_callback_result` 的 docstring 写「同一笔的重放算幂等，其余一律 409」，实际只对 paid 判了幂等：支付失败的单
再收到同一笔的失败回调，回 409「当前状态 支付失败 不接受支付回调」；全额退款后网关重投当初那条成功回调，回 409「当前状态
已退款 不接受支付回调」。网关没拿到 2xx 就当通知失败，一直重投下去，网关一侧也一直记「通知失败」（单据本身不受影响）。

修法：失败单遇同流水号的失败回调、已退款单遇原成功回调（同流水号；金额在前面已与本地单核对），按幂等回 200、不产生任何
写入；其余照旧——流水号不符 409、金额不符 422（金额核对那道防线不动）、终态对不上 409。

网关一律打桩（monkeypatch httpx.post），不出网。
"""
import itertools
import json
import time

import httpx
import pytest

from app.config import settings
from app.database import SessionLocal
from app.egress import gateway_sign
from app.models import PaymentOrder, PaymentRefund
from app.routers import billing

KEY = "p21246-gateway-key"
ITEM = "P21246-FEE"
_seq = itertools.count(1)


@pytest.fixture(autouse=True)
def gateway(monkeypatch):
    """注册 HTTP 网关：下单受理、各回一个新流水号；退款同步成功。用完摘除，不污染别的模块。"""
    monkeypatch.setattr(settings, "payment_gateway_url", "http://8.8.8.8/gw")   # 公网 IP 直写：过出网校验、不走 DNS
    monkeypatch.setattr(settings, "payment_gateway_key", KEY)
    assert billing.register_http_gateway() is True

    def fake_post(url, *args, **kwargs):
        if url.endswith("/refund"):
            return httpx.Response(200, json={"success": True, "refund_no": f"P21246-RF{next(_seq)}"})
        return httpx.Response(200, json={"accepted": True, "trade_no": f"P21246-GW{next(_seq)}"})

    monkeypatch.setattr(httpx, "post", fake_post)
    yield
    billing._GATEWAYS.pop("gateway", None)


@pytest.fixture(scope="module")
def org(client, admin):
    org_id = client.post("/api/organizations", headers=admin, json={
        "name": "P21246 医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    client.post("/api/dictionaries/charge/entries", headers=admin, json={"code": ITEM, "name": "诊查费(P21246)"})
    client.post("/api/billing/charge-items", headers=admin,
                json={"code": ITEM, "name": "诊查费(P21246)", "category": "treatment", "price": 100})
    return org_id


def _pending_order(client, admin, org) -> dict:
    """一张 100 元的网关支付单，停在 pending 等回调。"""
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21246 患者", "id_card": f"33010619820202{next(_seq):04d}"}).json()["id"]
    encounter = client.post("/api/encounters", headers=admin, json={
        "patient_id": patient, "org_id": org, "diagnosis_name": "复诊"}).json()["id"]
    client.post("/api/billing/details", headers=admin, json={
        "patient_id": patient, "encounter_id": encounter, "item_code": ITEM})
    settled = client.post("/api/billing/settlements", headers=admin, json={
        "bill_type": "outpatient", "encounter_id": encounter, "insurance_pay": 0}).json()
    order = client.post("/api/billing/payments", headers=admin, json={
        "settlement_id": settled["id"], "channel": "gateway"}).json()
    assert order["status"] == "pending" and order["trade_no"], order
    return order


def _signed(client, payload):
    body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))
    return client.post("/api/billing/payments/callback", content=body, headers={
        "Content-Type": "application/json", "X-Timestamp": ts, "X-Signature": gateway_sign(KEY, ts, body)})


def _snapshot(order_id: int) -> tuple[dict, int]:
    """支付单整行 + 逐笔退款流水条数：「不产生任何写入」按这个比。"""
    with SessionLocal() as db:
        row = db.get(PaymentOrder, order_id)
        refunds = db.query(PaymentRefund).filter(PaymentRefund.order_id == order_id).count()
        return {c.name: getattr(row, c.name) for c in PaymentOrder.__table__.columns}, refunds


def _failed_order(client, admin, org) -> tuple[dict, dict]:
    order = _pending_order(client, admin, org)
    fail = {"order_id": order["id"], "status": "failed", "trade_no": order["trade_no"], "amount_fen": 10000,
            "message": "用户取消支付"}
    first = _signed(client, fail)
    assert first.status_code == 200 and first.json()["status"] == "failed", first.text
    return order, fail


def _refunded_order(client, admin, org) -> tuple[dict, dict]:
    order = _pending_order(client, admin, org)
    pay = {"order_id": order["id"], "status": "paid", "trade_no": order["trade_no"], "amount_fen": 10000}
    assert _signed(client, pay).status_code == 200
    refund = client.post(f"/api/billing/payments/{order['id']}/refund", headers=admin, json={"reason": "退费"})
    assert refund.status_code == 200 and refund.json()["status"] == "refunded", refund.text
    return order, pay


def test_同一笔失败回调重放_幂等200且库里不变(client, admin, org):
    order, fail = _failed_order(client, admin, org)
    before = _snapshot(order["id"])
    replay = _signed(client, fail)
    assert replay.status_code == 200, replay.text   # 修前 409「当前状态 支付失败 不接受支付回调」
    assert replay.json() == {"ok": True, "order_id": order["id"], "status": "failed", "idempotent": True}
    assert _snapshot(order["id"]) == before


def test_全额退款后网关重投原成功回调_幂等200且库里不变(client, admin, org):
    order, pay = _refunded_order(client, admin, org)
    before = _snapshot(order["id"])
    assert before[0]["status"] == "refunded" and before[1] == 1
    replay = _signed(client, pay)
    assert replay.status_code == 200, replay.text   # 修前 409「当前状态 已退款 不接受支付回调」
    assert replay.json() == {"ok": True, "order_id": order["id"], "status": "refunded", "idempotent": True}
    assert _snapshot(order["id"]) == before


def test_流水号不符照旧409且库里不变(client, admin, org):
    failed, fail = _failed_order(client, admin, org)
    refunded, pay = _refunded_order(client, admin, org)
    for order, payload in ((failed, fail), (refunded, pay)):
        before = _snapshot(order["id"])
        resp = _signed(client, {**payload, "trade_no": "P21246-OTHER"})
        assert resp.status_code == 409, resp.text
        assert _snapshot(order["id"]) == before


def test_金额不符照旧422且库里不变(client, admin, org):
    failed, fail = _failed_order(client, admin, org)
    refunded, pay = _refunded_order(client, admin, org)
    for order, payload in ((failed, fail), (refunded, pay)):
        before = _snapshot(order["id"])
        resp = _signed(client, {**payload, "amount_fen": 9999})
        assert resp.status_code == 422 and "金额" in resp.json()["detail"], resp.text   # 金额核对排在幂等之前，不动
        assert _snapshot(order["id"]) == before


def test_终态对不上照旧409且库里不变(client, admin, org):
    """失败单收到成功回调、已退款单收到失败回调：不是重放，是异常单，照旧 409、不自动翻状态。"""
    failed, fail = _failed_order(client, admin, org)
    refunded, pay = _refunded_order(client, admin, org)
    for order, payload in ((failed, {**fail, "status": "paid"}), (refunded, {**pay, "status": "failed"})):
        before = _snapshot(order["id"])
        resp = _signed(client, payload)
        assert resp.status_code == 409, resp.text
        assert _snapshot(order["id"]) == before
