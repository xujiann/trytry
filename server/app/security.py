"""统一认证：口令散列与 HS256 令牌签发/校验（自包含实现，不依赖第三方 JWT 库）。"""
import base64
import hmac
import json
import os
import time

from . import gmcrypto
from .config import settings
from .state_store import TokenBlacklist

SECRET_KEY = settings.secret
TOKEN_TTL_SECONDS = settings.token_ttl_seconds

# 登出黑名单（M4 整改）：默认进程内存实现（带 TTL 清理），
# 配置 MEDPLAT_REDIS_URL 后自动切换 Redis 共享存储（多实例部署必须）。
revoked_tokens = TokenBlacklist(default_ttl_seconds=TOKEN_TTL_SECONDS)


def validate_password_strength(password: str) -> str | None:
    """密码复杂度校验：≥8位且同时含字母与数字。不合规时返回原因说明。"""
    if len(password) < 8:
        return "密码长度不得少于8位"
    if not any(c.isalpha() for c in password):
        return "密码必须包含字母"
    if not any(c.isdigit() for c in password):
        return "密码必须包含数字"
    return None


def hash_password(password: str, salt: bytes | None = None) -> str:
    """口令散列。算法由 `MEDPLAT_CRYPTO_SUITE` 决定（general / sm），
    具体实现见 `app/gmcrypto.py`。"""
    return gmcrypto.hash_password(password, salt)


def verify_password(password: str, stored: str) -> bool:
    """校验口令。按存储值自带的算法标识走，与当前配置无关——
    切换算法后老口令仍可登录，不必强制全员改密。"""
    return gmcrypto.verify_password(password, stored)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def create_token(
    username: str,
    role: str,
    extra: dict | None = None,
    ttl_seconds: int | None = None,
) -> str:
    """签发 HS256 令牌。

    `extra` 用于附加声明（居民端令牌用它携带 scope=portal 与 account_id，
    业务端 get_current_user 见到 scope=portal 直接拒绝，两套身份互不越界）；
    `ttl_seconds` 覆盖默认有效期（居民端移动会话更长）。
    """
    header = _b64url(json.dumps({"alg": gmcrypto.mac_alg(), "typ": "JWT"}).encode())
    claims = {
        "sub": username,
        "role": role,
        # 签发时刻（浮点秒）：与用户改密基线比较，改密即吊销此前全部令牌（M-4）
        "iat": time.time(),
        "exp": int(time.time()) + (ttl_seconds or TOKEN_TTL_SECONDS),
        # 唯一标识：保证同秒签发的令牌互不相同，登出黑名单可精确作废单个令牌
        "jti": os.urandom(8).hex(),
    }
    claims.update(extra or {})
    payload = _b64url(json.dumps(claims).encode())
    signature = _b64url(gmcrypto.mac(SECRET_KEY.encode(), f"{header}.{payload}".encode()))
    return f"{header}.{payload}.{signature}"


def decode_token(token: str) -> dict | None:
    try:
        header, payload, signature = token.split(".")
    except ValueError:
        return None
    expected = _b64url(gmcrypto.mac(SECRET_KEY.encode(), f"{header}.{payload}".encode()))
    if not hmac.compare_digest(signature, expected):
        return None
    claims = json.loads(_b64url_decode(payload))
    if claims.get("exp", 0) < time.time():
        return None
    return claims
