"""集中审方中心："系统+药师"双重审方，每方必审。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_admin, require_roles
from ..models import DrugRule, Organization, Patient, Prescription, PrescriptionItem, User
from ..schemas import (
    DrugRuleCreate,
    DrugRuleOut,
    PrescriptionCreate,
    PrescriptionOut,
    PrescriptionReview,
)

router = APIRouter(prefix="/api/prescriptions", tags=["集中审方"])


@router.post("/rules", response_model=DrugRuleOut, status_code=201, dependencies=[Depends(require_admin)])
def create_rule(body: DrugRuleCreate, db: Session = Depends(get_db)):
    if db.query(DrugRule).filter(DrugRule.drug_code == body.drug_code).first():
        raise HTTPException(status_code=409, detail="该药品规则已存在")
    rule = DrugRule(**body.model_dump())
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@router.get("/rules", response_model=list[DrugRuleOut], dependencies=[Depends(get_current_user)])
def list_rules(db: Session = Depends(get_db)):
    return db.query(DrugRule).order_by(DrugRule.drug_code).all()


@router.post("", response_model=PrescriptionOut, status_code=201)
def create_prescription(
    body: PrescriptionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")

    violations: list[str] = []
    names_by_code = {item.drug_code: item.drug_name for item in body.items}
    seen_pairs: set[frozenset[str]] = set()
    for item in body.items:
        rule = db.query(DrugRule).filter(DrugRule.drug_code == item.drug_code).first()
        if rule is None:
            continue
        if item.daily_dose > rule.max_daily_dose:
            violations.append(
                f"{item.drug_name} 日剂量 {item.daily_dose}{rule.dose_unit} 超过上限 "
                f"{rule.max_daily_dose}{rule.dose_unit}"
            )
        # 相互作用审查：同一处方内出现冲突药对 → 转药师审并注明
        conflict_codes = {c.strip() for c in rule.interactions.split(",") if c.strip()}
        for other_code in conflict_codes & set(names_by_code) - {item.drug_code}:
            pair = frozenset((item.drug_code, other_code))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            violations.append(
                f"药物相互作用：{item.drug_name} 与 {names_by_code[other_code]} 存在相互作用，需药师人工审核"
            )

    prescription = Prescription(
        patient_id=body.patient_id,
        org_id=body.org_id,
        diagnosis_name=body.diagnosis_name,
        status="pending_review" if violations else "auto_passed",
        review_comment="；".join(violations),
        created_by=user.id,
    )
    db.add(prescription)
    db.flush()
    for item in body.items:
        db.add(PrescriptionItem(prescription_id=prescription.id, **item.model_dump()))
    db.commit()
    db.refresh(prescription)
    return prescription


@router.get("", response_model=list[PrescriptionOut], dependencies=[Depends(get_current_user)])
def list_prescriptions(status: str | None = None, db: Session = Depends(get_db)):
    query = db.query(Prescription)
    if status:
        query = query.filter(Prescription.status == status)
    return query.order_by(Prescription.id.desc()).limit(200).all()


@router.post(
    "/{prescription_id}/review",
    response_model=PrescriptionOut,
    dependencies=[Depends(require_roles("pharmacist"))],
)
def review_prescription(prescription_id: int, body: PrescriptionReview, db: Session = Depends(get_db)):
    prescription = db.get(Prescription, prescription_id)
    if prescription is None:
        raise HTTPException(status_code=404, detail="处方不存在")
    if prescription.status != "pending_review":
        raise HTTPException(status_code=409, detail=f"当前状态 {prescription.status} 无需药师审核")
    prescription.status = "approved" if body.approve else "rejected"
    if body.comment:
        prescription.review_comment = (
            f"{prescription.review_comment}；药师意见：{body.comment}"
            if prescription.review_comment
            else f"药师意见：{body.comment}"
        )
    db.commit()
    db.refresh(prescription)
    return prescription
