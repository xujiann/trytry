"""统一认证：口令散列与 HS256 令牌签发/校验（自包含实现，不依赖第三方 JWT 库）。"""
import base64
import hashlib
import hmac
import json
import os
import time

from .config import settings

SECRET_KEY = settings.secret
TOKEN_TTL_SECONDS = settings.token_ttl_seconds
_PBKDF2_ITERATIONS = 120_000


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), _PBKDF2_ITERATIONS)
    return hmac.compare_digest(digest.hex(), digest_hex)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def create_token(username: str, role: str) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url(
        json.dumps({"sub": username, "role": role, "exp": int(time.time()) + TOKEN_TTL_SECONDS}).encode()
    )
    signature = _b64url(hmac.new(SECRET_KEY.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    return f"{header}.{payload}.{signature}"


def decode_token(token: str) -> dict | None:
    try:
        header, payload, signature = token.split(".")
    except ValueError:
        return None
    expected = _b64url(hmac.new(SECRET_KEY.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(signature, expected):
        return None
    claims = json.loads(_b64url_decode(payload))
    if claims.get("exp", 0) < time.time():
        return None
    return claims
