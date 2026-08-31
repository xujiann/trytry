"""集中审方中心："系统+药师"双重审方，每方必审。"""
from datetime import date

from pydantic import BaseModel, Field
from sqlalchemy import func

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from ..concurrency import insert_if_absent, insert_or_conflict
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


def _active_rule(db: Session, drug_code: str) -> DrugRule | None:
    """取生效中的规则。停用的规则一律当作"未维护"，与规则不存在同路处理。"""
    return (
        db.query(DrugRule)
        .filter(DrugRule.drug_code == drug_code, DrugRule.active.is_(True))
        .first()
    )


@router.post("/rules", response_model=DrugRuleOut, status_code=201, dependencies=[Depends(require_admin)])
def create_rule(body: DrugRuleCreate, db: Session = Depends(get_db)):
    if db.query(DrugRule).filter(DrugRule.drug_code == body.drug_code).first():
        raise HTTPException(status_code=409, detail="该药品规则已存在")
    rule = insert_or_conflict(db, DrugRule(**body.model_dump()), "该药品规则已存在")
    return rule


class RuleImportOut(BaseModel):
    imported: int
    updated: int


class RuleActiveOut(BaseModel):
    """停用/恢复回执同形：编码 + 最新生效位。"""

    drug_code: str
    active: bool


@router.post("/rules/import", response_model=RuleImportOut, dependencies=[Depends(require_admin)])
def import_rules(body: list[DrugRuleCreate], db: Session = Depends(get_db)):
    """审方规则批量导入：drug_code 已存在则整条更新，不存在则新建。"""
    imported, updated = 0, 0
    for entry in body:
        rule = db.query(DrugRule).filter(DrugRule.drug_code == entry.drug_code).first()
        # 先试插；撞了说明有人并发导入了同一个 drug_code，取回来按更新处理。
        # 反过来"查不到就插"是 check-then-act，而这里一次 commit 提交整批，
        # 一条撞车整批回滚——导入方看到的是 500 与一条都没进。
        if rule is None and insert_if_absent(db, DrugRule(**entry.model_dump())):
            imported += 1
            continue
        if rule is None:
            rule = db.query(DrugRule).filter(DrugRule.drug_code == entry.drug_code).first()
            if rule is None:  # pragma: no cover - 撞了约束却查不到，说明约束定义有误
                continue
        for field, value in entry.model_dump().items():
            setattr(rule, field, value)
        updated += 1
    db.commit()
    return {"imported": imported, "updated": updated}


@router.get("/rules", response_model=list[DrugRuleOut], dependencies=[Depends(get_current_user)])
def list_rules(include_inactive: bool = False, db: Session = Depends(get_db)):
    query = db.query(DrugRule)
    if not include_inactive:
        query = query.filter(DrugRule.active.is_(True))
    return query.order_by(DrugRule.drug_code).all()


@router.delete(
    "/rules/{drug_code}", response_model=RuleActiveOut, dependencies=[Depends(require_admin)]
)
def deactivate_rule(drug_code: str, db: Session = Depends(get_db)):
    """停用规则（不删行）。

    原先这里只有 POST 与 import，录错一条规则只能靠 import 覆盖同 drug_code
    的行，删不掉也停不掉——而通用规则引擎 `/api/rules/{key}` 一直是有停用的。
    不删行：规则改过什么、什么时候不再生效，处方点评复核时要回溯得到。
    """
    rule = db.query(DrugRule).filter(DrugRule.drug_code == drug_code).first()
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    if not rule.active:
        raise HTTPException(status_code=409, detail="该规则已停用")
    rule.active = False
    db.commit()
    return {"drug_code": drug_code, "active": False}


@router.post(
    "/rules/{drug_code}/reactivate",
    response_model=RuleActiveOut,
    dependencies=[Depends(require_admin)],
)
def reactivate_rule(drug_code: str, db: Session = Depends(get_db)):
    rule = db.query(DrugRule).filter(DrugRule.drug_code == drug_code).first()
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    rule.active = True
    db.commit()
    return {"drug_code": drug_code, "active": True}


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

    advisories: list[str] = []
    patient_groups = _patient_groups(db, patient)
    names_by_code = {item.drug_code: item.drug_name for item in body.items}
    seen_pairs: set[frozenset[str]] = set()
    for item in body.items:
        rule = _active_rule(db, item.drug_code)
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
        # 块2：肝肾功能提示为非拦截性提醒，随处方返回供医师调整剂量，不改变审方状态
        if rule.renal_hepatic_note:
            note = f"{item.drug_name} 肝肾功能提示：{rule.renal_hepatic_note}"
            if note not in advisories:
                advisories.append(note)

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
    # 非持久化字段：审方提示只随本次响应返回、不入库（`PrescriptionOut.advisories`）。
    # 用 setattr 挂上去——模型上没有这一列，写成属性赋值等于对 ORM 撒谎。
    setattr(prescription, "advisories", advisories)
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
def review_prescription(prescription_id: int, body: PrescriptionReview, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
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


class ReviewPointItemOut(BaseModel):
    """逐药点评要点行。`daily_dose`/`max_daily_dose` 是 **Float 列**
    （`prescription_items.daily_dose` / `drug_rules.max_daily_dose`）：整数入参 4
    落库读回就是 4.0，声明 float 才是原样——与 Money 列相反。
    无规则时上限为 null（键恒在值可空），单位与要点回落空串。"""

    drug_code: str
    drug_name: str
    daily_dose: float
    max_daily_dose: float | None
    dose_unit: str
    dose_exceeded: bool
    review_points: str
    renal_hepatic_note: str
    no_rule: bool


class ReviewPointsOut(BaseModel):
    prescription_id: int
    diagnosis_name: str
    status: str
    system_review_comment: str
    items: list[ReviewPointItemOut]
    # 两条产地（round(x*100.0/n, 2) 与空方兜底字面量 0.0）都是浮点
    rule_coverage_pct: float


@router.get(
    "/{prescription_id}/review-points",
    response_model=ReviewPointsOut,
    dependencies=[Depends(get_current_user)],
)
def prescription_review_points(prescription_id: int, db: Session = Depends(get_db)):
    """块2：处方点评规则化——按处方内药品汇总规则库点评要点与肝肾功能提示。

    药师点评前调阅本接口，把「凭经验点评」变为「按规则点评」：
    - review_points：该药的点评要点（剂量/疗程/联用/特殊人群等核对项）
    - renal_hepatic_note：肝肾功能剂量调整提示
    - dose_exceeded：本方日剂量是否已超规则上限（系统审拦截项复核）
    - no_rule：规则库中无该药规则，提示补充维护
    """
    rx = db.get(Prescription, prescription_id)
    if rx is None:
        raise HTTPException(status_code=404, detail="处方不存在")
    items = db.query(PrescriptionItem).filter(PrescriptionItem.prescription_id == rx.id).all()
    points, uncovered = [], 0
    for item in items:
        rule = _active_rule(db, item.drug_code)
        if rule is None:
            uncovered += 1
        points.append(
            {
                "drug_code": item.drug_code,
                "drug_name": item.drug_name,
                "daily_dose": item.daily_dose,
                "max_daily_dose": rule.max_daily_dose if rule else None,
                "dose_unit": rule.dose_unit if rule else "",
                "dose_exceeded": bool(rule and item.daily_dose > rule.max_daily_dose),
                "review_points": rule.review_points if rule else "",
                "renal_hepatic_note": rule.renal_hepatic_note if rule else "",
                "no_rule": rule is None,
            }
        )
    return {
        "prescription_id": rx.id,
        "diagnosis_name": rx.diagnosis_name,
        "status": rx.status,
        "system_review_comment": rx.review_comment,
        "items": points,
        "rule_coverage_pct": round((len(items) - uncovered) * 100.0 / len(items), 2) if items else 0.0,
    }


class RxCommentCreatedOut(BaseModel):
    id: int
    prescription_id: int
    grade: str


class RxCommentOut(BaseModel):
    id: int
    prescription_id: int
    grade: str
    issues: str
    comment: str
    at: str


class RxCommentStatsOut(BaseModel):
    commented: int
    unreasonable: int
    # 两条产地（真除法 *100.0 与零点评兜底字面量 0.0）都是浮点
    reasonable_rate_pct: float


@router.post(
    "/{prescription_id}/comment-review",
    response_model=RxCommentCreatedOut,
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
    record = insert_or_conflict(db, PrescriptionComment(
            prescription_id=prescription_id, reviewer_id=user.id, **body.model_dump()
        ), "该处方已点评")
    return {"id": record.id, "prescription_id": prescription_id, "grade": record.grade}


@router.get(
    "/comment-reviews",
    response_model=list[RxCommentOut],
    dependencies=[Depends(get_current_user)],
)
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


@router.get(
    "/comment-stats", response_model=RxCommentStatsOut, dependencies=[Depends(get_current_user)]
)
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
