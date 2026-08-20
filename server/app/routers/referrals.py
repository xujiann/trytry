"""双向转诊：医共体内上转/下转申请、接诊、结案、退回的状态流转。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import Organization, Patient, Referral, User
from ..schemas import ReferralCreate, ReferralOut, ReferralStatusUpdate

router = APIRouter(
    prefix="/api/referrals", tags=["双向转诊"], dependencies=[Depends(get_current_user)]
)

#: 业务端转诊状态文案。**与居民端刻意不同**：居民端说的是"待接收/已接收/已完成"
#: （见 `routers/portal._PLATFORM_REFERRAL_STATUS`），面向患者；这里是
#: "待接诊/已接诊/已结案"，面向医师。同一个状态、两个读者、两套措辞是对的；
#: 不对的是**同一套措辞在前后端各存一份**——那种复制迟早改一处漏一处。
#: 前端 `static/core.js` 现在只负责配色，文案取自这里。
STATUS_LABELS = {
    "pending": "待接诊",
    "accepted": "已接诊",
    "completed": "已结案",
    "rejected": "已退回",
}


def _with_label(referral: Referral) -> Referral:
    """给 ORM 对象挂上 `status_label` 供响应模型取用（不入库）。"""
    setattr(referral, "status_label", STATUS_LABELS.get(referral.status, referral.status))
    return referral

_ALLOWED_TRANSITIONS = {
    "pending": {"accepted", "rejected"},
    "accepted": {"completed"},
}


@router.post(
    "",
    response_model=ReferralOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "operator"))],  # H2: 转诊申请=医疗岗
)
def create_referral(
    body: ReferralCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    for org_id, label in ((body.from_org_id, "转出"), (body.to_org_id, "转入")):
        if db.get(Organization, org_id) is None:
            raise HTTPException(status_code=404, detail=f"{label}机构不存在")
    if body.from_org_id == body.to_org_id:
        raise HTTPException(status_code=422, detail="转出与转入机构不能相同")
    referral = Referral(**body.model_dump(), created_by=user.id)
    db.add(referral)
    db.commit()
    db.refresh(referral)
    return _with_label(referral)


@router.get("", response_model=list[ReferralOut])
def list_referrals(status: str | None = None, db: Session = Depends(get_db)):
    query = db.query(Referral)
    if status:
        query = query.filter(Referral.status == status)
    return [_with_label(r) for r in query.order_by(Referral.id.desc()).limit(200).all()]


@router.patch(
    "/{referral_id}/status",
    response_model=ReferralOut,
    dependencies=[Depends(require_roles("doctor"))],  # H2: 接诊/结案/退回属诊疗行为，限医师
)
def update_status(referral_id: int, body: ReferralStatusUpdate, db: Session = Depends(get_db)):
    referral = db.get(Referral, referral_id)
    if referral is None:
        raise HTTPException(status_code=404, detail="转诊记录不存在")
    if body.status not in _ALLOWED_TRANSITIONS.get(referral.status, set()):
        raise HTTPException(
            status_code=409, detail=f"状态不可从 {referral.status} 变更为 {body.status}"
        )
    referral.status = body.status
    db.commit()
    db.refresh(referral)
    return _with_label(referral)
