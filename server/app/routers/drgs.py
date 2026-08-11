"""DRGs 简化版（浙江省指南 M12，#53）：分组目录、出院病例自动入组、CMI 分析。

- DrgGroup：分组目录（编码/名称/基准权重/主诊断关键词），启动种子化 10 个常见组，
  admin 可增补与调权；
- 入组：病案首页（CaseSummary）结案时按出院主诊断关键词自动匹配入组
  （inpatient.create_case_summary 调用 assign_drg_group）；
- GET /api/drgs/stats：各机构 CMI（Σ权重/入组例数）、组均费用对比。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_admin
from ..models import Admission, CaseSummary, DrgGroup, Organization

router = APIRouter(prefix="/api/drgs", tags=["DRGs分析"], dependencies=[Depends(get_current_user)])

# 启动种子化的常见分组（简化版：编码/名称/基准权重/主诊断关键词）
SEED_DRG_GROUPS = [
    {"code": "ES31", "name": "呼吸系统感染（肺炎）", "base_weight": 0.95, "keywords": "肺炎,支气管炎"},
    {"code": "BR23", "name": "脑血管疾病", "base_weight": 1.35, "keywords": "脑梗,卒中,脑出血"},
    {"code": "FM19", "name": "缺血性心脏病", "base_weight": 1.42, "keywords": "冠心病,心肌梗死,心绞痛"},
    {"code": "HZ23", "name": "胆囊疾患", "base_weight": 1.10, "keywords": "胆囊炎,胆囊结石,胆石"},
    {"code": "GD29", "name": "阑尾疾患及手术", "base_weight": 0.85, "keywords": "阑尾"},
    {"code": "KS11", "name": "糖尿病及并发症", "base_weight": 0.78, "keywords": "糖尿病"},
    {"code": "FV29", "name": "高血压", "base_weight": 0.62, "keywords": "高血压"},
    {"code": "LU14", "name": "泌尿系结石与感染", "base_weight": 0.72, "keywords": "尿路,肾结石,输尿管结石,泌尿"},
    {"code": "OB25", "name": "阴道分娩及剖宫产", "base_weight": 0.68, "keywords": "分娩,剖宫产"},
    {"code": "GB29", "name": "消化道溃疡与胃肠炎", "base_weight": 0.70, "keywords": "胃溃疡,十二指肠,胃肠炎"},
]


def assign_drg_group(db: Session, summary: CaseSummary) -> dict | None:
    """出院病例入组：按出院主诊断关键词匹配（命中最长关键词的组优先）。

    命中则回填 summary.drg_code/drg_weight 并提交；未命中返回 None（不入组）。
    """
    diagnosis = summary.discharge_diagnosis or ""
    best: tuple[int, DrgGroup] | None = None
    for group in db.query(DrgGroup).filter(DrgGroup.active.is_(True)).all():
        for kw in [k.strip() for k in group.keywords.split(",") if k.strip()]:
            if kw in diagnosis and (best is None or len(kw) > best[0]):
                best = (len(kw), group)
    if best is None:
        return None
    group = best[1]
    summary.drg_code = group.code
    summary.drg_weight = group.base_weight
    db.commit()
    return {"drg_code": group.code, "drg_name": group.name, "weight": group.base_weight}


# ---------- 分组目录 ----------


class DrgGroupCreate(BaseModel):
    code: str = Field(min_length=1, max_length=16)
    name: str = Field(min_length=1, max_length=128)
    base_weight: float = Field(gt=0)
    keywords: str = ""
    active: bool = True


class DrgGroupUpdate(BaseModel):
    name: str | None = None
    base_weight: float | None = Field(default=None, gt=0)
    keywords: str | None = None
    active: bool | None = None


def _group_out(g: DrgGroup) -> dict:
    return {
        "id": g.id,
        "code": g.code,
        "name": g.name,
        "base_weight": g.base_weight,
        "keywords": g.keywords,
        "active": g.active,
    }


@router.get("/groups")
def list_groups(db: Session = Depends(get_db)):
    return [_group_out(g) for g in db.query(DrgGroup).order_by(DrgGroup.code).limit(500).all()]


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
    """DRGs 分析：各机构 CMI（Σ权重/入组例数）与入组率、各组例数/均费对比。"""
    org_rows = (
        db.query(
            Admission.org_id,
            Organization.name,
            func.count(CaseSummary.id).label("cases"),
            func.sum(case((CaseSummary.drg_code != "", 1), else_=0)).label("grouped"),
            func.coalesce(func.sum(CaseSummary.drg_weight), 0.0).label("weight_sum"),
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
    group_names = {g.code: g.name for g in db.query(DrgGroup).all()}
    return {
        "orgs": [
            {
                "org_id": r.org_id,
                "org_name": r.name,
                "cases": r.cases,
                "grouped": int(r.grouped or 0),
                "grouped_pct": round((r.grouped or 0) * 100.0 / r.cases, 2) if r.cases else 0.0,
                # CMI = Σ权重 / 入组例数
                "cmi": round(r.weight_sum / r.grouped, 3) if r.grouped else 0.0,
                "avg_cost": round(r.avg_cost, 2),
            }
            for r in org_rows
        ],
        "groups": [
            {
                "drg_code": r.drg_code,
                "drg_name": group_names.get(r.drg_code, r.drg_code),
                "cases": r.cases,
                "avg_cost": round(r.avg_cost, 2),
            }
            for r in group_rows
        ],
    }
