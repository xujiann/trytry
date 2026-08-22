"""工程包 I2：支付真通道（HTTP 网关 + 异步回调状态机 + 网关对账）。

网关一律打桩（monkeypatch httpx），不出网。重点盯三条安全性质，
且刻意写成"拿掉防线必红"：

- 验签：错签/改体/时间窗外 → 401，绝不入账（去掉验签，这些用例会看到 200）；
- 幂等：同一笔的重复回调不再产生任何写入（去掉幂等分支，重复回调会撞 409
  或重写 paid_at，两条断言都会红）；
- 金额核对：回调金额与本地单不一致 → 422 拒绝入账。

Mock 通道"下单即 paid"的同步语义由 test_payment_reconciliation.py 原样回归。
"""
import json
import time
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.config import settings
from app.egress import gateway_sign
from app.main import app
from app.payments import HttpGatewayPaymentGateway
from app.routers import billing

TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")
GATEWAY_URL = "http://8.8.8.8/gw"  # 公网 IP 直写：egress 校验放行且无需 DNS
GATEWAY_KEY = "test-gateway-key-1"


class DummyResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def gateway_channel(monkeypatch):
    """每个用例注册好 http 网关与验签密钥，用完摘除，不污染其他测试模块。"""
    monkeypatch.setattr(settings, "payment_gateway_url", GATEWAY_URL)
    monkeypatch.setattr(settings, "payment_gateway_key", GATEWAY_KEY)
    assert billing.register_http_gateway() is True
    yield
    billing._GATEWAYS.pop("gateway", None)


def login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def base(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "网关支付医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    for username, role in [("gw_doc", "doctor"), ("gw_op", "operator")]:
        client.post(
            "/api/users",
            json={"username": username, "password": "pass123456", "role": role, "org_id": org["id"]},
            headers=admin,
        )
    client.post(
        "/api/billing/charge-items",
        json={"code": "REG-GW", "name": "网关诊查费", "category": "treatment", "price": 100},
        headers=admin,
    )
    return {
        "org": org,
        "doctor": login(client, "gw_doc", "pass123456"),
        "operator": login(client, "gw_op", "pass123456"),
    }


_counter = {"n": 0}


def new_settlement(client, base, admin, quantity=1):
    _counter["n"] += 1
    seq = _counter["n"]
    patient = client.post(
        "/api/patients",
        json={"name": f"网关患者{seq}", "id_card": f"33098119900202{seq:04d}"},
        headers=admin,
    ).json()
    encounter = client.post(
        "/api/encounters",
        json={"patient_id": patient["id"], "org_id": base["org"]["id"], "diagnosis_name": "复诊"},
        headers=base["doctor"],
    ).json()
    client.post(
        "/api/billing/details",
        json={
            "patient_id": patient["id"],
            "encounter_id": encounter["id"],
            "item_code": "REG-GW",
            "quantity": quantity,
        },
        headers=base["operator"],
    )
    resp = client.post(
        "/api/billing/settlements",
        json={"bill_type": "outpatient", "encounter_id": encounter["id"], "insurance_pay": 0},
        headers=base["operator"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def stub_pay(monkeypatch, trade_no="GWTRADE001", accepted=True):
    calls = []

    def fake_post(url, content=b"", headers=None, timeout=None, **kwargs):
        calls.append({"url": url, "content": content, "headers": headers or {}})
        if not accepted:
            return DummyResp({"accepted": False, "message": "渠道维护中"})
        return DummyResp(
            {"accepted": True, "trade_no": trade_no, "pay_url": "https://pay.example/h5", "qr_code": "weixin://wxpay/q"}
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    return calls


def create_gateway_order(client, base, admin, monkeypatch, trade_no="GWTRADE001", quantity=1):
    calls = stub_pay(monkeypatch, trade_no=trade_no)
    settlement = new_settlement(client, base, admin, quantity=quantity)
    resp = client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "gateway"},
        headers=base["operator"],
    )
    assert resp.status_code == 201, resp.text
    return resp.json(), calls


def signed_callback(client, payload, *, key=GATEWAY_KEY, ts=None, sig=None):
    body = json.dumps(payload).encode("utf-8")
    ts = str(ts if ts is not None else int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-Timestamp": ts,
        "X-Signature": sig if sig is not None else gateway_sign(key, ts, body),
    }
    return client.post("/api/billing/payments/callback", content=body, headers=headers)


def get_order(client, base, order_id):
    rows = client.get("/api/billing/payments", headers=base["operator"]).json()
    return next(o for o in rows if o["id"] == order_id)


# ---------------------------------------------------------------------------
# 下单（异步受理）
# ---------------------------------------------------------------------------


def test_网关下单受理后订单停在pending并返回支付参数(client, admin, base, monkeypatch):
    order, calls = create_gateway_order(client, base, admin, monkeypatch)
    assert order["status"] == "pending" and order["paid_at"] is None
    assert order["trade_no"] == "GWTRADE001"
    assert order["channel"] == "gateway" and order["channel_name"] == "网关支付"
    assert order["pay_url"] == "https://pay.example/h5" and order["qr_code"].startswith("weixin://")
    # 出网请求：金额为分整数、带回调地址、带签名头
    req = calls[0]
    assert req["url"] == f"{GATEWAY_URL}/pay"
    sent = json.loads(req["content"])
    assert sent["amount_fen"] == 10000 and sent["notify_url"] == "/api/billing/payments/callback"
    assert gateway_sign(GATEWAY_KEY, req["headers"]["X-Timestamp"], req["content"]) == req["headers"]["X-Signature"]


def test_网关未受理按失败落单(client, admin, base, monkeypatch):
    stub_pay(monkeypatch, accepted=False)
    settlement = new_settlement(client, base, admin)
    order = client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "gateway"},
        headers=base["operator"],
    ).json()
    assert order["status"] == "failed" and "维护" in order["fail_reason"]


def test_网关未注册时gateway渠道拒绝而非落回Mock(client, admin, base):
    billing._GATEWAYS.pop("gateway", None)
    settlement = new_settlement(client, base, admin)
    resp = client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "gateway"},
        headers=base["operator"],
    )
    assert resp.status_code == 503
    # 缺省渠道向后兼容：仍是 Mock 同步即 paid
    mock_order = client.post(
        "/api/billing/payments",
        json={"settlement_id": settlement["id"], "channel": "online"},
        headers=base["operator"],
    ).json()
    assert mock_order["status"] == "paid" and mock_order["trade_no"].startswith("MOCK")


def test_内网网关地址拒绝注册(monkeypatch):
    monkeypatch.setattr(settings, "payment_gateway_url", "http://169.254.169.254/latest")
    assert billing.register_http_gateway() is False
    assert "gateway" not in billing._GATEWAYS
    monkeypatch.setattr(settings, "payment_gateway_url", "")
    assert billing.register_http_gateway() is False  # 未配置=不注册，Mock 现状


# ---------------------------------------------------------------------------
# 回调状态机（验签 / 防重放 / 幂等）
# ---------------------------------------------------------------------------


def test_回调验签通过后pending转paid且幂等(client, admin, base, monkeypatch):
    order, _ = create_gateway_order(client, base, admin, monkeypatch, trade_no="GWOK01")
    payload = {"order_id": order["id"], "trade_no": "GWOK01", "status": "paid", "amount_fen": 10000}
    first = signed_callback(client, payload)
    assert first.status_code == 200, first.text
    assert first.json() == {"ok": True, "order_id": order["id"], "status": "paid", "idempotent": False}
    after = get_order(client, base, order["id"])
    assert after["status"] == "paid" and after["paid_at"] and after["callback_at"]

    # 幂等（去掉幂等分支必红）：重复回调不产生任何写入，paid_at 一个字节不变
    second = signed_callback(client, payload)
    assert second.status_code == 200 and second.json()["idempotent"] is True
    again = get_order(client, base, order["id"])
    assert again["paid_at"] == after["paid_at"] and again["callback_at"] == after["callback_at"]


def test_错签回调401且不入账(client, admin, base, monkeypatch):
    order, _ = create_gateway_order(client, base, admin, monkeypatch, trade_no="GWBAD01")
    payload = {"order_id": order["id"], "trade_no": "GWBAD01", "status": "paid", "amount_fen": 10000}
    # 去掉验签必红：以下三种都应 401，且订单必须还停在 pending
    assert signed_callback(client, payload, sig="0" * 64).status_code == 401
    assert signed_callback(client, payload, key="wrong-key").status_code == 401
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    tampered = client.post(
        "/api/billing/payments/callback",
        content=json.dumps({**payload, "amount_fen": 1}).encode(),
        headers={"Content-Type": "application/json", "X-Timestamp": ts,
                 "X-Signature": gateway_sign(GATEWAY_KEY, ts, body)},
    )
    assert tampered.status_code == 401  # 改体后原签名失配
    assert get_order(client, base, order["id"])["status"] == "pending"


def test_时间窗外的重放回调拒绝(client, admin, base, monkeypatch):
    order, _ = create_gateway_order(client, base, admin, monkeypatch, trade_no="GWOLD01")
    payload = {"order_id": order["id"], "trade_no": "GWOLD01", "status": "paid", "amount_fen": 10000}
    resp = signed_callback(client, payload, ts=int(time.time()) - 3600)
    assert resp.status_code == 401 and "重放" in resp.json()["detail"]
    assert get_order(client, base, order["id"])["status"] == "pending"


def test_金额不一致的回调拒绝入账(client, admin, base, monkeypatch):
    order, _ = create_gateway_order(client, base, admin, monkeypatch, trade_no="GWAMT01")
    payload = {"order_id": order["id"], "trade_no": "GWAMT01", "status": "paid", "amount_fen": 9999}
    resp = signed_callback(client, payload)  # 签名正确、金额与本地单不符
    assert resp.status_code == 422 and "金额" in resp.json()["detail"]
    assert get_order(client, base, order["id"])["status"] == "pending"


def test_失败回调置failed并记原因(client, admin, base, monkeypatch):
    order, _ = create_gateway_order(client, base, admin, monkeypatch, trade_no="GWFAIL1")
    payload = {
        "order_id": order["id"], "trade_no": "GWFAIL1", "status": "failed",
        "amount_fen": 10000, "message": "用户取消支付",
    }
    assert signed_callback(client, payload).status_code == 200
    after = get_order(client, base, order["id"])
    assert after["status"] == "failed" and after["fail_reason"] == "用户取消支付"
    # 终态后再收 paid 回调：409（异常单进对账差异，不自动翻状态）
    late = signed_callback(client, {**payload, "status": "paid"})
    assert late.status_code == 409


def test_回调报文边界(client, admin, base, monkeypatch):
    order, _ = create_gateway_order(client, base, admin, monkeypatch, trade_no="GWEDGE1")
    ok_payload = {"order_id": order["id"], "trade_no": "GWEDGE1", "status": "paid", "amount_fen": 10000}
    assert signed_callback(client, {**ok_payload, "order_id": 999999}).status_code == 404
    assert signed_callback(client, {**ok_payload, "status": "weird"}).status_code == 422
    assert signed_callback(client, {"status": "paid"}).status_code == 422  # 缺字段
    # 已入账后回调携带不同流水号：409
    assert signed_callback(client, ok_payload).status_code == 200
    mismatch = signed_callback(client, {**ok_payload, "trade_no": "OTHER"})
    assert mismatch.status_code == 409


def test_未配置密钥时回调整体不可用(client, monkeypatch):
    monkeypatch.setattr(settings, "payment_gateway_key", "")
    resp = signed_callback(client, {"order_id": 1, "status": "paid", "amount_fen": 1})
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# 退款（网关同步应答）与对账（网关流水拉取）
# ---------------------------------------------------------------------------


def _pay_via_callback(client, base, admin, monkeypatch, trade_no):
    order, _ = create_gateway_order(client, base, admin, monkeypatch, trade_no=trade_no)
    assert signed_callback(
        client,
        {"order_id": order["id"], "trade_no": trade_no, "status": "paid", "amount_fen": 10000},
    ).status_code == 200
    return get_order(client, base, order["id"])


def test_网关退款成功与失败(client, admin, base, monkeypatch):
    order = _pay_via_callback(client, base, admin, monkeypatch, "GWRF001")

    refund_calls = []

    def fake_refund(url, content=b"", headers=None, timeout=None, **kwargs):
        refund_calls.append(json.loads(content))
        return DummyResp({"success": True, "refund_no": "RFGW001"})

    monkeypatch.setattr(httpx, "post", fake_refund)
    resp = client.post(
        f"/api/billing/payments/{order['id']}/refund", json={"amount": 40}, headers=base["operator"]
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["refund_no"] == "RFGW001" and out["refunded_amount"] == 40.0 and out["status"] == "paid"
    assert refund_calls[0] == {"trade_no": "GWRF001", "amount_fen": 4000}

    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **k: DummyResp({"success": False, "message": "渠道余额不足"}),
    )
    fail = client.post(
        f"/api/billing/payments/{order['id']}/refund", json={}, headers=base["operator"]
    )
    assert fail.status_code == 502
    assert get_order(client, base, order["id"])["refunded_amount"] == 40.0  # 失败不改本地


def test_对账改从网关拉流水并检出差异(client, admin, base, monkeypatch):
    paid = _pay_via_callback(client, base, admin, monkeypatch, "GWRC001")
    # 通道侧：本单金额少 15 元，另有一笔本地无单的幽灵流水；
    # 其余当日本地单（本模块前面用例产生的）通道侧如实镜像。
    local = client.get("/api/billing/payments", headers=base["operator"]).json()
    transactions = []
    for o in local:
        if o["status"] not in ("paid", "refunded") or not o["trade_no"]:
            continue
        net_fen = int(round((o["amount"] - o["refunded_amount"]) * 100))
        if o["id"] == paid["id"]:
            net_fen -= 1500
        transactions.append({"trade_no": o["trade_no"], "amount_fen": net_fen})
    transactions.append({"trade_no": "GWGHOST01", "amount_fen": 4550})

    get_calls = []

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        get_calls.append({"url": url, "params": params, "headers": headers})
        return DummyResp({"transactions": transactions})

    monkeypatch.setattr(httpx, "get", fake_get)
    batch = client.post(
        f"/api/billing/reconciliation/run?date={TODAY}", headers=base["operator"]
    ).json()
    assert get_calls[0]["url"] == f"{GATEWAY_URL}/transactions"
    assert get_calls[0]["params"] == {"date": TODAY}
    by_type = {}
    for d in batch["diffs"]:
        by_type.setdefault(d["diff_type"], []).append(d)
    mismatch = next(d for d in by_type["amount_mismatch"] if d["order_id"] == paid["id"])
    assert mismatch["local_amount"] == 100.0 and mismatch["remote_amount"] == 85.0
    ghost = next(d for d in by_type["missing_local"] if d["trade_no"] == "GWGHOST01")
    assert ghost["remote_amount"] == 45.5


def test_网关流水拉取失败时对账中止(client, admin, base, monkeypatch):
    def boom(*args, **kwargs):
        raise httpx.ConnectError("gateway down")

    monkeypatch.setattr(httpx, "get", boom)
    before = client.get(f"/api/billing/reconciliation?date={TODAY}", headers=base["operator"]).json()
    resp = client.post(
        f"/api/billing/reconciliation/run?date={TODAY}", headers=base["operator"]
    )
    assert resp.status_code == 502
    after = client.get(f"/api/billing/reconciliation?date={TODAY}", headers=base["operator"]).json()
    assert [b["id"] for b in after] == [b["id"] for b in before]  # 旧批次原样保留


def test_网关流水单位兼容元与分():
    gw = HttpGatewayPaymentGateway(GATEWAY_URL, GATEWAY_KEY)

    class R:
        status_code = 200
        text = ""

        def json(self):
            return {"transactions": [{"trade_no": "A", "amount_fen": 12345}, {"trade_no": "B", "amount": 6.7}]}

    import unittest.mock as mock

    with mock.patch.object(httpx, "get", return_value=R()):
        rows = gw.query_transactions(None, TODAY)
    assert rows == [{"trade_no": "A", "amount": 123.45}, {"trade_no": "B", "amount": 6.7}]
