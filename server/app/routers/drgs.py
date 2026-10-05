"""DRGs 简化版（浙江省指南 M12，#53；块3 扩充）：分组目录、出院病例自动入组、CMI 分析。

- DrgGroup：分组目录（编码/名称/MDC/基准权重/主诊断关键词/主手术关键词），
  启动种子化 62 个县域常见分组 + QY 兜底组，admin 可增补与调权；
- 入组（块3 由单关键词升级为多关键词 + 主手术标志）：
  主诊断命中越多、命中词越长得分越高；require_procedure 的外科组必须命中主手术，
  全部未命中则落入 QY 兜底组（病案首页需复核）；
- GET /api/drgs/stats：各机构 CMI（Σ权重/正式入组例数，兜底组不计入）、
  各组例数/均费、按 MDC 汇总。
"""
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, FiniteFloat
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from ..data.drg_groups_seed import FALLBACK_DRG_GROUP, SEED_DRG_GROUPS
from .. import clock
from ..concurrency import insert_or_conflict
from ..visibility import scope_org_list
from ..database import get_db
from ..deps import get_current_user, require_admin, require_roles, resolve_business_date, row_dict
from ..models import Admission, CaseSummary, DrgGroup, Organization, Patient, User
from ..texttypes import NON_BLANK, split_list, text_key

# 同组历史病例少于该数不做事中预警——3 个病例算出来的"均值"，预警的是噪声。
MIN_BASELINE_CASES = 5

router = APIRouter(prefix="/api/drgs", tags=["DRGs分析"], dependencies=[Depends(get_current_user)])

# 兜底组编码：未匹配任何分组的病例落入此组，统计单列且不计入 CMI
FALLBACK_CODE = FALLBACK_DRG_GROUP["code"]

__all__ = ["router", "assign_drg_group", "drg_label", "SEED_DRG_GROUPS", "FALLBACK_DRG_GROUP", "FALLBACK_CODE"]


def _split(value: str) -> list[str]:
    # 全角逗号、顿号也认（P1-137）：只按半角逗号拆，界面上用中文输入法填的「鼻息肉，鼻窦炎」是一个词、永远命中不了
    return split_list(value)


def _never_matches(keywords: str, procedure_keywords: str, require_procedure: bool) -> str:
    """这组配置永远入不了组：返回原因，入得了返回空串（P2-1019，与 P2-466 / P2-712「永不命中、也不报错的配置在建的时候拦」
    同一条规矩）。

    `_match_score`：勾了「必须命中主手术」、主手术关键词却一个有效词都没有，恒不命中；两类关键词都没有有效词（空、只有空白
    与分隔符），同样恒不命中。原先照建 201，出院入组一例都进不来、全落进 QY 兜底组，建组的人看不出来。
    """
    dx = [kw for kw in _split(keywords) if text_key(kw)]
    op = [kw for kw in _split(procedure_keywords) if text_key(kw)]
    if require_procedure and not op:
        return "勾了「必须命中主手术」却没有主手术关键词：这个组永远入不了，请填主手术关键词或取消勾选"
    if not dx and not op:
        return "主诊断关键词与主手术关键词都为空：这个组永远入不了，至少填一类"
    return ""


def _contains(text: str, keyword: str) -> bool:
    """`text` 是已过 `text_key` 的病例文字；关键词归一后为空的（只有空白）不算命中。"""
    key = text_key(keyword)
    return bool(key) and key in text


def _match_group(group: DrgGroup, diagnosis: str, operation: str) -> tuple[int, int] | None:
    """单组匹配打分。返回 (总分, 最长命中词长度)，不匹配返回 None。

    评分：每命中一个主诊断关键词 +10，每命中一个主手术关键词 +20（外科组权重更高），
    再加上最长命中关键词的字数作为细粒度区分（长词更具体，优先级更高）。
    """
    # 关键词与病例两侧都按比对键认（P2-792）：原先按原样找子串，`pci术`、`ＰＣＩ术`、`急诊Pci` 都命中不了「PCI」，
    # 经皮冠脉介入落进内科组（权重 3.6 → 1.42）
    diagnosis_key, operation_key = text_key(diagnosis), text_key(operation)
    dx_hits = [kw for kw in _split(group.keywords) if _contains(diagnosis_key, kw)]
    op_hits = [kw for kw in _split(group.procedure_keywords) if operation_key and _contains(operation_key, kw)]
    # 外科操作组：未命中主手术一律不得入组，避免内科保守治疗病例误入手术组
    if group.require_procedure and not op_hits:
        return None
    if not dx_hits and not op_hits:
        return None
    longest = max((len(kw) for kw in dx_hits + op_hits), default=0)
    return len(dx_hits) * 10 + len(op_hits) * 20 + longest, longest


def assign_drg_group(db: Session, summary: CaseSummary) -> dict | None:
    """出院病例入组：多关键词 + 主手术标志匹配，未命中落入 QY 兜底组。

    回填 summary.drg_code/drg_weight，返回入组结果（含 fallback 标志）。**不提交**（P2-822）：由调用方与病案首页同一次
    提交——原先这里再提交一次，首页先落了库、入组这一步出错就留下一份永不入组的首页。
    """
    diagnosis = summary.discharge_diagnosis or ""
    operation = summary.operation or ""
    best: tuple[tuple[int, int], DrgGroup] | None = None
    # 按组编号遍历（P2-963）：只有严格更高分才换组，两组匹配分并列时留下先遍历到的那组——原先不排序，谁先谁后由库决定，
    # PG 上对一组「调权」（原地 UPDATE）这一行就挪到堆尾，同一句诊断改入另一组、权重差一倍（与 P2-304 同形）。
    # 并列时入哪组的规矩（主诊断在前 / 入歧义组 / 取权重高低）另行待裁定，这里先让结果确定
    for group in (
        db.query(DrgGroup)
        .filter(DrgGroup.active.is_(True), DrgGroup.is_fallback.is_(False))
        .order_by(DrgGroup.id)
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
    return {
        "drg_code": chosen.code,
        "drg_name": chosen.name,
        "mdc": chosen.mdc,
        "mdc_name": chosen.mdc_name,
        "weight": chosen.base_weight,
        "fallback": bool(chosen.is_fallback),
    }


def drg_label(drg_code: str, drg_weight: float) -> str:
    """病案首页上「DRG 分组」那一句：打印件与住院页只读首页（回读接口的 `drg_label`）同一个产地（P2-1536）。

    QY 兜底组不印权重（P2-1279）：它收的是哪组都没入上的病例，种子权重 0.50 只是占位，CMI 统计也剔除了；原先照样印
    「QY（权重 0.5）」，拿首页对账的人当成一个权重 0.5 的组。兜底与没入组同样印「未入组」，带上兜底组编码与复核提示。
    原先这句只写在打印件里，住院页的只读首页另写一份 `drg_code || "未入组"`：同一份首页，页面上印「DRG：QY」、打印出来是
    「未入组（QY，需病案首页复核）」，正式入组的页面上也没有权重。
    """
    if not drg_code:
        return "未入组"
    if drg_code == FALLBACK_CODE:
        return f"未入组（{drg_code}，需病案首页复核）"
    return f"{drg_code}（权重 {drg_weight}）"


# ---------- 分组目录 ----------


class DrgGroupCreate(BaseModel):
    code: str = Field(min_length=1, max_length=16, pattern=NON_BLANK)
    name: str = Field(min_length=1, max_length=128, pattern=NON_BLANK)
    base_weight: FiniteFloat = Field(gt=0)
    keywords: str = Field(default="", max_length=256)
    mdc: str = Field(default="", max_length=8)
    mdc_name: str = Field(default="", max_length=64)
    procedure_keywords: str = Field(default="", max_length=256)
    require_procedure: bool = False
    active: bool = True


class DrgGroupUpdate(BaseModel):
    # 改档与建档同口径（P1-98）：原先改名为空串照收
    name: str | None = Field(default=None, min_length=1, max_length=128, pattern=NON_BLANK)
    base_weight: FiniteFloat | None = Field(default=None, gt=0)
    keywords: str | None = Field(default=None, max_length=256)
    mdc: str | None = Field(default=None, max_length=8)
    mdc_name: str | None = Field(default=None, max_length=64)
    procedure_keywords: str | None = Field(default=None, max_length=256)
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
    # 认人用的两项（P2-1537）：只增键、排在末尾，原有键与次序不动。预警表的「患者」「机构」两列原先只印编号（「患者 17、机构 3」），
    # 预警要人去处置，管理层看多家机构时更认不出是谁；住院页的同一个形状 P2-1335 早已补上姓名
    patient_name: str
    org_name: str


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
    problem = _never_matches(body.keywords, body.procedure_keywords, body.require_procedure)   # 编码重复先报（契约钉着 409）
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    group = insert_or_conflict(db, DrgGroup(**body.model_dump()), "分组编码已存在")
    return _group_out(group)


@router.patch(
    "/groups/{group_id}", response_model=DrgGroupOut, dependencies=[Depends(require_admin)]
)
def update_group(group_id: int, body: DrgGroupUpdate, db: Session = Depends(get_db)):
    group = db.get(DrgGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="分组不存在")
    changes = body.model_dump(exclude_unset=True)
    # 动了匹配配置才判、与存量合并后判（P2-1019）：只改名、改权重、停用的照旧放行——存量里已经写坏的组也改得了名、停得了。
    # 传 null 的等于不改（下面照旧跳过 None），合并时取存量值
    matching = ("keywords", "procedure_keywords", "require_procedure")
    if not group.is_fallback and any(changes.get(f) is not None for f in matching):
        merged = {f: getattr(group, f) if changes.get(f) is None else changes[f] for f in matching}
        problem = _never_matches(**merged)
        if problem:
            raise HTTPException(status_code=422, detail=problem)
    for field, value in changes.items():
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

    **只算已出院的病例**（P2-453）：页面列名是「出院病例」，模块口径是「出院病例入组」，事中预警的历史基线也只取已出院
    的——可病案首页出院前就得填（不填不让出院），在院患者早有分组与费用，原先照样进例数、入组率、CMI 与均次费用：
    两例都还在院、一例没出院，机构行照样报 2 例、CMI 0.95、均次 5500。在院的费用还没结完，是事中预警的对象。
    """
    # 正式入组判定：非空且非兜底组
    formal = (CaseSummary.drg_code != "") & (CaseSummary.drg_code != FALLBACK_CODE)
    discharged = Admission.discharged_at.isnot(None)
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
        .filter(discharged)
        .group_by(Admission.org_id, Organization.name)
        .order_by(Admission.org_id, Organization.name)
        .all()
    )
    group_rows = (
        db.query(
            CaseSummary.drg_code,
            func.count(CaseSummary.id).label("cases"),
            func.coalesce(func.avg(CaseSummary.total_cost), 0.0).label("avg_cost"),
            # 病例入组时的权重快照（P2-181）：MDC 的 CMI 原先拿目录现价乘例数，机构 CMI 与病案首页打印的却是快照——
            # 管理员一调权，同一页上两个 CMI 就对不上；调权只作用于此后入组的病例，与首页上的权重同一个口径
            func.coalesce(func.sum(CaseSummary.drg_weight), 0.0).label("weight_sum"),
        )
        .join(Admission, Admission.id == CaseSummary.admission_id)
        .filter(CaseSummary.drg_code != "", discharged)
        .group_by(CaseSummary.drg_code)
        .order_by(CaseSummary.drg_code)
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
        entry["weight_sum"] += float(r.weight_sum or 0.0)
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
    diagnosis: str = Field(min_length=1, max_length=256, pattern=NON_BLANK)
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
    for group in (   # 与出院入组同一个次序（P2-963）：并列的候选按组编号排，截前 5 与出院入组对得上
        db.query(DrgGroup)
        .filter(DrgGroup.active.is_(True), DrgGroup.is_fallback.is_(False))
        .order_by(DrgGroup.id)
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
    # （平台没有存 los_days，存一份就会与两个时刻不一致）。
    # 「已入组」指正式分组，兜底组 QY 除外（P2-166）：它收的是哪组都没匹配上的病例，「同组均值」在它身上
    # 没有意义——与 /stats 从 CMI 分母剔除兜底组同一口径。原先 QY 也建基线、在院的 QY 病例拿它预警
    history = (
        db.query(CaseSummary, Admission)
        .join(Admission, CaseSummary.admission_id == Admission.id)
        .filter(CaseSummary.drg_code != "", CaseSummary.drg_code != FALLBACK_CODE,
                Admission.discharged_at.isnot(None))
        .all()
    )
    # 住院日当日入当日出计 1 天（P2-533）：与病案打印、居民端「我的住院」、运行效率同一口径（printing.py 写明含 DRG）。
    # 原先基线与在院天数都是裸日期差：一组历史全是当日入出院的，均值 0、永不预警；掺几例当日的，均值被拉低一截，
    # 住 2 天的病人就被报成超均值 2.5 倍
    baseline: dict[str, list[int]] = {}
    for summary, adm in history:
        days = (adm.discharged_at.date() - adm.admitted_at.date()).days
        if days >= 0:
            baseline.setdefault(summary.drg_code, []).append(max(days, 1))

    query = (
        db.query(Admission, CaseSummary)
        .outerjoin(CaseSummary, CaseSummary.admission_id == Admission.id)
        .filter(Admission.status == "admitted")
    )
    query = scope_org_list(db, user, query, Admission, org_id)
    # **预警不能只看前 500 条在院病例**：下面这圈把 rows 分成 alerts /
    # insufficient_baseline / ungrouped 三类，`.limit(500)` 截断的是**预警的输入**，
    # 超过 500 张在院床位之后，第 501 个病例即使住院日超均值三倍也不会被报出来
    # ——而「没有预警」与「真的没问题」长得一模一样。`ungrouped_in_stay` 同样少算。
    # 行数由**在院床位数**封顶（`status == "admitted"`），不随历史增长；
    # 上面的 history 基线查询本来也没有上限，两边口径就此一致。
    rows = query.order_by(Admission.id.desc()).all()

    alerts, insufficient, ungrouped = [], [], 0
    for adm, summary in rows:
        drg_code = summary.drg_code if summary else ""
        if not drg_code or drg_code == FALLBACK_CODE:
            # 尚未填病案首页、或落入兜底组（未正式入组）的在院病例：没有同组均值可比，计数报出
            ungrouped += 1
            continue
        # 入院日换成本地日期再减（第十五批 S2-2）：`end` 是本地业务日，落库的入院时刻是 naive UTC——原先直接 `.date()`，
        # 一个减法两把尺子，东八区 0–8 点入院的多算 1 天、提前报警。基线两头是同一把尺子，日界按哪个时区随 P1-105 定
        stayed = max((end - clock.to_local(adm.admitted_at).date()).days, 1)
        samples = baseline.get(drg_code, [])
        if len(samples) < MIN_BASELINE_CASES:
            insufficient.append({
                "admission_id": adm.id, "drg_code": drg_code,
                "history_cases": len(samples), "stayed_days": stayed,
            })
            continue
        avg = sum(samples) / len(samples)
        # 门槛按整数与十进制精确比（P2-989）：原先拿浮点「均值 × 倍数」比，住院日 5、6、6、6、6、6、倍数 1.2 时门槛恰为 7 天，
        # 浮点算出 6.999999999999999，在院恰好 7 天的也报「已明显超出」、页面同时显示超出倍数 1.2×（与 P2-156 同形）。
        # 在院天数与样本住院日都是整数：stayed > Σ样本 × 倍数 ÷ n  ⇔  stayed × n > Σ样本 × 倍数
        if avg > 0 and stayed * len(samples) > sum(samples) * Decimal(str(los_multiplier)):
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
    # 患者姓名、机构名按这一批预警的 id 各取一次（P2-1537，与住院清单 `inpatient._admissions_out`、随访清单
    # `followups._name_maps` 同一写法），不逐行查库；取不到的给空串，页面回显编号
    if alerts:
        patients = row_dict(
            db.query(Patient.id, Patient.name).filter(Patient.id.in_({a["patient_id"] for a in alerts})).all()
        )
        orgs = row_dict(
            db.query(Organization.id, Organization.name).filter(Organization.id.in_({a["org_id"] for a in alerts})).all()
        )
        for alert in alerts:
            alert["patient_name"] = patients.get(alert["patient_id"], "")
            alert["org_name"] = orgs.get(alert["org_id"], "")
    return {
        "today": end.isoformat(),
        "los_multiplier": los_multiplier,
        "alerts": sorted(alerts, key=lambda a: -a["over_ratio"]),
        # 样本不足与尚未入组的都单列，不混进"无预警"
        "insufficient_baseline": insufficient,
        "ungrouped_in_stay": ungrouped,
        "caliber": f"基线取本院已出院且已入组病例的住院日（由入出院时刻现算，当日入出院计 1 天，在院天数同）；"
                   f"同组历史少于 {MIN_BASELINE_CASES} 例不预警，单列在 "
                   f"insufficient_baseline；尚未填病案首页的在院病例计入 ungrouped_in_stay；"
                   f"兜底组 {FALLBACK_CODE} 不算入组，既不建基线也不预警，在院的同样计入 ungrouped_in_stay",
    }
