"""工程包 I2：短信网关签名头（同支付口径）与出网校验。

HttpGatewaySmsProvider 配置了 api_key 时须带 X-Timestamp / X-Signature
（HMAC-SHA256 对"时间戳.请求体原始字节"签名）；网关 URL 指向内网/环回时
_build_provider 拒绝启用通道（url 置空 → send 一律失败并 log，绝不假装发出）。
"""
import json

import httpx

from app.config import settings
from app.egress import gateway_sign
from app.sms import ConsoleSmsProvider, HttpGatewaySmsProvider, _build_provider


class DummyResp:
    status_code = 200
    text = ""


def test_带key时请求带HMAC签名头(monkeypatch):
    captured = {}

    def fake_post(url, content=b"", headers=None, timeout=None, **kwargs):
        captured.update({"url": url, "content": content, "headers": headers})
        return DummyResp()

    monkeypatch.setattr(httpx, "post", fake_post)
    provider = HttpGatewaySmsProvider("http://8.8.8.8/sms", "sms-key-1", "县域医共体")
    assert provider.send("13800000000", "验证码 123456") is True
    body = captured["content"]
    assert json.loads(body) == {"phone": "13800000000", "content": "验证码 123456", "sign": "县域医共体"}
    headers = captured["headers"]
    assert headers["Authorization"] == "Bearer sms-key-1"
    # 签名可用同一口径验回来（去掉签名头这里必红）
    assert gateway_sign("sms-key-1", headers["X-Timestamp"], body) == headers["X-Signature"]


def test_无key时维持裸请求兼容旧网关(monkeypatch):
    captured = {}

    def fake_post(url, content=b"", headers=None, timeout=None, **kwargs):
        captured.update({"headers": headers})
        return DummyResp()

    monkeypatch.setattr(httpx, "post", fake_post)
    provider = HttpGatewaySmsProvider("http://8.8.8.8/sms", "", "签名")
    assert provider.send("13800000000", "hi") is True
    assert "X-Signature" not in captured["headers"] and "Authorization" not in captured["headers"]


def test_内网短信网关拒绝启用(monkeypatch, caplog):
    monkeypatch.setattr(settings, "sms_provider", "http")
    monkeypatch.setattr(settings, "sms_gateway_url", "http://127.0.0.1:9/sms")
    with caplog.at_level("ERROR"):
        provider = _build_provider()
    assert isinstance(provider, HttpGatewaySmsProvider) and provider.url == ""
    assert "MEDPLAT_SMS_GATEWAY_URL" in caplog.text
    # 通道被拒后发送必须失败（绝不能回退成 console 的"成功"）
    assert provider.send("13800000000", "hi") is False


def test_公网短信网关正常启用(monkeypatch):
    monkeypatch.setattr(settings, "sms_provider", "http")
    monkeypatch.setattr(settings, "sms_gateway_url", "https://8.8.8.8/sms")
    provider = _build_provider()
    assert isinstance(provider, HttpGatewaySmsProvider) and provider.url == "https://8.8.8.8/sms"
    monkeypatch.setattr(settings, "sms_provider", "console")
    assert isinstance(_build_provider(), ConsoleSmsProvider)
