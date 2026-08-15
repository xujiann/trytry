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
from datetime import date, timedelta
from secrets import randbelow

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...clock import now_naive
from ...concurrency import add_amount, insert_if_absent, take_amount
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


@router.post("/indicators", status_code=201, dependencies=[Depends(require_roles("director"))])
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


@router.get("/indicators")
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


@router.patch("/indicators/{indicator_id}", dependencies=[Depends(require_roles("director"))])
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


@router.get("/indicators/{indicator_id}/usage")
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


def _object_filter(db: Session, model, object_type: str, object_id: int):
    """把"考核对象"翻译成模型上的过滤条件。

    village_doctor 的过滤依据是 `village_doctor_id`（在管档案）或
    `assignee_id`（任务），不是机构——一个村卫生室可能有两位村医。
    """
    if object_type == "org":
        for col in ("org_id", "initiator_org_id"):
            if hasattr(model, col):
                return getattr(model, col) == object_id
    if object_type == "team" and hasattr(model, "team_id"):
        return model.team_id == object_id
    if object_type == "doctor":
        for col in ("assignee_id", "doctor_user_id", "operator_id", "initiator_id"):
            if hasattr(model, col):
                return getattr(model, col) == object_id
    if object_type == "village_doctor":
        for col in ("village_doctor_id", "assignee_id", "initiator_id", "reporter_id"):
            if hasattr(model, col):
                return getattr(model, col) == object_id
    return None


def collect_metrics(
    db: Session, indicator: SpdIndicator, object_type: str, object_id: int,
    period: str, program_code: str = "",
) -> dict[str, float]:
    """按取数口径统计一个考核对象在一个周期内的原始数字。"""
    start, end = _period_range(period)
    source = indicator.data_source

    def scoped(model, query):
        cond = _object_filter(db, model, object_type, object_id)
        if cond is not None:
            query = query.filter(cond)
        if program_code and hasattr(model, "program_code"):
            query = query.filter(model.program_code == program_code)
        return query

    if source == "task":
        base = scoped(SpdTask, db.query(SpdTask).filter(
            SpdTask.created_at >= f"{start} 00:00:00", SpdTask.created_at <= f"{end} 23:59:59"))
        total = base.count()
        return {
            "total": float(total),
            "done": float(base.filter(SpdTask.status == "done").count()),
            "overdue": float(base.filter(SpdTask.status == "overdue").count()),
        }
    if source in ("enrollment", "archive", "assessment"):
        base = scoped(SpdEnrollment, db.query(SpdEnrollment).filter(
            SpdEnrollment.created_at <= f"{end} 23:59:59"))
        enrolled = base.filter(SpdEnrollment.status == "active").count()
        if source == "enrollment":
            from ..models import SpdCandidate

            target_query = db.query(SpdCandidate).filter(
                SpdCandidate.status.in_(["target", "enrolled"]))
            if object_type == "org":
                target_query = target_query.filter(SpdCandidate.org_id == object_id)
            if program_code:
                target_query = target_query.filter(SpdCandidate.program_code == program_code)
            return {
                "enrolled": float(enrolled),
                "target": float(target_query.count() or enrolled),
                "high_risk": float(
                    base.filter(SpdEnrollment.risk_level.in_(["high", "very_high"])).count()
                ),
            }
        if source == "archive":
            return {
                "enrolled": float(enrolled),
                "archived": float(
                    base.filter(
                        SpdEnrollment.status == "active", SpdEnrollment.archived.is_(True)
                    ).count()
                ),
            }
        patient_ids = [
            e.patient_id for e in base.filter(SpdEnrollment.status == "active").all()
        ]
        assessed = (
            db.query(SpdAssessment.patient_id)
            .filter(
                SpdAssessment.patient_id.in_(patient_ids or [0]),
                SpdAssessment.created_at >= f"{start} 00:00:00",
                SpdAssessment.created_at <= f"{end} 23:59:59",
            )
            .distinct()
            .count()
        )
        return {"enrolled": float(enrolled), "assessed": float(assessed)}
    if source == "path":
        enroll_ids = [
            e.id
            for e in scoped(SpdEnrollment, db.query(SpdEnrollment)).all()
        ]
        base = db.query(SpdPathInstance).filter(
            SpdPathInstance.enrollment_id.in_(enroll_ids or [0]),
            SpdPathInstance.started_at <= f"{end} 23:59:59",
        )
        return {
            "total": float(base.count()),
            "completed": float(base.filter(SpdPathInstance.status == "completed").count()),
            "running": float(base.filter(SpdPathInstance.status == "running").count()),
        }
    if source == "referral":
        base = scoped(SpdReferralCase, db.query(SpdReferralCase).filter(
            SpdReferralCase.created_at >= f"{start} 00:00:00",
            SpdReferralCase.created_at <= f"{end} 23:59:59"))
        return {
            "total": float(base.count()),
            "closed": float(base.filter(SpdReferralCase.status == "closed").count()),
            "effective": float(base.filter(SpdReferralCase.effective_visit.is_(True)).count()),
        }
    if source == "measurement":
        patient_ids = [
            e.patient_id
            for e in scoped(SpdEnrollment, db.query(SpdEnrollment)).filter(
                SpdEnrollment.status == "active").all()
        ]
        base = db.query(SpdMeasurement).filter(
            SpdMeasurement.patient_id.in_(patient_ids or [0]),
            SpdMeasurement.measured_at >= f"{start} 00:00:00",
            SpdMeasurement.measured_at <= f"{end} 23:59:59",
        )
        total = base.count()
        normal = base.filter(SpdMeasurement.level == "normal").count()
        return {"total": float(total), "normal": float(normal),
                "abnormal": float(total - normal)}
    if source == "case_report":
        base = scoped(SpdCaseReport, db.query(SpdCaseReport).filter(
            SpdCaseReport.created_at >= f"{start} 00:00:00",
            SpdCaseReport.created_at <= f"{end} 23:59:59"))
        return {
            "reported": float(base.count()),
            "handled": float(base.filter(SpdCaseReport.status.in_(["done", "closed"])).count()),
        }
    return {"total": 0.0}


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


@router.post("/assess-plans", status_code=201, dependencies=[Depends(require_roles("director"))])
def create_plan(body: PlanIn, db: Session = Depends(get_db)):
    codes = [i.get("indicator_code") for i in body.items]
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


@router.get("/assess-plans")
def list_plans(level: str | None = None, active: bool | None = None,
               db: Session = Depends(get_db)):
    query = db.query(SpdAssessPlan)
    if level:
        query = query.filter(SpdAssessPlan.level == level)
    if active is not None:
        query = query.filter(SpdAssessPlan.active.is_(active))
    return [_plan_out(p) for p in query.order_by(SpdAssessPlan.id).limit(200).all()]


@router.patch("/assess-plans/{plan_id}", dependencies=[Depends(require_roles("director"))])
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
        query = db.query(SpdTeam).filter(SpdTeam.active.is_(True))
        if object_ids:
            query = query.filter(SpdTeam.id.in_(object_ids))
        return [(t.id, t.name) for t in query.limit(500).all()]
    if plan.object_type == "village_doctor":
        query = db.query(SpdVillageDoctor).filter(SpdVillageDoctor.active.is_(True))
        if object_ids:
            query = query.filter(SpdVillageDoctor.user_id.in_(object_ids))
        rows = query.limit(500).all()
        names = {
            u.id: u.full_name or u.username
            for u in db.query(User).filter(User.id.in_([v.user_id for v in rows] or [0]))
        }
        return [(v.user_id, names.get(v.user_id, "")) for v in rows]
    query = db.query(User).filter(User.role.in_(["doctor", "public_health"]))
    if object_ids:
        query = query.filter(User.id.in_(object_ids))
    return [(u.id, u.full_name or u.username) for u in query.limit(500).all()]


@router.post("/scores/run", dependencies=[Depends(require_roles("director"))])
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
    results = []
    for object_id, object_name in objects:
        total_score, detail = 0.0, []
        for item in plan.items or []:
            code = item.get("indicator_code")
            indicator = indicators.get(code)
            if indicator is None:
                detail.append({"indicator_code": code, "error": "指标不存在或已停用"})
                continue
            metrics = collect_metrics(
                db, indicator, plan.object_type, object_id, body.period, body.program_code
            )
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
            record = (
                db.query(SpdScore)
                .filter(
                    SpdScore.plan_id == plan.id, SpdScore.period == body.period,
                    SpdScore.object_type == plan.object_type,
                    SpdScore.object_id == object_id,
                )
                .first()
            )
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


@router.get("/scores")
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


@router.get("/scores/{score_id}")
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


@router.get("/scores-analysis")
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


@router.get("/workload")
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


@router.post("/point-rules", status_code=201, dependencies=[Depends(require_roles("director"))])
def create_point_rule(body: PointRuleIn, db: Session = Depends(get_db)):
    rule = SpdPointRule(**body.model_dump())
    db.add(rule)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该积分规则编码已存在") from None
    return {"id": rule.id, "code": rule.code, "event": rule.event, "points": rule.points}


@router.get("/point-rules")
def list_point_rules(db: Session = Depends(get_db)):
    return [
        {"id": r.id, "code": r.code, "name": r.name, "event": r.event,
         "points": r.points, "daily_limit": r.daily_limit, "active": r.active}
        for r in db.query(SpdPointRule).order_by(SpdPointRule.id).limit(100).all()
    ]


@router.patch("/point-rules/{rule_id}", dependencies=[Depends(require_roles("director"))])
def update_point_rule(rule_id: int, body: dict, db: Session = Depends(get_db)):
    rule = db.get(SpdPointRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="积分规则不存在")
    for key in ("name", "points", "daily_limit", "condition", "active"):
        if key in body:
            setattr(rule, key, body[key])
    db.commit()
    return {"id": rule.id, "points": rule.points, "active": rule.active}


@router.get("/point-accounts/me")
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


@router.get("/point-accounts")
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


@router.post("/point-accounts/signin")
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


@router.post("/goods", status_code=201, dependencies=[Depends(require_roles("director"))])
def create_goods(body: GoodsIn, db: Session = Depends(get_db)):
    goods = SpdGoods(**body.model_dump())
    db.add(goods)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该商品编码已存在") from None
    return {"id": goods.id, "code": goods.code, "name": goods.name, "stock": goods.stock}


@router.get("/goods")
def list_goods(db: Session = Depends(get_db)):
    return [
        {"id": g.id, "code": g.code, "name": g.name, "points": g.points,
         "stock": g.stock, "image_url": g.image_url, "active": g.active}
        for g in db.query(SpdGoods).filter(SpdGoods.active.is_(True))
        .order_by(SpdGoods.id).limit(200).all()
    ]


@router.patch("/goods/{goods_id}", dependencies=[Depends(require_roles("director"))])
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


@router.post("/redeems", status_code=201)
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
    db.add(
        SpdPointRecord(
            account_id=account.id, rule_code="", direction="out", points=goods.points,
            balance_after=account.balance, ref_type="redeem", note=f"兑换{goods.name}",
        )
    )
    db.commit()
    return {"id": record.id, "verify_code": record.verify_code, "balance": account.balance}


@router.get("/redeems")
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


@router.post("/redeems/verify", dependencies=[Depends(require_roles("director", "operator"))])
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
