"""打印件防伪验真：签名不透明令牌 + 最小披露核验（ADR-0015）。

## 为什么是签名令牌，而不是"按单据号查询"

核验的人在**平台外**（另一家医院的窗口、用人单位、保险公司），没有账号；
所以核验端点必须公开。而 CLAUDE.md §8 明令禁止"按 id 直取、不校验归属、
无留痕"的接口——公开 + 按号可查 = 可枚举全院单据的存在性。签名令牌两头都堵住：

- **不可枚举**：令牌含 HMAC 签名，猜不出别人的单据的合法令牌；
- **最小披露**：核验返回的每一个字段（单据类型/编号/机构/日期）都**已经印在
  纸面上**——核验不泄露任何纸上没有的东西，更没有任何患者身份信息。

## 令牌格式

``v1.<b64url(payload)>.<b64url(mac)>``，payload 为紧凑 JSON：
``{"t": doc_type, "i": doc_id, "n": doc_no, "o": org_name, "d": issued_date}``。

- 签名：`gmcrypto.mac`（套件为 sm 时是 HMAC-SM3，与令牌/审计链同一套算法口径）；
  密钥 `security.signing_key("print-verify")`（域分隔派生，不裸用主密钥）。
- 验签走 `security.verification_keys`：**纸会活很多年**，密钥轮换后旧打印件
  必须还验得过（宽限期内 previous 口径兜底，与令牌/审计链同一套轮换纪律）。
- **不设过期**：过期会把真件判成假件——纸面单据没有"过期就不算数"的语义。
  展示字段签在令牌里，篡改任何一个字都验不过；单据的**现势状态**（还在不在、
  同意是否已撤回）由核验时现查数据库补充，不靠令牌自证。
"""
import base64
import hmac
import json
import logging

from sqlalchemy.orm import Session

from . import gmcrypto, security
from .models import (
    Admission,
    ConsentRecord,
    ExamReport,
    ExamRequest,
    MedicalCert,
    PhysicalExam,
    Prescription,
    Referral,
    Settlement,
    VaccinationRecord,
)

logger = logging.getLogger("medplat.printing")

_PURPOSE = "print-verify"
_PREFIX = "v1"

#: doc_type → 存在性核验用的 ORM 模型。**必须与 printing.DOC_TYPES 的键一一对应**
#: （test_print_verify.py 钉住），少一个就是"这种单据印了验真码却永远查无此单"。
#: 三种住院单据（费用清单/病案首页/出院小结）都以住院记录为存在性依据——
#: 打印端点本就以 admission_id 为主键取数。
VERIFY_REGISTRY: dict[str, type] = {
    "exam_report": ExamReport,
    "prescription": Prescription,
    "exam_request": ExamRequest,
    "cert": MedicalCert,
    "inpatient_bill": Admission,
    "settlement": Settlement,
    "case_summary": Admission,
    "checkup_report": PhysicalExam,
    "consent": ConsentRecord,
    "vaccine_cert": VaccinationRecord,
    "referral": Referral,
    "discharge_summary": Admission,
}


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(payload: bytes, key: bytes) -> bytes:
    # 域串进 MAC 输入：同一密钥即便被误用在别处，签出的东西也互不通用
    return gmcrypto.mac(key, b"medplat:print-verify:v1:" + payload)


def make_verify_token(
    doc_type: str, doc_id: int, doc_no: str, org_name: str, issued_date: str
) -> str:
    """打印时生成验真令牌。展示字段一并签进去，核验端不需要按类型逐个取数。"""
    payload = json.dumps(
        {"t": doc_type, "i": doc_id, "n": doc_no, "o": org_name, "d": issued_date},
        ensure_ascii=False, separators=(",", ":"), sort_keys=True,
    ).encode()
    mac = _sign(payload, security.signing_key(_PURPOSE))
    return f"{_PREFIX}.{_b64e(payload)}.{_b64e(mac)}"


def parse_verify_token(token: str) -> dict | None:
    """验签并解出 payload；任何一步不对都返回 None，不区分"哪里不对"。

    多密钥回退（`verification_keys`）：纸面单据活得比密钥久，轮换宽限期内
    旧密钥签的令牌必须仍然有效——与登录令牌、审计链同一条轮换纪律。
    """
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _PREFIX:
        return None
    try:
        payload = _b64d(parts[1])
        given = _b64d(parts[2])
    except Exception:  # noqa: BLE001 - 非法 base64 与非法签名同罪，都是无效令牌
        return None
    for key in security.verification_keys(_PURPOSE):
        if hmac.compare_digest(_sign(payload, key), given):
            break
    else:
        return None
    try:
        data = json.loads(payload)
    except Exception:  # noqa: BLE001 - 签名对但载荷坏，只可能是我们自己的 bug，仍按无效处理
        return None
    if not isinstance(data, dict) or {"t", "i", "n", "o", "d"} - set(data):
        return None
    return data


def verify_document(db: Session, token: str) -> dict:
    """核验一枚令牌，返回统一形状的结果（无条件键，契约友好）。

    三种结局，语义刻意分开：
    - 签名无效 → 假件或抄错：``valid=False``，不透露任何信息；
    - 签名有效但记录已不存在 → 纸是真的、数据已清理/删除：``valid=False`` 但
      给出纸面字段，reason 说清"查无此单"——把它显示成"假件"会冤枉真纸；
    - 签名有效且记录在 → ``valid=True``；知情同意书额外报告**现势状态**
      （已撤回的同意，纸再真也不再代表同意）。
    """
    blank = {
        "valid": False, "reason": "", "doc_type": "", "doc_label": "",
        "doc_no": "", "org_name": "", "issued_date": "", "status": "",
    }
    data = parse_verify_token(token)
    if data is None:
        logger.info("打印件核验失败：令牌无效")
        return {**blank, "reason": "令牌无效：不是本平台签发，或内容被篡改"}
    from .routers.printing import DOC_TYPES  # 局部导入避免环

    model = VERIFY_REGISTRY.get(data["t"])
    label = DOC_TYPES.get(data["t"], data["t"])
    shown = {
        "doc_type": data["t"], "doc_label": label, "doc_no": str(data["n"]),
        "org_name": str(data["o"]), "issued_date": str(data["d"]),
    }
    record = db.get(model, data["i"]) if model is not None else None
    if record is None:
        logger.info("打印件核验：签名有效但记录不存在 type=%s id=%s", data["t"], data["i"])
        return {**blank, **shown,
                "reason": "签发信息有效，但平台已查不到这份单据（可能已按保留期清理）"}
    status = "有效"
    if data["t"] == "consent" and getattr(record, "revoked_at", None) is not None:
        status = "已撤回"
    logger.info("打印件核验通过 type=%s id=%s status=%s", data["t"], data["i"], status)
    return {**blank, **shown, "valid": True, "status": status}
