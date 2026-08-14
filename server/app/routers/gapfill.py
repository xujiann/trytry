"""块4：指引细目补齐——七个业务缺口的模型接口层。

按指引条目分区（各区一个 APIRouter，挂在既有业务前缀下，前端合并到既有页面）：
- ⑭ 中药制剂管理：制剂配方 / 批次 / 效期预警        /api/tcm/formulas、/api/tcm/preparation-batches
- ⑥ 消毒供应成本核算：批次成本项与单件成本统计      /api/cssd/cost-items、/api/cssd/cost-stats
- ⑳ 课件资源管理：课件挂附件、点播计数              /api/education/courses/{id}/materials
- ㉑ 适宜技术实训管理：计划 / 报名 / 考核            /api/education/training-plans
- ㉔ 产前筛查与诊断：唐筛/无创/超声，高危自动标记    /api/maternal/screenings
- ㉟ 绩效自评改进：问题→责任人→期限→完成确认        /api/performance/improvements
- ⑨ 上门服务调度：申请→派单→完成，关联家医签约      /api/homevisits
"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..datetypes import DateStr
from ..clock import now_naive
from ..concurrency import add_amount, insert_or_conflict, upsert_unique
from ..visibility import scope_org_list, scope_patient_list
from ..database import get_db
from ..deps import get_current_user, paginate, require_roles, resolve_business_date
from ..models import (
    Attachment,
    Course,
    CourseMaterial,
    CssdCostItem,
    FamilyDoctorContract,
    HomeVisitOrder,
    ImprovementTask,
    MaternalRecord,
    Organization,
    Patient,
    PrenatalScreening,
    SterilizationBatch,
    TcmFormula,
    TcmPreparationBatch,
    TcmTechnique,
    TrainingAssessment,
    TrainingEnrollment,
    TrainingPlan,
    User,
)

# ===========================================================================
# ⑭ 中药制剂管理（配方 / 批次 / 效期）
# ===========================================================================

tcm_router = APIRouter(
    prefix="/api/tcm", tags=["中药制剂管理"], dependencies=[Depends(get_current_user)]
)

DOSAGE_FORMS = {
    "pill": "丸剂",
    "powder": "散剂",
    "paste": "膏剂",
    "granule": "颗粒剂",
    "decoction": "合剂/汤剂",
}


class FormulaCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1)
    dosage_form: str = Field(default="decoction", pattern="^(pill|powder|paste|granule|decoction)$")
    composition: str = ""
    process: str = ""
    indication: str = ""
    shelf_life_months: int = Field(default=12, ge=1, le=120)


def _formula_out(f: TcmFormula) -> dict:
    return {
        "id": f.id,
        "code": f.code,
        "name": f.name,
        "dosage_form": f.dosage_form,
        "dosage_form_name": DOSAGE_FORMS.get(f.dosage_form, f.dosage_form),
        "composition": f.composition,
        "process": f.process,
        "indication": f.indication,
        "shelf_life_months": f.shelf_life_months,
        "active": f.active,
    }


@tcm_router.post(
    "/formulas",
    status_code=201,
    dependencies=[Depends(require_roles("pharmacist", "doctor"))],  # 制剂配方由药师/中医师维护
)
def create_formula(body: FormulaCreate, db: Session = Depends(get_db)):
    """新增中药制剂配方（编码唯一）。"""
    if db.query(TcmFormula).filter(TcmFormula.code == body.code).first():
        raise HTTPException(status_code=409, detail="制剂编码已存在")
    formula = insert_or_conflict(db, TcmFormula(**body.model_dump()), "制剂编码已存在")
    return _formula_out(formula)


@tcm_router.get("/formulas")
def list_formulas(active: bool | None = None, db: Session = Depends(get_db)):
    query = db.query(TcmFormula)
    if active is not None:
        query = query.filter(TcmFormula.active.is_(active))
    return [_formula_out(f) for f in query.order_by(TcmFormula.id.desc()).limit(200).all()]


class BatchCreate(BaseModel):
    formula_id: int
    batch_no: str = Field(min_length=1, max_length=32)
    org_id: int
    quantity: int = Field(ge=1)
    unit: str = "剂"
    produced_date: DateStr
    # 不传则按配方有效期（月）自动推算
    expire_date: str = ""


def _batch_out(b: TcmPreparationBatch, today: str) -> dict:
    return {
        "id": b.id,
        "formula_id": b.formula_id,
        "batch_no": b.batch_no,
        "org_id": b.org_id,
        "quantity": b.quantity,
        "unit": b.unit,
        "produced_date": b.produced_date,
        "expire_date": b.expire_date,
        "status": b.status,
        "expired": bool(b.expire_date and b.expire_date < today),
    }


@tcm_router.post(
    "/preparation-batches",
    status_code=201,
    dependencies=[Depends(require_roles("pharmacist", "operator"))],
)
def create_batch(body: BatchCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """制剂投料生产建批次：效期缺省按配方有效期（月）自动推算。"""
    formula = db.get(TcmFormula, body.formula_id)
    if formula is None:
        raise HTTPException(status_code=404, detail="制剂配方不存在")
    if not formula.active:
        raise HTTPException(status_code=409, detail="配方已停用，不可投产")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="生产机构不存在")
    if db.query(TcmPreparationBatch).filter(TcmPreparationBatch.batch_no == body.batch_no).first():
        raise HTTPException(status_code=409, detail="批号已存在")
    expire_date = body.expire_date
    if not expire_date:
        produced = date.fromisoformat(body.produced_date)
        expire_date = (produced + timedelta(days=30 * formula.shelf_life_months)).isoformat()
    if expire_date <= body.produced_date:
        raise HTTPException(status_code=422, detail="效期须晚于生产日期")
    batch = insert_or_conflict(db, TcmPreparationBatch(
            **body.model_dump(exclude={"expire_date"}), expire_date=expire_date, created_by=user.id
        ), "批号已存在")
    return _batch_out(batch, date.today().isoformat())


@tcm_router.get("/preparation-batches")
def list_batches(
    formula_id: int | None = None,
    status: str | None = None,
    today: str | None = None,
    db: Session = Depends(get_db),
):
    business_date = resolve_business_date(today).isoformat()
    query = db.query(TcmPreparationBatch)
    if formula_id is not None:
        query = query.filter(TcmPreparationBatch.formula_id == formula_id)
    if status:
        query = query.filter(TcmPreparationBatch.status == status)
    return [
        _batch_out(b, business_date)
        for b in query.order_by(TcmPreparationBatch.id.desc()).limit(200).all()
    ]


@tcm_router.get("/preparation-batches/expiring")
def expiring_batches(days: int = 30, today: str | None = None, db: Session = Depends(get_db)):
    """效期预警：N 天内到期或已过期的未召回批次。"""
    business_date = resolve_business_date(today)
    cutoff = (business_date + timedelta(days=max(days, 0))).isoformat()
    rows = (
        db.query(TcmPreparationBatch)
        .filter(
            TcmPreparationBatch.status != "recalled",
            TcmPreparationBatch.expire_date != "",
            TcmPreparationBatch.expire_date <= cutoff,
        )
        .order_by(TcmPreparationBatch.expire_date)
        .all()
    )
    return [_batch_out(b, business_date.isoformat()) for b in rows]


@tcm_router.post(
    "/preparation-batches/{batch_id}/release",
    dependencies=[Depends(require_roles("pharmacist", "operator"))],
)
def release_batch(batch_id: int, today: str | None = None, db: Session = Depends(get_db)):
    """批次发放：过期批次禁止发放（效期管控）。"""
    batch = db.get(TcmPreparationBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    if batch.status != "produced":
        raise HTTPException(status_code=409, detail=f"批次当前状态 {batch.status} 不可发放")
    business_date = resolve_business_date(today).isoformat()
    if batch.expire_date and batch.expire_date < business_date:
        raise HTTPException(status_code=409, detail="批次已过效期，禁止发放")
    batch.status = "released"
    db.commit()
    db.refresh(batch)
    return _batch_out(batch, business_date)


# ===========================================================================
# ⑥ 消毒供应成本核算
# ===========================================================================

cssd_router = APIRouter(
    prefix="/api/cssd", tags=["消毒供应成本核算"], dependencies=[Depends(get_current_user)]
)

COST_TYPES = {
    "labor": "人工",
    "material": "耗材",
    "energy": "能耗",
    "equipment": "设备折旧",
    "other": "其他",
}


class CostItemCreate(BaseModel):
    batch_id: int
    cost_type: str = Field(pattern="^(labor|material|energy|equipment|other)$")
    amount: float = Field(gt=0)
    note: str = ""


@cssd_router.post(
    "/cost-items", status_code=201, dependencies=[Depends(require_roles("operator", "director"))]
)
def create_cost_item(
    body: CostItemCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """登记灭菌批次成本项（人工/耗材/能耗/设备折旧）。"""
    if db.get(SterilizationBatch, body.batch_id) is None:
        raise HTTPException(status_code=404, detail="灭菌批次不存在")
    item = CssdCostItem(**body.model_dump(), created_by=user.id)
    db.add(item)
    db.commit()
    db.refresh(item)
    return {
        "id": item.id,
        "batch_id": item.batch_id,
        "cost_type": item.cost_type,
        "cost_type_name": COST_TYPES[item.cost_type],
        "amount": item.amount,
        "note": item.note,
    }


@cssd_router.get("/cost-items")
def list_cost_items(batch_id: int | None = None, db: Session = Depends(get_db)):
    query = db.query(CssdCostItem)
    if batch_id is not None:
        query = query.filter(CssdCostItem.batch_id == batch_id)
    return [
        {
            "id": i.id,
            "batch_id": i.batch_id,
            "cost_type": i.cost_type,
            "cost_type_name": COST_TYPES.get(i.cost_type, i.cost_type),
            "amount": i.amount,
            "note": i.note,
        }
        for i in query.order_by(CssdCostItem.id.desc()).limit(500).all()
    ]


@cssd_router.get("/cost-stats")
def cost_stats(batch_id: int | None = None, db: Session = Depends(get_db)):
    """成本统计：按批次汇总总成本与单件成本，并给出成本构成与整体单件成本。"""
    batch_query = db.query(SterilizationBatch)
    if batch_id is not None:
        batch_query = batch_query.filter(SterilizationBatch.id == batch_id)
    batches = batch_query.order_by(SterilizationBatch.id.desc()).limit(200).all()
    ids = [b.id for b in batches]
    totals: dict[int, float] = {}
    by_type: dict[str, float] = {}
    if ids:
        for bid, ctype, amount in (
            db.query(CssdCostItem.batch_id, CssdCostItem.cost_type, func.sum(CssdCostItem.amount))
            .filter(CssdCostItem.batch_id.in_(ids))
            .group_by(CssdCostItem.batch_id, CssdCostItem.cost_type)
            .all()
        ):
            totals[bid] = round(totals.get(bid, 0) + float(amount), 2)
            by_type[ctype] = round(by_type.get(ctype, 0) + float(amount), 2)
    rows = [
        {
            "batch_id": b.id,
            "batch_no": b.batch_no,
            "item_name": b.item_name,
            "quantity": b.quantity,
            "total_cost": round(totals.get(b.id, 0), 2),
            "unit_cost": round(totals.get(b.id, 0) / b.quantity, 2) if b.quantity else 0.0,
        }
        for b in batches
    ]
    total_cost = round(sum(r["total_cost"] for r in rows), 2)
    total_quantity = sum(r["quantity"] for r in rows)
    return {
        "batches": rows,
        "total_cost": total_cost,
        "total_quantity": total_quantity,
        "overall_unit_cost": round(total_cost / total_quantity, 2) if total_quantity else 0.0,
        "by_cost_type": {
            k: {"amount": v, "name": COST_TYPES.get(k, k)} for k, v in sorted(by_type.items())
        },
    }


# ===========================================================================
# ⑳ 课件资源管理 + ㉑ 适宜技术实训管理
# ===========================================================================

edu_router = APIRouter(
    prefix="/api/education", tags=["课件与实训"], dependencies=[Depends(get_current_user)]
)

MATERIAL_TYPES = {"slide": "课件", "video": "视频", "doc": "文档", "link": "外链"}


class MaterialCreate(BaseModel):
    title: str = Field(min_length=1)
    material_type: str = Field(default="slide", pattern="^(slide|video|doc|link)$")
    url: str = ""


def _material_out(m: CourseMaterial, attachments: int = 0) -> dict:
    return {
        "id": m.id,
        "course_id": m.course_id,
        "title": m.title,
        "material_type": m.material_type,
        "material_type_name": MATERIAL_TYPES.get(m.material_type, m.material_type),
        "url": m.url,
        "play_count": m.play_count,
        "attachments": attachments,
    }


@edu_router.post(
    "/courses/{course_id}/materials",
    status_code=201,
    dependencies=[Depends(require_roles("director", "public_health", "operator", "doctor"))],
)
def create_material(
    course_id: int,
    body: MaterialCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """课程下挂课件资源（附件另经 /api/attachments 以 owner_type=course_material 上传）。"""
    if db.get(Course, course_id) is None:
        raise HTTPException(status_code=404, detail="课程不存在")
    material = CourseMaterial(course_id=course_id, created_by=user.id, **body.model_dump())
    db.add(material)
    db.commit()
    db.refresh(material)
    return _material_out(material)


@edu_router.get("/courses/{course_id}/materials")
def list_materials(course_id: int, db: Session = Depends(get_db)):
    if db.get(Course, course_id) is None:
        raise HTTPException(status_code=404, detail="课程不存在")
    materials = (
        db.query(CourseMaterial)
        .filter(CourseMaterial.course_id == course_id)
        .order_by(CourseMaterial.id.desc())
        .all()
    )
    counts = dict(
        db.query(Attachment.owner_id, func.count(Attachment.id))
        .filter(
            Attachment.owner_type == "course_material",
            Attachment.owner_id.in_([m.id for m in materials] or [0]),
        )
        .group_by(Attachment.owner_id)
        .all()
    )
    return [_material_out(m, counts.get(m.id, 0)) for m in materials]


@edu_router.post("/materials/{material_id}/play")
def play_material(material_id: int, db: Session = Depends(get_db)):
    """点播计数：每次调阅 +1（点播量用于课件资源热度统计）。"""
    material = db.get(CourseMaterial, material_id)
    if material is None:
        raise HTTPException(status_code=404, detail="课件不存在")
    add_amount(db, CourseMaterial, material.id, "play_count", 1)
    db.commit()
    db.refresh(material)
    return _material_out(material)


@edu_router.get("/material-stats")
def material_stats(db: Session = Depends(get_db)):
    """课件点播排行（前 20）与总点播量。"""
    materials = (
        db.query(CourseMaterial).order_by(CourseMaterial.play_count.desc()).limit(20).all()
    )
    total_plays = db.query(func.coalesce(func.sum(CourseMaterial.play_count), 0)).scalar() or 0
    return {
        "total_materials": db.query(func.count(CourseMaterial.id)).scalar() or 0,
        "total_plays": int(total_plays),
        "top": [_material_out(m) for m in materials],
    }


class PlanCreate(BaseModel):
    title: str = Field(min_length=1)
    org_id: int
    technique_id: int | None = None
    plan_date: DateStr
    capacity: int = Field(default=30, ge=1, le=1000)
    trainer: str = ""


def _plan_out(p: TrainingPlan, enrolled: int = 0) -> dict:
    return {
        "id": p.id,
        "title": p.title,
        "technique_id": p.technique_id,
        "org_id": p.org_id,
        "plan_date": p.plan_date,
        "capacity": p.capacity,
        "trainer": p.trainer,
        "status": p.status,
        "enrolled": enrolled,
        "remaining": max(p.capacity - enrolled, 0),
    }


@edu_router.post(
    "/training-plans",
    status_code=201,
    dependencies=[Depends(require_roles("director", "doctor", "public_health"))],
)
def create_plan(body: PlanCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """发布适宜技术实训计划。"""
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="承办机构不存在")
    if body.technique_id is not None and db.get(TcmTechnique, body.technique_id) is None:
        raise HTTPException(status_code=404, detail="适宜技术不存在")
    plan = TrainingPlan(created_by=user.id, **body.model_dump())
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return _plan_out(plan)


def _enrolled_count(db: Session, plan_id: int) -> int:
    return (
        db.query(func.count(TrainingEnrollment.id))
        .filter(
            TrainingEnrollment.plan_id == plan_id, TrainingEnrollment.status == "enrolled"
        )
        .scalar()
        or 0
    )


@edu_router.get("/training-plans")
def list_plans(status: str | None = None, db: Session = Depends(get_db)):
    query = db.query(TrainingPlan)
    if status:
        query = query.filter(TrainingPlan.status == status)
    plans = query.order_by(TrainingPlan.id.desc()).limit(200).all()
    return [_plan_out(p, _enrolled_count(db, p.id)) for p in plans]


@edu_router.post("/training-plans/{plan_id}/enroll", status_code=201)
def enroll_plan(plan_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """学员报名（登录用户本人报名，名额满则 409）。"""
    plan = db.get(TrainingPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="实训计划不存在")
    if plan.status != "open":
        raise HTTPException(status_code=409, detail=f"计划当前状态 {plan.status} 不接受报名")
    existing = (
        db.query(TrainingEnrollment)
        .filter(TrainingEnrollment.plan_id == plan_id, TrainingEnrollment.user_id == user.id)
        .first()
    )
    if existing and existing.status == "enrolled":
        raise HTTPException(status_code=409, detail="已报名该实训计划")
    if _enrolled_count(db, plan_id) >= plan.capacity:
        raise HTTPException(status_code=409, detail="实训名额已满")
    if existing:
        existing.status = "enrolled"
        enrollment = existing
    else:
        enrollment = TrainingEnrollment(plan_id=plan_id, user_id=user.id)
        db.add(enrollment)
    try:
        db.commit()
    except IntegrityError:
        # 同一个人重复点报名，两个请求都查不到既有记录就都去插
        db.rollback()
        raise HTTPException(status_code=409, detail="已报名该实训计划") from None
    db.refresh(enrollment)
    return {
        "id": enrollment.id,
        "plan_id": plan_id,
        "user_id": user.id,
        "status": enrollment.status,
    }


@edu_router.post("/training-plans/{plan_id}/cancel-enroll")
def cancel_enroll(plan_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    enrollment = (
        db.query(TrainingEnrollment)
        .filter(TrainingEnrollment.plan_id == plan_id, TrainingEnrollment.user_id == user.id)
        .first()
    )
    if enrollment is None or enrollment.status != "enrolled":
        raise HTTPException(status_code=404, detail="未报名该实训计划")
    enrollment.status = "cancelled"
    db.commit()
    return {"plan_id": plan_id, "user_id": user.id, "status": "cancelled"}


@edu_router.get("/training-plans/{plan_id}/enrollments")
def list_enrollments(plan_id: int, db: Session = Depends(get_db)):
    if db.get(TrainingPlan, plan_id) is None:
        raise HTTPException(status_code=404, detail="实训计划不存在")
    rows = (
        db.query(TrainingEnrollment, User.username, User.full_name)
        .join(User, TrainingEnrollment.user_id == User.id)
        .filter(TrainingEnrollment.plan_id == plan_id)
        .order_by(TrainingEnrollment.id)
        .all()
    )
    return [
        {
            "id": e.id,
            "user_id": e.user_id,
            "username": username,
            "full_name": full_name,
            "status": e.status,
        }
        for e, username, full_name in rows
    ]


class AssessmentCreate(BaseModel):
    user_id: int
    score: float = Field(ge=0, le=100)
    comment: str = ""


@edu_router.post(
    "/training-plans/{plan_id}/assessments",
    status_code=201,
    dependencies=[Depends(require_roles("director", "doctor"))],
)
def create_assessment(
    plan_id: int,
    body: AssessmentCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """实训考核录入：须已报名，60 分及格，同一计划同一学员唯一（重录更新成绩）。"""
    if db.get(TrainingPlan, plan_id) is None:
        raise HTTPException(status_code=404, detail="实训计划不存在")
    enrolled = (
        db.query(TrainingEnrollment)
        .filter(
            TrainingEnrollment.plan_id == plan_id,
            TrainingEnrollment.user_id == body.user_id,
            TrainingEnrollment.status == "enrolled",
        )
        .first()
    )
    if enrolled is None:
        raise HTTPException(status_code=409, detail="该学员未报名本次实训，不可录入考核")
    record, _ = upsert_unique(
        db,
        TrainingAssessment,
        keys={"plan_id": plan_id, "user_id": body.user_id},
        values={
            "score": body.score,
            "passed": body.score >= 60,
            "comment": body.comment,
            "assessor": user.full_name or user.username,
        },
    )
    return {
        "id": record.id,
        "plan_id": plan_id,
        "user_id": record.user_id,
        "score": record.score,
        "passed": record.passed,
        "assessor": record.assessor,
    }


@edu_router.get("/training-plans/{plan_id}/assessments")
def list_assessments(plan_id: int, db: Session = Depends(get_db)):
    rows = (
        db.query(TrainingAssessment)
        .filter(TrainingAssessment.plan_id == plan_id)
        .order_by(TrainingAssessment.score.desc())
        .all()
    )
    passed = sum(1 for r in rows if r.passed)
    return {
        "total": len(rows),
        "passed": passed,
        "pass_rate_pct": round(passed * 100.0 / len(rows), 2) if rows else 0.0,
        "items": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "score": r.score,
                "passed": r.passed,
                "comment": r.comment,
                "assessor": r.assessor,
            }
            for r in rows
        ],
    }


# ===========================================================================
# ㉔ 产前筛查与诊断（高危自动标记）
# ===========================================================================

maternal_router = APIRouter(
    prefix="/api/maternal", tags=["产前筛查与诊断"], dependencies=[Depends(get_current_user)]
)

SCREEN_TYPES = {
    "down": "唐氏血清学筛查",
    "nipt": "无创产前基因检测",
    "ultrasound": "超声结构筛查",
    "diagnosis": "产前诊断",
}
# 触发孕产妇档案高危标记的筛查结论
HIGH_RISK_RESULTS = {"high_risk", "critical"}


class ScreeningCreate(BaseModel):
    record_id: int
    screen_type: str = Field(pattern="^(down|nipt|ultrasound|diagnosis)$")
    screen_date: DateStr
    gest_week: int | None = Field(default=None, ge=1, le=45)
    result: str = Field(default="low_risk", pattern="^(low_risk|high_risk|critical)$")
    indicator: str = ""
    conclusion: str = ""


def _screening_out(s: PrenatalScreening) -> dict:
    return {
        "id": s.id,
        "record_id": s.record_id,
        "screen_type": s.screen_type,
        "screen_type_name": SCREEN_TYPES.get(s.screen_type, s.screen_type),
        "screen_date": s.screen_date,
        "gest_week": s.gest_week,
        "result": s.result,
        "indicator": s.indicator,
        "conclusion": s.conclusion,
        "flagged_high_risk": s.flagged_high_risk,
    }


@maternal_router.post(
    "/screenings",
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],
)
def create_screening(
    body: ScreeningCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """产前筛查/诊断记录：高风险与临界风险结论自动标记孕产妇档案为高危并追加风险因素。"""
    record = db.get(MaternalRecord, body.record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="孕产妇档案不存在")
    screening = PrenatalScreening(created_by=user.id, **body.model_dump())
    screening.flagged_high_risk = body.result in HIGH_RISK_RESULTS
    if screening.flagged_high_risk:
        record.high_risk = True
        factor = f"{SCREEN_TYPES[body.screen_type]}{'高风险' if body.result == 'high_risk' else '临界风险'}"
        record.risk_factors = (
            f"{record.risk_factors}；{factor}" if record.risk_factors else factor
        )
    db.add(screening)
    db.commit()
    db.refresh(screening)
    return _screening_out(screening)


@maternal_router.get("/screenings")
def list_screenings(
    record_id: int | None = None, result: str | None = None, db: Session = Depends(get_db)
):
    query = db.query(PrenatalScreening)
    if record_id is not None:
        query = query.filter(PrenatalScreening.record_id == record_id)
    if result:
        query = query.filter(PrenatalScreening.result == result)
    return [
        _screening_out(s) for s in query.order_by(PrenatalScreening.id.desc()).limit(200).all()
    ]


@maternal_router.get("/screening-stats")
def screening_stats(db: Session = Depends(get_db)):
    """筛查统计：按筛查类型与结论分布，高危检出率。"""
    by_type = dict(
        db.query(PrenatalScreening.screen_type, func.count(PrenatalScreening.id))
        .group_by(PrenatalScreening.screen_type)
        .all()
    )
    by_result = dict(
        db.query(PrenatalScreening.result, func.count(PrenatalScreening.id))
        .group_by(PrenatalScreening.result)
        .all()
    )
    total = sum(by_result.values())
    high = sum(v for k, v in by_result.items() if k in HIGH_RISK_RESULTS)
    return {
        "total": total,
        "by_type": {k: {"count": v, "name": SCREEN_TYPES.get(k, k)} for k, v in by_type.items()},
        "by_result": by_result,
        "high_risk_detect_rate_pct": round(high * 100.0 / total, 2) if total else 0.0,
    }


# ===========================================================================
# ㉟ 绩效自评改进（整改任务闭环）
# ===========================================================================

perf_router = APIRouter(
    prefix="/api/performance", tags=["绩效自评改进"], dependencies=[Depends(get_current_user)]
)

TASK_STATUS = {
    "open": "待整改",
    "in_progress": "整改中",
    "completed": "已完成待确认",
    "verified": "已确认关闭",
}


class TaskCreate(BaseModel):
    org_id: int
    problem: str = Field(min_length=1)
    owner_name: str = Field(min_length=1)
    due_date: DateStr
    indicator_key: str = ""
    measures: str = ""


def _task_out(t: ImprovementTask, today: str) -> dict:
    return {
        "id": t.id,
        "org_id": t.org_id,
        "indicator_key": t.indicator_key,
        "problem": t.problem,
        "measures": t.measures,
        "owner_name": t.owner_name,
        "due_date": t.due_date,
        "status": t.status,
        "status_name": TASK_STATUS.get(t.status, t.status),
        "overdue": t.status in ("open", "in_progress") and t.due_date < today,
        "completion_note": t.completion_note,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        "verify_comment": t.verify_comment,
        "verified_by": t.verified_by,
    }


@perf_router.post(
    "/improvements", status_code=201, dependencies=[Depends(require_roles("director"))]
)
def create_task(body: TaskCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """下达绩效整改任务（问题 → 责任人 → 期限）。"""
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    task = ImprovementTask(created_by=user.id, **body.model_dump())
    db.add(task)
    db.commit()
    db.refresh(task)
    return _task_out(task, date.today().isoformat())


@perf_router.get("/improvements")
def list_tasks(
    response: Response,
    org_id: int | None = None,
    status: str | None = None,
    overdue_only: bool = False,
    today: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    business_date = resolve_business_date(today).isoformat()
    query = db.query(ImprovementTask)
    query = scope_org_list(db, user, query, ImprovementTask, org_id)
    if status:
        query = query.filter(ImprovementTask.status == status)
    if overdue_only:
        query = query.filter(
            ImprovementTask.status.in_(["open", "in_progress"]),
            ImprovementTask.due_date < business_date,
        )
    rows = paginate(query.order_by(ImprovementTask.id.desc()), response, offset, limit)
    return [_task_out(t, business_date) for t in rows]


class TaskProgress(BaseModel):
    measures: str = ""
    completion_note: str = ""
    # 置为 True 表示整改完成、提交确认
    complete: bool = False


@perf_router.post(
    "/improvements/{task_id}/progress",
    dependencies=[Depends(require_roles("director", "operator"))],
)
def progress_task(task_id: int, body: TaskProgress, db: Session = Depends(get_db)):
    """整改进展：登记措施；complete=true 时提交完成确认。"""
    task = db.get(ImprovementTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="整改任务不存在")
    if task.status == "verified":
        raise HTTPException(status_code=409, detail="任务已确认关闭")
    if body.measures:
        task.measures = body.measures
    if body.complete:
        if not body.completion_note:
            raise HTTPException(status_code=422, detail="提交完成须填写整改结果说明")
        task.status = "completed"
        task.completion_note = body.completion_note
        task.completed_at = now_naive()
    else:
        task.status = "in_progress"
    db.commit()
    db.refresh(task)
    return _task_out(task, date.today().isoformat())


class TaskVerify(BaseModel):
    approve: bool
    comment: str = ""


@perf_router.post(
    "/improvements/{task_id}/verify", dependencies=[Depends(require_roles("director"))]
)
def verify_task(
    task_id: int,
    body: TaskVerify,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """完成确认：通过则关闭任务，不通过退回整改中。"""
    task = db.get(ImprovementTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="整改任务不存在")
    if task.status != "completed":
        raise HTTPException(status_code=409, detail="仅已提交完成的任务可确认")
    task.verify_comment = body.comment
    task.verified_by = user.full_name or user.username
    if body.approve:
        task.status = "verified"
        task.verified_at = now_naive()
    else:
        task.status = "in_progress"
        task.completed_at = None
    db.commit()
    db.refresh(task)
    return _task_out(task, date.today().isoformat())


@perf_router.get("/improvement-stats")
def improvement_stats(today: str | None = None, db: Session = Depends(get_db)):
    business_date = resolve_business_date(today).isoformat()
    by_status = dict(
        db.query(ImprovementTask.status, func.count(ImprovementTask.id))
        .group_by(ImprovementTask.status)
        .all()
    )
    overdue = (
        db.query(func.count(ImprovementTask.id))
        .filter(
            ImprovementTask.status.in_(["open", "in_progress"]),
            ImprovementTask.due_date < business_date,
        )
        .scalar()
        or 0
    )
    total = sum(by_status.values())
    return {
        "total": total,
        "by_status": {k: {"count": v, "name": TASK_STATUS.get(k, k)} for k, v in by_status.items()},
        "overdue": overdue,
        "closed_rate_pct": round(by_status.get("verified", 0) * 100.0 / total, 2) if total else 0.0,
    }


# ===========================================================================
# ⑨ 上门服务调度（送医送护上门）
# ===========================================================================

home_router = APIRouter(
    prefix="/api/homevisits", tags=["上门服务调度"], dependencies=[Depends(get_current_user)]
)

VISIT_SERVICES = {
    "nursing": "上门护理",
    "doctor": "上门诊疗",
    "rehab": "康复指导",
    "sampling": "上门采样",
}


class VisitCreate(BaseModel):
    patient_id: int
    org_id: int
    service_type: str = Field(pattern="^(nursing|doctor|rehab|sampling)$")
    demand: str = ""
    address: str = ""
    expect_date: str = ""
    # 不传则自动关联该患者在该机构的履约中家医签约
    contract_id: int | None = None


def _visit_out(o: HomeVisitOrder) -> dict:
    return {
        "id": o.id,
        "patient_id": o.patient_id,
        "contract_id": o.contract_id,
        "org_id": o.org_id,
        "service_type": o.service_type,
        "service_type_name": VISIT_SERVICES.get(o.service_type, o.service_type),
        "demand": o.demand,
        "address": o.address,
        "expect_date": o.expect_date,
        "status": o.status,
        "assignee_name": o.assignee_name,
        "dispatched_at": o.dispatched_at.isoformat() if o.dispatched_at else None,
        "service_note": o.service_note,
        "completed_at": o.completed_at.isoformat() if o.completed_at else None,
    }


@home_router.post(
    "", status_code=201, dependencies=[Depends(require_roles("operator", "doctor", "public_health"))]
)
def create_visit(body: VisitCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """上门服务申请：显式指定签约或按患者+机构自动关联履约中的家医签约。"""
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="服务机构不存在")
    contract_id = body.contract_id
    if contract_id is not None:
        contract = db.get(FamilyDoctorContract, contract_id)
        if contract is None:
            raise HTTPException(status_code=404, detail="家医签约不存在")
        if contract.patient_id != body.patient_id:
            raise HTTPException(status_code=422, detail="签约与申请患者不一致")
        if contract.status != "active":
            raise HTTPException(status_code=409, detail="家医签约已解约，不可派生上门服务")
    else:
        contract = (
            db.query(FamilyDoctorContract)
            .filter(
                FamilyDoctorContract.patient_id == body.patient_id,
                FamilyDoctorContract.org_id == body.org_id,
                FamilyDoctorContract.status == "active",
            )
            .first()
        )
        contract_id = contract.id if contract else None
    order = HomeVisitOrder(
        **body.model_dump(exclude={"contract_id"}), contract_id=contract_id, created_by=user.id
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return _visit_out(order)


@home_router.get("")
def list_visits(
    response: Response,
    status: str | None = None,
    patient_id: int | None = None,
    org_id: int | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(HomeVisitOrder)
    if status:
        query = query.filter(HomeVisitOrder.status == status)
    query = scope_patient_list(db, user, query, HomeVisitOrder, patient_id, "home_visit")
    if org_id is not None:
        query = query.filter(HomeVisitOrder.org_id == org_id)
    return [
        _visit_out(o)
        for o in paginate(query.order_by(HomeVisitOrder.id.desc()), response, offset, limit)
    ]


class VisitDispatch(BaseModel):
    assignee_name: str = Field(min_length=1)


@home_router.post(
    "/{order_id}/dispatch", dependencies=[Depends(require_roles("operator", "doctor"))]
)
def dispatch_visit(order_id: int, body: VisitDispatch, db: Session = Depends(get_db)):
    """派单：指派上门人员（仅待派单工单可派）。"""
    order = db.get(HomeVisitOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="上门工单不存在")
    if order.status != "applied":
        raise HTTPException(status_code=409, detail=f"工单当前状态 {order.status} 不可派单")
    order.status = "dispatched"
    order.assignee_name = body.assignee_name
    order.dispatched_at = now_naive()
    db.commit()
    db.refresh(order)
    return _visit_out(order)


class VisitComplete(BaseModel):
    service_note: str = Field(min_length=1)


@home_router.post(
    "/{order_id}/complete", dependencies=[Depends(require_roles("operator", "doctor", "public_health"))]
)
def complete_visit(order_id: int, body: VisitComplete, db: Session = Depends(get_db)):
    """完成上门服务：登记服务记录（仅已派单工单可完成）。"""
    order = db.get(HomeVisitOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="上门工单不存在")
    if order.status != "dispatched":
        raise HTTPException(status_code=409, detail=f"工单当前状态 {order.status} 不可完成")
    order.status = "completed"
    order.service_note = body.service_note
    order.completed_at = now_naive()
    db.commit()
    db.refresh(order)
    return _visit_out(order)


@home_router.post(
    "/{order_id}/cancel", dependencies=[Depends(require_roles("operator", "doctor"))]
)
def cancel_visit(order_id: int, db: Session = Depends(get_db)):
    order = db.get(HomeVisitOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="上门工单不存在")
    if order.status == "completed":
        raise HTTPException(status_code=409, detail="已完成工单不可取消")
    order.status = "cancelled"
    db.commit()
    db.refresh(order)
    return _visit_out(order)


@home_router.get("/stats")
def visit_stats(db: Session = Depends(get_db)):
    """上门服务统计：状态分布、签约关联率（体现家医签约履约）。"""
    by_status = dict(
        db.query(HomeVisitOrder.status, func.count(HomeVisitOrder.id))
        .group_by(HomeVisitOrder.status)
        .all()
    )
    total = sum(by_status.values())
    linked = (
        db.query(func.count(HomeVisitOrder.id))
        .filter(HomeVisitOrder.contract_id.isnot(None))
        .scalar()
        or 0
    )
    return {
        "total": total,
        "by_status": by_status,
        "contract_linked": linked,
        "contract_linked_ratio_pct": round(linked * 100.0 / total, 2) if total else 0.0,
    }
