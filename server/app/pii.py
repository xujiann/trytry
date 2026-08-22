"""PII 列加密存储（工程包 E3）：`patients.id_card/phone`、`resident_accounts.phone`。

## 防什么、不防什么（边界如实写）

本层防的是**拖库/备份文件泄露后的明文直读**：数据库文件、dump、从库落到
攻击者手里时，身份证号与手机号是密文而非明文。它**不防持有应用密钥的
攻击者**——能读到 `MEDPLAT_SECRET`（或应用进程内存）的人可以解出全部密文，
这不是本层的失败，而是它声明的边界；密钥与数据分离（密钥只在环境变量/密管，
不落库不落备份）正是这层价值的全部来源。加解密只发生在应用进程内，
不对外提供任何在线加解密 oracle。侧信道边界见 `gmcrypto.py` 模块 docstring。

## 存储格式与算法

密文列格式：``pii1$base64(nonce16 + ct)$mac_hex``

- 加密：SM4-CTR（`gmcrypto.sm4_ctr`，nonce 每次随机 16 字节）；
- 完整性：MAC 覆盖 nonce+ct，算法随 `MEDPLAT_CRYPTO_SUITE` 走（general=
  HMAC-SHA256，sm=HMAC-SM3，即 `gmcrypto.mac`），校验失败**拒绝解密**——
  被篡改的密文宁可报错也不能悄悄回给业务一段乱码；
- 检索索引：`pii_index(plain)` = HMAC(idx_key, plain) 十六进制，落在旁列
  `*_idx`（String(64)）上做**等值检索**（确定性，同值同索引）。

两把密钥分开派生（加密泄露不牵连索引、反之亦然）：

- 加密/MAC 密钥：``security.derive_key(secret, "pii")``（info="medplat:pii"）；
- 索引密钥：``security.derive_key(secret, "pii-idx")``（info="medplat:pii-idx"）。

加密与 MAC 共用 pii 这把派生密钥：SM4-CTR 与 HMAC 是结构上不相干的两类原语，
单键跨用在此场景可接受；再多派生一把只会让轮换脚本多一个出错面。

## 开关语义（`MEDPLAT_PII_ENCRYPTION_ENABLED`，默认关）

- **关**：写入原样落明文、读取原样返回、等值查询走明文列——行为与字节与
  未引入本模块时完全一致（既有测试原样绿）。唯一的新增动作是旁路维护
  `*_idx` 索引列（对 API 行为零影响），这让"先跑迁移、后开开关"的切换
  不需要停机重算索引。
- **开**：写入落 `pii1$` 密文 + 维护索引列；读取识别前缀透明解密（无前缀
  的存量明文直接返回——回填前后混存期兼容）；等值查询走索引列。

## 密钥轮换（与 `MEDPLAT_SECRET_PREVIOUS` 的关系）

加密列一律用**当前** secret 的派生密钥写入；解密在 MAC 对不上时回退尝试
`secret_previous` 的派生密钥（与 security.verification_keys 同哲学）——
换钥后、重加密完成前，存量密文仍可读。**检索索引同口径回退**：
`pii_filter` 在配了 `secret_previous` 时把当前钥与旧钥各算一次索引取并集
（`_index_candidates`），宽限期内等值检索对旧钥数据照样命中。

这一条是硬要求而非优化：索引单口径时，宽限期内按证件号查"查不到"不是
报错而是"这人没建过档"——建档接口于是新建一条（明文列唯一约束因随机
nonce 永不冲突，idx 部分唯一索引因新钥算出新值也不冲突，两道约束同时
失效），居民端同理重复开户且绑不上本人档案。静默重复主数据比检索漏更
严重，故读侧多口径、写侧仍单口径（新数据统一收敛到当前钥）。

运维口径不变：换钥后仍应**立即**跑 ``scripts/pii_encrypt_backfill.py
--old-secret <旧值>`` 重加密并重算索引，跑完清掉 `MEDPLAT_SECRET_PREVIOUS`
即回到单口径；多口径只是宽限期的安全网，不是长期形态。

## 模糊检索的降级（开态，如实声明）

密文态不支持 like/contains：患者检索（routers/patients.py）的证件号关键字
在开态降级为**仅全值命中**（走索引列等值），前缀/中缀不再命中；姓名与
健康卡号检索不受影响。spd 子系统两处按证件号 contains 的筛选
（spd/routers/care.py、population.py）已按同一口径接入（P1-25）：`pii` 进了
tests/test_spd_boundary.py 的平台依赖白名单，`pii_filter` 经 spd/platform.py
再导出——关态保持 contains，开态仅全值命中。
"""
import base64
import hmac
import os

from sqlalchemy import String, event, or_
from sqlalchemy.types import TypeDecorator

from . import gmcrypto, security
from .config import settings

__all__ = [
    "PII_PREFIX",
    "encrypt_pii",
    "decrypt_pii",
    "pii_index",
    "pii_index_match",
    "pii_filter",
    "EncryptedPII",
    "register_pii_index_sync",
]

PII_PREFIX = "pii1$"


def _enc_key(secret: str | None = None) -> bytes:
    return security.derive_key(secret or settings.secret, "pii")


def _sm4_key(secret: str | None = None) -> bytes:
    """SM4 密钥：派生密钥（HMAC-SHA256，32 字节）截取前 16 字节——HMAC 输出
    各字节独立均匀，截断即标准 KDF 用法；MAC 用整个 32 字节，两者不重叠使用。"""
    return _enc_key(secret)[:16]


def _idx_key(secret: str | None = None) -> bytes:
    return security.derive_key(secret or settings.secret, "pii-idx")


def encrypt_pii(plain: str, secret: str | None = None) -> str:
    """明文 → ``pii1$base64(nonce+ct)$mac_hex``。空串不加密（语义保留：
    "未登记手机号"在库里仍是空串，`bool(value)` 判断不被破坏）。"""
    if plain == "":
        return plain
    nonce = os.urandom(16)
    ct = gmcrypto.sm4_ctr(_sm4_key(secret), nonce, plain.encode())
    blob = nonce + ct
    tag = gmcrypto.mac(_enc_key(secret), blob).hex()
    return f"{PII_PREFIX}{base64.b64encode(blob).decode()}${tag}"


def decrypt_pii(stored: str, secret: str | None = None) -> str:
    """密文 → 明文。无 ``pii1$`` 前缀视为存量明文直接返回（回填前兼容）。

    MAC 校验失败先回退 `secret_previous` 的派生密钥（轮换宽限期），仍不过
    则抛 ValueError——被篡改/密钥不符的密文必须拒绝，不能返回乱码。
    显式传入 secret 时（重加密脚本）不做回退，指定哪把就用哪把。
    """
    if not stored.startswith(PII_PREFIX):
        return stored
    try:
        b64, tag = stored[len(PII_PREFIX):].split("$", 1)
        blob = base64.b64decode(b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("PII 密文格式损坏") from exc
    candidates = [secret] if secret is not None else [settings.secret] + (
        [settings.secret_previous] if settings.secret_previous else []
    )
    for cand in candidates:
        if hmac.compare_digest(gmcrypto.mac(_enc_key(cand), blob).hex(), tag):
            return gmcrypto.sm4_ctr(_sm4_key(cand), blob[:16], blob[16:]).decode()
    raise ValueError("PII 密文完整性校验失败（被篡改或密钥不符）")


def pii_index(plain: str, secret: str | None = None) -> str:
    """等值检索索引：HMAC(idx_key, plain) 十六进制（确定性，64 字符）。"""
    return gmcrypto.mac(_idx_key(secret), plain.encode()).hex()


def _index_candidates(value: str) -> list[str]:
    """检索索引的**多口径**取值：当前钥 + 轮换宽限期内的旧钥。

    索引是写入时算好的列值：换钥之后、`scripts/pii_encrypt_backfill.py`
    重算完成之前，存量行的索引是**旧钥**算的，只用当前钥去比一定漏。
    解密早就有 `secret_previous` 回退（见 `decrypt_pii`），检索这一侧
    必须同口径，否则宽限期内 EMPI 去重/实名绑定静默失效——查不到不是
    报错而是"这人没建过档"，于是重复建档、重复开户。

    写入侧**不**多口径：新写入一律用当前钥（见 `register_pii_index_sync`
    与回填脚本），旧钥索引只读不写，回填跑完即自然收敛为单一口径。
    """
    candidates = [pii_index(value)]
    if settings.secret_previous:
        previous = pii_index(value, settings.secret_previous)
        if previous not in candidates:
            candidates.append(previous)
    return candidates


def pii_index_match(col_index, value: str):
    """索引列等值条件（多口径）——替换裸写 ``col_index == pii_index(value)``。

    独立导出是因为不是每个调用点都能用 `pii_filter`：patients 的关键字检索
    要把索引等值并进 name/ehc_no 的 like 里（见 routers/patients.py），
    只需要这半个条件。两处走同一函数，轮换口径不会再只修一边。
    """
    candidates = _index_candidates(value)
    if len(candidates) == 1:
        return col_index == candidates[0]
    return col_index.in_(candidates)


def pii_filter(col_index, col_plain, value: str):
    """PII 列等值过滤条件（替换 ``Model.col == value`` 的唯一入口）。

    - 开态：索引列等值（当前钥；配了 `MEDPLAT_SECRET_PREVIOUS` 时并上旧钥
      口径，见 `_index_candidates`）。回填迁移保证索引列完整（写入侧 event
      持续维护），走 `*_idx` 上的索引。
    - 关态：明文列等值 **OR** 索引列等值。取舍：只查明文列在"开过又关回"
      的场景会漏掉尚未解密回写的密文行；多出的 OR 分支对从未开启过的部署
      是恒假条件（索引确定性、无碰撞），不改变结果集，只多一个索引探查——
      拿这点开销换"开关来回切不丢数据"，值得。

    未配 `secret_previous`（绝大多数部署的常态）时条件与原来逐字节一致，
    只有轮换宽限期才多一个索引探查。
    """
    index_match = pii_index_match(col_index, value)
    if settings.pii_encryption_enabled:
        return index_match
    return or_(col_plain == value, index_match)


class EncryptedPII(TypeDecorator):
    """透明加密列类型：bind 时按开关加密，result 时按前缀透明解密。

    - 已带 ``pii1$`` 前缀的绑定值直通（回填脚本经 SQL 写入，正常业务不会）；
    - `coerce_compared_value` 固定返回 String：比较值（==、like 的右侧）
      **不做加密**——开态等值一律走 `pii_filter`，把比较值也加密只会产生
      一个永不相等的随机密文，还会连累关态 like 的字节行为。
    """

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None or value == "" or not settings.pii_encryption_enabled:
            return value
        if value.startswith(PII_PREFIX):
            return value
        return encrypt_pii(value)

    def process_result_value(self, value, dialect):
        if value and value.startswith(PII_PREFIX):
            return decrypt_pii(value)
        return value

    def coerce_compared_value(self, op, value):
        return String()


def register_pii_index_sync(model, *pairs: tuple[str, str]) -> None:
    """在模型上登记 ``*_idx`` 索引列的自动维护（(明文属性, 索引属性) 对）。

    用 mapper 级 before_insert/before_update 事件而不是各写入点显式赋值：
    写入点散在建档、HL7 入站更新、居民端绑定/回填等多处（还有 seed 与
    import 脚本），事件挂在模型上一处生效、**走 ORM 的新增写入点不会漏**。
    开关关闭时同样维护——索引列不进任何 API 响应，先把索引铺满，
    开启开关那一刻检索才无缝。

    **例外（如实写）**：`Session.bulk_insert_mappings` / `bulk_update_mappings`
    与 Core 层 `insert()` 绕过 mapper 事件，索引列不会被自动填上。仓库里唯一
    这么写的是 `scripts/seed_bulk.py`（压测灌数），它自己显式算索引；再有新的
    批量写入点必须照办，或改走 ORM 实例。
    """

    def _sync(mapper, connection, target) -> None:
        for plain_attr, idx_attr in pairs:
            value = getattr(target, plain_attr)
            if not value:
                setattr(target, idx_attr, None)
            elif value.startswith(PII_PREFIX):
                # 密文算不出索引；密文只可能来自回填脚本，索引由它自己维护
                continue
            else:
                setattr(target, idx_attr, pii_index(value))

    event.listen(model, "before_insert", _sync)
    event.listen(model, "before_update", _sync)
