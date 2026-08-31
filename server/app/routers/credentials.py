"""就诊凭据管理（浙江省指南 #27）：发放、回收、作废、挂失换发。

与电子健康卡号（`patients.ehc_no`）的关系，是这个模块唯一需要想清楚的事：

- 健康卡号是**身份**：终身唯一、跨机构通用、不回收、不换号；
- 就诊凭据是**介质**：卡片会丢、二维码会过期、临时凭据用完就该废。

合成一个字段的后果是患者丢一次卡就得换一次身份，历史档案跟着断。所以这里
另立一张表，凭据变更不影响档案归属。

同一患者同时只允许一张有效凭据：挂失换发时旧的必须当场失效，
否则捡到卡的人还能拿它挂号。
"""
import hashlib
import hmac

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..clock import now_naive
from ..concurrency import insert_or_conflict
from ..security import signing_key, verification_keys
from ..visibility import scope_patient_list
from ..database import get_db
from ..deps import get_current_user, paginate, require_roles
from ..models import Patient, User, VisitCredential, utcnow
from ..pii import pii_filter

router = APIRouter(
    prefix="/api/credentials", tags=["就诊凭据"], dependencies=[Depends(get_current_user)]
)

CREDENTIAL_TYPE_NAMES = {"card": "实体就诊卡", "qrcode": "电子二维码", "temp": "临时凭据"}
STATUS_NAMES = {"active": "有效", "recycled": "已回收", "void": "已作废"}


class CredentialIssue(BaseModel):
    patient_id: int
    credential_type: str = Field(default="card", pattern="^(card|qrcode|temp)$")
    # 留空即由系统生成；实体卡常有厂商预印的卡号，允许录入
    credential_no: str = Field(default="", max_length=64)
    # 换发原因（挂失/损坏/到期）。发新卡自动作废旧卡，理由记在旧卡上。
    reason: str = Field(default="", max_length=128)


class CredentialClose(BaseModel):
    reason: str = Field(default="", max_length=128)


def _credential_out(c: VisitCredential) -> dict:
    return {
        "id": c.id,
        "patient_id": c.patient_id,
        "credential_no": c.credential_no,
        "credential_type": c.credential_type,
        "credential_type_name": CREDENTIAL_TYPE_NAMES.get(c.credential_type, c.credential_type),
        "status": c.status,
        "status_name": STATUS_NAMES.get(c.status, c.status),
        "issued_at": c.issued_at.isoformat(),
        "closed_at": c.closed_at.isoformat() if c.closed_at else None,
        "close_reason": c.close_reason,
    }


class CredentialOut(BaseModel):
    """字段与顺序精确镜像 `_credential_out`（列表行与回收/作废回执）。"""

    id: int
    patient_id: int
    credential_no: str
    credential_type: str
    credential_type_name: str
    status: str
    status_name: str
    #: DateTime 列的 isoformat 字符串
    issued_at: str
    closed_at: str | None
    close_reason: str


class CredentialIssueOut(CredentialOut):
    """发放回执 = 列表行 + 本次被自动作废的旧凭据号（挂失换发语义；首发为空列表，键恒在）。"""

    superseded: list[str]


class CredentialPatientBriefOut(BaseModel):
    id: int
    name: str
    ehc_no: str


class CredentialLookupOut(CredentialOut):
    """核验回执 = 列表行 + valid + 持有人摘要（档案缺失时为 null）。"""

    valid: bool
    patient: CredentialPatientBriefOut | None


def _generate_no(db: Session, patient: Patient, credential_type: str) -> str:
    """系统生成凭据号：健康卡号 + 类型 + 序号。

    带上健康卡号是为了肉眼可追溯到人；带序号是因为同一人换发多次，
    只用卡号会撞唯一约束。
    """
    seq = db.query(VisitCredential).filter(VisitCredential.patient_id == patient.id).count() + 1
    return f"{patient.ehc_no}-{credential_type[:1].upper()}{seq:02d}"


@router.post(
    "", status_code=201, response_model=CredentialIssueOut,
    dependencies=[Depends(require_roles("operator", "doctor"))],
)
def issue_credential(
    body: CredentialIssue, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """发放凭据。该患者原有的有效凭据自动作废——挂失换发的正确语义。"""
    patient = db.get(Patient, body.patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    credential_no = body.credential_no.strip() or _generate_no(db, patient, body.credential_type)
    if db.query(VisitCredential).filter(VisitCredential.credential_no == credential_no).first():
        raise HTTPException(status_code=409, detail="该凭据号已存在")

    superseded = (
        db.query(VisitCredential)
        .filter(VisitCredential.patient_id == patient.id, VisitCredential.status == "active")
        .all()
    )
    for old in superseded:
        old.status = "void"
        old.closed_at = utcnow()
        old.close_reason = body.reason or "换发新凭据"

    # 作废旧凭据与发放新凭据同一次提交：撞了凭据号唯一约束就一起回滚，
    # 否则会出现"旧的作废了、新的没发出来"，患者手上一张能用的都没有。
    credential = insert_or_conflict(
        db,
        VisitCredential(
            patient_id=patient.id,
            credential_no=credential_no,
            credential_type=body.credential_type,
            issued_by=user.id,
            org_id=user.org_id,
        ),
        "该凭据号已存在",
    )
    result = _credential_out(credential)
    result["superseded"] = [c.credential_no for c in superseded]
    return result


@router.get("", response_model=list[CredentialOut])
def list_credentials(
    response: Response,
    patient_id: int | None = None,
    status: str | None = None,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    query = db.query(VisitCredential)
    query = scope_patient_list(db, user, query, VisitCredential, patient_id, "credential")
    if status:
        query = query.filter(VisitCredential.status == status)
    rows = paginate(query.order_by(VisitCredential.id.desc()), response, offset, limit)
    return [_credential_out(c) for c in rows]


@router.get("/lookup/{credential_no}", response_model=CredentialLookupOut)
def lookup(credential_no: str, db: Session = Depends(get_db)):
    """凭据核验：刷卡/扫码时用，返回持有人与是否有效。

    失效凭据也返回（附状态），不按 404 处理——窗口人员需要知道
    "这张卡作废了"，而不是"查无此卡"，两者的处置完全不同。
    """
    credential = (
        db.query(VisitCredential)
        .filter(VisitCredential.credential_no == credential_no)
        .first()
    )
    if credential is None:
        raise HTTPException(status_code=404, detail="凭据不存在")
    patient = db.get(Patient, credential.patient_id)
    result = _credential_out(credential)
    result["valid"] = credential.status == "active"
    result["patient"] = (
        {"id": patient.id, "name": patient.name, "ehc_no": patient.ehc_no} if patient else None
    )
    return result


@router.post(
    "/{credential_id}/recycle", response_model=CredentialOut,
    dependencies=[Depends(require_roles("operator"))],
)
def recycle(credential_id: int, body: CredentialClose, db: Session = Depends(get_db)):
    """回收：患者主动交回实体卡。与作废分开记——回收是正常结束，作废是异常终止，
    统计报损率时必须区分。"""
    return _close(db, credential_id, "recycled", body.reason or "患者交回")


@router.post(
    "/{credential_id}/void", response_model=CredentialOut,
    dependencies=[Depends(require_roles("operator", "doctor"))],
)
def void(credential_id: int, body: CredentialClose, db: Session = Depends(get_db)):
    """作废：挂失、损坏、盗用嫌疑。作废后该凭据立即不可用于核验。"""
    if not body.reason:
        raise HTTPException(status_code=422, detail="作废须填写原因")
    return _close(db, credential_id, "void", body.reason)


def _close(db: Session, credential_id: int, status: str, reason: str) -> dict:
    credential = db.get(VisitCredential, credential_id)
    if credential is None:
        raise HTTPException(status_code=404, detail="凭据不存在")
    if credential.status != "active":
        raise HTTPException(
            status_code=409, detail=f"凭据当前状态为{STATUS_NAMES.get(credential.status)}，不可再操作"
        )
    credential.status = status
    credential.closed_at = utcnow()
    credential.close_reason = reason
    db.commit()
    return _credential_out(credential)


# ---------------------------------------------------------------- ⑧ 一码通


class OneCodeIssue(BaseModel):
    patient_id: int
    # 有效期秒数。默认 60 秒——一码通的动态码就是靠短时效防截屏盗用，
    # 给得太长等于回到静态码。
    ttl_seconds: int = Field(default=60, ge=10, le=600)


class OneCodeResolve(BaseModel):
    code: str = Field(min_length=1, max_length=256)


def _one_code_payload(patient: Patient, expires_at: int) -> str:
    return f"{patient.ehc_no}.{expires_at}"


def _sign(payload: str, key: bytes | None = None) -> str:
    """动态码签名。签发一律用 qr 用途的派生子密钥（A1）；
    `key` 仅供核验时按候选密钥逐个复算。"""
    return hmac.new(
        key if key is not None else signing_key("qr"), payload.encode(), hashlib.sha256
    ).hexdigest()[:32]


def _signature_valid(payload: str, signature: str) -> bool:
    """核验动态码签名：多口径回退（新派生 → 旧原始 → previous 两口径），
    密钥轮换的换轮瞬间，已出示未核销的码不失效。"""
    return any(
        hmac.compare_digest(_sign(payload, key), signature)
        for key in verification_keys("qr")
    )


class OneCodeIssueOut(BaseModel):
    #: 自包含串「健康卡号.过期时刻.签名」，不落库
    code: str
    ehc_no: str
    expires_in: int
    note: str


@router.post(
    "/one-code",
    response_model=OneCodeIssueOut,
    dependencies=[Depends(require_roles("operator", "doctor", "public_health"))],
)
def issue_one_code(body: OneCodeIssue, db: Session = Depends(get_db)):
    """签发一码通动态码（指引⑧"一码通生成服务"）。

    码本身**不落库**：它是 `健康卡号.过期时刻.签名` 的自包含串，与平台的
    JWT 同一思路。落库的话每扫一次码就要写一行、还要清理过期行，
    而它的生命周期只有一分钟。

    签名用平台密钥的 qr 派生子密钥（A1）：换密钥并清掉 MEDPLAT_SECRET_PREVIOUS
    即全部作废；轮换宽限期内（previous 已设）旧码仍可核验，换轮瞬间不失效。
    """
    patient = db.get(Patient, body.patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    expires_at = int(now_naive().timestamp()) + body.ttl_seconds
    payload = _one_code_payload(patient, expires_at)
    return {
        "code": f"{payload}.{_sign(payload)}",
        "ehc_no": patient.ehc_no,
        "expires_in": body.ttl_seconds,
        "note": "动态码，过期即失效；不落库，换平台密钥即全部作废",
    }


class OneCodeResolveOut(BaseModel):
    patient_id: int
    name: str
    ehc_no: str
    remaining_seconds: int


@router.post("/one-code/resolve", response_model=OneCodeResolveOut)
def resolve_one_code(body: OneCodeResolve, db: Session = Depends(get_db)):
    """核验一码通动态码。过期与签名错误分别报出——前者让人重新出码，
    后者是伪造，处置完全不同。"""
    parts = body.code.split(".")
    if len(parts) != 3:
        raise HTTPException(status_code=422, detail="码格式不正确")
    ehc_no, expires_raw, signature = parts
    payload = f"{ehc_no}.{expires_raw}"
    if not _signature_valid(payload, signature):
        raise HTTPException(status_code=403, detail="码签名校验失败（可能为伪造）")
    try:
        expires_at = int(expires_raw)
    except ValueError:
        raise HTTPException(status_code=422, detail="码格式不正确") from None
    if expires_at < int(now_naive().timestamp()):
        raise HTTPException(status_code=410, detail="该码已过期，请重新出码")
    patient = db.query(Patient).filter(Patient.ehc_no == ehc_no).first()
    if patient is None:
        raise HTTPException(status_code=404, detail="健康卡号对应的患者不存在")
    return {
        "patient_id": patient.id,
        "name": patient.name,
        "ehc_no": patient.ehc_no,
        "remaining_seconds": expires_at - int(now_naive().timestamp()),
    }


class IdentityResolveOut(BaseModel):
    """多卡（码）协同命中回执。

    `credential_status` 是**条件键**（docs/接口标准与治理.md 陷阱二）：只在命中
    实体凭据号那一支出现，健康卡号/身份证分支整个不出现（不是 null）——
    端点带 `response_model_exclude_unset=True`。
    """

    matched_by: str
    credential_status: str | None = None
    valid: bool
    patient: CredentialPatientBriefOut | None


@router.get("/resolve", response_model=IdentityResolveOut, response_model_exclude_unset=True)
def resolve_any(identifier: str, db: Session = Depends(get_db)):
    """多卡（码）协同：一个入口认全部身份标识（指引⑧"多卡(码)协同应用"）。

    窗口人员手上可能是实体卡号、电子二维码、健康卡号或身份证号，
    原先要记住"这个查这个接口、那个查那个接口"。这里按**从具体到一般**
    的顺序依次尝试，并在返回里注明命中的是哪一类——不注明的话，
    同一个人从不同介质进来会看不出差别。
    """
    identifier = identifier.strip()
    credential = (
        db.query(VisitCredential).filter(VisitCredential.credential_no == identifier).first()
    )
    if credential is not None:
        patient = db.get(Patient, credential.patient_id)
        return {
            "matched_by": "credential_no",
            "credential_status": credential.status,
            "valid": credential.status == "active",
            "patient": _patient_brief(patient),
        }
    patient = db.query(Patient).filter(Patient.ehc_no == identifier).first()
    if patient is not None:
        return {"matched_by": "ehc_no", "valid": True, "patient": _patient_brief(patient)}
    patient = db.query(Patient).filter(pii_filter(Patient.id_card_idx, Patient.id_card, identifier)).first()
    if patient is not None:
        return {"matched_by": "id_card", "valid": True, "patient": _patient_brief(patient)}
    raise HTTPException(status_code=404, detail="未匹配到任何身份标识（卡号/健康卡号/身份证号）")


def _patient_brief(patient: Patient | None) -> dict | None:
    if patient is None:
        return None
    return {"id": patient.id, "name": patient.name, "ehc_no": patient.ehc_no}
