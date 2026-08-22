"""统一认证：口令散列与 HS256 令牌签发/校验（自包含实现，不依赖第三方 JWT 库）。

密钥派生与轮换（A1，阶段十三）：同一把 `MEDPLAT_SECRET` 原先直接用于三个用途——
JWT 签名（本文件）、审计哈希链 MAC（audit_chain.py）、一码通动态码签名
（routers/credentials.py）。任一用途的签名值泄露即可离线爆破出唯一主密钥，
三处同时沦陷。现按用途派生子密钥（HMAC-SHA256，info 标签 medplat:jwt /
medplat:audit / medplat:qr），**签发/写入一律用派生口径**。

兼容与轮换：历史令牌/审计链/动态码是用原始 secret 直签的，验证路径按
「当前派生 → 当前原始 → previous 派生 → previous 原始」多口径回退
（见 `verification_keys`）——存量数据不因升级变"无效"或"被篡改"；
设置 MEDPLAT_SECRET_PREVIOUS 后，旧密钥仅用于验证，宽限期结束清掉即可。
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from . import gmcrypto
from .config import settings
from .state_store import SessionRegistry, TokenBlacklist

TOKEN_TTL_SECONDS = settings.token_ttl_seconds

# ---------------------------------------------------------------------------
# 会话 Cookie 与 CSRF（G3，P1-23 收口）：令牌双模下发的命名与随机源。
#
# 业务端与居民端各一对 Cookie：令牌本体进 **HttpOnly** Cookie（浏览器 JS 读不
# 到，收掉 localStorage 存 JWT 的 XSS 失窃面），CSRF token 进**非 HttpOnly**
# Cookie（双提交模式要求前端 JS 读出来放进 X-CSRF-Token 请求头）。两套名字
# 严格分开——业务令牌与居民令牌本就互不越界（scope=portal），Cookie 也不共用。
# ---------------------------------------------------------------------------

#: 业务端令牌 / CSRF Cookie 名
AUTH_COOKIE = "medplat_token"
CSRF_COOKIE = "medplat_csrf"
#: 居民端（portal）令牌 / CSRF Cookie 名
PORTAL_AUTH_COOKIE = "medplat_portal_token"
PORTAL_CSRF_COOKIE = "medplat_portal_csrf"
#: 双提交校验的请求头名
CSRF_HEADER = "X-CSRF-Token"
#: 登录请求声明"走 Cookie 会话"的请求头（值须为 cookie）；见 deps.wants_cookie_auth
COOKIE_MODE_HEADER = "X-Token-Transport"


def new_csrf_token() -> str:
    """生成 CSRF token（双提交 Cookie 模式）。

    取舍：**无状态、不落库**。双提交只要求"请求头与随请求回带的 CSRF Cookie
    一致"，服务端不必记住它；能改写受害者浏览器里本域 Cookie 的攻击者（XSS/
    中间人）面前，落库版 CSRF 同样失守——落库换不来防护增量，只多一张表。
    """
    return secrets.token_urlsafe(32)


def derive_key(secret: str, purpose: str) -> bytes:
    """按用途派生子密钥：HMAC-SHA256(secret, "medplat:"+purpose)。

    单段定长 info 场景下，HMAC 派生与 HKDF-Expand 强度等价；不引 HKDF
    是"不为一个调用拉依赖/铺样板"的老原则。派生算法**固定 SHA-256**、
    不随 crypto_suite 切换——它只是密钥分隔，不是对外声明的 MAC 算法，
    固定住才能保证套件切换不使全部存量密钥作废。
    """
    return hmac.new(secret.encode(), f"medplat:{purpose}".encode(), hashlib.sha256).digest()


def signing_key(purpose: str) -> bytes:
    """签发/写入用的当前密钥：一律当前 secret 的派生口径。"""
    return derive_key(settings.secret, purpose)


def verification_keys(purpose: str) -> list[bytes]:
    """验证用密钥候选，按命中概率排序：

    1. 当前 secret 的派生口径（新签发的都是它）；
    2. 当前 secret 的原始口径（升级到派生方案**之前**签出的存量数据）；
    3./4. MEDPLAT_SECRET_PREVIOUS 的两种口径（密钥轮换宽限期内的旧数据）。

    去掉 2-4 的回退会把存量令牌全部登出、存量审计链整段误判为"被篡改"，
    tests/test_ops_key_rotation.py 有专门用例钉住。
    """
    keys = [derive_key(settings.secret, purpose), settings.secret.encode()]
    if settings.secret_previous:
        keys.append(derive_key(settings.secret_previous, purpose))
        keys.append(settings.secret_previous.encode())
    return keys

# 登出黑名单（M4 整改）：默认进程内存实现（带 TTL 清理），
# 配置 MEDPLAT_REDIS_URL 后自动切换 Redis 共享存储（多实例部署必须）。
revoked_tokens = TokenBlacklist(default_ttl_seconds=TOKEN_TTL_SECONDS)

# 会话登记（等保 E1）：空闲超时的活动时刻 + 并发会话计数。与黑名单同层、同一
# 内存/Redis 双实现约定；两项开关（session_idle_timeout_seconds /
# session_max_concurrent）为 0 时调用方（deps / routers/auth.py）直接旁路。
active_sessions = SessionRegistry(default_ttl_seconds=TOKEN_TTL_SECONDS)


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
    signature = _b64url(gmcrypto.mac(signing_key("jwt"), f"{header}.{payload}".encode()))
    return f"{header}.{payload}.{signature}"


def decode_token(token: str) -> dict | None:
    try:
        header, payload, signature = token.split(".")
    except ValueError:
        return None
    message = f"{header}.{payload}".encode()
    # 多口径回退：任一候选密钥验过即有效（新派生 → 旧原始 → previous 两口径）
    for key in verification_keys("jwt"):
        expected = _b64url(gmcrypto.mac(key, message))
        if hmac.compare_digest(signature, expected):
            break
    else:
        return None
    claims = json.loads(_b64url_decode(payload))
    if claims.get("exp", 0) < time.time():
        return None
    return claims
