"""支付网关只在 import 时注册一次：那一刻 DNS 抖一下，这个 worker 此后的网关下单、退款、对账一律 503，直到重启
（P2-1221 跟进，第三十五批「缓存与失效」扫描 T1-4 的同形一处）。

`billing.register_http_gateway()` 过出网校验（`egress` 现解析网关主机名），解析失败就不注册——而它只在模块导入时调一次，
DNS 恢复了也不重查。下单、退款、对账三处经 `_needs_real_gateway` 一律 503「支付网关未配置或未通过出网校验」（P1-165 修的
是别落回 Mock），运维照着文案查配置，配置没有毛病；多 worker 时是间歇 503。短信通道同形，已由 P2-1221 修。

修法：注册没成只因为这一次没解析出来（`egress.UnresolvedHost`）的，记下「用到时再试」，用到 gateway 渠道时补注册一次
（锁里再判）；解析到内网 / 环回、协议不对的照旧永久拒绝、不重试。外网一律打桩：`socket.getaddrinfo` 对网关主机名按
用例编排的顺序作答。
"""
import socket
from types import SimpleNamespace

import pytest

from app.config import settings
from app.routers import billing

GATEWAY_HOST = "pay-gw.example-county.cn"
PUBLIC_IP = "8.8.8.8"
PRIVATE_IP = "10.20.30.40"
DNS_BLIP = socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")


@pytest.fixture
def dns(monkeypatch):
    """网关主机名第 n 次解析拿 `answers[n]`（用尽后重复最后一个），异常即解析失败。"""
    monkeypatch.setattr(settings, "payment_gateway_url", f"https://{GATEWAY_HOST}/gw")
    monkeypatch.setattr(settings, "payment_gateway_key", "test-gateway-key-dns")
    state = SimpleNamespace(answers=[], lookups=0)
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host != GATEWAY_HOST:
            return real_getaddrinfo(host, port, *args, **kwargs)
        answer = state.answers[min(state.lookups, len(state.answers) - 1)]
        state.lookups += 1
        if isinstance(answer, BaseException):
            raise answer
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answer, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    yield state
    monkeypatch.undo()
    billing.register_http_gateway()   # 按原配置复位注册表与重试标记，不污染别的用例


def test_导入那一刻解析失败_用到网关时补注册上(dns):
    dns.answers = [DNS_BLIP, PUBLIC_IP]
    assert billing.register_http_gateway() is False   # 「启动」那一刻 DNS 抖了一下
    assert "gateway" not in billing._GATEWAYS
    assert billing._needs_real_gateway("gateway") is False   # 修前 True：此后一律 503，直到重启
    assert "gateway" in billing._GATEWAYS
    assert dns.lookups == 2
    billing._needs_real_gateway("gateway")
    assert dns.lookups == 2   # 注册上之后不再重复解析


def test_解析持续失败时每次用到都再试_照旧不可用(dns):
    dns.answers = [DNS_BLIP]
    assert billing.register_http_gateway() is False
    assert billing._needs_real_gateway("gateway") is True   # 照旧 503，不落回 Mock（P1-165）
    assert billing._needs_real_gateway("gateway") is True
    assert dns.lookups == 3
    assert billing._gateway("gateway") is billing.MOCK_GATEWAY   # 取通道也只是再试一次，没注册上就照旧


def test_解析到内网的照旧永久拒绝_不重试(dns):
    dns.answers = [PRIVATE_IP, PUBLIC_IP]   # 第二次就算解析到公网，也不该再去解析
    assert billing.register_http_gateway() is False
    assert billing._needs_real_gateway("gateway") is True
    assert billing._needs_real_gateway("gateway") is True
    assert dns.lookups == 1
