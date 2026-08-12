"""决策指标扩展（阶段四 T4.1–T4.3）。

- **T4.1 县外就诊登记**：补上"县域就诊率""外转率"缺的那半边数据源。
  平台原本只看得见县内发生的诊疗，这两项恰恰是紧密型医共体监测的头号指标。
- **T4.2 运行效率**：平均住院日、床位周转次数、床位使用率、医师担负。
- **T4.3 自定义绩效公式**：管理员用受限表达式定义指标，走 AST 白名单求值
  （见 app/formula.py），并汇总为期末综合绩效报告。
"""
from datetime import date, datetime, timedelta

from sqlalchemy import func

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, paginate, require_admin, require_roles, resolve_business_date
from ..formula import FormulaError, evaluate, validate
from ..models import (
    Admission,
    Bed,
    ChronicPatient,
    Employee,
    Encounter,
    ExamRequest,
    OutboundVisit,
    Organization,
    Patient,
    PerformanceFormula,
    Prescription,
    Referral,
    User,
    Ward,
)

router = APIRouter(prefix="/api/analytics", tags=["决策指标扩展"], dependencies=[Depends(get_current_user)])

# 医师职务关键词：Employee.position 含这些词即计入医师数（担负指标的分母）
DOCTOR_POSITION_KEYWORDS = ("医师", "医生")


def _period_bounds(period: str) -> tuple[date, date]:
    try:
        start = datetime.strptime(period + "-01", "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="period 须为 YYYY-MM 格式") from None
    return start, (start.replace(day=28) + timedelta(days=4)).replace(day=1)


# ============================================================================
# T4.1 县外就诊登记
# ============================================================================


class OutboundIn(BaseModel):
    patient_id: int
    visit_date: str = Field(min_length=10, max_length=10)
    external_org_name: str = Field(min_length=1, max_length=128)
    external_org_level: str = Field(default="city", pattern="^(city|province|other)$")
    visit_type: str = Field(default="outpatient", pattern="^(outpatient|inpatient)$")
    diagnosis_name: str = ""
    total_amount: float = Field(default=0, ge=0)
    insurance_pay: float = Field(default=0, ge=0)
    referral_id: int | None = None
    source: str = Field(default="manual", pattern="^(manual|insurance_import)$")


@router.post(
    "/outbound-visits", status_code=201, dependencies=[Depends(require_roles("operator", "director"))]
)
def create_outbound_visit(
    body: OutboundIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """登记一次县外就诊。"""
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if body.insurance_pay > body.total_amount:
        raise HTTPException(status_code=422, detail="医保支付不得超过总费用")
    if body.referral_id is not None:
        referral = db.get(Referral, body.referral_id)
        if referral is None:
            raise HTTPException(status_code=404, detail="转诊单不存在")
        if referral.patient_id != body.patient_id:
            # 挂到别人的转诊单上会把"有序转诊率"算高，必须拦
            raise HTTPException(status_code=422, detail="该转诊单不属于此患者")
    visit = OutboundVisit(**body.model_dump(), created_by=user.id)
    db.add(visit)
    db.commit()
    db.refresh(visit)
    return _outbound_out(db, visit)


def _outbound_out(db: Session, v: OutboundVisit) -> dict:
    patient = db.get(Patient, v.patient_id)
    return {
        "id": v.id,
        "patient_id": v.patient_id,
        "patient_name": patient.name if patient else "",
        "visit_date": v.visit_date,
        "external_org_name": v.external_org_name,
        "external_org_level": v.external_org_level,
        "visit_type": v.visit_type,
        "diagnosis_name": v.diagnosis_name,
        "total_amount": v.total_amount,
        "insurance_pay": v.insurance_pay,
        "referral_id": v.referral_id,
        "referred": v.referral_id is not None,
        "source": v.source,
    }


@router.get("/outbound-visits")
def list_outbound_visits(
    response: Response,
    visit_type: str | None = None,
    external_org_level: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(OutboundVisit)
    if visit_type:
        query = query.filter(OutboundVisit.visit_type == visit_type)
    if external_org_level:
        query = query.filter(OutboundVisit.external_org_level == external_org_level)
    return [
        _outbound_out(db, v)
        for v in paginate(query.order_by(OutboundVisit.id.desc()), response, offset, limit)
    ]


@router.get("/patient-flow")
def patient_flow(start: str | None = None, end: str | None = None, db: Session = Depends(get_db)):
    """就医流向：县域就诊率、外转率、有序转诊率。

    口径：
    - 县域就诊率 = 县内就诊人次 ÷（县内 + 县外）
    - 外转率 = 100 − 县域就诊率
    - 有序转诊率 = 关联转诊单的县外就诊 ÷ 县外就诊总数（衡量"自行外出"的比例）
    """
    inside_q = db.query(Encounter)
    outside_q = db.query(OutboundVisit)
    if start:
        inside_q = inside_q.filter(Encounter.created_at >= datetime.combine(
            resolve_business_date(start), datetime.min.time()))
        outside_q = outside_q.filter(OutboundVisit.visit_date >= start)
    if end:
        inside_q = inside_q.filter(Encounter.created_at < datetime.combine(
            resolve_business_date(end), datetime.min.time()))
        outside_q = outside_q.filter(OutboundVisit.visit_date < end)

    inside = inside_q.count()
    outside_rows = outside_q.all()
    outside = len(outside_rows)
    total = inside + outside
    referred = sum(1 for v in outside_rows if v.referral_id is not None)

    by_level: dict[str, int] = {}
    for v in outside_rows:
        by_level[v.external_org_level] = by_level.get(v.external_org_level, 0) + 1

    def pct(part: int, whole: int) -> float:
        return round(part * 100 / whole, 2) if whole else 0.0

    return {
        "inside_visits": inside,
        "outside_visits": outside,
        "total_visits": total,
        "county_visit_rate_pct": pct(inside, total),
        "outbound_rate_pct": round(100 - pct(inside, total), 2) if total else 0.0,
        "referred_outbound": referred,
        "ordered_referral_rate_pct": pct(referred, outside),
        "outside_by_level": by_level,
        "outside_amount": round(sum(v.total_amount for v in outside_rows), 2),
    }


# ============================================================================
# T4.2 运行效率
# ============================================================================


@router.get("/efficiency")
def efficiency(period: str, db: Session = Depends(get_db)):
    """运行效率：按机构给出平均住院日、床位周转次数、床位使用率、医师担负。

    口径说明（写死并对外公布，避免各院各算）：
    - 平均住院日 = 出院者占用总床日 ÷ 出院人次
    - 床位周转次数 = 出院人次 ÷ 实际开放床位数
    - 床位使用率 = 实际占用床日 ÷（实际开放床位数 × 期间天数）
    - 医师日均担负诊疗人次 = 期间诊疗人次 ÷ 医师数 ÷ 期间天数
    """
    start, end = _period_bounds(period)
    days = (end - start).days
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.min.time())

    org_names = {o.id: o.name for o in db.query(Organization).all()}
    bed_counts = dict(
        db.query(Ward.org_id, func.count(Bed.id))
        .join(Bed, Bed.ward_id == Ward.id)
        .group_by(Ward.org_id)
        .all()
    )
    doctor_counts: dict[int, int] = {}
    for emp in db.query(Employee).filter(Employee.status == "active").all():
        if any(k in (emp.position or "") for k in DOCTOR_POSITION_KEYWORDS):
            doctor_counts[emp.org_id] = doctor_counts.get(emp.org_id, 0) + 1

    admissions = db.query(Admission).filter(Admission.admitted_at < end_dt).all()
    discharged_days: dict[int, int] = {}
    discharged_count: dict[int, int] = {}
    occupied_days: dict[int, int] = {}
    for adm in admissions:
        admitted = adm.admitted_at.date()
        discharged = adm.discharged_at.date() if adm.discharged_at else end
        overlap_start = max(admitted, start)
        overlap_end = min(discharged, end)
        if overlap_end >= overlap_start:
            occupied_days[adm.org_id] = occupied_days.get(adm.org_id, 0) + max(
                (overlap_end - overlap_start).days, 1
            )
        # 出院者：出院日落在期间内才计入本期出院人次与平均住院日
        if adm.discharged_at is not None and start <= adm.discharged_at.date() < end:
            discharged_count[adm.org_id] = discharged_count.get(adm.org_id, 0) + 1
            discharged_days[adm.org_id] = discharged_days.get(adm.org_id, 0) + max(
                (adm.discharged_at.date() - admitted).days, 1
            )

    visits: dict[int, int] = {}
    for org_id, count in (
        db.query(Encounter.org_id, func.count(Encounter.id))
        .filter(Encounter.created_at >= start_dt, Encounter.created_at < end_dt)
        .group_by(Encounter.org_id)
        .all()
    ):
        visits[org_id] = count

    result = []
    for org_id in set(bed_counts) | set(visits) | set(occupied_days) | set(discharged_count):
        beds = bed_counts.get(org_id, 0)
        discharges = discharged_count.get(org_id, 0)
        doctors = doctor_counts.get(org_id, 0)
        result.append(
            {
                "org_id": org_id,
                "org_name": org_names.get(org_id, ""),
                "beds": beds,
                "discharges": discharges,
                "occupied_bed_days": occupied_days.get(org_id, 0),
                "avg_length_of_stay": round(discharged_days.get(org_id, 0) / discharges, 2)
                if discharges
                else 0.0,
                "bed_turnover": round(discharges / beds, 2) if beds else 0.0,
                "bed_occupancy_rate_pct": round(
                    occupied_days.get(org_id, 0) * 100 / (beds * days), 2
                )
                if beds and days
                else 0.0,
                "visits": visits.get(org_id, 0),
                "doctors": doctors,
                "visits_per_doctor_per_day": round(visits.get(org_id, 0) / doctors / days, 2)
                if doctors and days
                else 0.0,
            }
        )
    return sorted(result, key=lambda x: -x["visits"])


# ============================================================================
# T4.3 自定义绩效公式
# ============================================================================


def build_variable_index(db: Session, period: str) -> dict[int, dict[str, float]]:
    """一次性算出**全部机构**的公式变量（T6.3）。

    此前是逐机构 7 个 count + 1 次 efficiency()，25 家机构就是 175 条计数查询
    加 25 次全表扫描。这里改成按 org_id 分组聚合：无论多少家机构，
    固定 7 条分组查询 + 1 次 efficiency()。
    """
    start, end = _period_bounds(period)
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.min.time())

    def grouped(model, org_column, *conditions) -> dict[int, int]:
        query = db.query(org_column, func.count(model.id)).filter(*conditions)
        return dict(query.group_by(org_column).all())

    in_period = lambda model: (  # noqa: E731 - 三处复用的时间窗谓词
        model.created_at >= start_dt,
        model.created_at < end_dt,
    )
    encounters = grouped(Encounter, Encounter.org_id, *in_period(Encounter))
    referrals_up = grouped(
        Referral, Referral.from_org_id, Referral.direction == "up", *in_period(Referral)
    )
    referrals_down = grouped(
        Referral, Referral.to_org_id, Referral.direction == "down", *in_period(Referral)
    )
    exams = grouped(ExamRequest, ExamRequest.from_org_id, *in_period(ExamRequest))
    prescriptions = grouped(Prescription, Prescription.org_id, *in_period(Prescription))
    rejected = grouped(
        Prescription, Prescription.org_id, Prescription.status == "rejected", *in_period(Prescription)
    )
    chronic = dict(
        db.query(ChronicPatient.managed_by_org_id, func.count(ChronicPatient.id))
        .group_by(ChronicPatient.managed_by_org_id)
        .all()
    )
    efficiency_index = {row["org_id"]: row for row in efficiency(period, db)}

    org_ids = (
        set(encounters) | set(referrals_up) | set(referrals_down) | set(exams)
        | set(prescriptions) | set(chronic) | set(efficiency_index)
        | {o for (o,) in db.query(Organization.id).all()}
    )
    index = {}
    for org_id in org_ids:
        eff = efficiency_index.get(org_id, {})
        index[org_id] = {
            "encounters": float(encounters.get(org_id, 0)),
            "referrals_up": float(referrals_up.get(org_id, 0)),
            "referrals_down": float(referrals_down.get(org_id, 0)),
            "exams": float(exams.get(org_id, 0)),
            "prescriptions": float(prescriptions.get(org_id, 0)),
            "rejected_prescriptions": float(rejected.get(org_id, 0)),
            "chronic_patients": float(chronic.get(org_id, 0)),
            "beds": float(eff.get("beds", 0)),
            "discharges": float(eff.get("discharges", 0)),
            "occupied_bed_days": float(eff.get("occupied_bed_days", 0)),
            "avg_length_of_stay": float(eff.get("avg_length_of_stay", 0)),
            "bed_turnover": float(eff.get("bed_turnover", 0)),
            "bed_occupancy_rate_pct": float(eff.get("bed_occupancy_rate_pct", 0)),
            "doctors": float(eff.get("doctors", 0)),
        }
    return index


def formula_variables(db: Session, org_id: int, period: str) -> dict[str, float]:
    """单机构变量表（供单点查询使用）。

    批量场景一律走 `build_variable_index()`——逐机构调用本函数就是 N+1。
    新增变量时同步更新 VARIABLE_DESCRIPTIONS，否则管理员写公式时只能靠猜。
    """
    return build_variable_index(db, period).get(org_id, {name: 0.0 for name in VARIABLE_DESCRIPTIONS})


VARIABLE_DESCRIPTIONS = {
    "encounters": "期间诊疗人次",
    "referrals_up": "期间上转人次",
    "referrals_down": "期间下转接收人次",
    "exams": "期间共享中心检查申请数",
    "prescriptions": "期间处方总数",
    "rejected_prescriptions": "期间审核退回处方数",
    "chronic_patients": "在管慢病人数",
    "beds": "实际开放床位数",
    "discharges": "期间出院人次",
    "occupied_bed_days": "期间实际占用床日",
    "avg_length_of_stay": "平均住院日",
    "bed_turnover": "床位周转次数",
    "bed_occupancy_rate_pct": "床位使用率(%)",
    "doctors": "在岗医师数",
}


@router.get("/formula-variables")
def list_formula_variables():
    """公式可用变量清单：管理员写表达式时的字段字典。"""
    return [{"name": k, "description": v} for k, v in sorted(VARIABLE_DESCRIPTIONS.items())]


class FormulaIn(BaseModel):
    key: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    expression: str = Field(min_length=1, max_length=512)
    unit: str = ""
    higher_is_better: bool = True
    weight: float = Field(default=0, ge=0, le=100)


@router.post("/formulas", status_code=201, dependencies=[Depends(require_admin)])
def create_formula(body: FormulaIn, db: Session = Depends(get_db)):
    """录入自定义公式。表达式在录入时就用哑值试算一遍，非法直接 422，
    不留到出报表那天才炸。"""
    try:
        validate(body.expression, set(VARIABLE_DESCRIPTIONS))
    except FormulaError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    formula = PerformanceFormula(**body.model_dump())
    db.add(formula)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="公式编码已存在") from None
    db.refresh(formula)
    return _formula_out(formula)


def _formula_out(f: PerformanceFormula) -> dict:
    return {
        "id": f.id,
        "key": f.key,
        "name": f.name,
        "expression": f.expression,
        "unit": f.unit,
        "higher_is_better": f.higher_is_better,
        "weight": f.weight,
        "active": f.active,
    }


@router.get("/formulas")
def list_formulas(db: Session = Depends(get_db)):
    return [
        _formula_out(f)
        for f in db.query(PerformanceFormula).order_by(PerformanceFormula.key).all()
    ]


@router.delete("/formulas/{key}", dependencies=[Depends(require_admin)])
def deactivate_formula(key: str, db: Session = Depends(get_db)):
    """停用公式（不物理删除：历史报表还要能解释当时的口径）。"""
    formula = db.query(PerformanceFormula).filter(PerformanceFormula.key == key).first()
    if formula is None:
        raise HTTPException(status_code=404, detail="公式不存在")
    formula.active = False
    db.commit()
    return {"key": key, "active": False}


@router.get("/performance-report", dependencies=[Depends(require_roles("director"))])
def performance_report(period: str, db: Session = Depends(get_db)):
    """期末综合绩效报告：逐机构算出全部启用公式的取值，按加权总分排名。

    加权总分只对 weight>0 的公式生效；weight=0 的公式只观测不计分。
    某条公式求值失败不拖垮整张报表——该项记 error 并跳过计分。
    """
    formulas = (
        db.query(PerformanceFormula).filter(PerformanceFormula.active.is_(True)).order_by(
            PerformanceFormula.key
        ).all()
    )
    orgs = db.query(Organization).order_by(Organization.id).all()
    # 全部机构的变量一次性算出：固定 8 条聚合查询，不随机构数增长（T6.3）
    variable_index = build_variable_index(db, period)
    empty = {name: 0.0 for name in VARIABLE_DESCRIPTIONS}
    rows = []
    for org in orgs:
        variables = variable_index.get(org.id, empty)
        items = []
        weighted, weight_sum = 0.0, 0.0
        for formula in formulas:
            entry = {
                "key": formula.key,
                "name": formula.name,
                "unit": formula.unit,
                "weight": formula.weight,
            }
            try:
                value = evaluate(formula.expression, variables)
                entry["value"] = value
                if formula.weight > 0:
                    weighted += value * formula.weight
                    weight_sum += formula.weight
            except FormulaError as exc:
                entry["value"] = None
                entry["error"] = str(exc)
            items.append(entry)
        rows.append(
            {
                "org_id": org.id,
                "org_name": org.name,
                "level": org.level,
                "items": items,
                "weighted_score": round(weighted / weight_sum, 2) if weight_sum else 0.0,
            }
        )
    return {
        "period": period,
        "formula_count": len(formulas),
        "orgs": sorted(rows, key=lambda x: -x["weighted_score"]),
    }
