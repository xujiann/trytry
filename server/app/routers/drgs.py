"""DRGs 简化版（浙江省指南 M12，#53；块3 扩充）：分组目录、出院病例自动入组、CMI 分析。

- DrgGroup：分组目录（编码/名称/MDC/基准权重/主诊断关键词/主手术关键词），
  启动种子化 62 个县域常见分组 + QY 兜底组，admin 可增补与调权；
- 入组（块3 由单关键词升级为多关键词 + 主手术标志）：
  主诊断命中越多、命中词越长得分越高；require_procedure 的外科组必须命中主手术，
  全部未命中则落入 QY 兜底组（病案首页需复核）；
- GET /api/drgs/stats：各机构 CMI（Σ权重/正式入组例数，兜底组不计入）、
  各组例数/均费、按 MDC 汇总。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from ..data.drg_groups_seed import FALLBACK_DRG_GROUP, SEED_DRG_GROUPS
from ..concurrency import insert_or_conflict
from ..visibility import scope_org_list
from ..database import get_db
from ..deps import get_current_user, require_admin, require_roles, resolve_business_date
from ..models import Admission, CaseSummary, DrgGroup, Organization, User

# 同组历史病例少于该数不做事中预警——3 个病例算出来的"均值"，预警的是噪声。
MIN_BASELINE_CASES = 5

router = APIRouter(prefix="/api/drgs", tags=["DRGs分析"], dependencies=[Depends(get_current_user)])

# 兜底组编码：未匹配任何分组的病例落入此组，统计单列且不计入 CMI
FALLBACK_CODE = FALLBACK_DRG_GROUP["code"]

__all__ = ["router", "assign_drg_group", "SEED_DRG_GROUPS", "FALLBACK_DRG_GROUP", "FALLBACK_CODE"]


def _split(value: str) -> list[str]:
    return [k.strip() for k in (value or "").split(",") if k.strip()]


def _match_group(group: DrgGroup, diagnosis: str, operation: str) -> tuple[int, int] | None:
    """单组匹配打分。返回 (总分, 最长命中词长度)，不匹配返回 None。

    评分：每命中一个主诊断关键词 +10，每命中一个主手术关键词 +20（外科组权重更高），
    再加上最长命中关键词的字数作为细粒度区分（长词更具体，优先级更高）。
    """
    dx_hits = [kw for kw in _split(group.keywords) if kw in diagnosis]
    op_hits = [kw for kw in _split(group.procedure_keywords) if operation and kw in operation]
    # 外科操作组：未命中主手术一律不得入组，避免内科保守治疗病例误入手术组
    if group.require_procedure and not op_hits:
        return None
    if not dx_hits and not op_hits:
        return None
    longest = max((len(kw) for kw in dx_hits + op_hits), default=0)
    return len(dx_hits) * 10 + len(op_hits) * 20 + longest, longest


def assign_drg_group(db: Session, summary: CaseSummary) -> dict | None:
    """出院病例入组：多关键词 + 主手术标志匹配，未命中落入 QY 兜底组。

    回填 summary.drg_code/drg_weight 并提交，返回入组结果（含 fallback 标志）。
    """
    diagnosis = summary.discharge_diagnosis or ""
    operation = summary.operation or ""
    best: tuple[tuple[int, int], DrgGroup] | None = None
    for group in (
        db.query(DrgGroup)
        .filter(DrgGroup.active.is_(True), DrgGroup.is_fallback.is_(False))
        .all()
    ):
        score = _match_group(group, diagnosis, operation)
        if score is not None and (best is None or score > best[0]):
            best = (score, group)

    # 另起一个名字：`group` 是上面 for 的循环变量，复用它会让"选中的那个组"
    # 和"正在比对的那个组"混在一起读不清，类型上也说不通。
    chosen: DrgGroup | None
    if best is not None:
        chosen = best[1]
    else:
        chosen = db.query(DrgGroup).filter(DrgGroup.code == FALLBACK_CODE).first()
        if chosen is None:  # pragma: no cover - 兜底组缺失（种子未执行）
            return None
    summary.drg_code = chosen.code
    summary.drg_weight = chosen.base_weight
    db.commit()
    return {
        "drg_code": chosen.code,
        "drg_name": chosen.name,
        "mdc": chosen.mdc,
        "mdc_name": chosen.mdc_name,
        "weight": chosen.base_weight,
        "fallback": bool(chosen.is_fallback),
    }


# ---------- 分组目录 ----------


class DrgGroupCreate(BaseModel):
    code: str = Field(min_length=1, max_length=16)
    name: str = Field(min_length=1, max_length=128)
    base_weight: float = Field(gt=0)
    keywords: str = ""
    mdc: str = ""
    mdc_name: str = ""
    procedure_keywords: str = ""
    require_procedure: bool = False
    active: bool = True


class DrgGroupUpdate(BaseModel):
    name: str | None = None
    base_weight: float | None = Field(default=None, gt=0)
    keywords: str | None = None
    mdc: str | None = None
    mdc_name: str | None = None
    procedure_keywords: str | None = None
    require_procedure: bool | None = None
    active: bool | None = None


def _group_out(g: DrgGroup) -> dict:
    return {
        "id": g.id,
        "code": g.code,
        "name": g.name,
        "base_weight": g.base_weight,
        "keywords": g.keywords,
        "mdc": g.mdc,
        "mdc_name": g.mdc_name,
        "procedure_keywords": g.procedure_keywords,
        "require_procedure": g.require_procedure,
        "is_fallback": g.is_fallback,
        "active": g.active,
    }


# ---- 响应契约（字段精确镜像 `_group_out` 等现输出，勿改字节）----
# 取证与建模判断见 tests/test_drgs_contract.py 的 docstring。
# base_weight 是无量纲 Float 列（不是 Money）：整数入参也以 2.0 出参，float 才是原样；
# stats/预警的数值派生全是 SQL AVG/真除法/兜底 0.0 的浮点产地，声明 float 不改字节。


class DrgGroupOut(BaseModel):
    id: int
    code: str
    name: str
    base_weight: float
    keywords: str
    mdc: str
    mdc_name: str
    procedure_keywords: str
    require_procedure: bool
    is_fallback: bool
    active: bool


class DrgMatchScoreOut(BaseModel):
    # 存量出参的名义与语义有错位：diagnosis_hits 实为总分、procedure_hits 实为
    # 最长命中词长——契约照原样钉住，改名属破坏性变更（CLAUDE.md 第 7 条）。
    diagnosis_hits: int
    procedure_hits: int


class DrgPreCheckCandidateOut(DrgGroupOut):
    """候选组行 = 分组目录行 + 恒在尾键 match_score（继承加尾键保键序）。"""

    match_score: DrgMatchScoreOut


class DrgWeightRangeOut(BaseModel):
    min: float
    max: float


class DrgPreCheckOut(BaseModel):
    # weight_range 是「键恒在值可空」：未命中时为 null，不是键消失
    diagnosis: str
    operation: str
    matched: bool
    candidates: list[DrgPreCheckCandidateOut]
    weight_range: DrgWeightRangeOut | None
    caliber: str


class DrgOrgStatOut(BaseModel):
    org_id: int
    org_name: str
    cases: int
    grouped: int
    fallback: int
    grouped_pct: float
    fallback_pct: float
    cmi: float
    avg_cost: float


class DrgGroupStatOut(BaseModel):
    drg_code: str
    drg_name: str
    mdc: str
    fallback: bool
    cases: int
    avg_cost: float


class DrgMdcStatOut(BaseModel):
    mdc: str
    mdc_name: str
    groups: int
    cases: int
    cmi: float
    avg_cost: float
    fallback: bool


class DrgStatsOut(BaseModel):
    orgs: list[DrgOrgStatOut]
    groups: list[DrgGroupStatOut]
    mdcs: list[DrgMdcStatOut]


class DrgInStayAlertOut(BaseModel):
    admission_id: int
    patient_id: int
    org_id: int
    drg_code: str
    stayed_days: int
    baseline_avg_days: float
    baseline_cases: int
    over_ratio: float


class DrgInsufficientBaselineOut(BaseModel):
    admission_id: int
    drg_code: str
    history_cases: int
    stayed_days: int


class DrgInStayAlertsOut(BaseModel):
    today: str
    los_multiplier: float
    alerts: list[DrgInStayAlertOut]
    insufficient_baseline: list[DrgInsufficientBaselineOut]
    ungrouped_in_stay: int
    caliber: str


@router.get("/groups", response_model=list[DrgGroupOut])
def list_groups(mdc: str | None = None, db: Session = Depends(get_db)):
    query = db.query(DrgGroup)
    if mdc:
        query = query.filter(DrgGroup.mdc == mdc)
    return [_group_out(g) for g in query.order_by(DrgGroup.code).limit(500).all()]


@router.post(
    "/groups", status_code=201, response_model=DrgGroupOut, dependencies=[Depends(require_admin)]
)
def create_group(body: DrgGroupCreate, db: Session = Depends(get_db)):
    if db.query(DrgGroup).filter(DrgGroup.code == body.code).first():
        raise HTTPException(status_code=409, detail="分组编码已存在")
    group = insert_or_conflict(db, DrgGroup(**body.model_dump()), "分组编码已存在")
    return _group_out(group)


@router.patch(
    "/groups/{group_id}", response_model=DrgGroupOut, dependencies=[Depends(require_admin)]
)
def update_group(group_id: int, body: DrgGroupUpdate, db: Session = Depends(get_db)):
    group = db.get(DrgGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="分组不存在")
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(group, field, value)
    db.commit()
    return _group_out(group)


# ---------- CMI 与组均费用分析 ----------


@router.get("/stats", response_model=DrgStatsOut, dependencies=[Depends(require_roles("director"))])
def drg_stats(db: Session = Depends(get_db)):
    # 第十轮 P2：管理聚合限 director/admin。这是给管理者看的账（各机构 CMI、
    # 例数、均费），不是给一线的预警——与多点触发监测那类刻意保持宽的区分开。
    """DRGs 分析：机构 CMI 与入组率、各组例数/均费、按 MDC 汇总，QY 兜底组单列。

    口径（块3）：grouped 仅统计正式分组，QY 兜底组计入 fallback 并从 CMI 分母剔除，
    fallback_pct 反映病案首页填写质量（兜底率越高说明主诊断/主手术填写越不规范）。
    """
    # 正式入组判定：非空且非兜底组
    formal = (CaseSummary.drg_code != "") & (CaseSummary.drg_code != FALLBACK_CODE)
    org_rows = (
        db.query(
            Admission.org_id,
            Organization.name,
            func.count(CaseSummary.id).label("cases"),
            func.sum(case((formal, 1), else_=0)).label("grouped"),
            func.sum(case((CaseSummary.drg_code == FALLBACK_CODE, 1), else_=0)).label("fallback"),
            func.coalesce(
                func.sum(case((formal, CaseSummary.drg_weight), else_=0.0)), 0.0
            ).label("weight_sum"),
            func.coalesce(func.avg(CaseSummary.total_cost), 0.0).label("avg_cost"),
        )
        .join(CaseSummary, CaseSummary.admission_id == Admission.id)
        .join(Organization, Organization.id == Admission.org_id)
        .group_by(Admission.org_id, Organization.name)
        .all()
    )
    group_rows = (
        db.query(
            CaseSummary.drg_code,
            func.count(CaseSummary.id).label("cases"),
            func.coalesce(func.avg(CaseSummary.total_cost), 0.0).label("avg_cost"),
        )
        .filter(CaseSummary.drg_code != "")
        .group_by(CaseSummary.drg_code)
        .all()
    )
    catalog = {g.code: g for g in db.query(DrgGroup).all()}

    # 按 MDC 汇总：例数、Σ权重、CMI、均费（兜底组归入 QY 单列）
    mdc_agg: dict[str, dict] = {}
    for r in group_rows:
        group = catalog.get(r.drg_code)
        key = group.mdc if group and group.mdc else "UNKNOWN"
        entry = mdc_agg.setdefault(
            key,
            {
                "mdc": key,
                "mdc_name": group.mdc_name if group else "未知",
                "cases": 0,
                "weight_sum": 0.0,
                "cost_sum": 0.0,
                "groups": 0,
            },
        )
        entry["cases"] += r.cases
        entry["groups"] += 1
        entry["weight_sum"] += (group.base_weight if group else 0.0) * r.cases
        entry["cost_sum"] += (r.avg_cost or 0.0) * r.cases

    return {
        "orgs": [
            {
                "org_id": r.org_id,
                "org_name": r.name,
                "cases": r.cases,
                "grouped": int(r.grouped or 0),
                "fallback": int(r.fallback or 0),
                "grouped_pct": round((r.grouped or 0) * 100.0 / r.cases, 2) if r.cases else 0.0,
                "fallback_pct": round((r.fallback or 0) * 100.0 / r.cases, 2) if r.cases else 0.0,
                # CMI = Σ权重 / 正式入组例数（QY 兜底组不计入）
                "cmi": round(r.weight_sum / r.grouped, 3) if r.grouped else 0.0,
                "avg_cost": round(r.avg_cost, 2),
            }
            for r in org_rows
        ],
        "groups": [
            {
                "drg_code": r.drg_code,
                "drg_name": catalog[r.drg_code].name if r.drg_code in catalog else r.drg_code,
                "mdc": catalog[r.drg_code].mdc if r.drg_code in catalog else "",
                "fallback": bool(r.drg_code in catalog and catalog[r.drg_code].is_fallback),
                "cases": r.cases,
                "avg_cost": round(r.avg_cost, 2),
            }
            for r in group_rows
        ],
        "mdcs": [
            {
                "mdc": e["mdc"],
                "mdc_name": e["mdc_name"],
                "groups": e["groups"],
                "cases": e["cases"],
                "cmi": round(e["weight_sum"] / e["cases"], 3) if e["cases"] else 0.0,
                "avg_cost": round(e["cost_sum"] / e["cases"], 2) if e["cases"] else 0.0,
                "fallback": e["mdc"] == FALLBACK_DRG_GROUP["mdc"],
            }
            for e in sorted(mdc_agg.values(), key=lambda x: x["mdc"])
        ],
    }


# ---------- 阶段十：事前提示与事中预警 ----------


class PreCheckIn(BaseModel):
    diagnosis: str = Field(min_length=1, max_length=256)
    operation: str = Field(default="", max_length=256)


@router.post("/pre-check", response_model=DrgPreCheckOut)
def drg_pre_check(body: PreCheckIn, db: Session = Depends(get_db)):
    """事前提示：入院登记时按拟诊断预判入组与权重。

    **给出的是"可能入哪几组"而不是一个结论**：入院时诊断本就未定，
    报一个确定的组会让人照着组去写诊断——那是把 DRG 用反了。
    故返回候选组按匹配度排序，并明确标注这只是提示。

    未命中任何组也如实说"未匹配"，不落到兜底组：兜底组是出院入组时
    保证每个病例都有归属用的，事前拿它当预测结果毫无信息量。
    """
    diagnosis, operation = body.diagnosis, body.operation
    scored = []
    for group in (
        db.query(DrgGroup)
        .filter(DrgGroup.active.is_(True), DrgGroup.is_fallback.is_(False))
        .all()
    ):
        score = _match_group(group, diagnosis, operation)
        if score is not None:
            scored.append((score, group))
    scored.sort(key=lambda x: x[0], reverse=True)
    candidates = [
        {**_group_out(g), "match_score": {"diagnosis_hits": s[0], "procedure_hits": s[1]}}
        for s, g in scored[:5]
    ]
    weights = [c["base_weight"] for c in candidates]
    return {
        "diagnosis": diagnosis,
        "operation": operation,
        "matched": bool(candidates),
        "candidates": candidates,
        "weight_range": (
            {"min": min(weights), "max": max(weights)} if weights else None
        ),
        "caliber": "事前提示给候选组而非结论——入院时诊断未定，报一个确定的组"
                   "会让人照着组去写诊断；未命中不落兜底组，那在事前没有信息量",
    }


@router.get("/in-stay-alerts", response_model=DrgInStayAlertsOut)
def in_stay_alerts(
    org_id: int | None = None,
    los_multiplier: float = Query(default=1.5, ge=1.0, le=5.0),
    today: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """事中预警：在院病例住院日已明显超出同组均值。

    **均值取自本院已出院且已入组的历史病例**，不用外部标杆：各县病种结构
    差异极大，拿别处的均值来卡自己的病人，预警会多到没人看。

    同组历史病例少于 5 例的不预警，但**单列报出**——3 个病例算出来的"均值"，
    预警的是噪声不是问题；而不提的话，看的人会以为这些病例没问题。
    """
    end = resolve_business_date(today)

    # 历史基线：已出院且已入组的病例，住院日由入出院时刻现算
    # （平台没有存 los_days，存一份就会与两个时刻不一致）
    history = (
        db.query(CaseSummary, Admission)
        .join(Admission, CaseSummary.admission_id == Admission.id)
        .filter(CaseSummary.drg_code != "", Admission.discharged_at.isnot(None))
        .all()
    )
    baseline: dict[str, list[int]] = {}
    for summary, adm in history:
        days = (adm.discharged_at.date() - adm.admitted_at.date()).days
        if days >= 0:
            baseline.setdefault(summary.drg_code, []).append(days)

    query = (
        db.query(Admission, CaseSummary)
        .outerjoin(CaseSummary, CaseSummary.admission_id == Admission.id)
        .filter(Admission.status == "admitted")
    )
    query = scope_org_list(db, user, query, Admission, org_id)
    rows = query.order_by(Admission.id.desc()).limit(500).all()

    alerts, insufficient, ungrouped = [], [], 0
    for adm, summary in rows:
        drg_code = summary.drg_code if summary else ""
        if not drg_code:
            # 尚未填病案首页的在院病例：事中无从比对，计数报出
            ungrouped += 1
            continue
        stayed = (end - adm.admitted_at.date()).days
        samples = baseline.get(drg_code, [])
        if len(samples) < MIN_BASELINE_CASES:
            insufficient.append({
                "admission_id": adm.id, "drg_code": drg_code,
                "history_cases": len(samples), "stayed_days": stayed,
            })
            continue
        avg = sum(samples) / len(samples)
        if avg > 0 and stayed > avg * los_multiplier:
            alerts.append({
                "admission_id": adm.id,
                "patient_id": adm.patient_id,
                "org_id": adm.org_id,
                "drg_code": drg_code,
                "stayed_days": stayed,
                "baseline_avg_days": round(avg, 1),
                "baseline_cases": len(samples),
                "over_ratio": round(stayed / avg, 2),
            })
    return {
        "today": end.isoformat(),
        "los_multiplier": los_multiplier,
        "alerts": sorted(alerts, key=lambda a: -a["over_ratio"]),
        # 样本不足与尚未入组的都单列，不混进"无预警"
        "insufficient_baseline": insufficient,
        "ungrouped_in_stay": ungrouped,
        "caliber": f"基线取本院已出院且已入组病例的住院日（由入出院时刻现算）；"
                   f"同组历史少于 {MIN_BASELINE_CASES} 例不预警，单列在 "
                   f"insufficient_baseline；尚未填病案首页的在院病例计入 ungrouped_in_stay",
    }
