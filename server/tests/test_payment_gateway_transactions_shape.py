"""对账拉回的通道流水不成形时当成「当日一笔都没有」：好好的对账单被一张全是差异的顶掉（P2-243）。

`HttpGatewayPaymentGateway.query_transactions` 原先 `data.get("transactions", [])`：网关回 200 却没有流水表——
`{"code": "SIGN_ERROR", "message": "签名错误"}` 这类错误应答——被当成「当日通道一笔流水都没有」，对账照常往下走：
先删掉当日那张对平了的对账单，再写一张每笔网关单都是「本地有通道无」的。模块文档自己写着「对账拉不到流水时**必须**
失败——一份错误的对账单比没有对账单更糟」，上面网络异常、4xx/5xx、非 JSON 三种也都中止了，只漏了这一种。
同一处还有两个口子：`{"transactions": null}` 直接 500；流水行没有金额按 0 元比、没有流水号按空串比。

修法：应答里得有一张流水表、每行得有流水号与有限的金额，否则与上面几种失败同一句中止（对账 502、旧批次原样保留）。
"""
import json
import time

import httpx
import pytest

from app.config import settings
from app.egress import gateway_sign
from app.payments import HttpGatewayPaymentGateway
from app.routers import billing

GATEWAY_URL = "http://8.8.8.8/gw"   # 公网 IP 直写：出网校验放行且无需 DNS
KEY = "p2243-gateway-key"
ITEM = "P2243-FEE"
DAY = "2026-09-26"


class Resp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


def _query(monkeypatch, payload):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: Resp(payload))
    return HttpGatewayPaymentGateway(GATEWAY_URL, KEY).query_transactions(None, DAY)


@pytest.mark.parametrize("payload", [
    {"code": "SIGN_ERROR", "message": "签名错误"},            # 修前：当成当日零流水
    {"transactions": None},                                    # 修前：TypeError → 对账 500
    {"transactions": {"trade_no": "A", "amount_fen": 100}},   # 流水表不是表
    "OK",
], ids=["错误应答没有流水表", "流水表为null", "流水表不是列表", "应答是字符串"])
def test_应答里没有流水表_中止而不是当成零流水(monkeypatch, payload):
    with pytest.raises(RuntimeError, match="对账中止"):
        _query(monkeypatch, payload)


@pytest.mark.parametrize("row", [
    {"trade_no": "A"},                          # 修前：按 0 元比
    {"amount_fen": 100},                        # 修前：按空流水号比
    {"trade_no": "", "amount_fen": 100},
    {"trade_no": "A", "amount": None},          # 修前：TypeError → 对账 500
    {"trade_no": "A", "amount_fen": "一百"},    # 修前：ValueError → 对账 500
    {"trade_no": "A", "amount": "nan"},
    {"trade_no": "A", "amount": "inf"},
    "A,100",
], ids=["缺金额", "缺流水号", "流水号为空", "金额为null", "金额不是数", "金额nan", "金额inf", "行不是对象"])
def test_流水行缺流水号或金额_中止而不是按0元或空号比(monkeypatch, row):
    with pytest.raises(RuntimeError, match="对账中止"):
        _query(monkeypatch, {"transactions": [{"trade_no": "OK1", "amount_fen": 100}, row]})


def test_成形的应答照旧_空流水表是当日确实没有流水(monkeypatch):
    assert _query(monkeypatch, {"transactions": []}) == []
    assert _query(monkeypatch, {"transactions": [
        {"trade_no": "A", "amount_fen": 12345}, {"trade_no": "B", "amount": "6.7"}]}) == [
        {"trade_no": "A", "amount": 123.45}, {"trade_no": "B", "amount": 6.7}]
    # 顶层直接是流水表的旧写法照旧认
    assert _query(monkeypatch, [{"trade_no": 42, "amount_fen": 100}]) == [{"trade_no": "42", "amount": 1.0}]


@pytest.fixture()
def gateway(monkeypatch):
    monkeypatch.setattr(settings, "payment_gateway_url", GATEWAY_URL)
    monkeypatch.setattr(settings, "payment_gateway_key", KEY)
    assert billing.register_http_gateway() is True
    monkeypatch.setattr(httpx, "post", lambda *a, **k: Resp({"accepted": True, "trade_no": "P2243-GW"}))
    yield
    billing._GATEWAYS.pop("gateway", None)


def _signed(client, payload):
    body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))
    return client.post("/api/billing/payments/callback", content=body, headers={
        "Content-Type": "application/json", "X-Timestamp": ts, "X-Signature": gateway_sign(KEY, ts, body)})


def test_网关回错误应答_对账502且当日那张对平的对账单原样保留(client, admin, gateway, monkeypatch):
    from app.database import SessionLocal
    from app.models import PaymentOrder

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2243 医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    client.post("/api/dictionaries/charge/entries", headers=admin, json={"code": ITEM, "name": "诊查费(P2243)"})
    client.post("/api/billing/charge-items", headers=admin,
                json={"code": ITEM, "name": "诊查费(P2243)", "category": "treatment", "price": 100})
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2243 患者", "id_card": "330106198303032243"}).json()["id"]
    encounter = client.post("/api/encounters", headers=admin, json={
        "patient_id": patient, "org_id": org, "diagnosis_name": "复诊"}).json()["id"]
    client.post("/api/billing/details", headers=admin, json={
        "patient_id": patient, "encounter_id": encounter, "item_code": ITEM})
    settled = client.post("/api/billing/settlements", headers=admin, json={
        "bill_type": "outpatient", "encounter_id": encounter, "insurance_pay": 0}).json()
    order = client.post("/api/billing/payments", headers=admin, json={
        "settlement_id": settled["id"], "channel": "gateway"}).json()
    assert _signed(client, {"order_id": order["id"], "status": "paid", "trade_no": "P2243-GW",
                            "amount_fen": 10000}).status_code == 200
    with SessionLocal() as db:
        day = db.get(PaymentOrder, order["id"]).paid_at.strftime("%Y-%m-%d")
        mirrored = [{"trade_no": o.trade_no, "amount_fen": round((o.amount - o.refunded_amount) * 100)}
                    for o in billing._orders_of_day(db, day) if o.channel == "gateway"]

    monkeypatch.setattr(httpx, "get", lambda *a, **k: Resp({"transactions": mirrored}))
    good = client.post(f"/api/billing/reconciliation/run?date={day}", headers=admin)
    assert good.status_code == 201, good.text
    assert good.json()["diffs"] == []

    for payload in ({"code": "SIGN_ERROR", "message": "签名错误"},
                    {"transactions": [{"trade_no": "P2243-GW"}]}):
        monkeypatch.setattr(httpx, "get", lambda *a, _p=payload, **k: Resp(_p))
        resp = client.post(f"/api/billing/reconciliation/run?date={day}", headers=admin)
        assert resp.status_code == 502, resp.text   # 修前 201：顶掉好的那张，换成全是差异的
        assert "对账中止" in resp.json()["detail"]
        kept = client.get(f"/api/billing/reconciliation?date={day}", headers=admin).json()
        assert [(b["id"], b["diffs"]) for b in kept] == [(good.json()["id"], [])]
