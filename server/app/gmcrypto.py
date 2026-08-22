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

SM4（GB/T 32907-2016）同样是**纯 Python 实现**，只服务一个场景：PII 列的
**静态存储加密**（`app/pii.py`，工程包 E3）。要把边界说清楚——

- 它防的是"拖库/备份文件泄露后明文直读"，加解密只发生在应用进程内部，
  **不对外提供任何在线加解密 oracle**（没有接口接受外部密文并回答对错）；
- 纯 Python 无法保证恒定时间执行，抗侧信道**不在**承诺范围。能观测本机
  时序/缓存的攻击者通常已能直接读到进程内存里的密钥，此边界内侧信道
  不是新增的风险面；
- MAC 校验一律 `hmac.compare_digest`，密文完整性由 MAC 而非 SM4 本身保证。

**SM2 仍然不在这里实现**。SM2 是椭圆曲线公钥算法，纯 Python 实现既慢又难以
保证抗侧信道，自己写反而不如不写；真要上 SM2，应当接国密硬件密码机或
经检测认证的密码库。此处只留出接口位置与说明，不做假实现——
一个"能跑但不安全"的国密实现比没有国密更危险。SM4 敢自实现而 SM2 不敢，
差别在算法性质：对称分组密码是查表与异或的确定性组合，对着国标向量逐字节
验证即可获得正确性信心；椭圆曲线的坐标运算做不到这一点。
"""
from typing import Any, cast

import functools
import hashlib
import hmac
import os
import struct

from .config import settings

__all__ = [
    "sm3_hash",
    "sm4_encrypt_block",
    "sm4_ctr",
    "hash_password",
    "verify_password",
    "mac",
    "suite_name",
]

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


# ---------------------------------------------------------------- SM4
# 分组加密（GB/T 32907-2016）：128 位分组、128 位密钥、32 轮非平衡 Feistel。
# 只暴露两个原语：单块加密（ECB 一块）与其上的 CTR 流模式——列加密要的是
# "变长明文 + 随机 nonce"，CTR 恰好只用加密方向，不必实现解密轮。

_SM4_SBOX = bytes.fromhex(
    "d690e9fecce13db716b614c228fb2c05"
    "2b679a762abe04c3aa44132649860699"
    "9c4250f491ef987a33540b43edcfac62"
    "e4b31ca9c908e89580df94fa758f3fa6"
    "4707a7fcf37317ba83593c19e6854fa8"
    "686b81b27164da8bf8eb0f4b70569d35"
    "1e240e5e6358d1a225227c3b01217887"
    "d40046579fd327524c3602e7a0c4c89e"
    "eabf8ad240c738b5a3f7f2cef96115a1"
    "e0ae5da49b341a55ad933230f58cb1e3"
    "1df6e22e8266ca60c02923ab0d534e6f"
    "d5db3745defd8e2f03ff6a726d6c5b51"
    "8d1baf92bbddbc7f11d95c411f105ad8"
    "0ac13188a5cd7bbd2d74d012b8e5b4b0"
    "8969974a0c96777e65b9f109c56ec684"
    "18f07dec3adc4d2079ee5f3ed7cb3948"
)

# 系统参数 FK 与固定参数 CK（国标 7.3）。CK 有生成公式（ck_{i,j}=(4i+j)*7 mod 256），
# 按公式生成而不抄 128 个字面量——抄错一个字节向量都过不了，公式反而更可审。
_SM4_FK = (0xA3B1BAC6, 0x56AA3350, 0x677D9197, 0xB27022DC)
_SM4_CK = tuple(
    sum((((4 * i + j) * 7 % 256) << (24 - 8 * j)) for j in range(4)) for i in range(32)
)


def _sm4_tau(word: int) -> int:
    """非线性变换 τ：四个字节各过一次 S 盒。"""
    return (
        (_SM4_SBOX[(word >> 24) & 0xFF] << 24)
        | (_SM4_SBOX[(word >> 16) & 0xFF] << 16)
        | (_SM4_SBOX[(word >> 8) & 0xFF] << 8)
        | _SM4_SBOX[word & 0xFF]
    )


def _sm4_t(word: int) -> int:
    """轮函数合成变换 T：τ 后接线性变换 L。"""
    b = _sm4_tau(word)
    return b ^ _rotl(b, 2) ^ _rotl(b, 10) ^ _rotl(b, 18) ^ _rotl(b, 24)


def _sm4_t_key(word: int) -> int:
    """密钥扩展的合成变换 T'：τ 后接线性变换 L'。"""
    b = _sm4_tau(word)
    return b ^ _rotl(b, 13) ^ _rotl(b, 23)


@functools.lru_cache(maxsize=8)
def _sm4_round_keys(key: bytes) -> tuple[int, ...]:
    """密钥扩展：128 位密钥 → 32 个轮密钥。

    lru_cache：列加密场景同一把派生密钥反复使用，轮密钥只算一次；
    进程内最多几把密钥（当前 + 轮换旧钥），8 个槽位绰绰有余。
    """
    if len(key) != 16:
        raise ValueError("SM4 密钥必须为 16 字节")
    k = [mk ^ fk for mk, fk in zip(struct.unpack(">4I", key), _SM4_FK)]
    rks = []
    for i in range(32):
        rk = k[0] ^ _sm4_t_key(k[1] ^ k[2] ^ k[3] ^ _SM4_CK[i])
        rks.append(rk)
        k = [k[1], k[2], k[3], rk]
    return tuple(rks)


def sm4_encrypt_block(key: bytes, block: bytes) -> bytes:
    """SM4 单块加密（ECB 一块，GB/T 32907-2016）。16 字节进、16 字节出。

    只提供加密方向：CTR 模式的解密就是再加密一次密钥流，用不到解密轮。
    """
    if len(block) != 16:
        raise ValueError("SM4 分组必须为 16 字节")
    x = list(struct.unpack(">4I", block))
    for rk in _sm4_round_keys(bytes(key)):
        x = [x[1], x[2], x[3], x[0] ^ _sm4_t(x[1] ^ x[2] ^ x[3] ^ rk)]
    return struct.pack(">4I", x[3], x[2], x[1], x[0])


def sm4_ctr(key: bytes, nonce: bytes, data: bytes) -> bytes:
    """SM4-CTR：nonce（16 字节）作初始计数器块，整块按 128 位大端整数递增。

    加解密同一个函数（密钥流异或自反）。nonce 由调用方**每次随机生成**并随密文
    存储——CTR 的全部安全性都押在"同一密钥下计数器块不重复"上，随机 16 字节
    起点加上列加密的短明文（一条至多两个分组），碰撞概率可忽略。
    """
    if len(nonce) != 16:
        raise ValueError("SM4-CTR nonce 必须为 16 字节")
    counter = int.from_bytes(nonce, "big")
    out = bytearray()
    for offset in range(0, len(data), 16):
        keystream = sm4_encrypt_block(key, counter.to_bytes(16, "big"))
        chunk = data[offset:offset + 16]
        out.extend(b ^ s for b, s in zip(chunk, keystream))
        counter = (counter + 1) % (1 << 128)
    return bytes(out)


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
