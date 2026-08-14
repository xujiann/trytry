"""成本核算（T3.2）：科室成本、诊次成本、床日成本。

此前平台只有消毒供应的单件成本，医院最基本的三级成本口径是空的。

计算链条：

1. **直接成本**按期间×科室×成本项归集（人员经费/药品/卫生材料/折旧/其他）；
2. **分摊成本**按规则把行政后勤与医技科室的成本分摊到临床科室；
3. **单位成本** = 期间总成本 ÷ 业务量：诊次成本用门诊人次做分母，
   床日成本用**实际占用床日**做分母——不是床位数×天数，那是可用床日，
   两者混用会把成本算低。
"""
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..concurrency import upsert_unique
from ..visibility import assert_org_visible, scope_org_list
from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import (
    Admission,
    CostAllocationRule,
    Department,
    DepartmentCost,
    Encounter,
    Organization,
    User,
)

router = APIRouter(prefix="/api/cost", tags=["成本核算"], dependencies=[Depends(get_current_user)])

COST_TYPES = ("labor", "drug", "consumable", "depreciation", "overhead")
COST_TYPE_NAMES = {
    "labor": "人员经费",
    "drug": "药品",
    "consumable": "卫生材料",
    "depreciation": "折旧",
    "overhead": "其他运行",
}


def _period_bounds(period: str) -> tuple[date, date]:
    """期间 YYYY-MM → [首日, 次月首日)。"""
    try:
        start = datetime.strptime(period + "-01", "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="period 须为 YYYY-MM 格式") from None
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, end


# ---------------------------------------------------------------- 直接成本归集


class CostIn(BaseModel):
    dept_id: int
    period: str = Field(min_length=7, max_length=7)
    cost_type: str = Field(pattern="^(labor|drug|consumable|depreciation|overhead)$")
    amount: float = Field(ge=0)


@router.post("/departments", status_code=201, dependencies=[Depends(require_roles("director"))])
def upsert_department_cost(body: CostIn, db: Session = Depends(get_db)):
    """归集科室直接成本。同科室同期间同成本项重复提交按覆盖处理——
    月末成本数据往往要反复调整，报错逼人先删再建没有意义。"""
    dept = db.get(Department, body.dept_id)
    if dept is None:
        raise HTTPException(status_code=404, detail="科室不存在")
    record, updated = upsert_unique(
        db,
        DepartmentCost,
        keys={"dept_id": body.dept_id, "period": body.period, "cost_type": body.cost_type},
        values={"amount": body.amount, "org_id": dept.org_id},
    )
    return {"id": record.id, "updated": updated, "amount": record.amount}


class AllocationIn(BaseModel):
    from_dept_id: int
    to_dept_id: int
    ratio_pct: float = Field(gt=0, le=100)


@router.post("/allocation-rules", status_code=201, dependencies=[Depends(require_roles("director"))])
def create_allocation_rule(body: AllocationIn, db: Session = Depends(get_db)):
    """建立分摊规则（来源科室 → 目标科室 × 比例）。"""
    source = db.get(Department, body.from_dept_id)
    target = db.get(Department, body.to_dept_id)
    if source is None or target is None:
        raise HTTPException(status_code=404, detail="科室不存在")
    if source.id == target.id:
        raise HTTPException(status_code=422, detail="不可向本科室分摊")
    if source.org_id != target.org_id:
        raise HTTPException(status_code=422, detail="不可跨机构分摊成本")
    rule = CostAllocationRule(org_id=source.org_id, **body.model_dump())
    db.add(rule)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该分摊规则已存在") from None
    db.refresh(rule)
    return {"id": rule.id, "from_dept_id": rule.from_dept_id, "to_dept_id": rule.to_dept_id,
            "ratio_pct": rule.ratio_pct}


@router.get("/allocation-rules")
def list_allocation_rules(org_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user),):
    query = db.query(CostAllocationRule)
    query = scope_org_list(db, user, query, CostAllocationRule, org_id)
    return [
        {"id": r.id, "org_id": r.org_id, "from_dept_id": r.from_dept_id,
         "to_dept_id": r.to_dept_id, "ratio_pct": r.ratio_pct}
        for r in query.order_by(CostAllocationRule.id).all()
    ]


# ---------------------------------------------------------------- 科室成本汇总


@router.get("/departments")
def department_cost_summary(
    period: str,
    org_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """科室成本 = 直接成本 + 分摊转入 − 分摊转出。

    分摊只做**一轮**（行政/医技 → 临床），不做多级迭代分摊：县域机构科室层级浅，
    一轮足够；多级迭代要处理互相分摊的收敛问题，收益不抵复杂度。
    未配满 100% 的来源科室，未分摊部分留在原科室，并在返回里标出来。
    """
    assert_org_visible(db, user, org_id)
    _period_bounds(period)
    query = db.query(DepartmentCost).filter(DepartmentCost.period == period)
    if org_id is not None:
        query = query.filter(DepartmentCost.org_id == org_id)
    costs = query.all()

    depts = {d.id: d for d in db.query(Department).all()}
    org_names = {o.id: o.name for o in db.query(Organization).all()}

    direct: dict[int, dict] = {}
    for row in costs:
        entry = direct.setdefault(row.dept_id, {t: 0.0 for t in COST_TYPES})
        entry[row.cost_type] += row.amount

    rules = db.query(CostAllocationRule).all()
    if org_id is not None:
        rules = [r for r in rules if r.org_id == org_id]
    by_source: dict[int, list[CostAllocationRule]] = {}
    for rule in rules:
        by_source.setdefault(rule.from_dept_id, []).append(rule)

    allocated_in: dict[int, float] = {}
    allocated_out: dict[int, float] = {}
    unallocated: dict[int, float] = {}
    for dept_id, buckets in direct.items():
        total = sum(buckets.values())
        source_rules = by_source.get(dept_id, [])
        if not source_rules or total <= 0:
            continue
        ratio_sum = sum(r.ratio_pct for r in source_rules)
        for rule in source_rules:
            amount = round(total * rule.ratio_pct / 100, 2)
            allocated_in[rule.to_dept_id] = allocated_in.get(rule.to_dept_id, 0) + amount
            allocated_out[dept_id] = allocated_out.get(dept_id, 0) + amount
        if ratio_sum < 100:
            unallocated[dept_id] = round(total * (100 - ratio_sum) / 100, 2)

    result = []
    for dept_id in set(direct) | set(allocated_in):
        dept = depts.get(dept_id)
        buckets = direct.get(dept_id, {t: 0.0 for t in COST_TYPES})
        direct_total = round(sum(buckets.values()), 2)
        result.append(
            {
                "dept_id": dept_id,
                "dept_name": dept.name if dept else "",
                "dept_category": dept.category if dept else "",
                "org_name": org_names.get(dept.org_id, "") if dept else "",
                "direct_cost": direct_total,
                "by_type": {t: round(buckets[t], 2) for t in COST_TYPES},
                "allocated_in": round(allocated_in.get(dept_id, 0), 2),
                "allocated_out": round(allocated_out.get(dept_id, 0), 2),
                "total_cost": round(
                    direct_total + allocated_in.get(dept_id, 0) - allocated_out.get(dept_id, 0), 2
                ),
                "unallocated_ratio_amount": unallocated.get(dept_id, 0),
            }
        )
    return sorted(result, key=lambda x: -x["total_cost"])


# ---------------------------------------------------------------- 单位成本


def _occupied_bed_days(db: Session, org_id: int, start: date, end: date) -> int:
    """期间内实际占用床日：逐住院记录取 [入院, 出院) 与期间的交集天数。

    在院未出院的按期间末计。当日入当日出计 1 床日（不是 0）——住了一天就是一天。
    """
    admissions = (
        db.query(Admission)
        .filter(Admission.org_id == org_id, Admission.admitted_at < datetime.combine(end, datetime.min.time()))
        .all()
    )
    total = 0
    for adm in admissions:
        admitted = adm.admitted_at.date()
        discharged = adm.discharged_at.date() if adm.discharged_at else end
        overlap_start = max(admitted, start)
        overlap_end = min(discharged, end)
        if overlap_end < overlap_start:
            continue
        total += max((overlap_end - overlap_start).days, 1)
    return total


@router.get("/unit-cost")
def unit_cost(
    period: str,
    org_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """诊次成本与床日成本。

    分母口径写死在这里并对外说明：诊次成本 = 门诊总成本 ÷ 门诊人次；
    床日成本 = 住院总成本 ÷ **实际占用床日**（非床位数×天数）。
    成本的门急诊/住院拆分按科室类别粗分：临床科室成本全额进住院与门诊合计，
    再按人次与床日的相对权重拆——县域机构没有分科成本中心时这是可落地的近似，
    精确拆分需要收费明细带科室，属后续 T4 依赖项。
    """
    assert_org_visible(db, user, org_id)
    start, end = _period_bounds(period)
    if db.get(Organization, org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")

    total_cost = round(
        sum(
            row.amount
            for row in db.query(DepartmentCost)
            .filter(DepartmentCost.org_id == org_id, DepartmentCost.period == period)
            .all()
        ),
        2,
    )
    outpatient_visits = (
        db.query(Encounter)
        .filter(
            Encounter.org_id == org_id,
            Encounter.encounter_type != "inpatient",
            Encounter.created_at >= datetime.combine(start, datetime.min.time()),
            Encounter.created_at < datetime.combine(end, datetime.min.time()),
        )
        .count()
    )
    bed_days = _occupied_bed_days(db, org_id, start, end)

    # 权重拆分：门诊按人次、住院按床日，各自占总业务量的比例分配总成本
    units = outpatient_visits + bed_days
    outpatient_cost = round(total_cost * outpatient_visits / units, 2) if units else 0.0
    inpatient_cost = round(total_cost - outpatient_cost, 2) if units else 0.0
    return {
        "period": period,
        "org_id": org_id,
        "total_cost": total_cost,
        "outpatient_visits": outpatient_visits,
        "occupied_bed_days": bed_days,
        "outpatient_cost": outpatient_cost,
        "inpatient_cost": inpatient_cost,
        "cost_per_visit": round(outpatient_cost / outpatient_visits, 2) if outpatient_visits else 0.0,
        "cost_per_bed_day": round(inpatient_cost / bed_days, 2) if bed_days else 0.0,
    }
