"""远程会诊中心：申请→受理→出具意见→评价，全过程管理。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..visibility import assert_org_writable
from ..concurrency import insert_or_conflict
from ..database import get_db
from ..deps import get_current_user, require_admin, require_roles
from ..models import ConsultExpert, Consultation, Organization, Patient, User
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


class ConsultationFee(BaseModel):
    fee: float = Field(ge=0)
    fee_note: str = Field(default="", max_length=256)


@router.post(
    "/{consultation_id}/fee",
    dependencies=[Depends(require_roles("operator", "director"))],  # H2: 计费=经办/管理层
)
def settle_fee(consultation_id: int, body: ConsultationFee, db: Session = Depends(get_db)):
    """会诊计费（指引⑤"费用管理"）。

    只有已完成的会诊可计费——拒绝与未受理的会诊没有发生服务。
    `fee=0` 与"未计费"是两回事（本院内部会诊常不计费），故用 `fee_settled`
    区分而不是拿 0 当哨兵。
    """
    consultation = _get(db, consultation_id)
    if consultation.status != "completed":
        raise HTTPException(status_code=409, detail="仅已完成的会诊可计费")
    consultation.fee = body.fee
    consultation.fee_note = body.fee_note
    consultation.fee_settled = True
    db.commit()
    db.refresh(consultation)
    return {
        "id": consultation.id,
        "fee": consultation.fee,
        "fee_settled": consultation.fee_settled,
        "fee_note": consultation.fee_note,
    }


@router.get("/stats")
def consultation_stats(db: Session = Depends(get_db)):
    """会诊统计（指引⑤"统计分析"）：量、时效、评价与费用。

    评分均值只算**已评价**的（rating>0）：把未评价当 0 分，会让评价率越低
    分数越难看，最后逼出来的是"催评分"而不是"改服务"。
    """
    rows = db.query(Consultation).all()
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    rated = [r.rating for r in rows if r.rating > 0]
    settled = [r for r in rows if r.fee_settled]
    completed = by_status.get("completed", 0)
    applied_total = len(rows)
    return {
        "total": applied_total,
        "by_status": by_status,
        "completion_rate_pct": (
            round(completed * 100 / applied_total, 2) if applied_total else None
        ),
        "rating": {
            "rated_count": len(rated),
            # 未评价单列，不并进均值也不当 0 分
            "unrated_count": completed - len(rated),
            "avg": round(sum(rated) / len(rated), 2) if rated else None,
        },
        "fee": {
            "settled_count": len(settled),
            # 已完成但未计费的单列——可能是内部会诊不计费，也可能是漏计
            "unsettled_count": completed - len(settled),
            "total_amount": round(sum(r.fee for r in settled), 2),
        },
        "caliber": "评分均值只算已评价的（rating>0），未评价单列；"
                   "fee=0 与未计费是两回事，后者看 fee_settled",
    }


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

# ---------------------------------------------------------------- ADR-0006 搬家
#
# 以下自 `service_extras.py`（倾倒场）搬入：会诊专家档案。
# 路径一字未改（`/api/consultations...` 原样），两边 router 的鉴权本就一致
# （都是 `dependencies=[Depends(get_current_user)]`），故可直接并入本模块的
# router——不像 ADR-0006 第一批的 `/api/performance` 那样存在鉴权分裂。


# ---- ⑤ 会诊专家管理 ----


class ExpertCreate(BaseModel):
    name: str = Field(min_length=1)
    org_id: int
    specialty: str = ""
    available: bool = True


@router.post("/experts", status_code=201, dependencies=[Depends(require_admin)])
def create_expert(body: ExpertCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    if db.query(ConsultExpert).filter(ConsultExpert.name == body.name).first():
        raise HTTPException(status_code=409, detail="专家已存在")
    e = insert_or_conflict(db, ConsultExpert(**body.model_dump()), "专家已存在")
    return {"id": e.id}


@router.get("/experts")
def list_experts(available: bool | None = None, db: Session = Depends(get_db)):
    q = db.query(ConsultExpert)
    if available is not None:
        q = q.filter(ConsultExpert.available.is_(available))
    return [{"id": e.id, "name": e.name, "org_id": e.org_id, "specialty": e.specialty, "available": e.available} for e in q.all()]