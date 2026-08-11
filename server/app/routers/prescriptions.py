"""集中审方中心："系统+药师"双重审方，每方必审。"""
from datetime import date

from pydantic import BaseModel, Field
from sqlalchemy import func

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, paginate, require_admin, require_roles
from ..models import (
    DrugRule,
    MaternalRecord,
    Organization,
    Patient,
    Prescription,
    PrescriptionComment,
    PrescriptionItem,
    User,
)
from ..schemas import (
    DrugRuleCreate,
    DrugRuleOut,
    PrescriptionCreate,
    PrescriptionOut,
    PrescriptionReview,
)

router = APIRouter(prefix="/api/prescriptions", tags=["集中审方"])

# 特殊人群年龄界限：儿童 <14 岁，老年 ≥65 岁
CHILD_AGE_LIMIT = 14
ELDERLY_AGE_LIMIT = 65

GROUP_NAMES = {"pregnant": "孕产妇", "child": "儿童", "elderly": "老年人"}


def _age_of(birth_date: str, today: date | None = None) -> int | None:
    """按出生日期（YYYY-MM-DD）计算周岁；无法解析返回 None。"""
    try:
        born = date.fromisoformat(birth_date)
    except (TypeError, ValueError):
        return None
    ref = today or date.today()
    return ref.year - born.year - ((ref.month, ref.day) < (born.month, born.day))


def _patient_groups(db: Session, patient: Patient) -> set[str]:
    """推断患者所属特殊人群：儿童/老年按 birth_date，孕产妇按在册孕产记录+性别。"""
    groups: set[str] = set()
    age = _age_of(patient.birth_date)
    if age is not None:
        if age < CHILD_AGE_LIMIT:
            groups.add("child")
        if age >= ELDERLY_AGE_LIMIT:
            groups.add("elderly")
    if patient.gender == "女":
        maternal = (
            db.query(MaternalRecord)
            .filter(MaternalRecord.patient_id == patient.id, MaternalRecord.status == "registered")
            .first()
        )
        if maternal is not None:
            groups.add("pregnant")
    return groups


@router.post("/rules", response_model=DrugRuleOut, status_code=201, dependencies=[Depends(require_admin)])
def create_rule(body: DrugRuleCreate, db: Session = Depends(get_db)):
    if db.query(DrugRule).filter(DrugRule.drug_code == body.drug_code).first():
        raise HTTPException(status_code=409, detail="该药品规则已存在")
    rule = DrugRule(**body.model_dump())
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule


@router.post("/rules/import", dependencies=[Depends(require_admin)])
def import_rules(body: list[DrugRuleCreate], db: Session = Depends(get_db)):
    """审方规则批量导入：drug_code 已存在则整条更新，不存在则新建。"""
    imported, updated = 0, 0
    for entry in body:
        rule = db.query(DrugRule).filter(DrugRule.drug_code == entry.drug_code).first()
        if rule is None:
            db.add(DrugRule(**entry.model_dump()))
            imported += 1
        else:
            for field, value in entry.model_dump().items():
                setattr(rule, field, value)
            updated += 1
    db.commit()
    return {"imported": imported, "updated": updated}


@router.get("/rules", response_model=list[DrugRuleOut], dependencies=[Depends(get_current_user)])
def list_rules(db: Session = Depends(get_db)):
    return db.query(DrugRule).order_by(DrugRule.drug_code).all()


@router.post(
    "",
    response_model=PrescriptionOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor"))],  # H2: 处方开具限医师
)
def create_prescription(
    body: PrescriptionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    patient = db.get(Patient, body.patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")

    violations: list[str] = []

    # 同方重复药品编码 → 转药师审
    codes = [item.drug_code for item in body.items]
    duplicated = sorted({c for c in codes if codes.count(c) > 1})
    for code in duplicated:
        names = {item.drug_name for item in body.items if item.drug_code == code}
        violations.append(f"同方重复药品：{'/'.join(sorted(names))}（{code}）出现多次，需药师人工审核")

    patient_groups = _patient_groups(db, patient)
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
        # 禁忌诊断审查：诊断名命中禁忌关键词 → 转药师审并注明
        for keyword in (k.strip() for k in rule.contraindicated_diagnoses.split(",")):
            if keyword and keyword in body.diagnosis_name:
                violations.append(
                    f"禁忌诊断：{item.drug_name} 禁用于「{keyword}」相关诊断"
                    f"（本方诊断：{body.diagnosis_name}），需药师人工审核"
                )
        # 特殊人群审查：患者命中规则特殊人群 → 转药师审并注明
        rule_groups = {g.strip() for g in rule.special_groups.split(",") if g.strip()}
        for group in sorted(rule_groups & patient_groups):
            violations.append(
                f"特殊人群用药：{item.drug_name} 对{GROUP_NAMES.get(group, group)}需慎用，需药师人工审核"
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
def list_prescriptions(
    response: Response,
    status: str | None = None,
    offset: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
):
    """处方列表（L-3 分页：offset/limit，总数见 X-Total-Count 响应头）。"""
    query = db.query(Prescription)
    if status:
        query = query.filter(Prescription.status == status)
    return paginate(query.order_by(Prescription.id.desc()), response, offset, limit)


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


# ---------- 终审轮：处方点评（⑱事后点评与监管） ----------


class RxCommentCreate(BaseModel):
    grade: str = Field(pattern="^(reasonable|unreasonable)$")
    issues: str = ""
    comment: str = ""


@router.post(
    "/{prescription_id}/comment-review",
    status_code=201,
    dependencies=[Depends(require_roles("pharmacist"))],  # 处方点评=药师
)
def comment_prescription(
    prescription_id: int,
    body: RxCommentCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    rx = db.get(Prescription, prescription_id)
    if rx is None:
        raise HTTPException(status_code=404, detail="处方不存在")
    if db.query(PrescriptionComment).filter(
        PrescriptionComment.prescription_id == prescription_id
    ).first():
        raise HTTPException(status_code=409, detail="该处方已点评")
    if body.grade == "unreasonable" and not (body.issues or body.comment):
        raise HTTPException(status_code=422, detail="不合理处方须注明问题类型或点评意见")
    record = PrescriptionComment(
        prescription_id=prescription_id, reviewer_id=user.id, **body.model_dump()
    )
    db.add(record)
    db.commit()
    return {"id": record.id, "prescription_id": prescription_id, "grade": record.grade}


@router.get("/comment-reviews", dependencies=[Depends(get_current_user)])
def list_comment_reviews(grade: str | None = None, db: Session = Depends(get_db)):
    q = db.query(PrescriptionComment)
    if grade:
        q = q.filter(PrescriptionComment.grade == grade)
    return [
        {
            "id": c.id,
            "prescription_id": c.prescription_id,
            "grade": c.grade,
            "issues": c.issues,
            "comment": c.comment,
            "at": c.created_at.isoformat(),
        }
        for c in q.order_by(PrescriptionComment.id.desc()).limit(200).all()
    ]


@router.get("/comment-stats", dependencies=[Depends(get_current_user)])
def comment_stats(db: Session = Depends(get_db)):
    """点评统计：点评覆盖数、合理率（事后监管口径）。"""
    total = db.query(func.count(PrescriptionComment.id)).scalar() or 0
    unreasonable = (
        db.query(func.count(PrescriptionComment.id))
        .filter(PrescriptionComment.grade == "unreasonable")
        .scalar()
        or 0
    )
    return {
        "commented": total,
        "unreasonable": unreasonable,
        "reasonable_rate_pct": round((total - unreasonable) * 100.0 / total, 2) if total else 0.0,
    }
