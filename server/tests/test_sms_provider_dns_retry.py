"""短信通道把第一次建通道那一刻的 DNS 解析失败缓存到进程结束（P2-1221，第三十五批扫描 T1-4）。

`sms.get_sms_provider()` 是进程内单例：首次调用时 `_build_provider()` 过出网校验（`egress.egress_url_problem` 现解析
网关主机名，「解析失败视为不可用」），不过就把网关地址置空——而这个降级的通道照样存进模块级 `_provider`，此后再不重建。
某个 worker 第一次发短信那一刻 DNS 抖一下，这个 worker 此后的验证码登录、补绑、宣教短信一律 502「短信通道暂不可用」，
DNS 恢复了也不重查，直到重启；多 worker 时表现为间歇失败。

修法：出网校验本身不放宽——解析到内网 / 环回 / 保留段的照旧永久拒绝、降级的通道照旧缓存；只把「这次没解析出来」分出来：
这一次照旧按不可用处理（502 与日志不变），但不缓存降级的通道，下次调用重建、重新校验。校验通过的照旧缓存。

外网一律打桩：`socket.getaddrinfo` 对网关主机名按用例编排的顺序作答，`httpx.post` 只记下投递、不出网。
"""
import json
import socket
from types import SimpleNamespace

import httpx
import pytest

from app.config import settings
from app.routers.portal import _reset_portal_failures
from app.sms import HttpGatewaySmsProvider, get_sms_provider, set_sms_provider

GATEWAY_HOST = "sms-gw.example-county.cn"
GATEWAY_URL = f"https://{GATEWAY_HOST}/send"
PUBLIC_IP = "8.8.8.8"        # 公网地址：出网校验放行（外呼打了桩，不会真发）
PRIVATE_IP = "10.20.30.40"   # 县内专网地址：出网校验永久拒绝
DNS_BLIP = socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")


@pytest.fixture
def gateway(monkeypatch):
    """短信走 http 通道；网关主机名第 n 次解析拿 `answers[n]`（用尽后重复最后一个），异常即解析失败。"""
    _reset_portal_failures()
    set_sms_provider(None)
    monkeypatch.setattr(settings, "sms_provider", "http")
    monkeypatch.setattr(settings, "sms_gateway_url", GATEWAY_URL)
    gw = SimpleNamespace(answers=[], lookups=0, posts=[])
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host != GATEWAY_HOST:
            return real_getaddrinfo(host, port, *args, **kwargs)
        answer = gw.answers[min(gw.lookups, len(gw.answers) - 1)]
        gw.lookups += 1
        if isinstance(answer, BaseException):
            raise answer
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answer, port))]

    def fake_post(url, *args, content=b"", **kwargs):
        gw.posts.append((url, json.loads(content)["phone"]))
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(httpx, "post", fake_post)
    yield gw
    set_sms_provider(None)
    _reset_portal_failures()


def _send_code(client, phone: str):
    return client.post("/api/portal/auth/sms/code", json={"phone": phone})


def test_首次建通道时DNS抖动_这次502_下次重建重校验即发得出(client, gateway):
    gateway.answers[:] = [DNS_BLIP, PUBLIC_IP]
    first = _send_code(client, "13912211221")
    assert first.status_code == 502, first.text   # 这一次照旧按不可用处理
    assert first.json()["detail"] == "短信通道暂不可用，请稍后重试"
    assert gateway.lookups == 1 and gateway.posts == []

    second = _send_code(client, "13912221221")
    assert second.status_code == 200, second.text   # 修前 502：降级的通道被缓存，DNS 恢复了也不重查
    assert second.json()["sent"] is True
    assert gateway.lookups == 2                       # 重建时重新过了一遍出网校验
    assert gateway.posts == [(GATEWAY_URL, "13912221221")]


def test_解析到内网的网关两次都拒绝_不外发也不重查(client, gateway):
    # 第二次若重查会拿到公网地址：永久拒绝就不该再查、更不该因此放行
    gateway.answers[:] = [PRIVATE_IP, PUBLIC_IP]
    for phone in ("13912231221", "13912241221"):
        resp = _send_code(client, phone)
        assert resp.status_code == 502, resp.text
    assert gateway.posts == []
    assert gateway.lookups == 1   # 配置本身的问题，重试也一样：被拒的降级通道照旧缓存
    provider = get_sms_provider()
    assert isinstance(provider, HttpGatewaySmsProvider) and provider.url == ""


def test_校验通过的通道照旧缓存_后续发送不再重复解析(client, gateway):
    gateway.answers[:] = [PUBLIC_IP]
    phones = ["13912251221", "13912261221", "13912271221"]
    for phone in phones:
        resp = _send_code(client, phone)
        assert resp.status_code == 200, resp.text
    assert gateway.lookups == 1   # 只在首次建通道时解析过一次
    assert gateway.posts == [(GATEWAY_URL, phone) for phone in phones]
    assert get_sms_provider() is get_sms_provider()
