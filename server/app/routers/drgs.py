"""DRGs 简化版（浙江省指南 M12，#53；块3 扩充）：分组目录、出院病例自动入组、CMI 分析。

- DrgGroup：分组目录（编码/名称/MDC/基准权重/主诊断关键词/主手术关键词），
  启动种子化 62 个县域常见分组 + QY 兜底组，admin 可增补与调权；
- 入组（块3 由单关键词升级为多关键词 + 主手术标志）：
  主诊断命中越多、命中词越长得分越高；require_procedure 的外科组必须命中主手术，
  全部未命中则落入 QY 兜底组（病案首页需复核）；
- GET /api/drgs/stats：各机构 CMI（Σ权重/正式入组例数，兜底组不计入）、
  各组例数/均费、按 MDC 汇总。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from ..data.drg_groups_seed import FALLBACK_DRG_GROUP, SEED_DRG_GROUPS
from ..database import get_db
from ..deps import get_current_user, require_admin
from ..models import Admission, CaseSummary, DrgGroup, Organization

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

    if best is not None:
        group = best[1]
    else:
        group = db.query(DrgGroup).filter(DrgGroup.code == FALLBACK_CODE).first()
        if group is None:  # pragma: no cover - 兜底组缺失（种子未执行）
            return None
    summary.drg_code = group.code
    summary.drg_weight = group.base_weight
    db.commit()
    return {
        "drg_code": group.code,
        "drg_name": group.name,
        "mdc": group.mdc,
        "mdc_name": group.mdc_name,
        "weight": group.base_weight,
        "fallback": bool(group.is_fallback),
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


@router.get("/groups")
def list_groups(mdc: str | None = None, db: Session = Depends(get_db)):
    query = db.query(DrgGroup)
    if mdc:
        query = query.filter(DrgGroup.mdc == mdc)
    return [_group_out(g) for g in query.order_by(DrgGroup.code).limit(500).all()]


@router.post("/groups", status_code=201, dependencies=[Depends(require_admin)])
def create_group(body: DrgGroupCreate, db: Session = Depends(get_db)):
    if db.query(DrgGroup).filter(DrgGroup.code == body.code).first():
        raise HTTPException(status_code=409, detail="分组编码已存在")
    group = DrgGroup(**body.model_dump())
    db.add(group)
    db.commit()
    return _group_out(group)


@router.patch("/groups/{group_id}", dependencies=[Depends(require_admin)])
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


@router.get("/stats")
def drg_stats(db: Session = Depends(get_db)):
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
