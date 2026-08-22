"""医共体绩效考核：按机构自动汇算业务数据，生成绩效评分。

维度（与规划第35项功能对应）：
- 转诊结案率（服务协同）
- 远程诊断服务量（资源下沉）
- 慢病随访覆盖（医防融合）
- 处方合格率（合理用药）
- 家医签约履约量（签约服务）
"""
from datetime import date, datetime
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
    period_bounds,
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
    #: 本次计分覆盖的考核周期（YYYY 或 YYYY-MM）。**必须回给前端**——
    #: 分数从"开天辟地累计"改成"周期内"之后，不标周期的数字是没法解读的。
    period: str
    #: 键来自 `performance_indicators` 表（可增删指标），是**动态**的，
    #: 只能写 dict[str, float]，不能逐个字段写死。
    weights: dict[str, float]
    scorecards: list[OrgScorecard]


@router.get("/orgs", response_model=OrgScorecardsOut)
def org_scorecards(
    period: str | None = None,
    volume_cap: int = 5,
    include_auto_passed: bool = True,
    org_id: int | None = None,
    group_id: int | None = None,
    db: Session = Depends(get_db),
):
    """机构绩效计分（**按考核周期**）。

    `period`：`YYYY` 年度或 `YYYY-MM` 月度，缺省为当年。

    **口径变更（2026-08-21，产品裁定）**：此前本接口不带任何时间维度，算的是
    开天辟地以来的累计数。累计口径的指标只涨不跌、随机构运营年限自然趋近满分，
    考核意义会随时间流失——尤其"慢病随访覆盖"，一个管了三年的患者只要三年前
    随访过一次就永远计入已覆盖。现改为周期口径，详见 `docs/统计口径对照表.md`。

    分母的时间语义**逐项不同**，不是无脑全加时间窗：

    - 转诊 / 远程诊断 / 处方 / 家医服务：分子分母都取**本期发生**的记录；
    - 慢病随访覆盖：分母是**在管存量**患者（不按期），分子是**本期内随访过**的人。
      分母若也按期就变成"本期新入组的有多少随访过"，那是另一个指标。

    L-1 口径参数化（向卫健考核口径过渡）：
    - volume_cap：量类维度（远程诊断/家医履约）封顶次数，达到即满分（默认 5，可按机构规模调大）；
    - include_auto_passed：处方合格率是否将系统自动通过（auto_passed）计为合格（默认 True，
      收紧口径时传 False 仅计药师人工审核通过）。

    `group_id` 按机构协作分组筛选。注意：**排名是在筛选后的集合内产生的**——
    片区内排名与全县排名本来就是两件事，不要把片区第一当成全县第一。
    """
    if volume_cap < 1:
        raise HTTPException(status_code=422, detail="volume_cap 须≥1")
    period, start, end = period_bounds(period)   # UTC 口径，与 created_at 一致
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.min.time())
    weights = _normalized_weights(db)
    rx_ok_statuses = ["auto_passed", "approved"] if include_auto_passed else ["approved"]
    scope = resolve_org_scope(db, group_id, org_id)
    orgs_q = db.query(Organization).order_by(Organization.id)
    if scope is not None:
        orgs_q = orgs_q.filter(Organization.id.in_(scope))
    orgs = orgs_q.all()
    org_ids = [o.id for o in orgs]

    def by_org(query, org_col) -> dict[int, int]:
        """一次分组聚合替代"每机构一条 count"。

        原先这里是 N+1：每家机构 8 条查询，200 家就是 1600 条往返。改成 8 条
        `GROUP BY org_id` 后总条数与机构数无关（`test_查询条数不随机构数增长` 钉住）。
        没出现在结果里的机构就是 0——与原先 `.scalar() or 0` 完全同值。

        `scope is not None` 时才加 `IN` 过滤：全域查询下把全部机构 id 灌进 IN
        是纯开销，分组本来就只会产出有数据的那些机构。
        """
        if scope is not None:
            query = query.filter(org_col.in_(org_ids))
        return {oid: n for oid, n in query.group_by(org_col).all() if oid is not None}

    window = (start_dt, end_dt)
    ref_total_by = by_org(
        db.query(Referral.from_org_id, func.count(Referral.id))
        .filter(Referral.created_at >= window[0], Referral.created_at < window[1]),
        Referral.from_org_id,
    )
    ref_completed_by = by_org(
        db.query(Referral.from_org_id, func.count(Referral.id))
        .filter(Referral.status == "completed",
                Referral.created_at >= window[0], Referral.created_at < window[1]),
        Referral.from_org_id,
    )
    exam_count_by = by_org(
        db.query(ExamRequest.from_org_id, func.count(ExamRequest.id))
        .filter(ExamRequest.status.in_(["reported", "recognized"]),
                ExamRequest.created_at >= window[0], ExamRequest.created_at < window[1]),
        ExamRequest.from_org_id,
    )
    # 分母是"周期结束时的在管存量"：**只设上界、不设下界**。
    # 不设下界 → 三年前入组、至今在管的人今年照样要考核（这正是"存量"的意思）；
    # 但必须设上界 → 否则查 2025 年度的分数会把 2026 年才入组的人算进分母，
    # 历史分数会随新入组不断漂移、永远复现不出来。
    chronic_total_by = by_org(
        db.query(ChronicPatient.managed_by_org_id, func.count(ChronicPatient.id))
        .filter(ChronicPatient.created_at < window[1]),
        ChronicPatient.managed_by_org_id,
    )
    # 分子按期、分母不按期：问的是"在管的这些人里，本期随访到了几个"
    chronic_followed_by = by_org(
        db.query(
            ChronicPatient.managed_by_org_id, func.count(func.distinct(FollowUp.chronic_id))
        )
        .join(ChronicPatient, FollowUp.chronic_id == ChronicPatient.id)
        .filter(FollowUp.created_at >= window[0], FollowUp.created_at < window[1]),
        ChronicPatient.managed_by_org_id,
    )
    rx_total_by = by_org(
        db.query(Prescription.org_id, func.count(Prescription.id))
        .filter(Prescription.created_at >= window[0], Prescription.created_at < window[1]),
        Prescription.org_id,
    )
    rx_ok_by = by_org(
        db.query(Prescription.org_id, func.count(Prescription.id))
        .filter(Prescription.status.in_(rx_ok_statuses),
                Prescription.created_at >= window[0], Prescription.created_at < window[1]),
        Prescription.org_id,
    )
    contract_services_by = by_org(
        db.query(FamilyDoctorContract.org_id, func.count(ContractService.id))
        .join(FamilyDoctorContract, ContractService.contract_id == FamilyDoctorContract.id)
        .filter(ContractService.created_at >= window[0],
                ContractService.created_at < window[1]),
        FamilyDoctorContract.org_id,
    )

    results: list[dict[str, Any]] = []
    for org in orgs:
        ref_total = ref_total_by.get(org.id, 0)
        ref_completed = ref_completed_by.get(org.id, 0)
        exam_count = exam_count_by.get(org.id, 0)
        chronic_total = chronic_total_by.get(org.id, 0)
        chronic_followed = chronic_followed_by.get(org.id, 0)
        rx_total = rx_total_by.get(org.id, 0)
        rx_ok = rx_ok_by.get(org.id, 0)
        contract_services = contract_services_by.get(org.id, 0)

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
    return {"period": period, "weights": weights, "scorecards": results}


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




class ImprovementTaskOut(BaseModel):
    """整改任务出参，与 `_task_out()` 一一对应。"""

    id: int
    org_id: int
    indicator_key: str
    problem: str
    measures: str
    owner_name: str
    due_date: str
    status: str
    status_name: str
    #: 派生字段：未关闭且已过期。不是库里的列，是 `_task_out` 现算的
    overdue: bool
    completion_note: str
    #: 未提交完成时为 null
    completed_at: str | None
    verify_comment: str
    verified_by: str


class StatusCount(BaseModel):
    count: int
    name: str


class ImprovementStatsOut(BaseModel):
    total: int
    #: 键是任务状态码（open/in_progress/completed/verified），只列**出现过的**状态，
    #: 所以是 dict 而不是逐个字段——没有 open 的机构就不该凭空多一个 open: 0
    by_status: dict[str, StatusCount]
    overdue: int
    #: `round(x, 2)` 或字面量 `0.0`，两条分支都是 float
    closed_rate_pct: float


@improvement_router.post(
    "/improvements", status_code=201, response_model=ImprovementTaskOut,
    dependencies=[Depends(require_roles("director"))],
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


@improvement_router.get("/improvements", response_model=list[ImprovementTaskOut])
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
    response_model=ImprovementTaskOut,
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
    "/improvements/{task_id}/verify", response_model=ImprovementTaskOut,
    dependencies=[Depends(require_roles("director"))],
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


@improvement_router.get("/improvement-stats", response_model=ImprovementStatsOut)
def improvement_stats(
    today: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """整改任务汇总。

    **按可见范围过滤**（`stats=True` 用医共体范围，牵头医院能看到片区汇总）。
    此前这个函数连 `user` 参数都没有，任何登录账号拿到的都是**全县**汇总——
    村医、药师都能看到全县有多少条整改任务、多少条超期、闭环率多少。
    这与 `routers/jobs.py` T6.7 整改掉的是同一类问题："任务摘要里带着各类超期
    数量……属于运营管理信息，没有理由对医师、药师开放"。

    更要紧的是它与紧挨着的 `GET /improvements` **对不上**：那个用
    `scope_org_list` 只给本机构的明细，这个却给全县的汇总。同一个页面上
    （`pages-public.js` 把两者放在一个 Promise.all 里同时取），
    列表显示"本机构 2 条"、上面的汇总却写"全县 87 条"——既漏数据又自相矛盾。

    全域角色（admin/director）的可见范围本就是全域，响应与整改前一模一样。
    """
    business_date = resolve_business_date(today).isoformat()
    by_status = row_dict(
        scope_org_list(
            db, user,
            db.query(ImprovementTask.status, func.count(ImprovementTask.id)),
            ImprovementTask, None, stats=True,
        )
        .group_by(ImprovementTask.status)
        .all()
    )
    overdue = (
        scope_org_list(
            db, user, db.query(func.count(ImprovementTask.id)),
            ImprovementTask, None, stats=True,
        )
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
