"""TOTP 动态口令（RFC 6238）——标准库自实现，零新依赖。

等保 E1 双因素认证的算法层：HOTP（RFC 4226）+ 按 30 秒时间步取计数器（RFC 6238），
HMAC-SHA1 + base32 密钥，与 Google Authenticator / FreeOTP 等主流令牌 App 完全兼容。

**为什么不引 pyotp**：本仓库运行时依赖只有 13 项（CLAUDE.md 第 12 条），而 RFC 6238
的核心不过是一次 HMAC 加动态截断，标准库 `hmac`/`base64`/`struct` 足够。正确性由
`tests/test_esec_account.py` 里 RFC 4226 附录 D 与 RFC 6238 附录 B 的官方测试向量钉住。

算法固定 SHA1：不是安全性妥协——HOTP 场景下 HMAC-SHA1 未被攻破（碰撞攻击对 HMAC
无效），而主流令牌 App 对 SHA256/512 的支持参差不齐，扫码即用比纸面强度重要。
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse

#: 验证码位数与时间步长（RFC 6238 默认值，主流令牌 App 的默认口径）
DIGITS = 6
PERIOD_SECONDS = 30


def generate_secret(nbytes: int = 20) -> str:
    """生成 base32 密钥（默认 160 bit，RFC 4226 推荐的密钥长度）。"""
    return base64.b32encode(secrets.token_bytes(nbytes)).decode()


def _decode_secret(secret: str) -> bytes:
    """base32 解码：容忍空格/小写/缺 padding（令牌 App 展示时常四位一组带空格）。"""
    normalized = secret.strip().replace(" ", "").upper()
    return base64.b32decode(normalized + "=" * (-len(normalized) % 8))


def hotp(secret: str, counter: int, digits: int = DIGITS) -> str:
    """RFC 4226 HOTP：HMAC-SHA1(密钥, 计数器) 动态截断取低位十进制。"""
    digest = hmac.new(_decode_secret(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % 10 ** digits).zfill(digits)


def totp_at(secret: str, at: float, digits: int = DIGITS, period: int = PERIOD_SECONDS) -> str:
    """RFC 6238 TOTP：计数器 = Unix 时间 // 时间步。`at` 独立成参是为了测试向量可验。"""
    return hotp(secret, int(at // period), digits)


def verify(
    secret: str,
    code: str,
    at: float | None = None,
    window: int = 1,
    digits: int = DIGITS,
    period: int = PERIOD_SECONDS,
) -> bool:
    """校验验证码，允许 ±window 个时间窗（默认 ±1，容忍手机与服务器 30 秒内的钟差）。

    比较用 `hmac.compare_digest` 防时序侧信道；格式不符（非纯数字/位数不对）直接失败，
    不进入运算。
    """
    if not code or not code.isdigit() or len(code) != digits:
        return False
    now = time.time() if at is None else at
    counter = int(now // period)
    return any(
        counter + offset >= 0 and hmac.compare_digest(hotp(secret, counter + offset, digits), code)
        for offset in range(-window, window + 1)
    )


def otpauth_uri(username: str, secret: str, issuer: str = "medplat") -> str:
    """生成 otpauth:// URI（令牌 App 扫码入口的标准格式）。"""
    label = urllib.parse.quote(f"{issuer}:{username}")
    query = urllib.parse.urlencode(
        {"secret": secret, "issuer": issuer, "algorithm": "SHA1",
         "digits": DIGITS, "period": PERIOD_SECONDS}
    )
    return f"otpauth://totp/{label}?{query}"
