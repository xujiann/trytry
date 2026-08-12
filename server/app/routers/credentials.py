"""就诊凭据管理（浙江省指南 #27）：发放、回收、作废、挂失换发。

与电子健康卡号（`patients.ehc_no`）的关系，是这个模块唯一需要想清楚的事：

- 健康卡号是**身份**：终身唯一、跨机构通用、不回收、不换号；
- 就诊凭据是**介质**：卡片会丢、二维码会过期、临时凭据用完就该废。

合成一个字段的后果是患者丢一次卡就得换一次身份，历史档案跟着断。所以这里
另立一张表，凭据变更不影响档案归属。

同一患者同时只允许一张有效凭据：挂失换发时旧的必须当场失效，
否则捡到卡的人还能拿它挂号。
"""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, paginate, require_roles
from ..models import Patient, User, VisitCredential, utcnow

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


def _generate_no(db: Session, patient: Patient, credential_type: str) -> str:
    """系统生成凭据号：健康卡号 + 类型 + 序号。

    带上健康卡号是为了肉眼可追溯到人；带序号是因为同一人换发多次，
    只用卡号会撞唯一约束。
    """
    seq = db.query(VisitCredential).filter(VisitCredential.patient_id == patient.id).count() + 1
    return f"{patient.ehc_no}-{credential_type[:1].upper()}{seq:02d}"


@router.post("", status_code=201, dependencies=[Depends(require_roles("operator", "doctor"))])
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

    credential = VisitCredential(
        patient_id=patient.id,
        credential_no=credential_no,
        credential_type=body.credential_type,
        issued_by=user.id,
    )
    db.add(credential)
    db.commit()
    result = _credential_out(credential)
    result["superseded"] = [c.credential_no for c in superseded]
    return result


@router.get("")
def list_credentials(
    response: Response,
    patient_id: int | None = None,
    status: str | None = None,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    query = db.query(VisitCredential)
    if patient_id is not None:
        query = query.filter(VisitCredential.patient_id == patient_id)
    if status:
        query = query.filter(VisitCredential.status == status)
    rows = paginate(query.order_by(VisitCredential.id.desc()), response, offset, limit)
    return [_credential_out(c) for c in rows]


@router.get("/lookup/{credential_no}")
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


@router.post("/{credential_id}/recycle", dependencies=[Depends(require_roles("operator"))])
def recycle(credential_id: int, body: CredentialClose, db: Session = Depends(get_db)):
    """回收：患者主动交回实体卡。与作废分开记——回收是正常结束，作废是异常终止，
    统计报损率时必须区分。"""
    return _close(db, credential_id, "recycled", body.reason or "患者交回")


@router.post("/{credential_id}/void", dependencies=[Depends(require_roles("operator", "doctor"))])
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
