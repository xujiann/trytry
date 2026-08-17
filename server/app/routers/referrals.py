"""双向转诊：医共体内上转/下转申请、接诊、结案、退回的状态流转。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import Organization, Patient, Referral, User
from ..visibility import visible_org_ids
from ..schemas import ReferralCreate, ReferralOut, ReferralStatusUpdate

router = APIRouter(
    prefix="/api/referrals", tags=["双向转诊"], dependencies=[Depends(get_current_user)]
)

_ALLOWED_TRANSITIONS = {
    "pending": {"accepted", "rejected"},
    # E3：接诊后允许退回（材料不全/需补检），退回后可再次接诊
    "accepted": {"completed", "returned"},
    "returned": {"accepted", "rejected"},
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
    return referral


@router.get("", response_model=list[ReferralOut])
def list_referrals(
    status: str | None = None, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(Referral)
    if status:
        query = query.filter(Referral.status == status)
    # 转诊天然跨机构：按"转出方或转入方任一为本机构"过滤
    orgs = visible_org_ids(db, user)
    if orgs is not None:
        query = query.filter(
            Referral.from_org_id.in_(orgs) | Referral.to_org_id.in_(orgs)
        )
    return query.order_by(Referral.id.desc()).limit(200).all()


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
    return referral
