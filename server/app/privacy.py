"""公共脱敏模块：所有返回居民敏感字段（身份证号、电话）的出口统一调用。

适用范围（H1 整改）：
- 工作人员侧接口（患者检索/详情、FHIR/HL7 出入站回显、360 视图等）：非 admin 一律掩码；
- 居民本人侧（portal，须电子健康卡号+身份证号双因子核验）：可返回本人明文。

新增返回身份证号/电话的接口必须复用本模块，禁止各自实现。

## 这条纪律由谁盯着（三道闸门，判定各不相同，别合并）

本仓库对同一批 PII 列有三道静态闸门，**分工是按"数据往哪儿走"切的**：

| 闸门 | 守的方向 | 判据 |
|---|---|---|
| `tests/test_pii_query_point_guard.py` | **检索入口**（值进 WHERE） | 加密列等值比较必须走 `pii_filter`/`pii_index_match` |
| `tests/test_pii_output_masking_guard.py` | **响应出口**（值进 HTTP body） | 响应里带 PII 的端点必须经本模块脱敏 |
| `tests/test_log_pii_leak_guard.py` | **日志出口**（值进 stdout/留存文件） | 日志行不得含手机号/验证码明文 |

三者的分母各自从代码结构推导（前两者都从 `EncryptedPII` 列类型推导列名，
第三者从真实日志字节判定），**互不重复判定**：检索闸门不看响应体，
出口闸门不看 WHERE 子句。改本模块时三道都要顾到。

`visible_id_card` / `visible_phone` 是"按角色决定明文还是掩码"的唯一入口——
在它们之前，同一句 `x if user.role == "admin" else mask_x(x)` 在 certs / integration
各抄了一遍，慢专病侧则干脆没抄（于是明文出网）。
"""
from .models import Patient, User
from .schemas import PatientOut


def mask_id_card(value: str) -> str:
    """身份证号脱敏：保留前4后4。"""
    if len(value) <= 8:
        return value
    return value[:4] + "*" * (len(value) - 8) + value[-4:]


def mask_phone(value: str) -> str:
    """电话号码脱敏：保留前3后2。"""
    if len(value) <= 5:
        return value
    return value[:3] + "*" * (len(value) - 5) + value[-2:]


def visible_id_card(value: str, user: User) -> str:
    """按角色决定身份证号出口形态：admin 明文，其余一律掩码。

    与 `desensitize` 同口径，供"响应不是 `PatientOut`、只夹带一两个字段"的出口用
    （慢专病侧的任务/随访/档案聚合视图都是这个形状）。
    """
    return value if user.role == "admin" else mask_id_card(value or "")


def visible_phone(value: str, user: User) -> str:
    """按角色决定电话出口形态：admin 明文，其余一律掩码。"""
    return value if user.role == "admin" else mask_phone(value or "")


def desensitize(patient: Patient, user: User) -> PatientOut:
    """敏感字段脱敏：非 admin 角色返回掩码后的身份证号与电话。"""
    out = PatientOut.model_validate(patient)
    if user.role != "admin":
        out.id_card = mask_id_card(out.id_card)
        out.phone = mask_phone(out.phone)
    return out
