"""全域慢专病 · 考核域：指标库、分级考核方案、自动取数计分、下钻、村医积分与兑换。

对应招标文件：平台管理端 #17~#20、卫健管理端 #12、全程管理中心端 #6/#7/#19、
服务团队专家端 #11、医生移动端 #19/#20。

## 计分是怎么算出来的

指标不写死在代码里，而是"取数口径（`data_source`）+ 公式（`formula`）+
评分规则（`score_rule`）"三段式：

1. `data_source` 决定去哪张表数数——本文件的 `collect_metrics_batch` 是唯一的
   取数实现，每个口径返回一组命名数字（`{"total": 40, "done": 36}`；口径与变量表见
   `service.INDICATOR_SOURCES`）；
2. `formula` 用这些数字算出指标值，走既有的 `formula.evaluate`（AST 白名单）；
3. `score_rule` 把指标值折成得分。

分三段而不是让人直接写 SQL：SQL 口径一旦开放，各县会写出各自的取数逻辑，
数字对不上时无从对账；而这三段里唯一可自由填写的公式是受限表达式。
"""
import calendar
import math
import re
from typing import Any, cast

from secrets import randbelow

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, field_validator
from sqlalchemy import String, func, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ... import clock, datetypes
from ...datetypes import OptionalDateStr
from ...clock import now_naive
from ...concurrency import add_amount, ensure_present, insert_if_absent, take_amount
from ...database import get_db
from ...patchtypes import UNSET
from ...texttypes import NON_BLANK
from ...deps import get_current_user, paginate, require_roles, through_day
from ...formula import FormulaError, evaluate as eval_formula, validate as validate_formula
from ...numtypes import non_finite_path
from ..platform import Organization, User
from ..rules import scale_overlap_problem
from ..service import INDICATOR_SOURCES, point_account_for, task_overdue, unknown_program, unknown_programs
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
from ...visibility import assert_org_visible, visible_org_ids

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
    level_name: str
    program_codes: list[str]
    object_type: str
    period_type: str
    period_type_name: str
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
    code: str = Field(min_length=1, max_length=32, pattern=NON_BLANK)
    name: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
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
    target_value: FiniteFloat | None = None
    abnormal_rule: str = Field(default="", max_length=256)
    version: str = Field(default="v1", max_length=16)
    # 日期走真源（P2-334）：原先裸 str 限长 10，「2026/09/01」「2026-02-31」「abcdefghij」都照收——字段名里没有 date，
    # 请求体日期字段的棘轮（P1-61）看不见它
    effective_from: OptionalDateStr = ""
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
    formula_problem = indicator_formula_problem(body.formula, body.data_source)   # 留空的公式同样查（P2-1119）
    if formula_problem:
        raise HTTPException(status_code=422, detail=f"公式非法：{formula_problem}")
    # 评分规则写坏了计分时 500（P2-79）；分档重叠只在写入口查（P2-1120）
    rule_problem = score_rule_problem(body.score_rule) or step_overlap_problem(body.score_rule)
    if rule_problem:
        raise HTTPException(status_code=422, detail=f"评分规则非法：{rule_problem}")
    target_problem = ratio_target_problem(body.score_rule, body.target_value)   # P2-718
    if target_problem:
        raise HTTPException(status_code=422, detail=target_problem)
    program_problem = unknown_programs(db, body.program_codes)  # 病种列表先查在不在（P1-120 第二层）
    if program_problem:
        raise HTTPException(status_code=404, detail=program_problem)
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
    if program_code:
        # 按病种筛挪到分页之前（P2-300）：原先先分页、再在这一页里挑「不限病种或含这个病种」的——总数是没筛的、页里
        # 少几条，管这个病种的指标排在第一页之后就整个看不见。病种列表是 JSON 列，与团队清单按病种筛（P2-177）同一个做法
        matched = [
            iid for iid, codes in query.with_entities(SpdIndicator.id, SpdIndicator.program_codes).all()
            if not (codes or []) or program_code in (codes or [])
        ]
        query = query.filter(SpdIndicator.id.in_(matched or [0]))
    rows = paginate(query.order_by(SpdIndicator.id), response, offset, limit)
    return [_indicator_out(i) for i in rows]


class IndicatorPatch(BaseModel):
    """改档与建档同一套约束（P1-94）：原先收裸 dict、照单全收。不传即不改；不可空的列显式传 null 是 422。"""

    name: str = Field(default=UNSET, min_length=1, max_length=64, pattern=NON_BLANK)
    program_codes: list[str] = Field(default=UNSET)
    data_source: str = Field(
        default=UNSET,
        pattern="^(task|enrollment|path|referral|measurement|assessment|archive|case_report)$",
    )
    scope_expr: str = Field(default=UNSET, max_length=256)
    formula: str = Field(default=UNSET, max_length=256)
    score_rule: dict = Field(default=UNSET)
    weight: float = Field(default=UNSET, ge=0, le=1000)
    target_value: FiniteFloat | None = None
    abnormal_rule: str = Field(default=UNSET, max_length=256)
    effective_from: OptionalDateStr = Field(default=UNSET)
    effective_scope: str = Field(default=UNSET, max_length=64)
    active: bool = Field(default=UNSET)


@router.patch("/indicators/{indicator_id}", response_model=IndicatorOut,
              dependencies=[Depends(require_roles("director"))])
def update_indicator(indicator_id: int, body: IndicatorPatch, db: Session = Depends(get_db)):
    indicator = db.get(SpdIndicator, indicator_id)
    if indicator is None:
        raise HTTPException(status_code=404, detail="指标不存在")
    changes = body.model_dump(exclude_unset=True)
    # 与建指标同一句；清空公式（`formula: ""`）同样查（P2-1119）：原先只查非空的，PATCH 空串直接清空。
    # 只改取数口径、不带公式的不在此列（原地改口径不重验存量公式，随 P2-520 定）
    if "formula" in changes:
        formula_problem = indicator_formula_problem(
            changes["formula"], changes.get("data_source", indicator.data_source))
        if formula_problem:
            raise HTTPException(status_code=422, detail=f"公式非法：{formula_problem}")
    if "score_rule" in changes:   # 与建指标同一句（P2-79 / P2-1120）
        rule_problem = score_rule_problem(changes["score_rule"]) or step_overlap_problem(changes["score_rule"])
        if rule_problem:
            raise HTTPException(status_code=422, detail=f"评分规则非法：{rule_problem}")
    if "score_rule" in changes or "target_value" in changes:   # 改完之后的组合与建指标同一句（P2-718）
        target_problem = ratio_target_problem(changes.get("score_rule", indicator.score_rule),
                                              changes.get("target_value", indicator.target_value))
        if target_problem:
            raise HTTPException(status_code=422, detail=target_problem)
    # 病种列表同建档一句（P1-120 第二层）；原有的编码不再查
    program_problem = unknown_programs(db, changes.get("program_codes"), already=indicator.program_codes)
    if program_problem:
        raise HTTPException(status_code=404, detail=program_problem)
    for key, value in changes.items():
        setattr(indicator, key, value)
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
    """各取数口径产出的变量名——公式只能引用这些名字（口径表见 `service.INDICATOR_SOURCES`）。"""
    source = INDICATOR_SOURCES.get(data_source)
    return tuple(source[1]) if source else ("total",)


def indicator_formula_problem(formula: str, data_source: str) -> str:
    """指标公式在这个取数口径下写得对不对，没问题返回空串（P2-1119）。建 / 改指标同一句。

    公式留空按 `total` 取值（计分 `run_scoring` 与报告段落 `reporting._indicator` 同一口径），就按 `total` 查：纳管 /
    评估 / 建档 / 上报四个口径没有 total，留空与写 `total` 同一句报错——原先只查非空的公式，留空的照收，计分恒 0 分、
    不记错。校验与平台绩效公式同一口径（`formula.validate`）：哑值下算不出不算写错（`(total - done - overdue) ** 0.5`
    在三者都是 1 时开负数的平方根，换组真实取值就算得出，算不出的计分时逐指标记错）——原先拿严格的 `evaluate` 代哑值
    试算，能算的公式挡在门外。
    """
    try:
        validate_formula(formula or "total", set(_metric_names(data_source)))
    except FormulaError as exc:
        return str(exc)
    return ""


#: 考核期的三种写法（跑分弹窗与工作量筛选框的占位符都这么写）。形状用 `[0-9]`
#: 不用 `\d`：`\d` 认全角数字（同 datetypes.MONTH_SHAPE 的理由）。月度那种不在这里
#: 另写一遍——交给月度期间的唯一真源 `datetypes.check_month`。
_YEAR_PERIOD = re.compile(r"[0-9]{4}")
_QUARTER_PERIOD = re.compile(r"([0-9]{4})-Q([1-4])")
PERIOD_HINT = "考核期须为 YYYY（年度）、YYYY-Qn（季度，n=1~4）或 YYYY-MM（月度）"


def check_assess_period(period: str) -> str:
    """考核期的唯一校验，非法抛 ValueError（带人话）（P1-62）。

    此前 `_period_range` 手写解析、不做校验：`2026/08`、`abc`、`2026-Qx` 炸成 500；
    `2026-13`、`2026-Q5` 解析出 `2026-13-01` 这类区间；`2026-8` 的起点 `2026-8-01`
    在字典序上大于整个 8 月——后三种都**照常出分并写库**，一套以垃圾期为名的
    考核结果就此存在（修复前实测）。
    """
    if _YEAR_PERIOD.fullmatch(period) or _QUARTER_PERIOD.fullmatch(period):
        return period
    if datetypes.MONTH_SHAPE.fullmatch(period):
        return datetypes.check_month(period)  # 形状对、月份不存在：给日历那句
    raise ValueError(PERIOD_HINT)


def _period_range(period: str) -> tuple[str, str]:
    """把 `2026-08` / `2026-Q3` / `2026` 展开成 [起, 止] 日期字符串；非法抛 ValueError。"""
    period = check_assess_period(period)
    if _YEAR_PERIOD.fullmatch(period):
        return f"{period}-01-01", f"{period}-12-31"
    quarter = _QUARTER_PERIOD.fullmatch(period)
    if quarter:
        year, q = quarter.groups()
        start_month = (int(q) - 1) * 3 + 1
        end_month = start_month + 2
        last_day = 31 if end_month in (1, 3, 5, 7, 8, 10, 12) else 30
        return f"{year}-{start_month:02d}-01", f"{year}-{end_month:02d}-{last_day}"
    year, month = period.split("-")
    # monthrange 而不是"次月一号减一天"：后者在 9999-12 上溢出到一万年
    last = calendar.monthrange(int(year), int(month))[1]
    return f"{year}-{month}-01", f"{year}-{month}-{last:02d}"


def effective_versions(db: Session, codes: list, period: str) -> tuple[dict[str, SpdIndicator], set[str]]:
    """每个指标编码取本期（`period` 的期末之前）已经生效的最新一版；返回 (编码 → 那一版, 有启用版本但都还没生效的编码)。

    同一编码有多个启用版本时按（生效日期, 编号）升序、后到的覆盖（P2-519）：原先不排序、按编码塞进字典，取哪一版看
    数据库返回顺序，下个月才生效的新口径照样拿来算这个月。计分（`run_scoring`）与报告的指标段落共用这一处（P2-529）：
    原先报告段落自己取编号最大的那一版、不看生效日期，印出来的是还没生效的新口径算出的数、不是考核分。
    `period` 非法抛 ValueError（与 `_period_range` 同一句）。
    """
    _period_start, period_end = _period_range(period)
    chosen: dict[str, SpdIndicator] = {}
    not_yet: set[str] = set()   # 有启用的版本，但都还没到生效日期
    for candidate in (
        db.query(SpdIndicator)
        .filter(SpdIndicator.code.in_(codes), SpdIndicator.active.is_(True))
        .order_by(SpdIndicator.code, SpdIndicator.effective_from, SpdIndicator.id)
        .all()
    ):
        if candidate.effective_from and candidate.effective_from > period_end:
            not_yet.add(candidate.code)
            continue
        chosen[candidate.code] = candidate
    return chosen, not_yet


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
            .order_by(col)
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
                through_day(SpdTask.created_at, end),
            ),
            {"total": func.count(SpdTask.id),
             "done": _count_if(SpdTask.status == "done"),
             # 与工作台同一个判定（P2-549）：只数状态，调度没跑时期内过期的任务一条都不算超期
             "overdue": _count_if(task_overdue(clock.today().isoformat()))},
        )
    if source == "referral":
        return grouped(
            SpdReferralCase,
            db.query(SpdReferralCase).filter(
                SpdReferralCase.created_at >= f"{start} 00:00:00",
                through_day(SpdReferralCase.created_at, end),
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
                through_day(SpdCaseReport.created_at, end),
            ),
            {"reported": func.count(SpdCaseReport.id),
             "handled": _count_if(SpdCaseReport.status.in_(["done", "closed"]))},
        )

    if source not in ("enrollment", "archive", "assessment", "path", "measurement"):
        return {oid: {"total": 0.0} for oid in ids}

    # ---- 以下口径都从"该对象名下的在管档案"出发：档案取一次、按对象分桶
    enroll_col = _object_column(SpdEnrollment, object_type)
    enroll_query = prog(SpdEnrollment, db.query(SpdEnrollment))
    # 监测同样只数期末之前纳管的人（P2-1073）：「期内在管患者的监测次数」——原先取现在在管的全部档案，期末之后才纳管的人
    # 纳管前的读数（公卫随访同步、设备、手录都不要求纳管）补跑往期时进了往期的达标率，同一期越晚跑结果越不一样
    if source in ("enrollment", "archive", "assessment", "measurement"):
        enroll_query = enroll_query.filter(through_day(SpdEnrollment.created_at, end))
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

        # 分母与分子同一个期末（P2-688）：分子只数期末之前建的档，分母原先不设上界——补跑往期时，期末之后才入池的人
        # 全进了分母，越晚跑分越低、同一期永远复现不出来（机构绩效的存量分母同一条：必须设上界，performance.py）。
        # 入池之后状态怎么变（排除、签约）仍按现在的状态算，那是 P2-670 待裁定的另一半
        cand_query = prog(SpdCandidate, db.query(SpdCandidate).filter(
            SpdCandidate.status.in_(["target", "enrolled"]), through_day(SpdCandidate.created_at, end)))
        if object_type == "org":
            cand_rows = dict(
                cand_query.filter(SpdCandidate.org_id.in_(ids))
                .with_entities(SpdCandidate.org_id, func.count(SpdCandidate.id))
                .group_by(SpdCandidate.org_id)
                .order_by(SpdCandidate.org_id).all()
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
                # 变量字典写的是「在管的高危 / 极高危」：去世、迁出、排除的高危档案不算（P2-139）
                "high_risk": float(
                    sum(1 for e in buckets[oid] if e.status == "active" and e.risk_level in ("high", "very_high"))
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
        # 按病种跑分时评估也按病种取（P2-690）：同时管高血压与糖尿病的患者，只做过高血压评估，按糖尿病跑分原先
        # 也算「已评估」——与任务、转诊、上报、建档、目标池同一个 prog()，与专家工作台的评估人次同一个判据（P2-552）
        assessed = {
            pid for (pid,) in prog(SpdAssessment, db.query(SpdAssessment.patient_id))
            .filter(
                SpdAssessment.patient_id.in_(union or [0]),
                SpdAssessment.created_at >= f"{start} 00:00:00",
                through_day(SpdAssessment.created_at, end),
            )
            .distinct().all()
        }
        # 分子分母同一个单位：变量字典写的是「在管患者数」，按人去重——同时管两个病种的患者
        # 原先在分母里算两次，评估过了完成率也只有一半（P2-139）
        return {
            oid: {
                "enrolled": float(len(patients[oid])),
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
                through_day(SpdPathInstance.started_at, end),
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
        # 按病种跑分时监测值也按病种取（P2-690）：原先糖尿病的达标率拿血压读数算
        prog(SpdMeasurement, db.query(SpdMeasurement.patient_id, SpdMeasurement.level))
        .filter(
            SpdMeasurement.patient_id.in_(union or [0]),
            SpdMeasurement.measured_at >= f"{start} 00:00:00",
            through_day(SpdMeasurement.measured_at, end),
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


#: 评分规则的两种类型，即 `score_of` 的两支；空规则 = 未配置，按指标值计分
SCORE_RULE_TYPES = ("ratio", "step")


def _is_number(value: Any) -> bool:
    """int，或有限的 float；布尔不算，NaN / Infinity 也不算（P2-466）：满分、分档、权重写成它们，算进分数里
    整张方案的出参编码失败（500）。计分时对存量里的坏规则、坏权重同样逐指标记错。"""
    if isinstance(value, bool):
        return False
    return isinstance(value, int) or (isinstance(value, float) and math.isfinite(value))


def score_rule_problem(rule: dict, *, known_type_only: bool = True) -> str:
    """评分规则里会让 `score_of` 抛错的写法，没问题返回空串（P2-79）。

    原先照单全收：满分写成文字、分档不是列表、分档边界或分值不是数，建指标照样 201，整张考核方案一计分就 500。
    建 / 改指标时拦（422）；计分时对存量里的坏规则逐指标记错、不 500，与公式求值失败同一个处理。
    `known_type_only=False`（计分时）：存量里的未知类型照旧按「未配置」计分——修前就是这么算的，这里只拦会 500 的。

    满分、分档分值写成负数（P2-1579）不抛错，却算出负分倒扣总分（满分 -100 时达标反而得分最低），与负权重（P2-108）
    同一个毛病，也在这里拦：建 / 改指标 422，存量里的照上一段逐指标记错。
    """
    if not rule:
        return ""
    bad = non_finite_path(rule, "score_rule")   # P2-466
    if bad:
        return f"{bad} 不能是 NaN / Infinity"
    kind = rule.get("type", "")
    if kind not in SCORE_RULE_TYPES:
        return f"评分规则类型只能是 ratio（按比例）/ step（分档），收到 {kind!r}" if known_type_only else ""
    if kind == "ratio":
        if "full" in rule and not _is_number(rule["full"]):
            return "按比例计分的满分（full）必须是数"
        if "full" in rule and rule["full"] < 0:   # P2-1579
            return f"按比例计分的满分（full）不能小于 0（收到 {rule['full']}）：{_NEGATIVE_SCORE_WHY}"
        if rule.get("target") is not None and not _is_number(rule["target"]):
            return "按比例计分的目标值（target）必须是数"
        if rule.get("target") is not None and rule["target"] <= 0:
            return f"按比例计分的目标值（target）须大于 0（收到 {rule['target']}）：{_RATIO_TARGET_WHY}"   # P2-718
        return ""
    steps = rule.get("steps", [])
    if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
        return "分档计分的 steps 必须是分档列表"
    for index, step in enumerate(steps, start=1):
        if any(step.get(key) is not None and not _is_number(step[key]) for key in ("min", "max")):
            return "分档的上下限（min / max）必须是数，不设限留空"
        if step.get("min") is not None and step.get("max") is not None and step["min"] > step["max"]:
            return f"分档的下限 {step['min']} 大于上限 {step['max']}，这一档永远命中不了"   # P2-712
        if "score" in step and not _is_number(step["score"]):
            return "分档的分值（score）必须是数"
        if "score" in step and step["score"] < 0:   # P2-1579
            return f"分档的分值（score）不能小于 0（第 {index} 档收到 {step['score']}）：{_NEGATIVE_SCORE_WHY}"
    return ""


#: 满分、分档分值不能是负数的理由（P2-1579）：得分按权重折算后累加进总分，负分就是倒扣，与负权重（P2-108）同一个毛病
_NEGATIVE_SCORE_WHY = "负分按权重折算进总分就是倒扣分，扣分会超过这一项的权重"


def step_overlap_problem(rule: dict) -> str:
    """分档计分的档与档两两不重叠（P2-1120），没问题返回空串。先过 `score_rule_problem`（分档是列表、上下限是数）再调。

    `score_of` 上下限都含、取第一个命中的档：两档 60–80 / 80–100，指标值 80 落进先写的那档得 60 分，倒过来写得 100 分——
    分数取决于书写顺序。与量表评分分段同一个口径（P2-887，复用 `rules.scale_overlap_problem`），也同样只在建 / 改指标时
    拦，不进 `score_rule_problem`：那一句计分时也查，存量里重叠的分档会整项记错；存量的照旧按书写顺序计分，改档时改。
    档与档之间的缺口（79.5 落在 0–79 与 80–100 之间，「未落入任何评分档」给 0 分）另待裁定，不在这里拦。
    """
    if (rule or {}).get("type") != "step":
        return ""
    return scale_overlap_problem({"ranges": rule.get("steps", [])})


#: 按比例计分的目标值不是正数时的后果（P2-718）：负数目标谁都达标、一件事没做也满分；0 原先被悄悄换成 100
_RATIO_TARGET_WHY = "负数目标谁都算达标、一件事没做也满分，0 没法按比例折算"


def ratio_target_problem(rule: dict | None, target_value: float | None) -> str:
    """按比例计分的指标，目标值（`target_value`）须大于 0，没问题返回空串（P2-718）。

    `ratio` 未达标按 `满分 × 实际 / 目标` 折算：目标是负数时 `实际 >= 目标` 恒成立，人人满分；目标是 0 时原先被
    `preset or 100` 悄悄换成 100。只管按比例计分的——分档计分的目标值只作展示（「目标 0 例投诉」是合法的）。
    建 / 改指标时 422；计分时存量里的逐指标记错、不计分，与坏评分规则同一个处理（P2-79）。"""
    if (rule or {}).get("type") == "ratio" and target_value is not None and target_value <= 0:
        return f"按比例计分的指标目标值须大于 0（收到 {target_value:g}）：{_RATIO_TARGET_WHY}"
    return ""


def score_of(indicator: SpdIndicator, value: float) -> tuple[float, str]:
    """把指标值折成得分，返回 (得分, 扣分理由)。

    两种评分规则：
    - `ratio`：达标即满分，未达标按比例给分（`full * value / target`，截在 0 与满分之间，指标值为负按 0 计——P2-1579）；
    - `step`：分档给分，取第一个命中的档。

    `ratio` 的目标以指标的 `target_value` 为准，指标没设目标值才看规则里的 `target`，都没有按 100（P2-104）。
    原先反过来先看规则里的：种子指标两处各写一份，界面（考核指标库的编辑弹窗、扣分理由、报告里的「目标」）
    看到、改到的都是 `target_value`，改了之后计分照旧按规则里那份旧的。

    没配规则时按"值即得分"处理并截到 0~100——总比整张考核表算不出来强，
    但会在理由里写明"未配置评分规则"，让人知道这个数不是精心设计的。
    """
    rule = indicator.score_rule or {}
    kind = rule.get("type", "")
    if kind == "ratio":
        full = float(rule.get("full", 100))
        preset = indicator.target_value if indicator.target_value is not None else rule.get("target")
        target = float(preset) if preset is not None else 100.0   # 0 不再悄悄换成 100（P2-718，写入与计分前都挡了非正数）
        if value >= target:
            return full, ""
        # 「实际」按判定用的精度印（P2-991，与公式求值同一个 4 位，P2-892）：原先印两位，分母上万时 89.9955 判未达 90、
        # 理由却写「未达目标值90.0（实际90.0）」，自相矛盾
        reason = f"未达目标值{target}（实际{round(value, 4)}）"
        if value < 0:
            # 指标值为负按 0 计（P2-1579）：公式框写得出减法（「净完成率」`(done - (total - done)) / total * 100`），原先照
            # `满分 × 实际 / 目标` 折成负分，扣分超过这一项的权重、把总分拉成负数；与负权重（P2-108）、基金分配算出负权重按 0 计
            # （P2-191）同一个处理，未配置评分规则那一支本就截到 0~100
            return 0.0, f"{reason}，指标值为负，按 0 计"
        got = round(full * value / target, 2) if target else 0.0
        return max(0.0, min(got, full)), reason   # 得分截在 [0, 满分] 里（P2-1579）
    if kind == "step":
        for step in rule.get("steps", []):
            low, high = step.get("min"), step.get("max")
            if (low is None or value >= float(low)) and (high is None or value <= float(high)):
                return float(step.get("score", 0)), step.get("reason", "")
        return 0.0, "未落入任何评分档"
    return max(0.0, min(value, 100.0)), "未配置评分规则，按指标值计分"


# ============================================================ 考核方案


class PlanIn(BaseModel):
    code: str = Field(min_length=1, max_length=32, pattern=NON_BLANK)
    name: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    level: str = Field(default="township", pattern="^(hospital|township|station|village|team)$")
    program_codes: list[str] = Field(default_factory=list)
    object_type: str = Field(default="org", pattern="^(org|doctor|village_doctor|team)$")
    period_type: str = Field(default="month", pattern="^(month|quarter|year)$")
    items: list[dict] = Field(default_factory=list)


def _check_plan_items(db: Session, items: list[dict]) -> None:
    """建方案与改方案同一句（P1-94：改方案原先不查）：至少一个指标，指标编码得有、得存在、不得重复。

    编码缺失或不是字符串的条目先挡掉——原先拼「以下指标不存在」时 `'、'.join` 撞上 None / 整数，422 成了 500。

    同一指标不许写两条（P2-1276）：计分逐条累加，原先「enroll_rate:40, followup_rate:60, enroll_rate:40」照收，甲院 80 分
    （同样的数据不重复时 40 分），分项明细两条 enroll_rate，指标「被引用」页却只报第一条的权重 40。与服务包项目编码（P2-631）、
    随访时间点（P2-719）、量表题目 key 重复同一句。"""
    if not items:
        raise HTTPException(status_code=422, detail="考核方案至少要有一个指标")
    bad = non_finite_path(items, "items")   # P2-466
    if bad:
        raise HTTPException(status_code=422, detail=f"考核方案的 {bad} 不能是 NaN / Infinity")
    codes: list[Any] = [i.get("indicator_code") for i in items]
    if not all(isinstance(c, str) and c for c in codes):
        raise HTTPException(status_code=422, detail="考核方案的每一项都要给出指标编码 indicator_code")
    known = {
        code for (code,) in db.query(SpdIndicator.code).filter(SpdIndicator.code.in_(codes)).all()
    }
    missing = [c for c in codes if c not in known]
    if missing:
        raise HTTPException(status_code=422, detail=f"以下指标不存在：{'、'.join(missing)}")
    repeated = sorted({c for c in codes if codes.count(c) > 1})
    if repeated:
        raise HTTPException(status_code=422,
                            detail=f"考核方案里指标重复：{'、'.join(repeated)}（同一指标只写一条，写两条会按两次计分）")
    weight_problem = plan_weight_problem(items)
    if weight_problem:
        raise HTTPException(status_code=422, detail=weight_problem)


def plan_weight_problem(items: list[dict]) -> str:
    """条目权重不写（按指标库的默认权重计）、或写成不小于 0 的数；没问题返回空串（P2-108）。

    原先照单全收：写成文字的计分时 `float()` 抛错，整张方案、所有考核对象一起 500；写成负数的倒扣分。建 / 改方案时 422；
    计分时对存量里的坏权重逐指标记错、不 500（与 P2-79 的坏评分规则同一个处理）。显式写 null 的照旧按 0 计（修前就是）。"""
    for item in items:
        weight = item.get("weight")
        if weight is not None and (not _is_number(weight) or weight < 0):
            return f"考核方案里指标 {item.get('indicator_code')} 的权重必须是不小于 0 的数"
    return ""


def _first_item_per_indicator(items: list) -> list:
    """计分用的方案条目：同一指标写了几条的只留首条、保持原顺序（P2-1276）。

    建 / 改方案现在拦重复（`_check_plan_items`），修前存下的照收——原先逐条累加，同一指标按几次计分、分项明细出几条。只计首条
    与指标「被引用」页（`indicator_usage`）取第一条的权重同一个口径。编码不是字符串的存量坏条目原样留着，照旧逐条记错。"""
    seen: set[str] = set()
    kept: list = []
    for item in items:
        code = item.get("indicator_code")
        if isinstance(code, str):
            if code in seen:
                continue
            seen.add(code)
        kept.append(item)
    return kept


# 考核方案层级、周期文案（措辞照抄 SpdAssessPlan.level / period_type 列注释——P2-74）
ASSESS_LEVEL_NAMES = {"hospital": "县级医院", "township": "卫生院", "station": "服务站", "village": "村医", "team": "团队"}
PERIOD_TYPE_NAMES = {"month": "月度", "quarter": "季度", "year": "年度"}


def _plan_out(p: SpdAssessPlan) -> dict:
    return {
        "id": p.id, "code": p.code, "name": p.name, "level": p.level,
        "level_name": ASSESS_LEVEL_NAMES.get(p.level, p.level),
        "program_codes": p.program_codes or [], "object_type": p.object_type,
        "period_type": p.period_type, "period_type_name": PERIOD_TYPE_NAMES.get(p.period_type, p.period_type),
        "items": p.items or [], "active": p.active,
    }


@router.post("/assess-plans", response_model=PlanOut, status_code=201,
             dependencies=[Depends(require_roles("director"))])
def create_plan(body: PlanIn, db: Session = Depends(get_db)):
    _check_plan_items(db, body.items)
    program_problem = unknown_programs(db, body.program_codes)  # 病种列表先查在不在（P1-120 第二层）
    if program_problem:
        raise HTTPException(status_code=404, detail=program_problem)
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


class PlanPatch(BaseModel):
    """改档与建档同一套约束（P1-94）：原先收裸 dict、照单全收。不传即不改；不可空的列显式传 null 是 422。"""

    name: str = Field(default=UNSET, min_length=1, max_length=64, pattern=NON_BLANK)
    level: str = Field(default=UNSET, pattern="^(hospital|township|station|village|team)$")
    program_codes: list[str] = Field(default=UNSET)
    object_type: str = Field(default=UNSET, pattern="^(org|doctor|village_doctor|team)$")
    period_type: str = Field(default=UNSET, pattern="^(month|quarter|year)$")
    items: list[dict] = Field(default=UNSET)
    active: bool = Field(default=UNSET)


@router.patch("/assess-plans/{plan_id}", response_model=PlanOut,
              dependencies=[Depends(require_roles("director"))])
def update_plan(plan_id: int, body: PlanPatch, db: Session = Depends(get_db)):
    plan = db.get(SpdAssessPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="考核方案不存在")
    changes = body.model_dump(exclude_unset=True)
    if "items" in changes:
        _check_plan_items(db, changes["items"])
    # 病种列表同建档一句（P1-120 第二层）；原有的编码不再查
    program_problem = unknown_programs(db, changes.get("program_codes"), already=plan.program_codes)
    if program_problem:
        raise HTTPException(status_code=404, detail=program_problem)
    for key, value in changes.items():
        setattr(plan, key, value)
    db.commit()
    return _plan_out(plan)


# ============================================================ 计分


#: 考核分的对象名是快照、列宽 64（P2-1046）：机构名本身收 128 字，全量展开时一家机构名过长，生产库上撞列宽即 500——
#: 整轮跑分回滚、所有机构都没分（开发库照存）。对象号另存，名称按列宽截断
SCORE_OBJECT_NAME_MAX = cast(String, SpdScore.__table__.c.object_name.type).length or 64

class RunScoreIn(BaseModel):
    plan_id: int
    period: str = Field(min_length=4, max_length=16, pattern=NON_BLANK)
    program_code: str = Field(default="", max_length=32)
    object_ids: list[int] = Field(default_factory=list)

    @field_validator("period")
    @classmethod
    def _period_shape(cls, value: str) -> str:
        return check_assess_period(value)


def _objects_of(db: Session, plan: SpdAssessPlan, object_ids: list[int]) -> list[tuple[int, str]]:
    """考核对象清单：给了 id 就按 id，没给就按方案层级全量展开。

    全量就是全量（P1-84）：原先四个分支各有一个不带排序的 `.limit(500)`，村医 / 医生过 500 人的县，
    多出来的没有分数、排名只在 500 人里排，重跑还可能换一批。按 id 排序展开，并列总分的名次也稳定。
    """
    if plan.object_type == "org":
        query = db.query(Organization)
        if object_ids:
            query = query.filter(Organization.id.in_(object_ids))
        elif plan.level in ("hospital", "township", "village"):
            level_map = {"hospital": "county", "township": "township", "village": "village"}
            query = query.filter(Organization.level == level_map[plan.level])
        return [(o.id, o.name) for o in query.order_by(Organization.id).all()]
    if plan.object_type == "team":
        team_query = db.query(SpdTeam).filter(SpdTeam.active.is_(True))
        if object_ids:
            team_query = team_query.filter(SpdTeam.id.in_(object_ids))
        return [(t.id, t.name) for t in team_query.order_by(SpdTeam.id).all()]
    if plan.object_type == "village_doctor":
        doctor_query = db.query(SpdVillageDoctor).filter(SpdVillageDoctor.active.is_(True))
        if object_ids:
            doctor_query = doctor_query.filter(SpdVillageDoctor.user_id.in_(object_ids))
        rows = doctor_query.order_by(SpdVillageDoctor.id).all()
        names = {
            u.id: u.full_name or u.username
            for u in db.query(User).filter(User.id.in_([v.user_id for v in rows] or [0]))
        }
        return [(v.user_id, names.get(v.user_id, "")) for v in rows]
    user_query = db.query(User).filter(User.role.in_(["doctor", "public_health"]))
    if object_ids:
        user_query = user_query.filter(User.id.in_(object_ids))
    return [(u.id, u.full_name or u.username) for u in user_query.order_by(User.id).all()]


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
    # 重跑同一周期会覆盖上次结果：病种编码填错时各指标按它筛出空数据，本期正式分数被改写成零（P1-120）
    program_problem = unknown_program(db, body.program_code)  # 病种编码先查在不在（P1-120）
    if program_problem:
        raise HTTPException(status_code=404, detail=program_problem)
    items = _first_item_per_indicator(plan.items or [])   # 存量里同一指标写了几条的只计首条（P2-1276）
    indicators, not_yet = effective_versions(db, [i.get("indicator_code") for i in items], body.period)
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
        for item in items:
            code = item.get("indicator_code")
            indicator = indicators.get(code)
            if indicator is None:
                detail.append({"indicator_code": code,
                               "error": "指标在本期尚未生效" if code in not_yet else "指标不存在或已停用"})
                continue
            metrics = metrics_by_code[code][object_id]
            try:
                # 公式留空按 total 取值，照 total 求值（P2-1119）：口径没有 total 的存量空公式记「未知变量：total」，
                # 原先 `metrics.get("total", 0)` 恒 0 分、理由写「未达目标值」，看着像真没做到
                value = eval_formula(indicator.formula or "total", metrics)
            except FormulaError as exc:
                detail.append({"indicator_code": code, "error": f"公式求值失败：{exc}"})
                continue
            # 修前存进去的坏评分规则：逐指标记错、不 500（P2-79），与公式求值失败同一个处理
            rule_problem = score_rule_problem(indicator.score_rule or {}, known_type_only=False)
            if rule_problem:
                detail.append({"indicator_code": code, "error": f"评分规则非法：{rule_problem}"})
                continue
            target_problem = ratio_target_problem(indicator.score_rule, indicator.target_value)   # 存量非正目标（P2-718）
            if target_problem:
                detail.append({"indicator_code": code, "error": f"目标值非法：{target_problem}"})
                continue
            weight_problem = plan_weight_problem([item])   # 修前存进去的坏权重：逐指标记错、不 500（P2-108）
            if weight_problem:
                detail.append({"indicator_code": code, "error": f"权重非法：{weight_problem}"})
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
                # 用的是哪一版、哪条公式（P2-519）：docstring 说历史留痕靠 detail，原先不记版本与公式——指标原地改过口径、
                # 或同编码并存几版时，这期分数是按哪条公式算出来的无从查起
                "indicator_id": indicator.id, "version": indicator.version, "formula": indicator.formula,
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
        record.object_name = object_name[:SCORE_OBJECT_NAME_MAX]   # 名称是快照，对象号另存（P2-1046）
        record.program_code = body.program_code
        record.total_score = round(total_score, 2)
        record.detail = detail
        record.created_at = now_naive()
        results.append(record)

    results.sort(key=lambda r: r.total_score, reverse=True)
    # 名次按本方案本期的全部分数排（P2-298）：带 object_ids 的局部重跑原先只在这几个对象里排、写回——重跑的那个
    # 记成第 1，没重跑的第 1 名还是第 1，同一期出两个第 1；方案层级下已不在考核范围里的旧分数行同理。
    # 并列总分按对象编号排，与重跑了哪几个无关
    db.flush()
    everyone = (
        db.query(SpdScore)
        .filter(SpdScore.plan_id == plan.id, SpdScore.period == body.period,
                SpdScore.object_type == plan.object_type)
        .all()
    )
    everyone.sort(key=lambda r: (-(r.total_score or 0), r.object_id))
    for index, record in enumerate(everyone, start=1):
        record.rank = index
    results.sort(key=lambda r: r.rank)
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
        # 「高频扣分项」按扣分次数排（P2-964）：原先按累计扣分排——权重 90 的指标只扣过 1 次，排在五家都扣过的权重 10 的
        # 指标前面，指标一多，真正高频的被截在前 10 之外。次数并列再按累计扣分、再按指标编码，结果与取数次序无关
        "top_deductions": sorted(
            deductions.values(), key=lambda d: (-d["count"], -d["total_deduction"], d["indicator_code"])
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

    统计口径：本期（按任务创建时刻）的任务总数、办结数、按任务类型拆分的办结数与完成率；对象是机构时按任务的机构、
    是人员时按任务的承办人（`assignee_id`）。与考核指标的任务取数共用同一张表——工作量报表和考核得分对不上，
    是这类系统最常见的投诉。纳管数、转诊数不在本接口（原说明写着有，P2-337 订正），要看走考核指标的对应取数口径。
    """
    period = period or clock.today().strftime("%Y-%m")
    try:
        start, end = _period_range(period)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"period：{exc}") from None
    task_query = db.query(SpdTask).filter(
        SpdTask.created_at >= f"{start} 00:00:00", through_day(SpdTask.created_at, end)
    )
    orgs = visible_org_ids(db, user)
    if orgs is not None:
        task_query = task_query.filter(SpdTask.org_id.in_(orgs))
    # 点名看不见的机构 403（P2-831，与平台 `visibility.scope_org_list` 同一句）：原先先按可见范围过滤、再按 org_id 等值，
    # 看不见的机构悄悄回空表——看的人以为那家机构没有数据
    if org_id is not None:
        assert_org_visible(db, user, org_id)
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
        .order_by(group_col, SpdTask.task_type, SpdTask.status)
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
    code: str = Field(min_length=1, max_length=32, pattern=NON_BLANK)
    name: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
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


class PointRulePatch(BaseModel):
    """改档与建档同一套约束（P1-94）：原先收裸 dict、照单全收。不传即不改；不可空的列显式传 null 是 422。"""

    name: str = Field(default=UNSET, min_length=1, max_length=64, pattern=NON_BLANK)
    points: int = Field(default=UNSET, ge=0, le=1000)
    daily_limit: int = Field(default=UNSET, ge=0, le=100000)
    condition: str = Field(default=UNSET, max_length=256)
    active: bool = Field(default=UNSET)


@router.patch("/point-rules/{rule_id}", response_model=PointRuleUpdatedOut,
              dependencies=[Depends(require_roles("director"))])
def update_point_rule(rule_id: int, body: PointRulePatch, db: Session = Depends(get_db)):
    rule = db.get(SpdPointRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="积分规则不存在")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(rule, key, value)
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
    # 积分榜按余额排，余额天然大量并列（新账户全是 0）——补 id 尾键才翻得动页。
    rows = paginate(
        query.order_by(SpdPointAccount.balance.desc(), SpdPointAccount.id),
        response, offset, limit,
    )
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
    rule = (   # 几条启用的签到规则取编号最小的那条，与 award_points 同一个次序（P2-693）
        db.query(SpdPointRule)
        .filter(SpdPointRule.event == "signin", SpdPointRule.active.is_(True))
        .order_by(SpdPointRule.id)
        .first()
    )
    if rule is None:
        raise HTTPException(status_code=404, detail="未配置签到积分规则")
    account = point_account_for(db, user.id, user.org_id)   # 头一回签到与入账撞在一起不再 500（P2-336）
    today = clock.today().isoformat()
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
    code: str = Field(min_length=1, max_length=32, pattern=NON_BLANK)
    name: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
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
def list_goods(include_inactive: bool = False, db: Session = Depends(get_db)):
    """积分商品。缺省只列上架的（村医端兑换页用）；`include_inactive` 连下架的一起列（P2-1580，照随访问卷 P2-294）——
    管理端商品表原先也只拿得到上架的，下架的当场从表里消失，编辑弹窗里的「上架」永远用不上，同编码重建又 409（编码唯一），
    补货后只能换编码另建。"""
    query = db.query(SpdGoods)
    if not include_inactive:
        query = query.filter(SpdGoods.active.is_(True))
    return [
        {"id": g.id, "code": g.code, "name": g.name, "points": g.points,
         "stock": g.stock, "image_url": g.image_url, "active": g.active}
        for g in query.order_by(SpdGoods.id).limit(200).all()
    ]


class GoodsPatch(BaseModel):
    """改档与建档同一套约束（P1-94）：原先收裸 dict、照单全收。不传即不改；不可空的列显式传 null 是 422。"""

    name: str = Field(default=UNSET, min_length=1, max_length=64, pattern=NON_BLANK)
    points: int = Field(default=UNSET, ge=1, le=100000)
    stock: int = Field(default=UNSET, ge=0, le=100000)
    image_url: str = Field(default=UNSET, max_length=256)
    active: bool = Field(default=UNSET)
    # 改库存时带上页面看到的库存（P2-920）：库存还是那个数才改，否则 409——兑换是条件扣减（`take_amount`），改档原先把
    # 页面加载时的库存整值写回，这期间兑换占掉的件数被「还」回库存、造成超兑。不带的旧调用照旧直接改
    stock_seen: int | None = Field(default=None, ge=0, le=100000)


@router.patch("/goods/{goods_id}", response_model=GoodsUpdatedOut,
              dependencies=[Depends(require_roles("director"))])
def update_goods(goods_id: int, body: GoodsPatch, db: Session = Depends(get_db)):
    goods = db.get(SpdGoods, goods_id)
    if goods is None:
        raise HTTPException(status_code=404, detail="商品不存在")
    changes = body.model_dump(exclude_unset=True)
    seen = changes.pop("stock_seen", None)
    if "stock" in changes and seen is not None:
        moved = db.query(SpdGoods).filter(SpdGoods.id == goods_id, SpdGoods.stock == seen).update(
            {SpdGoods.stock: changes.pop("stock")}, synchronize_session=False)
        if not moved:
            db.rollback()
            raise HTTPException(status_code=409, detail="库存刚变过（有人兑换或别人改过），请刷新后再改")
    for key, value in changes.items():
        setattr(goods, key, value)
    db.commit()
    db.refresh(goods)
    return {"id": goods.id, "stock": goods.stock, "active": goods.active}


class RedeemIn(BaseModel):
    goods_id: int


#: 出核销码最多抽几次（P2-1578）。6 位码一百万个，同时有 N 张待核销单时一抽撞上的概率约 N/10⁶：常驻一百来张时连撞 8 次
#: 是 10⁻³² 量级、等于不会发生；真连撞 8 次说明待核销单已堆到码空间的大半，宁可 409 让人重试，也不发一个与别人同码的单
VERIFY_CODE_ATTEMPTS = 8


def _fresh_verify_code(db: Session) -> str | None:
    """抽一个与现有待核销单都不同的 6 位核销码；连抽 `VERIFY_CODE_ATTEMPTS` 次都撞上返回 None（P2-1578）。

    原先随手一抽、不判重：两张待核销单撞上同一个码时，核销只认码，同一个码核得了两次——第二次核掉的是别人的单，兑换人
    再去只剩「核销码无效或已核销」，积分已扣、也没有退回入口。核销只认待核销的单，所以只跟待核销的比，已核销 / 已取消的
    码可以再发。判重与插入不在一个临界区：两笔兑换同一瞬间抽中同一个码的概率还要再乘百万分之一，不另加锁；要根除得给待
    核销的码建部分唯一索引（迁移与存量撞码的处置），不在本条。
    """
    for _ in range(VERIFY_CODE_ATTEMPTS):
        code = f"{randbelow(1000000):06d}"
        taken = (
            db.query(SpdRedeem.id)
            .filter(SpdRedeem.verify_code == code, SpdRedeem.status == "pending")
            .first()
        )
        if taken is None:
            return code
    return None


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
    # 先出码、后扣减（P2-1578）：码在待核销单里判过重；抽不出不撞的码时库存与积分都还没动
    verify_code = _fresh_verify_code(db)
    if verify_code is None:
        raise HTTPException(status_code=409, detail="核销码连续撞上未核销的兑换单，请重试（库存与积分未扣）")
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
        verify_code=verify_code, status="pending",
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
    account = db.query(SpdPointAccount).filter(SpdPointAccount.user_id == user.id).first()
    my_account_id = account.id if account else 0
    if mine:
        query = query.filter(SpdRedeem.account_id == my_account_id)
    rows = paginate(query.order_by(SpdRedeem.id.desc()), response, offset, limit)
    goods = {g.id: g.name for g in db.query(SpdGoods).all()}
    # P0-41：核销码是线下领奖的凭证，核销只认码不认人——只给兑换人本人看，别人（含经办与管理员）一律打码。
    return [
        {"id": r.id, "goods_id": r.goods_id, "goods_name": goods.get(r.goods_id, ""),
         "points": r.points,
         "verify_code": r.verify_code if r.account_id == my_account_id else "******",
         "status": r.status,
         "created_at": r.created_at.isoformat(),
         "verified_at": r.verified_at.isoformat() if r.verified_at else ""}
        for r in rows
    ]


class VerifyIn(BaseModel):
    verify_code: str = Field(min_length=4, max_length=16, pattern=NON_BLANK)


@router.post("/redeems/verify", response_model=RedeemVerifiedOut,
             dependencies=[Depends(require_roles("director", "operator"))])
def verify_redeem(
    body: VerifyIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """线下核销：凭核销码核销，已核销的不能重复核销。"""
    record = (
        db.query(SpdRedeem)
        .filter(SpdRedeem.verify_code == body.verify_code, SpdRedeem.status == "pending")
        # 按兑换先后取（P2-1578）：出码已在待核销单里判重，但修前存下的待核销单里可能已有同码的——原先不排序，PG 上
        # 取到哪张看堆里的物理顺序，后兑换的人先来核销时两人的奖品直接对调；按单号先后核，结果确定
        .order_by(SpdRedeem.id)
        .first()
    )
    if record is None:
        raise HTTPException(status_code=404, detail="核销码无效或已核销")
    # 状态闸门：判定与翻转同一条 SQL（P2-115）。原先查到待核销就无条件改 verified：同一个码在两个点位同时出示，两路都查到
    # 这张待核销单、都 200——兑换时只扣了一件库存、一份积分，奖品却发了两份
    verified = cast(CursorResult, db.execute(
        update(SpdRedeem)
        .where(SpdRedeem.id == record.id, SpdRedeem.status == "pending")
        .values(status="verified", verified_by=user.id, verified_at=now_naive())
        .execution_options(synchronize_session=False)
    ))
    if not verified.rowcount:
        db.rollback()
        raise HTTPException(status_code=404, detail="核销码无效或已核销")
    db.commit()
    return {"id": record.id, "status": "verified"}
