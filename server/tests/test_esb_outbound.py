"""工程包 I1：ESB 出站投递闭环——签名投递、失败重试/死信、仅登记语义、worker 批量消费。"""
import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app
from app.routers import esb as esb_module

HL7_MESSAGE = (
    "MSH|^~\\&|MEDPLAT|COUNTY|HIS|COUNTY|20260821090000||ADT^A01|OUT0001|P|2.4\r"
    "PID|1||330281199203046014^^^CN^ID||王出站||19920304|M|||杭州市||13800001111"
)


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


def register_endpoint(client, admin, code, **kwargs):
    body = {
        "code": code,
        "name": kwargs.pop("name", f"接入方{code}"),
        "system_type": kwargs.pop("system_type", "provincial"),
        **kwargs,
    }
    resp = client.post("/api/esb/endpoints", json=body, headers=admin)
    assert resp.status_code == 201, resp.text
    return resp.json()


def enqueue(client, endpoint, msg_type, payload, max_retries=3):
    resp = client.post(
        "/api/esb/messages",
        json={"msg_type": msg_type, "payload": payload, "max_retries": max_retries},
        headers={"X-Esb-Endpoint": endpoint["code"], "X-Esb-Token": endpoint["auth_token"]},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


class FakeHttpx:
    """替身 httpx：记录每次投递的 url/报文体/头，按预设状态码应答。"""

    class HTTPError(Exception):
        pass

    def __init__(self, status_code=200, raise_error=False):
        self.status_code = status_code
        self.raise_error = raise_error
        self.calls: list[dict] = []

    def post(self, url, content=b"", headers=None, timeout=None):
        self.calls.append(
            {"url": url, "content": content, "headers": dict(headers or {}), "timeout": timeout}
        )
        if self.raise_error:
            raise FakeHttpx.HTTPError("connection refused")
        return SimpleNamespace(status_code=self.status_code)


@pytest.fixture()
def fake_httpx(monkeypatch):
    fake = FakeHttpx()
    monkeypatch.setattr(esb_module, "httpx", fake)
    return fake


# ---------------------------------------------------------------------------
# 投递与签名
# ---------------------------------------------------------------------------


def test_outbound_delivery_posts_signed_body(client, admin, fake_httpx):
    ep = register_endpoint(
        client, admin, "PROV_SIGN", direction="outbound",
        endpoint_url="https://province.example/ingest", secret="sign-key-01",
    )
    # 端点列表回显投递地址、但不回显签名密钥
    listed = next(e for e in client.get("/api/esb/endpoints", headers=admin).json() if e["code"] == "PROV_SIGN")
    assert listed["endpoint_url"] == "https://province.example/ingest"
    assert "secret" not in listed

    msg = enqueue(client, ep, "settlement_sync", {"settlement_id": 7, "amount": 120.5})
    resp = client.post(f"/api/esb/messages/{msg['id']}/process", headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "succeeded"
    assert "delivered" in body["detail"] and "province.example" in body["detail"]

    assert len(fake_httpx.calls) == 1
    call = fake_httpx.calls[0]
    assert call["url"] == "https://province.example/ingest"
    # 报文体 = 原始 payload 的 JSON 字节（非转换类型透传）
    assert json.loads(call["content"].decode("utf-8")) == {"settlement_id": 7, "amount": 120.5}
    assert call["headers"]["X-Esb-Msg-Type"] == "settlement_sync"
    # 非空洞断言①：签名头必须是对报文体字节的 HMAC-SHA256（去掉签名逻辑即红）
    expected = hmac.new(b"sign-key-01", call["content"], hashlib.sha256).hexdigest()
    assert call["headers"]["X-Esb-Signature"] == expected

    # 出站消费落交换日志（direction=outbound）
    logs = client.get(
        "/api/integration/exchange-logs?message_type=esb_settlement_sync", headers=admin
    ).json()
    assert logs["logs"] and logs["logs"][0]["success"] is True
    assert logs["logs"][0]["direction"] == "outbound"


def test_outbound_transform_message_delivers_parsed_body(client, admin, fake_httpx):
    ep = register_endpoint(
        client, admin, "PROV_HL7", direction="outbound",
        endpoint_url="https://province.example/hl7", secret="k2",
    )
    msg = enqueue(client, ep, "hl7v2_patient", {"message": HL7_MESSAGE})
    resp = client.post(f"/api/esb/messages/{msg['id']}/process", headers=admin)
    assert resp.json()["status"] == "succeeded"
    delivered = json.loads(fake_httpx.calls[0]["content"].decode("utf-8"))
    # 投的是转换后的标准化报文，不是 HL7 原文
    assert delivered["name"] == "王出站"
    assert delivered["id_card"] == "330281199203046014"


def test_outbound_without_secret_sends_no_signature(client, admin, fake_httpx):
    ep = register_endpoint(
        client, admin, "PROV_NOSIG", direction="outbound",
        endpoint_url="https://province.example/nosig",
    )
    msg = enqueue(client, ep, "generic", {"a": 1})
    assert client.post(f"/api/esb/messages/{msg['id']}/process", headers=admin).json()["status"] == "succeeded"
    assert "X-Esb-Signature" not in fake_httpx.calls[0]["headers"]


def test_outbound_failure_goes_retry_then_dead(client, admin, fake_httpx):
    fake_httpx.status_code = 502
    ep = register_endpoint(
        client, admin, "PROV_BAD", direction="outbound",
        endpoint_url="https://province.example/bad", secret="k3",
    )
    msg = enqueue(client, ep, "generic", {"x": 1}, max_retries=2)
    first = client.post(f"/api/esb/messages/{msg['id']}/process", headers=admin).json()
    assert first["status"] == "failed" and first["retry_count"] == 1
    assert "HTTP 502" in first["last_error"] and first["next_retry_at"]
    second = client.post(f"/api/esb/messages/{msg['id']}/process", headers=admin).json()
    assert second["status"] == "dead" and second["retry_count"] == 2
    # 死信不可再消费（复用既有重试/死信机制，不另起一套）
    assert client.post(f"/api/esb/messages/{msg['id']}/process", headers=admin).status_code == 409
    logs = client.get(
        "/api/integration/exchange-logs?message_type=esb_generic&success=false", headers=admin
    ).json()
    assert any("502" in log["error_detail"] for log in logs["logs"])


def test_outbound_network_error_counts_as_failure(client, admin, fake_httpx):
    fake_httpx.raise_error = True
    ep = register_endpoint(
        client, admin, "PROV_NET", direction="outbound",
        endpoint_url="https://province.example/net",
    )
    msg = enqueue(client, ep, "generic", {"x": 1}, max_retries=3)
    first = client.post(f"/api/esb/messages/{msg['id']}/process", headers=admin).json()
    assert first["status"] == "failed" and "网络异常" in first["last_error"]


def test_outbound_without_url_stays_registered_only(client, admin, fake_httpx):
    ep = register_endpoint(client, admin, "PROV_REG", direction="outbound")
    msg = enqueue(client, ep, "generic", {"a": 1})
    body = client.post(f"/api/esb/messages/{msg['id']}/process", headers=admin).json()
    assert body["status"] == "succeeded"
    # 响应明确说明"仅登记不投递"，且确实没有发起任何 HTTP 调用
    assert "仅登记" in body["detail"] and "endpoint_url" in body["detail"]
    assert fake_httpx.calls == []


def test_flow_route_step_delivers_to_configured_target(client, admin, fake_httpx):
    src = register_endpoint(client, admin, "HIS_SRC", system_type="his")
    register_endpoint(
        client, admin, "PROV_ROUTE", direction="outbound",
        endpoint_url="https://province.example/route", secret="rk",
    )
    client.post(
        "/api/esb/flows",
        json={
            "code": "UP_FLOW",
            "name": "上报编排",
            "steps": [{"type": "route", "config": {"target_endpoint": "PROV_ROUTE"}}],
        },
        headers=admin,
    )
    msg = enqueue(client, src, "report", {"k": "v"})
    run = client.post(f"/api/esb/flows/UP_FLOW/run?message_id={msg['id']}", headers=admin).json()
    assert run["status"] == "succeeded"
    assert "已投递" in run["step_results"][0]["detail"]
    assert json.loads(fake_httpx.calls[0]["content"].decode("utf-8")) == {"k": "v"}
    # 路由投递沿用消息自身的类型作为报文类型头
    assert fake_httpx.calls[0]["headers"]["X-Esb-Msg-Type"] == "report"


def test_flow_route_step_without_url_stays_registered_only(client, admin, fake_httpx):
    src = register_endpoint(client, admin, "HIS_SRC2", system_type="his")
    register_endpoint(client, admin, "PROV_ROUTE2", direction="outbound")
    client.post(
        "/api/esb/flows",
        json={
            "code": "UP_FLOW2",
            "name": "上报编排（未配地址）",
            "steps": [{"type": "route", "config": {"target_endpoint": "PROV_ROUTE2"}}],
        },
        headers=admin,
    )
    msg = enqueue(client, src, "report", {"k": "v"})
    run = client.post(f"/api/esb/flows/UP_FLOW2/run?message_id={msg['id']}", headers=admin).json()
    assert run["status"] == "succeeded"
    assert "仅登记" in run["step_results"][0]["detail"]
    assert fake_httpx.calls == []


# ---------------------------------------------------------------------------
# esb_outbound_worker：周期批量消费
# ---------------------------------------------------------------------------


def test_worker_consumes_outbound_batch(client, admin, fake_httpx):
    from app.database import SessionLocal
    from app.jobs import esb_outbound_worker
    from app.routers.esb import consume_pending_outbound

    out_ep = register_endpoint(
        client, admin, "PROV_WORKER", direction="outbound",
        endpoint_url="https://province.example/worker", secret="wk",
    )
    in_ep = register_endpoint(client, admin, "HIS_WORKER", system_type="his")
    ids = [enqueue(client, out_ep, "generic", {"i": i})["id"] for i in range(3)]
    inbound_msg = enqueue(client, in_ep, "generic", {"i": 99})

    # 每批上限生效：batch_size=2 时一轮只消费 2 条
    with SessionLocal() as db:
        count, summary = consume_pending_outbound(db, batch_size=2)
    assert count == 2 and "成功 2" in summary

    # worker 任务把余下 1 条消完；入站端点的消息不归它管
    with SessionLocal() as db:
        affected, summary = esb_outbound_worker(db)
    assert affected == 1 and "成功 1" in summary
    assert len(fake_httpx.calls) == 3

    statuses = {
        m["id"]: m["status"]
        for m in client.get(f"/api/esb/messages?endpoint_id={out_ep['id']}", headers=admin).json()
    }
    assert all(statuses[i] == "succeeded" for i in ids)
    inbound_status = client.get(
        f"/api/esb/messages?endpoint_id={in_ep['id']}", headers=admin
    ).json()[0]["status"]
    assert inbound_status == "queued"
    assert inbound_msg["id"] not in {c.get("id") for c in fake_httpx.calls}

    # 再跑一轮：无待投消息，worker 空转
    with SessionLocal() as db:
        affected, _ = esb_outbound_worker(db)
    assert affected == 0


def test_worker_registered_in_scheduler():
    from app.scheduler import REGISTRY

    assert "esb_outbound_worker" in REGISTRY
    assert "fhir_batch_export" in REGISTRY
