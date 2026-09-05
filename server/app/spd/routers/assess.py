"""全域慢专病 · 考核域：指标库、分级考核方案、自动取数计分、下钻、村医积分与兑换。

对应招标文件：平台管理端 #17~#20、卫健管理端 #12、全程管理中心端 #6/#7/#19、
服务团队专家端 #11、医生移动端 #19/#20。

## 计分是怎么算出来的

指标不写死在代码里，而是"取数口径（`data_source`）+ 公式（`formula`）+
评分规则（`score_rule`）"三段式：

1. `data_source` 决定去哪张表数数——本文件的 `INDICATOR_METRICS` 是唯一的
   取数实现，每个口径返回一组命名数字（`{"total": 40, "done": 36}`）；
2. `formula` 用这些数字算出指标值，走既有的 `formula.evaluate`（AST 白名单）；
3. `score_rule` 把指标值折成得分。

分三段而不是让人直接写 SQL：SQL 口径一旦开放，各县会写出各自的取数逻辑，
数字对不上时无从对账；而这三段里唯一可自由填写的公式是受限表达式。
"""
from typing import Any

from datetime import date, timedelta
from secrets import randbelow

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...clock import now_naive
from ...concurrency import add_amount, ensure_present, insert_if_absent, take_amount
from ...database import get_db
from ...deps import get_current_user, paginate, require_roles
from ...formula import FormulaError, evaluate as eval_formula
from ..platform import Organization, User
from ..models import (
    SpdAssessPlan,
    SpdAssessment,
    SpdCaseReport,
    SpdEnrollment,
    SpdGoods,
    SpdIndicator,
    SpdMeasurement,
    SpdPathInstance,
    SpdPointAccount,
    SpdPointRecord,
    SpdPointRule,
    SpdRedeem,
    SpdReferralCase,
    SpdScore,
    SpdSignin,
    SpdTask,
    SpdTeam,
    SpdVillageDoctor,
)
from ...visibility import visible_org_ids

router = APIRouter(
    prefix="/api/spd",
    tags=["全域慢专病·考核"],
    dependencies=[Depends(get_current_user)],
)


# ============================================================ 响应契约
#
# 模型集中放在所有端点之前（`response_model=` 是装饰器参数，导入时求值）。
# 字段与各 handler 的当前出参**逐字段逐序**对应（治理不得改响应字节，第7条）。
# 本模块没有 Money 列，数值分两类：Float 列（weight / target_value / total_score
# 及其 round() 派生值）整数取值读回来带 `.0`，声明 float 才是原样；Integer 列
# （points / balance / stock / rank）是裸 int。两类取值都有契约测试钉住
# （tests/test_spd_assess_contract.py）。


class IndicatorOut(BaseModel):
    id: int
    code: str
    name: str
    program_codes: list[str]
    object_type: str
    data_source: str
    scope_expr: str
    formula: str
    # 评分规则原始 JSON（ratio / step 两种形状），照存照出
    score_rule: dict[str, Any]
    weight: float
    target_value: float | None
    abnormal_rule: str
    version: str
    effective_from: str
    effective_scope: str
    active: bool


class IndicatorPlanRefOut(BaseModel):
    """引用该指标的考核方案。`weight` 取的是方案 `items` 里的**原始 JSON 值**：
    int 就是 int（100 不是 100.0），该项没配权重时为 null——别学 IndicatorOut
    的 Float 列声明。"""

    id: int
    code: str
    name: str
    level: str
    weight: int | float | None


class IndicatorUsageOut(BaseModel):
    indicator: IndicatorOut
    plans: list[IndicatorPlanRefOut]
    used_by: int


class PlanOut(BaseModel):
    id: int
    code: str
    name: str
    level: str
    program_codes: list[str]
    object_type: str
    period_type: str
    # [{"indicator_code": ..., "weight": ...}] 原始 JSON，照存照出
    items: list[dict[str, Any]]
    active: bool


class ScoreRankRowOut(BaseModel):
    """排名行：`/scores/run` 的 top 与 `/scores-analysis` 的 ranking 同形共用。"""

    object_id: int
    object_name: str
    total_score: float
    rank: int


class RunScoreOut(BaseModel):
    plan: PlanOut
    period: str
    scored: int
    top: list[ScoreRankRowOut]


class ScoreRowOut(BaseModel):
    id: int
    plan_id: int
    period: str
    object_type: str
    object_id: int
    object_name: str
    program_code: str
    total_score: float
    rank: int
    created_at: str


class ScoreDetailOut(BaseModel):
    id: int
    # 方案被物理删除时为 null（当前无删除接口，防御性与 handler 一致）
    plan: PlanOut | None
    period: str
    object_type: str
    object_id: int
    object_name: str
    total_score: float
    rank: int
    # 分项明细是**多态行**：正常项十个键（metrics/value/raw_score/…），指标缺失或
    # 公式失败的项只有 indicator_code + error 两个键。逐字段建模会把两种行的键
    # 互相注入 null，故宽字典；error 键自描述行的形状（有契约测试钉两种行）。
    detail: list[dict[str, Any]]
    created_at: str


class ScoreDeductionOut(BaseModel):
    indicator_code: str
    indicator_name: str
    count: int
    total_deduction: float


class ScoreAnalysisEmptyOut(BaseModel):
    """得分分析·无数据分支。**键序与满分支不同**（average 在最后、没有 ranking），
    一个模型排不出两种顺序，所以是二选一联合的左支：`extra="forbid"` 让带
    ranking 的满分支进不来，反向由满分支的必填 ranking 挡住空分支——两条分支
    各自按各自的声明序序列化，字节与治理前一致（契约测试钉住两种键序）。"""

    model_config = ConfigDict(extra="forbid")

    total: int
    distribution: dict[str, int]
    top_deductions: list[ScoreDeductionOut]
    average: float


class ScoreAnalysisOut(BaseModel):
    """得分分析·有数据分支：见 `ScoreAnalysisEmptyOut` 的联合说明。"""

    total: int
    average: float
    # 分布桶的键（"90+"/"80-89"/…）由 handler 维护，宽键映射
    distribution: dict[str, int]
    top_deductions: list[ScoreDeductionOut]
    ranking: list[ScoreRankRowOut]


class WorkloadItemOut(BaseModel):
    object_id: int
    total: int
    done: int
    # 任务类型 → 办结数，键随任务类型扩充
    by_type: dict[str, int]
    object_name: str
    completion_rate: float


class WorkloadOut(BaseModel):
    period: str
    object_type: str
    items: list[WorkloadItemOut]


class PointRuleCreatedOut(BaseModel):
    """新建回执与列表行键集合不同（无 name/daily_limit/active），两个模型。"""

    id: int
    code: str
    event: str
    points: int


class PointRuleOut(BaseModel):
    id: int
    code: str
    name: str
    event: str
    points: int
    daily_limit: int
    active: bool


class PointRuleUpdatedOut(BaseModel):
    id: int
    points: int
    active: bool


class PointRecordOut(BaseModel):
    id: int
    rule_code: str
    direction: str
    points: int
    balance_after: int
    note: str
    created_at: str


class MyPointsOut(BaseModel):
    """本人积分账户。**无账户分支没有 `account_id` 键**——不是 null，是整个键
    不出现，所以它声明在最前并配 `response_model_exclude_unset=True`；其余四个
    键两条分支都有且顺序一致，同一个模型对齐两种形状。"""

    account_id: int | None = None
    balance: int
    earned: int
    used: int
    records: list[PointRecordOut]


class PointAccountOut(BaseModel):
    id: int
    user_id: int
    user_name: str
    org_id: int | None
    balance: int
    earned: int
    used: int


class SigninOut(BaseModel):
    points: int
    balance: int


class GoodsCreatedOut(BaseModel):
    id: int
    code: str
    name: str
    stock: int


class GoodsOut(BaseModel):
    id: int
    code: str
    name: str
    points: int
    stock: int
    image_url: str
    active: bool


class GoodsUpdatedOut(BaseModel):
    id: int
    stock: int
    active: bool


class RedeemCreatedOut(BaseModel):
    id: int
    verify_code: str
    balance: int


class RedeemOut(BaseModel):
    id: int
    goods_id: int
    goods_name: str
    points: int
    verify_code: str
    status: str
    created_at: str
    # 未核销是空串不是 null（isoformat() if ... else ""）
    verified_at: str


class RedeemVerifiedOut(BaseModel):
    id: int
    status: str


# ============================================================ 指标库


class IndicatorIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    program_codes: list[str] = Field(default_factory=list)
    object_type: str = Field(default="org", pattern="^(org|doctor|village_doctor|team)$")
    data_source: str = Field(
        default="task",
        pattern="^(task|enrollment|path|referral|measurement|assessment|archive|case_report)$",
    )
    scope_expr: str = Field(default="", max_length=256)
    formula: str = Field(default="", max_length=256)
    score_rule: dict = Field(default_factory=dict)
    weight: float = Field(default=1.0, ge=0, le=1000)
    target_value: float | None = None
    abnormal_rule: str = Field(default="", max_length=256)
    version: str = Field(default="v1", max_length=16)
    effective_from: str = Field(default="", max_length=10)
    effective_scope: str = Field(default="region", max_length=64)


def _indicator_out(i: SpdIndicator) -> dict:
    return {
        "id": i.id, "code": i.code, "name": i.name,
        "program_codes": i.program_codes or [], "object_type": i.object_type,
        "data_source": i.data_source, "scope_expr": i.scope_expr, "formula": i.formula,
        "score_rule": i.score_rule or {}, "weight": i.weight,
        "target_value": i.target_value, "abnormal_rule": i.abnormal_rule,
        "version": i.version, "effective_from": i.effective_from,
        "effective_scope": i.effective_scope, "active": i.active,
    }


@router.post("/indicators", response_model=IndicatorOut, status_code=201,
             dependencies=[Depends(require_roles("director"))])
def create_indicator(body: IndicatorIn, db: Session = Depends(get_db)):
    if body.formula:
        try:
            eval_formula(body.formula, dict.fromkeys(_metric_names(body.data_source), 1.0))
        except FormulaError as exc:
            raise HTTPException(status_code=422, detail=f"公式非法：{exc}") from None
    indicator = SpdIndicator(**body.model_dump())
    db.add(indicator)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该指标编码与版本已存在") from None
    return _indicator_out(indicator)


@router.get("/indicators", response_model=list[IndicatorOut])
def list_indicators(
    response: Response,
    object_type: str | None = None,
    active: bool | None = None,
    program_code: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdIndicator)
    if object_type:
        query = query.filter(SpdIndicator.object_type == object_type)
    if active is not None:
        query = query.filter(SpdIndicator.active.is_(active))
    rows = paginate(query.order_by(SpdIndicator.id), response, offset, limit)
    if program_code:
        rows = [
            i for i in rows
            if not (i.program_codes or []) or program_code in (i.program_codes or [])
        ]
    return [_indicator_out(i) for i in rows]


@router.patch("/indicators/{indicator_id}", response_model=IndicatorOut,
              dependencies=[Depends(require_roles("director"))])
def update_indicator(indicator_id: int, body: dict, db: Session = Depends(get_db)):
    indicator = db.get(SpdIndicator, indicator_id)
    if indicator is None:
        raise HTTPException(status_code=404, detail="指标不存在")
    if "formula" in body and body["formula"]:
        try:
            eval_formula(
                body["formula"],
                dict.fromkeys(_metric_names(body.get("data_source", indicator.data_source)), 1.0),
            )
        except FormulaError as exc:
            raise HTTPException(status_code=422, detail=f"公式非法：{exc}") from None
    for key in ("name", "program_codes", "data_source", "scope_expr", "formula",
                "score_rule", "weight", "target_value", "abnormal_rule",
                "effective_from", "effective_scope", "active"):
        if key in body:
            setattr(indicator, key, body[key])
    db.commit()
    return _indicator_out(indicator)


@router.get("/indicators/{indicator_id}/usage", response_model=IndicatorUsageOut)
def indicator_usage(indicator_id: int, db: Session = Depends(get_db)):
    """指标使用情况：被哪些考核方案引用（平台管理端 #17"使用情况查询"）。"""
    indicator = db.get(SpdIndicator, indicator_id)
    if indicator is None:
        raise HTTPException(status_code=404, detail="指标不存在")
    plans = [
        {"id": p.id, "code": p.code, "name": p.name, "level": p.level,
         "weight": next(
             (i.get("weight") for i in p.items or [] if i.get("indicator_code") == indicator.code),
             None,
         )}
        for p in db.query(SpdAssessPlan).all()
        if any(i.get("indicator_code") == indicator.code for i in p.items or [])
    ]
    return {"indicator": _indicator_out(indicator), "plans": plans, "used_by": len(plans)}


# ============================================================ 取数口径


def _metric_names(data_source: str) -> tuple[str, ...]:
    """各取数口径产出的变量名——公式只能引用这些名字。"""
    return {
        "task": ("total", "done", "overdue"),
        "enrollment": ("enrolled", "target", "high_risk"),
        "path": ("total", "completed", "running"),
        "referral": ("total", "closed", "effective"),
        "measurement": ("total", "normal", "abnormal"),
        "assessment": ("assessed", "enrolled"),
        "archive": ("archived", "enrolled"),
        "case_report": ("reported", "handled"),
    }.get(data_source, ("total",))


def _period_range(period: str) -> tuple[str, str]:
    """把 `2026-08` / `2026-Q3` / `2026` 展开成 [起, 止] 日期字符串。"""
    if len(period) == 4 and period.isdigit():
        return f"{period}-01-01", f"{period}-12-31"
    if "-Q" in period:
        year, quarter = period.split("-Q")
        start_month = (int(quarter) - 1) * 3 + 1
        end_month = start_month + 2
        last_day = 31 if end_month in (1, 3, 5, 7, 8, 10, 12) else 30
        return f"{year}-{start_month:02d}-01", f"{year}-{end_month:02d}-{last_day}"
    year, month = period.split("-")[:2]
    nxt = date(int(year) + (int(month) == 12), (int(month) % 12) + 1, 1)
    return f"{year}-{month}-01", (nxt - timedelta(days=1)).isoformat()


def _object_column(model, object_type: str):
    """"考核对象"在模型上的归属列；模型没有对应列时返回 None（不过滤）。

    village_doctor 的归属依据是 `village_doctor_id`（在管档案）或
    `assignee_id`（任务），不是机构——一个村卫生室可能有两位村医。
    """
    candidates = {
        "org": ("org_id", "initiator_org_id"),
        "team": ("team_id",),
        "doctor": ("assignee_id", "doctor_user_id", "operator_id", "initiator_id"),
        "village_doctor": ("village_doctor_id", "assignee_id", "initiator_id", "reporter_id"),
    }.get(object_type, ())
    for name in candidates:
        if hasattr(model, name):
            return getattr(model, name)
    return None


def collect_metrics(
    db: Session, indicator: SpdIndicator, object_type: str, object_id: int,
    period: str, program_code: str = "",
) -> dict[str, float]:
    """按取数口径统计**一个**考核对象在一个周期内的原始数字。

    单对象就是 N=1 的批量——委托给 `collect_metrics_batch`，让报告段落、
    下钻等单对象调用与整表计分**结构上共用同一份口径**：两套实现迟早会在
    某个数字上算出两个结果，而考核数字要进绩效。
    """
    return collect_metrics_batch(
        db, indicator, object_type, [object_id], period, program_code
    )[object_id]


def collect_metrics_batch(
    db: Session, indicator: SpdIndicator, object_type: str, object_ids: list[int],
    period: str, program_code: str = "",
) -> dict[int, dict[str, float]]:
    """一个指标 × N 个考核对象的批量取数：查询数与对象数无关。

    P2-1 之前整表计分是对象 × 指标逐个查询——一个县 20 家机构 × 10 个指标
    一次考核要打几百条 SQL。这里的做法：

    - 有对象归属列的表（任务/转诊/上报）：一条 GROUP BY + 条件聚合；
    - 从"在管档案"出发的口径（纳管/评估/路径/监测）：档案取一次、按对象分桶，
      再对下游表各打一条 IN 查询，在内存里归到对象上。

    模型上没有对象归属列时退化为全局数字、人人相同——与单对象版
    "翻译不出过滤条件就不过滤"的行为一致。
    """
    from sqlalchemy import case

    ids = list(dict.fromkeys(object_ids))
    if not ids:
        return {}
    start, end = _period_range(period)
    source = indicator.data_source

    def prog(model, query):
        if program_code and hasattr(model, "program_code"):
            query = query.filter(model.program_code == program_code)
        return query

    def grouped(model, query, columns: dict) -> dict[int, dict[str, float]]:
        """按对象归属列 GROUP BY 聚合；没有归属列时退化为一条全局查询。"""
        col = _object_column(model, object_type)
        query = prog(model, query)
        if col is None:
            row = query.with_entities(*columns.values()).one()
            values = {k: float(v or 0) for k, v in zip(columns, row)}
            return {oid: dict(values) for oid in ids}
        rows = (
            query.filter(col.in_(ids))
            .with_entities(col, *columns.values())
            .group_by(col)
            .all()
        )
        found = {r[0]: {k: float(v or 0) for k, v in zip(columns, r[1:])} for r in rows}
        return {oid: found.get(oid, {k: 0.0 for k in columns}) for oid in ids}

    def _count_if(cond):
        return func.sum(case((cond, 1), else_=0))

    if source == "task":
        return grouped(
            SpdTask,
            db.query(SpdTask).filter(
                SpdTask.created_at >= f"{start} 00:00:00",
                SpdTask.created_at <= f"{end} 23:59:59",
            ),
            {"total": func.count(SpdTask.id),
             "done": _count_if(SpdTask.status == "done"),
             "overdue": _count_if(SpdTask.status == "overdue")},
        )
    if source == "referral":
        return grouped(
            SpdReferralCase,
            db.query(SpdReferralCase).filter(
                SpdReferralCase.created_at >= f"{start} 00:00:00",
                SpdReferralCase.created_at <= f"{end} 23:59:59",
            ),
            {"total": func.count(SpdReferralCase.id),
             "closed": _count_if(SpdReferralCase.status == "closed"),
             "effective": _count_if(SpdReferralCase.effective_visit.is_(True))},
        )
    if source == "case_report":
        return grouped(
            SpdCaseReport,
            db.query(SpdCaseReport).filter(
                SpdCaseReport.created_at >= f"{start} 00:00:00",
                SpdCaseReport.created_at <= f"{end} 23:59:59",
            ),
            {"reported": func.count(SpdCaseReport.id),
             "handled": _count_if(SpdCaseReport.status.in_(["done", "closed"]))},
        )

    if source not in ("enrollment", "archive", "assessment", "path", "measurement"):
        return {oid: {"total": 0.0} for oid in ids}

    # ---- 以下口径都从"该对象名下的在管档案"出发：档案取一次、按对象分桶
    enroll_col = _object_column(SpdEnrollment, object_type)
    enroll_query = prog(SpdEnrollment, db.query(SpdEnrollment))
    if source in ("enrollment", "archive", "assessment"):
        enroll_query = enroll_query.filter(SpdEnrollment.created_at <= f"{end} 23:59:59")
    if enroll_col is not None:
        enroll_query = enroll_query.filter(enroll_col.in_(ids))
    buckets: dict[int, list[SpdEnrollment]] = {oid: [] for oid in ids}
    for e in enroll_query.all():
        owners = ids if enroll_col is None else (
            [getattr(e, enroll_col.key)] if getattr(e, enroll_col.key) in buckets else []
        )
        for oid in owners:
            buckets[oid].append(e)

    if source == "enrollment":
        from ..models import SpdCandidate

        cand_query = prog(SpdCandidate, db.query(SpdCandidate).filter(
            SpdCandidate.status.in_(["target", "enrolled"])))
        if object_type == "org":
            cand_rows = dict(
                cand_query.filter(SpdCandidate.org_id.in_(ids))
                .with_entities(SpdCandidate.org_id, func.count(SpdCandidate.id))
                .group_by(SpdCandidate.org_id).all()
            )
            targets = {oid: cand_rows.get(oid, 0) for oid in ids}
        else:
            total = cand_query.count()  # 单对象版对非机构对象也不按对象过滤目标池
            targets = {oid: total for oid in ids}
        out = {}
        for oid in ids:
            enrolled = sum(1 for e in buckets[oid] if e.status == "active")
            out[oid] = {
                "enrolled": float(enrolled),
                "target": float(targets[oid] or enrolled),
                "high_risk": float(
                    sum(1 for e in buckets[oid] if e.risk_level in ("high", "very_high"))
                ),
            }
        return out
    if source == "archive":
        return {
            oid: {
                "enrolled": float(sum(1 for e in buckets[oid] if e.status == "active")),
                "archived": float(
                    sum(1 for e in buckets[oid] if e.status == "active" and e.archived)
                ),
            }
            for oid in ids
        }
    if source == "assessment":
        patients = {
            oid: {e.patient_id for e in buckets[oid] if e.status == "active"}
            for oid in ids
        }
        union = set().union(*patients.values()) if patients else set()
        assessed = {
            pid for (pid,) in db.query(SpdAssessment.patient_id)
            .filter(
                SpdAssessment.patient_id.in_(union or [0]),
                SpdAssessment.created_at >= f"{start} 00:00:00",
                SpdAssessment.created_at <= f"{end} 23:59:59",
            )
            .distinct().all()
        }
        return {
            oid: {
                "enrolled": float(sum(1 for e in buckets[oid] if e.status == "active")),
                "assessed": float(len(patients[oid] & assessed)),
            }
            for oid in ids
        }
    if source == "path":
        enroll_owner: dict[int, list[int]] = {}
        for oid in ids:
            for e in buckets[oid]:
                enroll_owner.setdefault(e.id, []).append(oid)
        rows = (
            db.query(SpdPathInstance.enrollment_id, SpdPathInstance.status)
            .filter(
                SpdPathInstance.enrollment_id.in_(list(enroll_owner) or [0]),
                SpdPathInstance.started_at <= f"{end} 23:59:59",
            )
            .all()
        )
        out = {oid: {"total": 0.0, "completed": 0.0, "running": 0.0} for oid in ids}
        for enrollment_id, status in rows:
            for oid in enroll_owner.get(enrollment_id, []):
                out[oid]["total"] += 1
                if status in ("completed", "running"):
                    out[oid][status] += 1
        return out
    # measurement
    patients = {
        oid: {e.patient_id for e in buckets[oid] if e.status == "active"}
        for oid in ids
    }
    union = set().union(*patients.values()) if patients else set()
    rows = (
        db.query(SpdMeasurement.patient_id, SpdMeasurement.level)
        .filter(
            SpdMeasurement.patient_id.in_(union or [0]),
            SpdMeasurement.measured_at >= f"{start} 00:00:00",
            SpdMeasurement.measured_at <= f"{end} 23:59:59",
        )
        .all()
    )
    out = {oid: {"total": 0.0, "normal": 0.0, "abnormal": 0.0} for oid in ids}
    for patient_id, level in rows:
        for oid in ids:
            if patient_id in patients[oid]:
                out[oid]["total"] += 1
                out[oid]["normal" if level == "normal" else "abnormal"] += 1
    return out


def score_of(indicator: SpdIndicator, value: float) -> tuple[float, str]:
    """把指标值折成得分，返回 (得分, 扣分理由)。

    两种评分规则：
    - `ratio`：达标即满分，未达标按比例给分（`full * value / target`）；
    - `step`：分档给分，取第一个命中的档。

    没配规则时按"值即得分"处理并截到 0~100——总比整张考核表算不出来强，
    但会在理由里写明"未配置评分规则"，让人知道这个数不是精心设计的。
    """
    rule = indicator.score_rule or {}
    kind = rule.get("type", "")
    if kind == "ratio":
        full = float(rule.get("full", 100))
        target = float(rule.get("target", indicator.target_value or 100) or 100)
        if value >= target:
            return full, ""
        got = round(full * value / target, 2) if target else 0.0
        return got, f"未达目标值{target}（实际{round(value, 2)}）"
    if kind == "step":
        for step in rule.get("steps", []):
            low, high = step.get("min"), step.get("max")
            if (low is None or value >= float(low)) and (high is None or value <= float(high)):
                return float(step.get("score", 0)), step.get("reason", "")
        return 0.0, "未落入任何评分档"
    return max(0.0, min(value, 100.0)), "未配置评分规则，按指标值计分"


# ============================================================ 考核方案


class PlanIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    level: str = Field(default="township", pattern="^(hospital|township|station|village|team)$")
    program_codes: list[str] = Field(default_factory=list)
    object_type: str = Field(default="org", pattern="^(org|doctor|village_doctor|team)$")
    period_type: str = Field(default="month", pattern="^(month|quarter|year)$")
    items: list[dict] = Field(default_factory=list)


def _plan_out(p: SpdAssessPlan) -> dict:
    return {
        "id": p.id, "code": p.code, "name": p.name, "level": p.level,
        "program_codes": p.program_codes or [], "object_type": p.object_type,
        "period_type": p.period_type, "items": p.items or [], "active": p.active,
    }


@router.post("/assess-plans", response_model=PlanOut, status_code=201,
             dependencies=[Depends(require_roles("director"))])
def create_plan(body: PlanIn, db: Session = Depends(get_db)):
    codes: list[Any] = [i.get("indicator_code") for i in body.items]
    if not codes:
        raise HTTPException(status_code=422, detail="考核方案至少要有一个指标")
    known = {
        code for (code,) in db.query(SpdIndicator.code).filter(SpdIndicator.code.in_(codes)).all()
    }
    missing = [c for c in codes if c not in known]
    if missing:
        raise HTTPException(status_code=422, detail=f"以下指标不存在：{'、'.join(missing)}")
    plan = SpdAssessPlan(**body.model_dump())
    db.add(plan)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该考核方案编码已存在") from None
    return _plan_out(plan)


@router.get("/assess-plans", response_model=list[PlanOut])
def list_plans(level: str | None = None, active: bool | None = None,
               db: Session = Depends(get_db)):
    query = db.query(SpdAssessPlan)
    if level:
        query = query.filter(SpdAssessPlan.level == level)
    if active is not None:
        query = query.filter(SpdAssessPlan.active.is_(active))
    return [_plan_out(p) for p in query.order_by(SpdAssessPlan.id).limit(200).all()]


@router.patch("/assess-plans/{plan_id}", response_model=PlanOut,
              dependencies=[Depends(require_roles("director"))])
def update_plan(plan_id: int, body: dict, db: Session = Depends(get_db)):
    plan = db.get(SpdAssessPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="考核方案不存在")
    for key in ("name", "level", "program_codes", "object_type", "period_type",
                "items", "active"):
        if key in body:
            setattr(plan, key, body[key])
    db.commit()
    return _plan_out(plan)


# ============================================================ 计分


class RunScoreIn(BaseModel):
    plan_id: int
    period: str = Field(min_length=4, max_length=16)
    program_code: str = Field(default="", max_length=32)
    object_ids: list[int] = Field(default_factory=list)


def _objects_of(db: Session, plan: SpdAssessPlan, object_ids: list[int]) -> list[tuple[int, str]]:
    """考核对象清单：给了 id 就按 id，没给就按方案层级全量展开。"""
    if plan.object_type == "org":
        query = db.query(Organization)
        if object_ids:
            query = query.filter(Organization.id.in_(object_ids))
        elif plan.level in ("hospital", "township", "village"):
            level_map = {"hospital": "county", "township": "township", "village": "village"}
            query = query.filter(Organization.level == level_map[plan.level])
        return [(o.id, o.name) for o in query.limit(500).all()]
    if plan.object_type == "team":
        team_query = db.query(SpdTeam).filter(SpdTeam.active.is_(True))
        if object_ids:
            team_query = team_query.filter(SpdTeam.id.in_(object_ids))
        return [(t.id, t.name) for t in team_query.limit(500).all()]
    if plan.object_type == "village_doctor":
        doctor_query = db.query(SpdVillageDoctor).filter(SpdVillageDoctor.active.is_(True))
        if object_ids:
            doctor_query = doctor_query.filter(SpdVillageDoctor.user_id.in_(object_ids))
        rows = doctor_query.limit(500).all()
        names = {
            u.id: u.full_name or u.username
            for u in db.query(User).filter(User.id.in_([v.user_id for v in rows] or [0]))
        }
        return [(v.user_id, names.get(v.user_id, "")) for v in rows]
    user_query = db.query(User).filter(User.role.in_(["doctor", "public_health"]))
    if object_ids:
        user_query = user_query.filter(User.id.in_(object_ids))
    return [(u.id, u.full_name or u.username) for u in user_query.limit(500).all()]


@router.post("/scores/run", response_model=RunScoreOut,
             dependencies=[Depends(require_roles("director"))])
def run_scoring(body: RunScoreIn, db: Session = Depends(get_db)):
    """按方案跑一次考核计分，写入 `SpdScore`（含分项明细与扣分依据）。

    重跑同一周期会**覆盖**上次结果（唯一键 plan+period+object）：考核期内数据
    还在变，保留多份历史结果只会让人不知道该看哪一份；需要历史留痕的是
    `detail` 里的原始数字，那部分每次都完整写入。
    """
    plan = db.get(SpdAssessPlan, body.plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="考核方案不存在")
    indicators = {
        i.code: i
        for i in db.query(SpdIndicator)
        .filter(
            SpdIndicator.code.in_([i.get("indicator_code") for i in plan.items or []]),
            SpdIndicator.active.is_(True),
        )
        .all()
    }
    objects = _objects_of(db, plan, body.object_ids)
    # 每个指标一次批量取数（P2-1）：查询数只随指标数增长，不随对象数增长
    all_ids = [object_id for object_id, _ in objects]
    metrics_by_code = {
        code: collect_metrics_batch(
            db, indicator, plan.object_type, all_ids, body.period, body.program_code
        )
        for code, indicator in indicators.items()
    }
    results = []
    for object_id, object_name in objects:
        total_score, detail = 0.0, []
        for item in plan.items or []:
            code = item.get("indicator_code")
            indicator = indicators.get(code)
            if indicator is None:
                detail.append({"indicator_code": code, "error": "指标不存在或已停用"})
                continue
            metrics = metrics_by_code[code][object_id]
            try:
                value = (
                    eval_formula(indicator.formula, metrics)
                    if indicator.formula else float(metrics.get("total", 0))
                )
            except FormulaError as exc:
                detail.append({"indicator_code": code, "error": f"公式求值失败：{exc}"})
                continue
            score, reason = score_of(indicator, value)
            weight = float(item.get("weight", indicator.weight) or 0)
            weighted = round(score * weight / 100, 2)
            total_score += weighted
            detail.append({
                "indicator_code": code, "indicator_name": indicator.name,
                "metrics": metrics, "value": round(value, 2), "raw_score": score,
                "weight": weight, "score": weighted,
                "deduction": round(weight - weighted, 2), "reason": reason,
                "target_value": indicator.target_value,
            })
        # 先查后插在并发重跑时会双双插入并撞唯一约束（plan+period+object），
        # 用 SAVEPOINT 版的"不在就插"把冲突圈在单行内，冲突时退回来更新既有行。
        record = SpdScore(
            plan_id=plan.id, period=body.period, object_type=plan.object_type,
            object_id=object_id,
        )
        if not insert_if_absent(db, record):
            record = ensure_present((
                db.query(SpdScore)
                .filter(
                    SpdScore.plan_id == plan.id, SpdScore.period == body.period,
                    SpdScore.object_type == plan.object_type,
                    SpdScore.object_id == object_id,
                )
                .first()
            ), "考核记录")
        record.object_name = object_name
        record.program_code = body.program_code
        record.total_score = round(total_score, 2)
        record.detail = detail
        record.created_at = now_naive()
        results.append(record)

    results.sort(key=lambda r: r.total_score, reverse=True)
    for index, record in enumerate(results, start=1):
        record.rank = index
    db.commit()
    return {
        "plan": _plan_out(plan), "period": body.period, "scored": len(results),
        "top": [
            {"object_id": r.object_id, "object_name": r.object_name,
             "total_score": r.total_score, "rank": r.rank}
            for r in results[:10]
        ],
    }


@router.get("/scores", response_model=list[ScoreRowOut])
def list_scores(
    response: Response,
    plan_id: int | None = None,
    period: str | None = None,
    object_type: str | None = None,
    object_id: int | None = None,
    program_code: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdScore)
    for column, value in (
        (SpdScore.plan_id, plan_id), (SpdScore.period, period),
        (SpdScore.object_type, object_type), (SpdScore.object_id, object_id),
        (SpdScore.program_code, program_code),
    ):
        if value is not None and value != "":
            query = query.filter(column == value)
    rows = paginate(query.order_by(SpdScore.rank, SpdScore.id), response, offset, limit)
    return [
        {"id": r.id, "plan_id": r.plan_id, "period": r.period,
         "object_type": r.object_type, "object_id": r.object_id,
         "object_name": r.object_name, "program_code": r.program_code,
         "total_score": r.total_score, "rank": r.rank,
         "created_at": r.created_at.isoformat()}
        for r in rows
    ]


@router.get("/scores/{score_id}", response_model=ScoreDetailOut)
def score_detail(score_id: int, db: Session = Depends(get_db)):
    """下钻到指标、原始数据与扣分依据（卫健端 #12）。"""
    record = db.get(SpdScore, score_id)
    if record is None:
        raise HTTPException(status_code=404, detail="考核结果不存在")
    plan = db.get(SpdAssessPlan, record.plan_id)
    return {
        "id": record.id, "plan": _plan_out(plan) if plan else None,
        "period": record.period, "object_type": record.object_type,
        "object_id": record.object_id, "object_name": record.object_name,
        "total_score": record.total_score, "rank": record.rank,
        "detail": record.detail or [],
        "created_at": record.created_at.isoformat(),
    }


@router.get("/scores-analysis", response_model=ScoreAnalysisEmptyOut | ScoreAnalysisOut)
def score_analysis(
    plan_id: int, period: str, db: Session = Depends(get_db)
):
    """得分分布与高频扣分项分析（卫健端 #12）。"""
    rows = (
        db.query(SpdScore)
        .filter(SpdScore.plan_id == plan_id, SpdScore.period == period)
        .all()
    )
    if not rows:
        return {"total": 0, "distribution": {}, "top_deductions": [], "average": 0.0}
    buckets = {"90+": 0, "80-89": 0, "70-79": 0, "60-69": 0, "<60": 0}
    for row in rows:
        score = row.total_score
        key = ("90+" if score >= 90 else "80-89" if score >= 80 else
               "70-79" if score >= 70 else "60-69" if score >= 60 else "<60")
        buckets[key] += 1
    deductions: dict[str, dict] = {}
    for row in rows:
        for item in row.detail or []:
            if item.get("deduction", 0) <= 0:
                continue
            entry = deductions.setdefault(
                item.get("indicator_code", ""),
                {"indicator_code": item.get("indicator_code", ""),
                 "indicator_name": item.get("indicator_name", ""),
                 "count": 0, "total_deduction": 0.0},
            )
            entry["count"] += 1
            entry["total_deduction"] = round(
                entry["total_deduction"] + item.get("deduction", 0), 2
            )
    return {
        "total": len(rows),
        "average": round(sum(r.total_score for r in rows) / len(rows), 2),
        "distribution": buckets,
        "top_deductions": sorted(
            deductions.values(), key=lambda d: d["total_deduction"], reverse=True
        )[:10],
        "ranking": [
            {"object_id": r.object_id, "object_name": r.object_name,
             "total_score": r.total_score, "rank": r.rank}
            for r in sorted(rows, key=lambda r: r.rank or 999)[:50]
        ],
    }


# ============================================================ 工作量统计


@router.get("/workload", response_model=WorkloadOut)
def workload(
    object_type: str = "doctor",
    period: str = "",
    org_id: int | None = None,
    program_code: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """按机构与人员统计服务工作量（中心端 #19、卫健端 #11、专家端 #11）。

    统计口径固定为"已完成任务数按类型拆分 + 纳管数 + 转诊数"，与考核指标
    共用同一批表——工作量报表和考核得分对不上，是这类系统最常见的投诉。
    """
    period = period or date.today().strftime("%Y-%m")
    start, end = _period_range(period)
    task_query = db.query(SpdTask).filter(
        SpdTask.created_at >= f"{start} 00:00:00", SpdTask.created_at <= f"{end} 23:59:59"
    )
    orgs = visible_org_ids(db, user)
    if orgs is not None:
        task_query = task_query.filter(SpdTask.org_id.in_(orgs))
    if org_id is not None:
        task_query = task_query.filter(SpdTask.org_id == org_id)
    if program_code:
        task_query = task_query.filter(SpdTask.program_code == program_code)

    group_col = SpdTask.org_id if object_type == "org" else SpdTask.assignee_id
    rows = (
        task_query.with_entities(
            group_col, SpdTask.task_type, SpdTask.status, func.count(SpdTask.id)
        )
        .group_by(group_col, SpdTask.task_type, SpdTask.status)
        .all()
    )
    agg: dict[int, dict] = {}
    for key, task_type, status, count in rows:
        if key is None:
            continue
        entry = agg.setdefault(key, {"object_id": key, "total": 0, "done": 0, "by_type": {}})
        entry["total"] += count
        if status == "done":
            entry["done"] += count
            entry["by_type"][task_type] = entry["by_type"].get(task_type, 0) + count

    if object_type == "org":
        names = {o.id: o.name for o in db.query(Organization).all()}
    else:
        names = {
            u.id: u.full_name or u.username
            for u in db.query(User).filter(User.id.in_(list(agg.keys()) or [0]))
        }
    items = []
    for key, entry in agg.items():
        entry["object_name"] = names.get(key, "")
        entry["completion_rate"] = (
            round(entry["done"] / entry["total"] * 100, 1) if entry["total"] else 0.0
        )
        items.append(entry)
    items.sort(key=lambda e: e["done"], reverse=True)
    return {"period": period, "object_type": object_type, "items": items}


# ============================================================ 村医积分


class PointRuleIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    event: str = Field(
        pattern="^(sign|referral_up|referral_down|followup|abnormal_report|signin)$"
    )
    points: int = Field(default=1, ge=0, le=1000)
    daily_limit: int = Field(default=0, ge=0, le=100000)
    condition: str = Field(default="", max_length=256)


@router.post("/point-rules", response_model=PointRuleCreatedOut, status_code=201,
             dependencies=[Depends(require_roles("director"))])
def create_point_rule(body: PointRuleIn, db: Session = Depends(get_db)):
    rule = SpdPointRule(**body.model_dump())
    db.add(rule)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该积分规则编码已存在") from None
    return {"id": rule.id, "code": rule.code, "event": rule.event, "points": rule.points}


@router.get("/point-rules", response_model=list[PointRuleOut])
def list_point_rules(db: Session = Depends(get_db)):
    return [
        {"id": r.id, "code": r.code, "name": r.name, "event": r.event,
         "points": r.points, "daily_limit": r.daily_limit, "active": r.active}
        for r in db.query(SpdPointRule).order_by(SpdPointRule.id).limit(100).all()
    ]


@router.patch("/point-rules/{rule_id}", response_model=PointRuleUpdatedOut,
              dependencies=[Depends(require_roles("director"))])
def update_point_rule(rule_id: int, body: dict, db: Session = Depends(get_db)):
    rule = db.get(SpdPointRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="积分规则不存在")
    for key in ("name", "points", "daily_limit", "condition", "active"):
        if key in body:
            setattr(rule, key, body[key])
    db.commit()
    return {"id": rule.id, "points": rule.points, "active": rule.active}


@router.get("/point-accounts/me", response_model=MyPointsOut,
            response_model_exclude_unset=True)
def my_points(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """本人积分账户与明细（医生移动端 #20）。"""
    account = db.query(SpdPointAccount).filter(SpdPointAccount.user_id == user.id).first()
    if account is None:
        return {"balance": 0, "earned": 0, "used": 0, "records": []}
    records = (
        db.query(SpdPointRecord)
        .filter(SpdPointRecord.account_id == account.id)
        .order_by(SpdPointRecord.id.desc())
        .limit(100)
        .all()
    )
    return {
        "account_id": account.id, "balance": account.balance, "earned": account.earned,
        "used": account.used,
        "records": [
            {"id": r.id, "rule_code": r.rule_code, "direction": r.direction,
             "points": r.points, "balance_after": r.balance_after, "note": r.note,
             "created_at": r.created_at.isoformat()}
            for r in records
        ],
    }


@router.get("/point-accounts", response_model=list[PointAccountOut])
def list_point_accounts(
    response: Response, org_id: int | None = None, offset: int = 0, limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdPointAccount)
    if org_id is not None:
        query = query.filter(SpdPointAccount.org_id == org_id)
    rows = paginate(query.order_by(SpdPointAccount.balance.desc()), response, offset, limit)
    names = {
        u.id: u.full_name or u.username
        for u in db.query(User).filter(User.id.in_([r.user_id for r in rows] or [0]))
    }
    return [
        {"id": r.id, "user_id": r.user_id, "user_name": names.get(r.user_id, ""),
         "org_id": r.org_id, "balance": r.balance, "earned": r.earned, "used": r.used}
        for r in rows
    ]


@router.post("/point-accounts/signin", response_model=SigninOut)
def signin(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """每日签到积分。唯一约束保证一天只能签一次，重复签到返回 409 而不是静默。"""
    rule = (
        db.query(SpdPointRule)
        .filter(SpdPointRule.event == "signin", SpdPointRule.active.is_(True))
        .first()
    )
    if rule is None:
        raise HTTPException(status_code=404, detail="未配置签到积分规则")
    account = db.query(SpdPointAccount).filter(SpdPointAccount.user_id == user.id).first()
    if account is None:
        account = SpdPointAccount(user_id=user.id, org_id=user.org_id)
        db.add(account)
        db.flush()
    today = date.today().isoformat()
    db.add(SpdSignin(account_id=account.id, day=today, points=rule.points))
    # 原子累加而不是 `account.balance += n`：读-改-写在并发下丢更新，
    # 平台第八轮为这一类缺陷专门抽了 `concurrency.add_amount`，新代码直接用。
    add_amount(db, SpdPointAccount, account.id, "balance", rule.points)
    add_amount(db, SpdPointAccount, account.id, "earned", rule.points)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="今日已签到") from None
    db.refresh(account)  # Core UPDATE 不经过 ORM，会话里的对象还是旧值
    # 这条流水**不带 ref_id**：签到不挂在某次业务事件上，因而落在 spd_point_records
    # 的 (rule_code, ref_type, ref_id) 事件键之外。"一天只入一笔"由上面 spd_signins
    # 的唯一约束 + IntegrityError 守住，不必也不该再加一层按 ref 的去重
    # （带 ref 的入账才走 service.award_points，见 tests/test_spd_point_record_ledger.py）。
    db.add(
        SpdPointRecord(
            account_id=account.id, rule_code=rule.code, direction="in", points=rule.points,
            balance_after=account.balance, ref_type="signin", note="每日签到",
        )
    )
    db.commit()
    return {"points": rule.points, "balance": account.balance}


class GoodsIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    points: int = Field(default=100, ge=1, le=100000)
    stock: int = Field(default=0, ge=0, le=100000)
    image_url: str = Field(default="", max_length=256)


@router.post("/goods", response_model=GoodsCreatedOut, status_code=201,
             dependencies=[Depends(require_roles("director"))])
def create_goods(body: GoodsIn, db: Session = Depends(get_db)):
    goods = SpdGoods(**body.model_dump())
    db.add(goods)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该商品编码已存在") from None
    return {"id": goods.id, "code": goods.code, "name": goods.name, "stock": goods.stock}


@router.get("/goods", response_model=list[GoodsOut])
def list_goods(db: Session = Depends(get_db)):
    return [
        {"id": g.id, "code": g.code, "name": g.name, "points": g.points,
         "stock": g.stock, "image_url": g.image_url, "active": g.active}
        for g in db.query(SpdGoods).filter(SpdGoods.active.is_(True))
        .order_by(SpdGoods.id).limit(200).all()
    ]


@router.patch("/goods/{goods_id}", response_model=GoodsUpdatedOut,
              dependencies=[Depends(require_roles("director"))])
def update_goods(goods_id: int, body: dict, db: Session = Depends(get_db)):
    goods = db.get(SpdGoods, goods_id)
    if goods is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    for key in ("name", "points", "stock", "image_url", "active"):
        if key in body:
            setattr(goods, key, body[key])
    db.commit()
    return {"id": goods.id, "stock": goods.stock, "active": goods.active}


class RedeemIn(BaseModel):
    goods_id: int


@router.post("/redeems", response_model=RedeemCreatedOut, status_code=201)
def redeem(
    body: RedeemIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """积分兑换：扣积分 + 扣库存 + 出核销码，三步在同一事务里。

    库存用**条件更新**而不是"先查后减"：两个村医同时兑换最后一件时，
    先查后减会双双成功，库存变成 -1。这是平台第八轮专门整改过的缺陷家族，
    新代码不能再犯。
    """
    goods = db.get(SpdGoods, body.goods_id)
    if goods is None or not goods.active:
        raise HTTPException(status_code=404, detail="商品不存在或已下架")
    account = db.query(SpdPointAccount).filter(SpdPointAccount.user_id == user.id).first()
    if account is None:
        raise HTTPException(status_code=409, detail="积分余额不足")
    if not take_amount(db, SpdGoods, goods.id, "stock", 1):
        db.rollback()
        raise HTTPException(status_code=409, detail="商品库存不足")
    # 扣积分同样走"够才扣"的原子 UPDATE：先判余额再相减，并发下两笔兑换
    # 都会判定够，最后扣成负积分。
    if not take_amount(db, SpdPointAccount, account.id, "balance", goods.points):
        db.rollback()
        raise HTTPException(status_code=409, detail="积分余额不足")
    add_amount(db, SpdPointAccount, account.id, "used", goods.points)
    db.flush()
    db.refresh(account)
    account.updated_at = now_naive()
    record = SpdRedeem(
        account_id=account.id, goods_id=goods.id, points=goods.points,
        verify_code=f"{randbelow(1000000):06d}", status="pending",
    )
    db.add(record)
    # 同样落在事件键之外：direction='out' 且不带 ref_id，同一账户多次兑换本就合法。
    # "扣得起才扣"由上面两次 take_amount 的条件 UPDATE 守住。
    db.add(
        SpdPointRecord(
            account_id=account.id, rule_code="", direction="out", points=goods.points,
            balance_after=account.balance, ref_type="redeem", note=f"兑换{goods.name}",
        )
    )
    db.commit()
    return {"id": record.id, "verify_code": record.verify_code, "balance": account.balance}


@router.get("/redeems", response_model=list[RedeemOut])
def list_redeems(
    response: Response, status: str | None = None, mine: bool = False,
    offset: int = 0, limit: int = 100,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    query = db.query(SpdRedeem)
    if status:
        query = query.filter(SpdRedeem.status == status)
    if mine:
        account = db.query(SpdPointAccount).filter(SpdPointAccount.user_id == user.id).first()
        query = query.filter(SpdRedeem.account_id == (account.id if account else 0))
    rows = paginate(query.order_by(SpdRedeem.id.desc()), response, offset, limit)
    goods = {g.id: g.name for g in db.query(SpdGoods).all()}
    return [
        {"id": r.id, "goods_id": r.goods_id, "goods_name": goods.get(r.goods_id, ""),
         "points": r.points, "verify_code": r.verify_code, "status": r.status,
         "created_at": r.created_at.isoformat(),
         "verified_at": r.verified_at.isoformat() if r.verified_at else ""}
        for r in rows
    ]


class VerifyIn(BaseModel):
    verify_code: str = Field(min_length=4, max_length=16)


@router.post("/redeems/verify", response_model=RedeemVerifiedOut,
             dependencies=[Depends(require_roles("director", "operator"))])
def verify_redeem(
    body: VerifyIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """线下核销：凭核销码核销，已核销的不能重复核销。"""
    record = (
        db.query(SpdRedeem)
        .filter(SpdRedeem.verify_code == body.verify_code, SpdRedeem.status == "pending")
        .first()
    )
    if record is None:
        raise HTTPException(status_code=404, detail="核销码无效或已核销")
    record.status = "verified"
    record.verified_by = user.id
    record.verified_at = now_naive()
    db.commit()
    return {"id": record.id, "status": record.status}
