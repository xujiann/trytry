"""国密算法适配层（阶段十一）。

指引"二、建设模式（四）"明文要求"加强信息系统的安全等保和**密码应用**"。
但通用平台不该假定每个县都上了国密——有的县还在等省侧统一改造。
所以做成**可配置的算法适配层**：默认仍用通用算法，配置切换到国密。
与"地方差异走配置不走代码分支"是同一条原则。

`MEDPLAT_CRYPTO_SUITE`：

- `general`（默认）：口令 PBKDF2-HMAC-SHA256，令牌 HMAC-SHA256
- `sm`：口令 SM3 迭代，令牌 HMAC-SM3

SM3 在这里是**纯 Python 实现**（GB/T 32905-2016），不引第三方依赖：
平台已有一条 build-free 的既定约束，为一个算法拉一整套加密库不划算，
而 SM3 本身不到百行。实现对着国标的两个标准测试向量验证（见 test_gmcrypto）。

**SM2/SM4 不在这里实现**。SM2 是椭圆曲线公钥算法，纯 Python 实现既慢又难以
保证抗侧信道，自己写反而不如不写；真要上 SM2，应当接国密硬件密码机或
经检测认证的密码库。此处只留出接口位置与说明，不做假实现——
一个"能跑但不安全"的国密实现比没有国密更危险。
"""
from typing import Any, cast

import hashlib
import hmac
import os
import struct

from .config import settings

__all__ = ["sm3_hash", "hash_password", "verify_password", "mac", "suite_name"]

# ---------------------------------------------------------------- SM3

_IV = [
    0x7380166F, 0x4914B2B9, 0x172442D7, 0xDA8A0600,
    0xA96F30BC, 0x163138AA, 0xE38DEE4D, 0xB0FB0E4E,
]
_T = [0x79CC4519] * 16 + [0x7A879D8A] * 48


def _rotl(x: int, n: int) -> int:
    n %= 32
    return ((x << n) | (x >> (32 - n))) & 0xFFFFFFFF


def _ff(x: int, y: int, z: int, j: int) -> int:
    return (x ^ y ^ z) if j < 16 else ((x & y) | (x & z) | (y & z))


def _gg(x: int, y: int, z: int, j: int) -> int:
    return (x ^ y ^ z) if j < 16 else ((x & y) | ((~x & 0xFFFFFFFF) & z))


def _p0(x: int) -> int:
    return x ^ _rotl(x, 9) ^ _rotl(x, 17)


def _p1(x: int) -> int:
    return x ^ _rotl(x, 15) ^ _rotl(x, 23)


def _cf(v: list[int], block: bytes) -> list[int]:
    w = list(struct.unpack(">16I", block))
    for j in range(16, 68):
        w.append(
            _p1(w[j - 16] ^ w[j - 9] ^ _rotl(w[j - 3], 15))
            ^ _rotl(w[j - 13], 7)
            ^ w[j - 6]
        )
    w1 = [w[j] ^ w[j + 4] for j in range(64)]

    a, b, c, d, e, f, g, h = v
    for j in range(64):
        ss1 = _rotl((_rotl(a, 12) + e + _rotl(_T[j], j)) & 0xFFFFFFFF, 7)
        ss2 = ss1 ^ _rotl(a, 12)
        tt1 = (_ff(a, b, c, j) + d + ss2 + w1[j]) & 0xFFFFFFFF
        tt2 = (_gg(e, f, g, j) + h + ss1 + w[j]) & 0xFFFFFFFF
        d = c
        c = _rotl(b, 9)
        b = a
        a = tt1
        h = g
        g = _rotl(f, 19)
        f = e
        e = _p0(tt2)
    return [x ^ y for x, y in zip(v, [a, b, c, d, e, f, g, h])]


def sm3_hash(data: bytes) -> bytes:
    """SM3 杂凑（GB/T 32905-2016）。返回 32 字节摘要。"""
    length = len(data) * 8
    data += b"\x80"
    while len(data) % 64 != 56:
        data += b"\x00"
    data += struct.pack(">Q", length)
    v = _IV
    for i in range(0, len(data), 64):
        v = _cf(v, data[i:i + 64])
    return struct.pack(">8I", *v)


class _Sm3Digest:
    """够 `hmac.new(digestmod=...)` 用的最小 hashlib 兼容外壳。

    只实现 HMAC 需要的那几个成员（block_size/digest_size/update/digest/copy），
    不假装是一个完整的 hashlib 对象——多实现的部分没人用，却会让人误以为
    它能替代 hashlib。
    """

    block_size = 64
    digest_size = 32
    name = "sm3"

    def __init__(self, data: bytes = b""):
        self._buffer = bytes(data)

    def update(self, data: bytes) -> None:
        self._buffer += data

    def digest(self) -> bytes:
        return sm3_hash(self._buffer)

    def hexdigest(self) -> str:
        return self.digest().hex()

    def copy(self) -> "_Sm3Digest":
        return _Sm3Digest(self._buffer)


def _sm3_new(data: bytes = b"") -> _Sm3Digest:
    return _Sm3Digest(data)


# ---------------------------------------------------------------- 适配层

_ITERATIONS = 120_000
_SM_ITERATIONS = 20_000  # SM3 是纯 Python，迭代数按实测耗时对齐到同一量级


def suite_name() -> str:
    return "sm" if settings.crypto_suite == "sm" else "general"


def hash_password(password: str, salt: bytes | None = None) -> str:
    """口令散列。存储格式带算法前缀，**切换算法后老口令仍可校验**——
    不带前缀就只能强制全员改密，那是没人能接受的升级方式。"""
    salt = salt or os.urandom(16)
    if suite_name() == "sm":
        digest = _sm_kdf(password.encode(), salt)
        return f"sm3${salt.hex()}${digest.hex()}"
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验口令。按存储值自带的算法标识走，与当前配置无关——
    配置是给**新口令**用的，已存的口令该用什么算什么。"""
    if stored.startswith("sm3$"):
        try:
            _, salt_hex, digest_hex = stored.split("$", 2)
        except ValueError:
            return False
        digest = _sm_kdf(password.encode(), bytes.fromhex(salt_hex))
        return hmac.compare_digest(digest.hex(), digest_hex)
    try:
        salt_hex, digest_hex = stored.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), _ITERATIONS)
    return hmac.compare_digest(digest.hex(), digest_hex)


def _sm_kdf(password: bytes, salt: bytes) -> bytes:
    """SM3 迭代派生。不用 hashlib.pbkdf2_hmac 是因为它只认注册过的算法名。"""
    digest = salt + password
    for _ in range(_SM_ITERATIONS):
        digest = sm3_hash(digest)
    return digest


def mac(key: bytes, message: bytes) -> bytes:
    """消息认证码：令牌签名与审计哈希链共用。"""
    if suite_name() == "sm":
        # hmac 的 digestmod 允许 new(data) 形态的可调用，但 typeshed 只声明了无参形态
        return hmac.new(key, message, cast(Any, _sm3_new)).digest()
    return hmac.new(key, message, hashlib.sha256).digest()


def mac_alg() -> str:
    """写进令牌头部的算法名，便于对端识别与将来平滑切换。"""
    return "HS-SM3" if suite_name() == "sm" else "HS256"
