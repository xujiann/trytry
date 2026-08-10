"""公共脱敏模块：所有返回居民敏感字段（身份证号、电话）的出口统一调用。

适用范围（H1 整改）：
- 工作人员侧接口（患者检索/详情、FHIR/HL7 出入站回显、360 视图等）：非 admin 一律掩码；
- 居民本人侧（portal，须电子健康卡号+身份证号双因子核验）：可返回本人明文。

新增返回身份证号/电话的接口必须复用本模块，禁止各自实现。
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


def desensitize(patient: Patient, user: User) -> PatientOut:
    """敏感字段脱敏：非 admin 角色返回掩码后的身份证号与电话。"""
    out = PatientOut.model_validate(patient)
    if user.role != "admin":
        out.id_card = mask_id_card(out.id_card)
        out.phone = mask_phone(out.phone)
    return out
