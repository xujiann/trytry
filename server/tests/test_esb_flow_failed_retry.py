"""按编排执行失败的消息只能按那条编排重试，不走默认消费（P2-820，第二十二批「失败路径的半截状态」扫描 X1-1）。

`run_flow` 失败记的是「第 N 步失败、待重试」，要重做的是那条编排；可之后的每一次重试都不走编排：页面对失败行只给
「消费/重试」（`/process`，默认消费不看编排）——入站透传消息不投任何地方就记「成功」，编排里后面那步路由的目标再也
收不到，页面上显示成功；出站消息到点由定时出站（`consume_pending_outbound`）把原始报文投出去，编排里的校验被绕过，
也记成功。修后以这条消息最近一次编排执行为准：失败的，手工消费 409、定时出站不挑它；清单行带 `retry_flow`，页面给
「按编排重试」。重跑从第几步起（已投成功的目标会不会再收一份）属 X1-2，另行待裁定。
"""
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.database import SessionLocal
from app.models import EsbMessage, utcnow
from app.routers import esb as esb_module

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-public.js").read_text(encoding="utf-8")


class _FakeHttpx:
    class HTTPError(Exception):
        pass

    def __init__(self):
        self.status_code = 200
        self.calls: list[str] = []

    def post(self, url, content=b"", headers=None, timeout=None):
        self.calls.append(url)
        return SimpleNamespace(status_code=self.status_code)


@pytest.fixture
def fake_httpx(monkeypatch):
    fake = _FakeHttpx()
    monkeypatch.setattr(esb_module, "httpx", fake)
    return fake


def _endpoint(client, admin, code, **extra):
    resp = client.post("/api/esb/endpoints", headers=admin, json={
        "code": code, "name": code, "system_type": "his", "rate_limit_per_min": 1000, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _enqueue(client, endpoint, payload):
    resp = client.post("/api/esb/messages", json={"msg_type": "generic", "payload": payload}, headers={
        "X-Esb-Endpoint": endpoint["code"], "X-Esb-Token": endpoint["auth_token"]})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _flow(client, admin, code, steps):
    resp = client.post("/api/esb/flows", headers=admin, json={"code": code, "name": code, "steps": steps})
    assert resp.status_code == 201, resp.text


def _row(client, admin, endpoint, message_id):
    rows = client.get("/api/esb/messages", headers=admin, params={"endpoint_id": endpoint["id"], "limit": 50}).json()
    return next(r for r in rows if r["id"] == message_id)


def test_入站透传消息编排路由失败后_手工消费409_只能按编排重试(client, admin, fake_httpx):
    inbound = _endpoint(client, admin, "P2820_IN")
    _endpoint(client, admin, "P2820_B", direction="outbound", endpoint_url="https://b.example/ingest")
    _flow(client, admin, "P2820_ROUTE", [{"type": "route", "config": {"target_endpoint": "P2820_B"}}])
    message_id = _enqueue(client, inbound, {"a": 1, "b": 2})

    fake_httpx.status_code = 503
    run = client.post(f"/api/esb/flows/P2820_ROUTE/run?message_id={message_id}", headers=admin)
    assert run.status_code == 200 and run.json()["status"] == "failed", run.text
    row = _row(client, admin, inbound, message_id)
    assert (row["status"], row["retry_flow"]) == ("failed", "P2820_ROUTE")   # 修前没有这个键

    fake_httpx.status_code = 200   # B 恢复了
    resp = client.post(f"/api/esb/messages/{message_id}/process", headers=admin)
    assert resp.status_code == 409 and "P2820_ROUTE" in resp.json()["detail"], resp.text   # 修前 200「透传消息已处理」
    assert _row(client, admin, inbound, message_id)["status"] == "failed"
    assert fake_httpx.calls == ["https://b.example/ingest"]   # 修前 B 永远只收到故障时那一次

    rerun = client.post(f"/api/esb/flows/P2820_ROUTE/run?message_id={message_id}", headers=admin)
    assert rerun.status_code == 200 and rerun.json()["message_status"] == "succeeded", rerun.text
    assert fake_httpx.calls == ["https://b.example/ingest"] * 2
    row = _row(client, admin, inbound, message_id)
    assert (row["status"], row["retry_flow"]) == ("succeeded", "")


def test_出站消息编排校验失败后_定时出站不把原始报文投出去(client, admin, fake_httpx):
    out = _endpoint(client, admin, "P2820_OUT", direction="outbound", endpoint_url="https://out.example/ingest")
    _flow(client, admin, "P2820_CHECK", [{"type": "validate", "config": {"required": ["id_card"]}}])
    checked = _enqueue(client, out, {"name": "张三", "diagnosis": "肺结核"})
    plain = _enqueue(client, out, {"name": "李四", "id_card": "110101196501012820"})   # 对照：没走过编排的
    run = client.post(f"/api/esb/flows/P2820_CHECK/run?message_id={checked}", headers=admin)
    assert run.status_code == 200 and run.json()["status"] == "failed", run.text
    with SessionLocal() as db:   # 到了下次重试时间
        db.get(EsbMessage, checked).next_retry_at = utcnow() - timedelta(minutes=1)
        db.commit()
        esb_module.consume_pending_outbound(db)
    rows = {m: _row(client, admin, out, m) for m in (checked, plain)}
    assert rows[checked]["status"] == "failed" and rows[checked]["retry_flow"] == "P2820_CHECK"   # 修前 succeeded
    assert rows[plain]["status"] == "succeeded" and rows[plain]["retry_flow"] == ""
    assert fake_httpx.calls.count("https://out.example/ingest") == 1   # 只有对照那一条；修前缺字段的原文也投了


def test_没走过编排的消息手工消费照旧(client, admin, fake_httpx):
    inbound = _endpoint(client, admin, "P2820_PLAIN")
    message_id = _enqueue(client, inbound, {"a": 1})
    resp = client.post(f"/api/esb/messages/{message_id}/process", headers=admin)
    assert resp.status_code == 200 and resp.json()["status"] == "succeeded", resp.text
    assert "retry_flow" not in resp.json()   # 清单行才带，消费回执一字不动


def test_页面对编排失败的行给按编排重试():
    start = PAGE.index("async function renderEsb()")
    body = PAGE[start:PAGE.index("\nasync function ", start + 10)]
    assert "const retryOp = (m) => !m.retry_flow ?" in body
    assert 'data-esbrerun="${m.id}" data-flow="${esc(m.retry_flow)}"' in body
    assert "/api/esb/flows/${encodeURIComponent(flow)}/run?message_id=${encodeURIComponent(esbrerun)}" in body
    assert "<td>${retryable ? retryOp(m)" in body   # 修前一律「消费/重试」
