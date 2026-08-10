"""⑲医保业务协同：转诊证明、本地/异地结算记录、特殊病种申报、基金监测。"""
import secrets

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import (
    InsuranceSettlement,
    Organization,
    Patient,
    Referral,
    ReferralCert,
    SpecialDiseaseApp,
)

router = APIRouter(prefix="/api/insurance", tags=["医保协同"], dependencies=[Depends(get_current_user)])


class SettlementCreate(BaseModel):
    patient_id: int
    org_id: int
    settle_type: str = Field(default="local", pattern="^(local|remote)$")
    total_amount: float = Field(gt=0)
    insurance_pay: float = Field(ge=0)
    self_pay: float = Field(ge=0)


class SettlementOut(SettlementCreate):
    id: int

    model_config = {"from_attributes": True}


@router.post(
    "/settlements",
    response_model=SettlementOut,
    status_code=201,
    dependencies=[Depends(require_roles("operator"))],  # H2: 医保结算=经办
)
def create_settlement(body: SettlementCreate, db: Session = Depends(get_db)):
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    if round(body.insurance_pay + body.self_pay, 2) != round(body.total_amount, 2):
        raise HTTPException(status_code=422, detail="医保支付与自付之和须等于总额")
    settlement = InsuranceSettlement(**body.model_dump())
    db.add(settlement)
    db.commit()
    db.refresh(settlement)
    return settlement


@router.get("/settlements", response_model=list[SettlementOut])
def list_settlements(patient_id: int | None = None, db: Session = Depends(get_db)):
    query = db.query(InsuranceSettlement)
    if patient_id is not None:
        query = query.filter(InsuranceSettlement.patient_id == patient_id)
    return query.order_by(InsuranceSettlement.id.desc()).limit(200).all()


@router.post(
    "/referral-certs/{referral_id}",
    dependencies=[Depends(require_roles("operator"))],  # H2: 转诊证明签发=经办
)
def issue_referral_cert(referral_id: int, db: Session = Depends(get_db)):
    """转诊证明：仅对已接诊/已结案的转诊签发，幂等返回既有证明。"""
    referral = db.get(Referral, referral_id)
    if referral is None:
        raise HTTPException(status_code=404, detail="转诊记录不存在")
    if referral.status not in ("accepted", "completed"):
        raise HTTPException(status_code=409, detail="转诊尚未接诊，不可签发证明")
    existing = db.query(ReferralCert).filter(ReferralCert.referral_id == referral_id).first()
    if existing:
        return {"cert_no": existing.cert_no, "referral_id": referral_id}
    cert = ReferralCert(referral_id=referral_id, cert_no="ZZ" + secrets.token_hex(5).upper())
    db.add(cert)
    db.commit()
    return {"cert_no": cert.cert_no, "referral_id": referral_id}


class SpecialDiseaseCreate(BaseModel):
    patient_id: int
    disease_name: str = Field(min_length=1)
    reason: str = ""


class SpecialDiseaseOut(SpecialDiseaseCreate):
    id: int
    status: str

    model_config = {"from_attributes": True}


@router.post(
    "/special-diseases",
    response_model=SpecialDiseaseOut,
    status_code=201,
    dependencies=[Depends(require_roles("operator", "doctor"))],  # H2: 特病申报
)
def apply_special_disease(body: SpecialDiseaseCreate, db: Session = Depends(get_db)):
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    app_ = SpecialDiseaseApp(**body.model_dump())
    db.add(app_)
    db.commit()
    db.refresh(app_)
    return app_


@router.get("/special-diseases", response_model=list[SpecialDiseaseOut])
def list_special_diseases(status: str | None = None, db: Session = Depends(get_db)):
    query = db.query(SpecialDiseaseApp)
    if status:
        query = query.filter(SpecialDiseaseApp.status == status)
    return query.order_by(SpecialDiseaseApp.id.desc()).limit(200).all()


@router.post(
    "/special-diseases/{app_id}/review",
    response_model=SpecialDiseaseOut,
    dependencies=[Depends(require_roles("director", "operator"))],
)
def review_special_disease(app_id: int, approve: bool, db: Session = Depends(get_db)):
    app_ = db.get(SpecialDiseaseApp, app_id)
    if app_ is None:
        raise HTTPException(status_code=404, detail="申报不存在")
    if app_.status != "applied":
        raise HTTPException(status_code=409, detail="该申报已处理")
    app_.status = "approved" if approve else "rejected"
    db.commit()
    db.refresh(app_)
    return app_


@router.get("/fund-stats")
def fund_stats(db: Session = Depends(get_db)):
    """基金监测：县域内/基层医保支出占比（监测指标8、9口径）。"""
    total = db.query(func.coalesce(func.sum(InsuranceSettlement.insurance_pay), 0.0)).scalar()
    grassroots = (
        db.query(func.coalesce(func.sum(InsuranceSettlement.insurance_pay), 0.0))
        .join(Organization, InsuranceSettlement.org_id == Organization.id)
        .filter(Organization.level.in_(["township", "village"]))
        .scalar()
    )
    local = (
        db.query(func.coalesce(func.sum(InsuranceSettlement.insurance_pay), 0.0))
        .filter(InsuranceSettlement.settle_type == "local")
        .scalar()
    )

    def pct(part: float, whole: float) -> float:
        return round(part * 100.0 / whole, 2) if whole else 0.0

    return {
        "insurance_pay_total": round(total, 2),
        "local_ratio_pct": pct(local, total),
        "grassroots_ratio_pct": pct(grassroots, total),
    }
