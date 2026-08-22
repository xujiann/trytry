"""出网防线（app/egress.py）：网关 URL 的 SSRF 校验与统一签名口径。

覆盖三件事：协议白名单（仅 http/https）、地址段拦截（环回/内网/链路本地/
保留段一律拒绝，含"域名解析到内网"的 DNS rebinding 前半段）、签名与时间窗
（HMAC-SHA256 + ±300s 防重放）。这些函数被支付网关注册与短信通道构建复用，
拦截失效等于三条出网通道同时失守。
"""
import socket
import time

from app.egress import (
    MAX_SIGN_SKEW_SECONDS,
    egress_url_allowed,
    egress_url_problem,
    gateway_sign,
    signed_headers,
    verify_signature,
)


# ---------------------------------------------------------------------------
# URL 校验
# ---------------------------------------------------------------------------


def test_公网地址放行():
    assert egress_url_problem("https://8.8.8.8/gateway") is None
    assert egress_url_problem("http://1.1.1.1:8080/pay") is None


def test_非http协议拒绝():
    assert "http" in egress_url_problem("ftp://8.8.8.8/x")
    assert egress_url_problem("file:///etc/passwd") is not None
    assert egress_url_problem("") == "URL 为空"
    assert egress_url_problem("http://") is not None  # 无主机名


def test_环回与内网与链路本地一律拒绝():
    for url in (
        "http://127.0.0.1:8000/pay",        # 环回
        "http://localhost/pay",             # 域名解析到环回
        "http://10.1.2.3/pay",              # 内网 A
        "http://172.16.0.1/pay",            # 内网 B
        "http://192.168.1.1/pay",           # 内网 C
        "http://169.254.169.254/latest",    # 链路本地（云元数据地址，SSRF 经典靶）
        "http://0.0.0.0/pay",               # 未指定
        "http://[::1]/pay",                 # IPv6 环回
    ):
        problem = egress_url_problem(url)
        assert problem is not None, f"{url} 应被拒绝"
        assert "地址" in problem or "解析" in problem


def test_域名解析到内网同样拒绝(monkeypatch):
    """DNS rebinding 前半段：域名看着像公网，解析结果是内网。"""

    def fake_getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.9.9.9", port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    problem = egress_url_problem("https://gateway.example.com/pay")
    assert problem is not None and "内网" in problem


def test_解析失败拒绝(monkeypatch):
    def boom(host, port, *args, **kwargs):
        raise socket.gaierror("no dns")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert "解析失败" in egress_url_problem("https://nx.example.invalid/pay")


def test_便捷入口只落日志不抛异常(caplog):
    assert egress_url_allowed("https://8.8.8.8/x", "T") is True
    with caplog.at_level("ERROR"):
        assert egress_url_allowed("http://127.0.0.1/x", "MEDPLAT_PAYMENT_GATEWAY_URL") is False
    assert "MEDPLAT_PAYMENT_GATEWAY_URL" in caplog.text


# ---------------------------------------------------------------------------
# 签名与时间窗
# ---------------------------------------------------------------------------


def test_签名往返一致():
    headers = signed_headers("k1", b'{"a":1}')
    assert verify_signature("k1", headers["X-Timestamp"], b'{"a":1}', headers["X-Signature"]) is None


def test_错签与改体拒绝():
    ts = str(int(time.time()))
    sign = gateway_sign("k1", ts, b"body")
    assert verify_signature("k1", ts, b"body", "deadbeef") == "签名不匹配"
    assert verify_signature("k1", ts, b"tampered", sign) == "签名不匹配"
    assert verify_signature("other-key", ts, b"body", sign) == "签名不匹配"
    assert "X-Signature" in verify_signature("k1", ts, b"body", "")


def test_时间窗外按重放拒绝():
    old = str(int(time.time()) - MAX_SIGN_SKEW_SECONDS - 10)
    assert "重放" in verify_signature("k1", old, b"x", gateway_sign("k1", old, b"x"))
    future = str(int(time.time()) + MAX_SIGN_SKEW_SECONDS + 10)
    assert "重放" in verify_signature("k1", future, b"x", gateway_sign("k1", future, b"x"))
    assert "X-Timestamp" in verify_signature("k1", "", b"x", "sig")
    assert "X-Timestamp" in verify_signature("k1", "not-a-number", b"x", "sig")
