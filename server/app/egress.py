"""统一出网防线（SSRF）与网关签名口径。

平台有三处"配置一个 URL、进程主动外呼"的通道：支付网关
（MEDPLAT_PAYMENT_GATEWAY_URL）、短信网关（MEDPLAT_SMS_GATEWAY_URL）、
慢专病呼叫中心网关（MEDPLAT_SPD_CALL_GATEWAY_URL，接入时同样应过本校验，
经 spd 白名单登记后复用）。这些 URL 由运维配置而非终端用户输入，但配错或
被注入（如云主机元数据地址 169.254.169.254）时，进程就成了内网探针。
故收口一个校验，三处复用：

- 仅允许 http/https；
- 主机经 socket 解析（IP 直写与域名同一条路径），任一解析结果落在
  环回/内网/链路本地/保留段（即非公网可路由地址）一律拒绝——域名解析到
  内网同样拒绝，DNS rebinding 的第一步在这里挡住；
- 解析失败视为不可用：起不了通道的 URL 没有放行的意义。

不通过时调用方应**拒绝启用通道并 log**（billing.register_http_gateway /
sms._build_provider 均如此），而不是带病运行。

签名口径（HMAC-SHA256，支付与短信网关共用；回调方向同一算法反向验签）：

    X-Timestamp: 秒级 Unix 时间戳
    X-Signature: hex( HMAC_SHA256(key, f"{timestamp}." + body_bytes) )

body_bytes 是请求体的**原始字节**（GET 无体时为空串），拼时间戳后整体签名，
时间戳参与签名故不可被单独篡改；验签配 ±MAX_SIGN_SKEW_SECONDS 时间窗防重放，
窗内重放由业务幂等兜底（订单状态机，见 routers/billing.py 回调端点）。
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import socket
import time
from urllib.parse import urlsplit

logger = logging.getLogger("medplat.egress")

ALLOWED_SCHEMES = ("http", "https")
#: 回调/请求签名允许的时钟偏差窗口（秒）。窗口外的时间戳按重放拒绝。
MAX_SIGN_SKEW_SECONDS = 300


def egress_url_problem(url: str) -> str | None:
    """返回该出网 URL 不可用的原因；可用返回 None。

    与 config._credential_problem 同一形状：调用方拿到的是能写进日志的
    人话，而不是布尔值加猜。
    """
    if not url:
        return "URL 为空"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "URL 无法解析"
    if parts.scheme not in ALLOWED_SCHEMES:
        return f"仅允许 http/https，实为 {parts.scheme or '(无协议)'}"
    host = parts.hostname
    if not host:
        return "URL 缺少主机名"
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except OSError as exc:
        return f"主机名解析失败：{exc}"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:  # pragma: no cover - getaddrinfo 不该返回非法地址
            return f"解析出非法地址 {info[4][0]!r}"
        if not ip.is_global:
            kind = (
                "环回" if ip.is_loopback
                else "链路本地" if ip.is_link_local
                else "内网" if ip.is_private
                else "保留段"
            )
            return f"解析到{kind}地址 {ip}，出网通道禁止指向非公网地址"
    return None


def egress_url_allowed(url: str, label: str) -> bool:
    """校验并落日志的便捷入口：不通过时 log 原因并返回 False。"""
    problem = egress_url_problem(url)
    if problem is None:
        return True
    logger.error("[EGRESS] %s=%r 未通过出网校验：%s；通道不予启用", label, url, problem)
    return False


def gateway_sign(key: str, timestamp: int | str, body: bytes) -> str:
    """网关请求/回调的统一签名：HMAC-SHA256(key, f"{timestamp}." + body)。"""
    return hmac.new(
        key.encode("utf-8"), f"{timestamp}.".encode("utf-8") + body, hashlib.sha256
    ).hexdigest()


def signed_headers(key: str, body: bytes) -> dict[str, str]:
    """给出网请求生成签名头（X-Timestamp / X-Signature）。"""
    timestamp = int(time.time())
    return {"X-Timestamp": str(timestamp), "X-Signature": gateway_sign(key, timestamp, body)}


def verify_signature(
    key: str,
    timestamp: str,
    body: bytes,
    signature: str,
    *,
    max_skew_seconds: int = MAX_SIGN_SKEW_SECONDS,
) -> str | None:
    """验回调签名；返回失败原因（人话），通过返回 None。

    先验时间窗再验签：窗口外的报文即使签名正确也按重放拒绝。
    比较用 compare_digest，防时序侧信道。
    """
    if not timestamp.isdigit():
        return "缺少或非法的 X-Timestamp"
    if abs(int(timestamp) - int(time.time())) > max_skew_seconds:
        return f"时间戳超出 ±{max_skew_seconds}s 窗口（疑似重放）"
    if not signature:
        return "缺少 X-Signature"
    if not hmac.compare_digest(gateway_sign(key, timestamp, body), signature):
        return "签名不匹配"
    return None
