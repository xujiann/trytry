"""支付「失败」回调无条件写 failed：与「成功」回调同时到，成功那一路入账提交之后照样把单子改回失败（P2-242）。

回调入账（paid）早就圈进结算单的临界区、锁里重读再翻；失败回调那一支还是「锁外读到 pending → 内存里改 failed → 提交」，
flush 出来的 UPDATE 只有 `WHERE id = ?`。网关对同一笔先推「失败」、紧接着又推「成功」（重试成功），两路同时到：成功那一路
入账提交，失败那一路随后把单子改回 failed——两路都 200，钱收了，单子却是失败、额度释放，收银还能再收一遍；同样两个回调
先后到达，第二路是 409。修法：失败也只从 pending 翻（条件 UPDATE），翻不到按真实状态回（与入账那一路共用判定）。

这里用「失败那一路读完状态、写库之前，成功那一路先提交」把并发窗口钉成确定的时序（SQLite 与 PG 同样成立）。
"""
import json
import time

import httpx
import pytest

from app.config import settings
from app.egress import gateway_sign
from app.routers import billing

ITEM = "P2242-FEE"
KEY = "p2242-gateway-key"


@pytest.fixture()
def gateway(monkeypatch):
    monkeypatch.setattr(settings, "payment_gateway_url", "http://8.8.8.8/gw")
    monkeypatch.setattr(settings, "payment_gateway_key", KEY)
    assert billing.register_http_gateway() is True
    monkeypatch.setattr(httpx, "post", lambda *a, **k: type("R", (), {
        "status_code": 200, "text": "", "json": lambda self: {"accepted": True, "trade_no": "P2242-GW"}})())
    yield
    billing._GATEWAYS.pop("gateway", None)


def _signed(client, payload):
    body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))
    return client.post("/api/billing/payments/callback", content=body, headers={
        "Content-Type": "application/json", "X-Timestamp": ts, "X-Signature": gateway_sign(KEY, ts, body)})


def test_失败回调读完状态后成功回调先入账_不把已入账的单改回失败(client, admin, gateway, monkeypatch):
    from app.database import SessionLocal
    from app.models import PaymentOrder
    from app.models._base import utcnow as real_utcnow

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2242 医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    client.post("/api/dictionaries/charge/entries", headers=admin, json={"code": ITEM, "name": "诊查费(P2242)"})
    client.post("/api/billing/charge-items", headers=admin,
                json={"code": ITEM, "name": "诊查费(P2242)", "category": "treatment", "price": 100})
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2242 患者", "id_card": "330106198202022242"}).json()["id"]
    encounter = client.post("/api/encounters", headers=admin, json={
        "patient_id": patient, "org_id": org, "diagnosis_name": "复诊"}).json()["id"]
    client.post("/api/billing/details", headers=admin, json={
        "patient_id": patient, "encounter_id": encounter, "item_code": ITEM})
    settled = client.post("/api/billing/settlements", headers=admin, json={
        "bill_type": "outpatient", "encounter_id": encounter, "insurance_pay": 0}).json()
    order = client.post("/api/billing/payments", headers=admin, json={
        "settlement_id": settled["id"], "channel": "gateway"}).json()
    assert order["status"] == "pending"

    fired = []

    def paid_meanwhile():
        if not fired:   # 失败那一路读完 pending、写库之前：成功那一路先入账提交
            fired.append(True)
            with SessionLocal() as other:
                row = other.get(PaymentOrder, order["id"])
                row.status, row.trade_no, row.paid_at = "paid", "P2242-GW", real_utcnow()
                other.commit()
        return real_utcnow()

    monkeypatch.setattr(billing, "utcnow", paid_meanwhile)
    resp = _signed(client, {"order_id": order["id"], "status": "failed", "trade_no": "P2242-GW",
                            "amount_fen": 10000, "message": "渠道超时"})
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200 failed：已入账的单被改回失败
    with SessionLocal() as db:
        assert db.get(PaymentOrder, order["id"]).status == "paid"
