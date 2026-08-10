"""远程会诊中心：申请→受理→出具意见→评价，全过程管理。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import Consultation, Organization, Patient, User
from ..schemas import (
    ConsultationAccept,
    ConsultationComplete,
    ConsultationCreate,
    ConsultationOut,
    ConsultationRate,
)

router = APIRouter(prefix="/api/consultations", tags=["远程会诊"], dependencies=[Depends(get_current_user)])


@router.post(
    "",
    response_model=ConsultationOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "operator"))],  # H2: 会诊申请
)
def apply(body: ConsultationCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    for org_id, label in ((body.from_org_id, "申请"), (body.to_org_id, "受邀")):
        if db.get(Organization, org_id) is None:
            raise HTTPException(status_code=404, detail=f"{label}机构不存在")
    if body.from_org_id == body.to_org_id:
        raise HTTPException(status_code=422, detail="申请与受邀机构不能相同")
    consultation = Consultation(**body.model_dump(), created_by=user.id)
    db.add(consultation)
    db.commit()
    db.refresh(consultation)
    return consultation


@router.get("", response_model=list[ConsultationOut])
def list_consultations(status: str | None = None, db: Session = Depends(get_db)):
    query = db.query(Consultation)
    if status:
        query = query.filter(Consultation.status == status)
    return query.order_by(Consultation.id.desc()).limit(200).all()


def _get(db: Session, consultation_id: int) -> Consultation:
    consultation = db.get(Consultation, consultation_id)
    if consultation is None:
        raise HTTPException(status_code=404, detail="会诊申请不存在")
    return consultation


@router.post(
    "/{consultation_id}/accept",
    response_model=ConsultationOut,
    dependencies=[Depends(require_roles("doctor"))],  # H2: 受理属诊疗行为
)
def accept(consultation_id: int, body: ConsultationAccept, db: Session = Depends(get_db)):
    consultation = _get(db, consultation_id)
    if consultation.status != "applied":
        raise HTTPException(status_code=409, detail=f"当前状态 {consultation.status} 不可受理")
    consultation.status = "accepted"
    consultation.expert_name = body.expert_name
    db.commit()
    db.refresh(consultation)
    return consultation


@router.post(
    "/{consultation_id}/decline",
    response_model=ConsultationOut,
    dependencies=[Depends(require_roles("doctor"))],  # H2
)
def decline(consultation_id: int, db: Session = Depends(get_db)):
    consultation = _get(db, consultation_id)
    if consultation.status != "applied":
        raise HTTPException(status_code=409, detail=f"当前状态 {consultation.status} 不可拒绝")
    consultation.status = "declined"
    db.commit()
    db.refresh(consultation)
    return consultation


@router.post(
    "/{consultation_id}/complete",
    response_model=ConsultationOut,
    dependencies=[Depends(require_roles("doctor"))],  # H2: 出具会诊意见限医师
)
def complete(consultation_id: int, body: ConsultationComplete, db: Session = Depends(get_db)):
    consultation = _get(db, consultation_id)
    if consultation.status != "accepted":
        raise HTTPException(status_code=409, detail=f"当前状态 {consultation.status} 不可出具意见")
    consultation.status = "completed"
    consultation.opinion = body.opinion
    db.commit()
    db.refresh(consultation)
    return consultation


@router.post(
    "/{consultation_id}/rate",
    response_model=ConsultationOut,
    dependencies=[Depends(require_roles("doctor", "operator"))],  # H2: 评价代录
)
def rate(consultation_id: int, body: ConsultationRate, db: Session = Depends(get_db)):
    consultation = _get(db, consultation_id)
    if consultation.status != "completed":
        raise HTTPException(status_code=409, detail="仅已完成的会诊可评价")
    consultation.rating = body.rating
    db.commit()
    db.refresh(consultation)
    return consultation
