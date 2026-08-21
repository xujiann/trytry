"""医共体绩效考核：按机构自动汇算业务数据，生成绩效评分。

维度（与规划第35项功能对应）：
- 转诊结案率（服务协同）
- 远程诊断服务量（资源下沉）
- 慢病随访覆盖（医防融合）
- 处方合格率（合理用药）
- 家医签约履约量（签约服务）
"""
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..datetypes import DateStr
from ..deps import (
    get_current_user,
    paginate,
    require_roles,
    resolve_business_date,
    resolve_org_scope,
    row_dict,
)
from ..clock import now_naive
from ..visibility import (
    assert_obj_org_writable,
    assert_org_writable,
    scope_org_list,
)
from ..models import (
    ChronicPatient,
    ContractService,
    ExamRequest,
    FamilyDoctorContract,
    FollowUp,
    ImprovementTask,
    Organization,
    PerformanceIndicator,
    Prescription,
    Referral,
    User,
)

router = APIRouter(
    prefix="/api/performance", tags=["绩效考核"], dependencies=[Depends(require_roles("director"))]
)

# 指标目录种子（启动时写入 performance_indicators 表；权重和不必为100，计分时按比例归一化）
DEFAULT_INDICATORS: dict[str, dict[str, Any]] = {
    "referral": {"name": "转诊结案率", "weight": 20},
    "remote_exam": {"name": "远程诊断服务量", "weight": 20},
    "chronic": {"name": "慢病随访覆盖", "weight": 25},
    "rx": {"name": "处方合格率", "weight": 20},
    "contract": {"name": "家医签约履约量", "weight": 15},
}


def _normalized_weights(db: Session) -> dict[str, float]:
    """从指标表读取 active 指标权重，按比例归一化到总分100。表空时退回默认。"""
    rows = (
        db.query(PerformanceIndicator)
        .filter(PerformanceIndicator.active.is_(True), PerformanceIndicator.weight > 0)
        .all()
    )
    raw: dict[str, float] = (
        {r.key: r.weight for r in rows}
        if rows
        else {k: float(v["weight"]) for k, v in DEFAULT_INDICATORS.items()}
    )
    total = sum(raw.values())
    return {k: round(w / total * 100, 2) for k, w in raw.items()}


class IndicatorPatch(BaseModel):
    weight: float | None = Field(default=None, ge=0)
    name: str | None = None
    active: bool | None = None


class IndicatorOut(BaseModel):
    id: int
    key: str
    name: str
    weight: float
    active: bool

    model_config = {"from_attributes": True}


@router.get("/indicators", response_model=list[IndicatorOut])
def list_indicators(db: Session = Depends(get_db)):
    return db.query(PerformanceIndicator).order_by(PerformanceIndicator.id).all()


@router.patch("/indicators/{key}", response_model=IndicatorOut)
def update_indicator(key: str, body: IndicatorPatch, db: Session = Depends(get_db)):
    """管理层调节指标权重/启停：调整立即反映到 /orgs 计分（按比例归一化）。"""
    indicator = db.query(PerformanceIndicator).filter(PerformanceIndicator.key == key).first()
    if indicator is None:
        raise HTTPException(status_code=404, detail="指标不存在")
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(indicator, field, value)
    db.flush()
    active_weight = (
        db.query(func.sum(PerformanceIndicator.weight))
        .filter(PerformanceIndicator.active.is_(True))
        .scalar()
        or 0
    )
    if active_weight <= 0:
        db.rollback()
        raise HTTPException(status_code=422, detail="启用指标的权重合计必须大于0")
    db.commit()
    db.refresh(indicator)
    return indicator


class ReferralCompletion(BaseModel):
    completed: int
    total: int


class ChronicFollowup(BaseModel):
    followed: int
    total: int


class RxPass(BaseModel):
    passed: int
    total: int


class ScorecardDetail(BaseModel):
    """计分明细：三段是「分子/分母」小字典，两段是裸计数——形状本就不齐，
    逐段建模而不是 `dict[str, Any]`（写成 Any 等于没声明契约）。"""

    referral_completion: ReferralCompletion
    remote_exams: int
    chronic_followup: ChronicFollowup
    rx_pass: RxPass
    contract_services: int


class OrgScorecard(BaseModel):
    org_id: int
    org_name: str
    level: str
    #: `round(sum(...), 1)`。`_normalized_weights` 表空时退回非空默认，
    #: 求和恒在浮点上做，故这里恒为 float（`0.0` 而非 `0`）——
    #: 若可能是 int，声明成 float 就会改掉响应字节。
    score: float
    detail: ScorecardDetail


class OrgScorecardsOut(BaseModel):
    #: 键来自 `performance_indicators` 表（可增删指标），是**动态**的，
    #: 只能写 dict[str, float]，不能逐个字段写死。
    weights: dict[str, float]
    scorecards: list[OrgScorecard]


@router.get("/orgs", response_model=OrgScorecardsOut)
def org_scorecards(
    volume_cap: int = 5,
    include_auto_passed: bool = True,
    org_id: int | None = None,
    group_id: int | None = None,
    db: Session = Depends(get_db),
):
    """机构绩效计分。

    L-1 口径参数化（向卫健考核口径过渡）：
    - volume_cap：量类维度（远程诊断/家医履约）封顶次数，达到即满分（默认 5，可按机构规模调大）；
    - include_auto_passed：处方合格率是否将系统自动通过（auto_passed）计为合格（默认 True，
      收紧口径时传 False 仅计药师人工审核通过）。

    `group_id` 按机构协作分组筛选。注意：**排名是在筛选后的集合内产生的**——
    片区内排名与全县排名本来就是两件事，不要把片区第一当成全县第一。
    """
    if volume_cap < 1:
        raise HTTPException(status_code=422, detail="volume_cap 须≥1")
    weights = _normalized_weights(db)
    rx_ok_statuses = ["auto_passed", "approved"] if include_auto_passed else ["approved"]
    scope = resolve_org_scope(db, group_id, org_id)
    orgs_q = db.query(Organization).order_by(Organization.id)
    if scope is not None:
        orgs_q = orgs_q.filter(Organization.id.in_(scope))
    orgs = orgs_q.all()
    results: list[dict[str, Any]] = []
    for org in orgs:
        ref_total = db.query(func.count(Referral.id)).filter(Referral.from_org_id == org.id).scalar() or 0
        ref_completed = (
            db.query(func.count(Referral.id))
            .filter(Referral.from_org_id == org.id, Referral.status == "completed")
            .scalar()
            or 0
        )
        exam_count = (
            db.query(func.count(ExamRequest.id))
            .filter(ExamRequest.from_org_id == org.id, ExamRequest.status.in_(["reported", "recognized"]))
            .scalar()
            or 0
        )
        chronic_total = (
            db.query(func.count(ChronicPatient.id))
            .filter(ChronicPatient.managed_by_org_id == org.id)
            .scalar()
            or 0
        )
        chronic_followed = (
            db.query(func.count(func.distinct(FollowUp.chronic_id)))
            .join(ChronicPatient, FollowUp.chronic_id == ChronicPatient.id)
            .filter(ChronicPatient.managed_by_org_id == org.id)
            .scalar()
            or 0
        )
        rx_total = db.query(func.count(Prescription.id)).filter(Prescription.org_id == org.id).scalar() or 0
        rx_ok = (
            db.query(func.count(Prescription.id))
            .filter(Prescription.org_id == org.id, Prescription.status.in_(rx_ok_statuses))
            .scalar()
            or 0
        )
        contract_services = (
            db.query(func.count(ContractService.id))
            .join(FamilyDoctorContract, ContractService.contract_id == FamilyDoctorContract.id)
            .filter(FamilyDoctorContract.org_id == org.id)
            .scalar()
            or 0
        )

        def ratio(part: int, total: int) -> float:
            return part / total if total else 0.0

        # 量类维度按封顶计分：达到 volume_cap 次即满分（L-1 参数化）
        def volume_score(count: int, cap: int = volume_cap) -> float:
            return min(count, cap) / cap

        # 各维度得分率（0-1）；仅对启用的指标计分
        dimension_ratios = {
            "referral": ratio(ref_completed, ref_total),
            "remote_exam": volume_score(exam_count),
            "chronic": ratio(chronic_followed, chronic_total),
            "rx": ratio(rx_ok, rx_total),
            "contract": volume_score(contract_services),
        }
        score = round(
            sum(dimension_ratios.get(key, 0.0) * weight for key, weight in weights.items()), 1
        )
        results.append(
            {
                "org_id": org.id,
                "org_name": org.name,
                "level": org.level,
                "score": score,
                "detail": {
                    "referral_completion": {"completed": ref_completed, "total": ref_total},
                    "remote_exams": exam_count,
                    "chronic_followup": {"followed": chronic_followed, "total": chronic_total},
                    "rx_pass": {"passed": rx_ok, "total": rx_total},
                    "contract_services": contract_services,
                },
            }
        )
    results.sort(key=lambda r: float(r["score"]), reverse=True)
    return {"weights": weights, "scorecards": results}


# ===========================================================================
# ㉟ 绩效自评改进（整改任务闭环）
# ===========================================================================

# ⚠️ 这是**第二个**挂在 /api/performance 上的路由，而且鉴权比上面那个松：
# 上面的 `router` 是 require_roles("director")，这里是 get_current_user（登录即可）。
#
# 同前缀两套鉴权正是 ADR-0006 problem 里点名的「鉴权分裂」。搬家这一步**刻意不动它**
# ——把两个路由并成一个会把这 5 个端点从"登录可见"收紧到"仅 director"，
# 那是行为变更不是搬家。收益是：此前这个分裂散在两个文件里根本看不见，
# 现在它们并排躺在同一个文件里，谁读都会发现。
# 要不要统一到 director 是一次鉴权口径决策，已登记 ROADMAP 另案。
improvement_router = APIRouter(
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


@improvement_router.post(
    "/improvements", status_code=201, dependencies=[Depends(require_roles("director"))]
)
def create_task(body: TaskCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    """下达绩效整改任务（问题 → 责任人 → 期限）。"""
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    task = ImprovementTask(created_by=user.id, **body.model_dump())
    db.add(task)
    db.commit()
    db.refresh(task)
    return _task_out(task, date.today().isoformat())


@improvement_router.get("/improvements")
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


@improvement_router.post(
    "/improvements/{task_id}/progress",
    dependencies=[Depends(require_roles("director", "operator"))],
)
def progress_task(task_id: int, body: TaskProgress, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """整改进展：登记措施；complete=true 时提交完成确认。"""
    task = db.get(ImprovementTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="整改任务不存在")
    assert_obj_org_writable(db, user, task)
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


@improvement_router.post(
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
    assert_obj_org_writable(db, user, task)
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


@improvement_router.get("/improvement-stats")
def improvement_stats(today: str | None = None, db: Session = Depends(get_db)):
    business_date = resolve_business_date(today).isoformat()
    by_status = row_dict(
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
