"""E3 双向转诊闭环深化：病历随转、下转康复计划/回复单、转诊号源预留。

转诊天然跨机构：这里所有写操作要求操作者是本转诊的**相关方**（转出或转入机构可写），
读操作要求相关方可见——沿用 referrals.py「from_org 或 to_org 任一为本机构」的口径。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import (
    AppointmentSlot,
    Patient,
    Referral,
    ReferralClinicalRef,
    ReferralPlan,
    ReferralSlotReservation,
    User,
)
from ..routers.appointments import book_slot
from ..visibility import assert_org_writable, visible_org_ids

router = APIRouter(prefix="/api/referrals", tags=["转诊闭环"],
                   dependencies=[Depends(get_current_user)])


# ---------------------------------------------------------------- schemas
class ClinicalRefIn(BaseModel):
    ref_type: str = Field(pattern="^(encounter|exam_report|prescription|summary)$")
    ref_id: int = 0
    summary: str = Field(default="", max_length=1024)


class PlanIn(BaseModel):
    plan_type: str = Field(pattern="^(rehab_plan|reply)$")
    content: str = Field(min_length=1, max_length=2048)


class ReserveIn(BaseModel):
    slot_id: int


# ---------------------------------------------------------------- helpers
def _referral_or_404(db: Session, referral_id: int) -> Referral:
    ref = db.get(Referral, referral_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="转诊记录不存在")
    return ref


def _assert_party_write(db: Session, user: User, ref: Referral) -> None:
    """操作者须能写本转诊的转出或转入机构之一（全域角色恒通过）。"""
    for oid in (ref.from_org_id, ref.to_org_id):
        try:
            assert_org_writable(db, user, oid)
            return
        except HTTPException:
            continue
    raise HTTPException(status_code=403, detail="非本转诊相关机构，无权操作")


def _assert_party_read(db: Session, user: User, ref: Referral) -> None:
    vis = visible_org_ids(db, user)
    if vis is None:
        return
    if ref.from_org_id not in vis and ref.to_org_id not in vis:
        raise HTTPException(status_code=403, detail="非本转诊相关机构，无权查看")


# ---------------------------------------------------------------- 病历随转
@router.post("/{referral_id}/clinical-refs", status_code=201,
             dependencies=[Depends(require_roles("doctor", "operator"))])
def add_clinical_ref(referral_id: int, body: ClinicalRefIn, db: Session = Depends(get_db),
                     user: User = Depends(get_current_user)):
    ref = _referral_or_404(db, referral_id)
    _assert_party_write(db, user, ref)
    row = ReferralClinicalRef(referral_id=referral_id, created_by=user.id, **body.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return _cref_out(row)


@router.get("/{referral_id}/clinical-refs")
def list_clinical_refs(referral_id: int, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user)):
    ref = _referral_or_404(db, referral_id)
    _assert_party_read(db, user, ref)
    rows = db.query(ReferralClinicalRef).filter(
        ReferralClinicalRef.referral_id == referral_id).order_by(ReferralClinicalRef.id).all()
    return [_cref_out(r) for r in rows]


def _cref_out(r: ReferralClinicalRef) -> dict:
    return {"id": r.id, "referral_id": r.referral_id, "ref_type": r.ref_type,
            "ref_id": r.ref_id, "summary": r.summary, "created_by": r.created_by}


# ---------------------------------------------------------------- 下转康复计划/回复单
@router.post("/{referral_id}/plans", status_code=201,
             dependencies=[Depends(require_roles("doctor"))])
def add_plan(referral_id: int, body: PlanIn, db: Session = Depends(get_db),
             user: User = Depends(get_current_user)):
    """rehab_plan=上级下转康复计划；reply=基层接诊回复单。作者机构取操作者本机构。"""
    ref = _referral_or_404(db, referral_id)
    _assert_party_write(db, user, ref)
    if user.org_id is None:
        raise HTTPException(status_code=422, detail="操作者未归属机构，无法出具计划/回复")
    row = ReferralPlan(referral_id=referral_id, plan_type=body.plan_type,
                       content=body.content, org_id=user.org_id, created_by=user.id)
    db.add(row)
    db.commit()
    db.refresh(row)
    return _plan_out(row)


@router.get("/{referral_id}/plans")
def list_plans(referral_id: int, db: Session = Depends(get_db),
               user: User = Depends(get_current_user)):
    ref = _referral_or_404(db, referral_id)
    _assert_party_read(db, user, ref)
    rows = db.query(ReferralPlan).filter(
        ReferralPlan.referral_id == referral_id).order_by(ReferralPlan.id).all()
    return [_plan_out(r) for r in rows]


def _plan_out(r: ReferralPlan) -> dict:
    return {"id": r.id, "referral_id": r.referral_id, "plan_type": r.plan_type,
            "content": r.content, "org_id": r.org_id, "created_by": r.created_by}


# ---------------------------------------------------------------- 转诊号源预留
@router.post("/{referral_id}/reserve-slot", status_code=201,
             dependencies=[Depends(require_roles("doctor", "operator"))])
def reserve_slot(referral_id: int, body: ReserveIn, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """为转诊患者从号源池原子预留一个号（复用 appointments 条件 UPDATE 占位）。

    一条转诊只能预留一个号（唯一约束）；号占满/患者已占同号由 book_slot 兜底。
    """
    ref = _referral_or_404(db, referral_id)
    _assert_party_write(db, user, ref)
    # referral_id 唯一：只要已存在任一预留行（无论状态）就先拦，避免 book_slot
    # 占号后再撞唯一约束、把号占了却没留下预留记录（孤儿预约）。
    if db.query(ReferralSlotReservation).filter(
        ReferralSlotReservation.referral_id == referral_id,
    ).first():
        raise HTTPException(status_code=409, detail="本转诊已预留号源")
    slot = db.get(AppointmentSlot, body.slot_id)
    if slot is None:
        raise HTTPException(status_code=404, detail="号源不存在")
    if db.get(Patient, ref.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    from ..concurrency import insert_or_conflict

    appt = book_slot(db, body.slot_id, ref.patient_id)  # 原子占号，超卖/重复由其内部处理
    # referral_id 唯一：并发下两个预留请求只成一个（另一个 409），避免重复占号留痕
    row = insert_or_conflict(
        db,
        ReferralSlotReservation(
            referral_id=referral_id, slot_id=body.slot_id, appointment_id=appt.id,
            patient_id=ref.patient_id, reserved_by=user.id, status="reserved"),
        "本转诊已预留号源",
    )
    return {"id": row.id, "referral_id": referral_id, "slot_id": body.slot_id,
            "appointment_id": appt.id, "patient_id": ref.patient_id, "status": row.status}


@router.get("/{referral_id}/reservation")
def get_reservation(referral_id: int, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    ref = _referral_or_404(db, referral_id)
    _assert_party_read(db, user, ref)
    row = db.query(ReferralSlotReservation).filter(
        ReferralSlotReservation.referral_id == referral_id).order_by(
        ReferralSlotReservation.id.desc()).first()
    if row is None:
        raise HTTPException(status_code=404, detail="本转诊未预留号源")
    return {"id": row.id, "referral_id": referral_id, "slot_id": row.slot_id,
            "appointment_id": row.appointment_id, "patient_id": row.patient_id,
            "status": row.status}
