"""决策指标扩展（阶段四 T4.1–T4.3）。

- **T4.1 县外就诊登记**：补上"县域就诊率""外转率"缺的那半边数据源。
  平台原本只看得见县内发生的诊疗，这两项恰恰是紧密型医共体监测的头号指标。
- **T4.2 运行效率**：平均住院日、床位周转次数、床位使用率、医师担负。
- **T4.3 自定义绩效公式**：管理员用受限表达式定义指标，走 AST 白名单求值
  （见 app/formula.py），并汇总为期末综合绩效报告。
"""
from typing import Any

from datetime import date, datetime

import sqlalchemy as sa
from sqlalchemy import func

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import (
    get_current_user,
    month_bounds,
    paginate,
    row_dict,
    require_admin,
    require_roles,
    resolve_business_date,
    resolve_org_scope,
)
from ..formula import FormulaError, evaluate, validate
from ..models import (
    Admission,
    Bed,
    BillDetail,
    CaseSummary,
    ChargeItem,
    ChronicPatient,
    Employee,
    Encounter,
    ExamRequest,
    OutboundVisit,
    Organization,
    Patient,
    DrugRule,
    PerformanceFormula,
    Prescription,
    PrescriptionItem,
    Referral,
    User,
    Ward,
)

router = APIRouter(prefix="/api/analytics", tags=["决策指标扩展"], dependencies=[Depends(get_current_user)])

# 医师职务关键词：Employee.position 含这些词即计入医师数（担负指标的分母）
DOCTOR_POSITION_KEYWORDS = ("医师", "医生")

# 抗菌药物使用强度的合理性上限（DDDs/百人天）。国家控制目标为 ≤40，
# 三甲实际值多在 30-60；在样本足够的前提下破这个阈值，几乎只有一种原因：
# DDD 与日剂量单位不一致。
INTENSITY_IMPLAUSIBLE = 200
# 强度的最小可解释样本量（收治人天）。一个月只出院两人时，一个疗程的抗菌药
# 就能把强度顶到几百——那不是用药过度，是分母太小。样本不足时既不告警也
# 不该拿这个数去比，标注出来即可。
MIN_BED_DAYS_FOR_INTENSITY = 100




# ---------------------------------------------------------------------------
# 数据库内日期算术（A3 聚合化的方言层）
# ---------------------------------------------------------------------------
# 运行效率与用药强度的口径都建立在"天数差"上。此前是把全量 Admission /
# BillDetail / PrescriptionItem 拉进内存逐行算——统计月报把三年历史搬一遍。
# 聚合下推后，日期算术必须在数据库里做，而 SQLite（开发/测试）与
# PostgreSQL（生产）没有共同的日期差函数，只能各写一句、收在这几个助手里。
# 语义对齐 Python 侧原实现：日期级用 `date() 差`，时刻级用 `timedelta.days`
# （对正值即向下取整），特征化测试锁住两种方言下的取值一致。


def _is_sqlite(db: Session) -> bool:
    return db.get_bind().dialect.name == "sqlite"


def _sql_date_day(db: Session, col):
    """列的**日期部分**在"天数轴"上的位置：同方言内两值之差即相隔天数。

    SQLite 用儒略日（日期在 x.5 上，双精度可精确表示，差值为精确整数）；
    PostgreSQL 转 date 后与固定纪元相减得整数。绝对值无意义，只比差。
    """
    if _is_sqlite(db):
        return func.julianday(func.date(col))
    return sa.cast(col, sa.Date) - date(1970, 1, 1)


def _py_date_day(db: Session, d: date):
    """把 Python date 常量换算到 `_sql_date_day` 同一条天数轴上。"""
    if _is_sqlite(db):
        # 儒略日 = 公历序数 + 1721424.5（午夜）
        return d.toordinal() + 1721424.5
    return (d - date(1970, 1, 1)).days


def _sql_stay_days(db: Session, later, earlier):
    """两个**时间戳**相隔的整天数，对齐 Python `(later - earlier).days` 的正值域。

    SQLite 取 Unix 秒差再整除（避免儒略日小数在整天边界上的浮点抖动）；
    PostgreSQL 取 interval 的 epoch 秒差后向下取整。
    """
    if _is_sqlite(db):
        return (
            sa.cast(func.strftime("%s", later), sa.Integer)
            - sa.cast(func.strftime("%s", earlier), sa.Integer)
        ) / 86400
    return sa.cast(func.floor(sa.extract("epoch", later - earlier) / 86400.0), sa.Integer)


def _at_least_one_day(expr):
    """住院日/床日的 1 天下限：当日入出院也算 1 天（原 `max(days, 1)`）。"""
    return sa.case((expr < 1, 1), else_=sa.cast(expr, sa.Integer))


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

    **本接口刻意不支持按机构分组筛选**：县外就诊（`outbound_visits`）记录的是
    "去了县外哪家医院"，没有县内机构归属——它本来就是县域整体的流出。
    只筛县内一侧会得到"片区内就诊 ÷（片区内就诊 + 全县县外就诊）"这种
    分子分母不同域的比率，比不提供筛选更容易误导人。
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
    # 聚合下推（工程包 P2）：此前把 OutboundVisit 全行取回内存逐行数——县外
    # 就诊表按年增长，月报把历史全量搬一遍。改为按机构层级 GROUP BY 一次取回
    # 计数/转诊计数/金额（COUNT(referral_id) 只数非 NULL，语义即"关联了转诊单"），
    # 总量在 ≤3 行的分组结果上相加，明细行不再离库。
    grouped = (
        outside_q.with_entities(
            OutboundVisit.external_org_level,
            func.count(OutboundVisit.id),
            func.count(OutboundVisit.referral_id),
            func.coalesce(func.sum(OutboundVisit.total_amount), 0.0),
        )
        .group_by(OutboundVisit.external_org_level)
        .all()
    )
    outside = sum(count for _, count, _, _ in grouped)
    total = inside + outside
    referred = sum(ref_count for _, _, ref_count, _ in grouped)
    by_level = {level: count for level, count, _, _ in grouped}

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
        "outside_amount": round(sum(amount for *_, amount in grouped), 2),
    }


# ============================================================================
# T4.2 运行效率
# ============================================================================


@router.get("/efficiency")
def efficiency(
    period: str,
    org_id: int | None = None,
    group_id: int | None = None,
    db: Session = Depends(get_db),
):
    """运行效率：按机构给出平均住院日、床位周转次数、床位使用率、医师担负。

    口径说明（写死并对外公布，避免各院各算）：
    - 平均住院日 = 出院者占用总床日 ÷ 出院人次
    - 床位周转次数 = 出院人次 ÷ 实际开放床位数
    - 床位使用率 = 实际占用床日 ÷（实际开放床位数 × 期间天数）
    - 医师日均担负诊疗人次 = 期间诊疗人次 ÷ 医师数 ÷ 期间天数
    """
    start, end = month_bounds(period)
    days = (end - start).days
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.min.time())

    org_names = {o.id: o.name for o in db.query(Organization).all()}
    scope = resolve_org_scope(db, group_id, org_id)
    if scope is not None:
        org_names = {oid: name for oid, name in org_names.items() if oid in set(scope)}
    bed_counts = row_dict(
        db.query(Ward.org_id, func.count(Bed.id))
        .join(Bed, Bed.ward_id == Ward.id)
        .group_by(Ward.org_id)
        .all()
    )
    # 医师数：关键词匹配下推为 LIKE（语义同 Python 的子串 in），按机构分组计数
    doctor_counts = row_dict(
        db.query(Employee.org_id, func.count(Employee.id))
        .filter(
            Employee.status == "active",
            sa.or_(*[Employee.position.like(f"%{k}%") for k in DOCTOR_POSITION_KEYWORDS]),
        )
        .group_by(Employee.org_id)
        .all()
    )

    # 住院指标全部数据库内聚合。此前把 `admitted_at < 期末` 的**历史全量**住院
    # 拉进内存逐行算，统计一个月要搬三年数据；现在按"与统计期有重叠"过滤
    # （期前出院的记录本来就贡献 0，被 WHERE 直接裁掉），并把床日/住院日的
    # 天数算术交给数据库（见上方方言助手）。
    admitted_day = _sql_date_day(db, Admission.admitted_at)
    start_day, end_day = _py_date_day(db, start), _py_date_day(db, end)
    # 出院日：在院者视同期末（与原实现 `discharged or end` 一致）
    discharged_day = sa.case(
        (Admission.discharged_at.is_(None), end_day),
        else_=_sql_date_day(db, Admission.discharged_at),
    )
    overlap_start = sa.case((admitted_day > start_day, admitted_day), else_=start_day)
    overlap_end = sa.case((discharged_day < end_day, discharged_day), else_=end_day)
    overlap = overlap_end - overlap_start
    occupied_days: dict[int, int] = row_dict(
        db.query(Admission.org_id, func.sum(_at_least_one_day(overlap)))
        .filter(
            Admission.admitted_at < end_dt,
            sa.or_(Admission.discharged_at.is_(None), Admission.discharged_at >= start_dt),
            overlap >= 0,
        )
        .group_by(Admission.org_id)
        .all()
    )
    # 出院者：出院日落在期间内才计入本期出院人次与平均住院日
    stay = _sql_date_day(db, Admission.discharged_at) - admitted_day
    discharged_count: dict[int, int] = {}
    discharged_days: dict[int, int] = {}
    for oid, count, day_sum in (
        db.query(Admission.org_id, func.count(Admission.id), func.sum(_at_least_one_day(stay)))
        .filter(
            Admission.admitted_at < end_dt,
            Admission.discharged_at.isnot(None),
            Admission.discharged_at >= start_dt,
            Admission.discharged_at < end_dt,
        )
        .group_by(Admission.org_id)
        .all()
    ):
        discharged_count[oid] = count
        discharged_days[oid] = day_sum

    visits: dict[int, int] = {}
    for oid, count in (
        db.query(Encounter.org_id, func.count(Encounter.id))
        .filter(Encounter.created_at >= start_dt, Encounter.created_at < end_dt)
        .group_by(Encounter.org_id)
        .all()
    ):
        visits[oid] = count

    result = []
    # 有数据的机构取并集，再按范围过滤。只过滤 org_names 是不够的——
    # 结果行是从各数据字典的键拼出来的，不经过 org_names。
    candidates = set(bed_counts) | set(visits) | set(occupied_days) | set(discharged_count)
    if scope is not None:
        candidates &= set(scope)
    for oid in candidates:
        beds = bed_counts.get(oid, 0)
        discharges = discharged_count.get(oid, 0)
        doctors = doctor_counts.get(oid, 0)
        result.append(
            {
                "org_id": oid,
                "org_name": org_names.get(oid, ""),
                "beds": beds,
                "discharges": discharges,
                "occupied_bed_days": occupied_days.get(oid, 0),
                "avg_length_of_stay": round(discharged_days.get(oid, 0) / discharges, 2)
                if discharges
                else 0.0,
                "bed_turnover": round(discharges / beds, 2) if beds else 0.0,
                "bed_occupancy_rate_pct": round(
                    occupied_days.get(oid, 0) * 100 / (beds * days), 2
                )
                if beds and days
                else 0.0,
                "visits": visits.get(oid, 0),
                "doctors": doctors,
                "visits_per_doctor_per_day": round(visits.get(oid, 0) / doctors / days, 2)
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
    start, end = month_bounds(period)
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
    chronic = row_dict(
        db.query(ChronicPatient.managed_by_org_id, func.count(ChronicPatient.id))
        .group_by(ChronicPatient.managed_by_org_id)
        .all()
    )
    # 显式传 db=：efficiency 的签名里 period 之后是 org_id/group_id，
    # 位置传参会把 db 塞进 org_id。
    efficiency_index = {row["org_id"]: row for row in efficiency(period, db=db)}

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


# ============================================================================
# 用药结构分析（指南 #48 抗菌药物使用强度 / #49 药占比）
# ============================================================================


@router.get("/drug-use")
def drug_use(
    period: str,
    org_id: int | None = None,
    group_id: int | None = None,
    db: Session = Depends(get_db),
):
    """药占比与抗菌药物使用强度。

    两个指标都会被写进考核，所以口径写死在这里并随结果一起返回：

    - 住院药占比 = Σ病案首页药品费 ÷ Σ病案首页总费用
    - 门诊药占比 = Σ(收费目录 category=drug 的明细金额) ÷ Σ门诊明细金额
    - 抗菌药物使用强度 = Σ(日剂量×天数 ÷ DDD) × 100 ÷ 同期收治人天

    强度分母用"收治人天"（出院者占用总床日），与国家监测口径一致。
    未维护 DDD 的抗菌药按**未覆盖**单独计数返回，不按 0 算也不悄悄丢掉——
    一个漏算了一半抗菌药的强度值，比明说"目录没维护全"更容易误导人。
    """
    start, end = month_bounds(period)
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.min.time())
    org_names = {o.id: o.name for o in db.query(Organization).all()}
    scope = resolve_org_scope(db, group_id, org_id)
    org_ids = sorted(org_names) if scope is None else sorted(scope)

    # ---- 住院药占比（病案首页）----
    inpatient = dict.fromkeys(org_ids, (0.0, 0.0))
    summary_rows = (
        db.query(
            Admission.org_id,
            func.sum(CaseSummary.total_cost),
            func.sum(CaseSummary.drug_cost),
        )
        .join(Admission, CaseSummary.admission_id == Admission.id)
        .filter(CaseSummary.created_at >= start_dt, CaseSummary.created_at < end_dt)
        .group_by(Admission.org_id)
        .all()
    )
    for oid, total, drug in summary_rows:
        if oid in inpatient:
            inpatient[oid] = (float(total or 0), float(drug or 0))

    # ---- 门诊药占比（费用明细 × 收费目录分类）----
    # 此前把整月 BillDetail 全行拉进内存逐条累加；改为按机构分组求和，
    # 药品口径用 IN (收费目录 category=drug) 子查询在库内判定。
    drug_code_sq = (
        sa.select(ChargeItem.code).where(ChargeItem.category == "drug").scalar_subquery()
    )
    outpatient_total: dict[int, float] = dict.fromkeys(org_ids, 0.0)
    outpatient_drug: dict[int, float] = dict.fromkeys(org_ids, 0.0)
    detail_rows = (
        db.query(
            Encounter.org_id,
            func.sum(BillDetail.amount),
            func.sum(sa.case((BillDetail.item_code.in_(drug_code_sq), BillDetail.amount), else_=0)),
        )
        .select_from(BillDetail)
        .join(Encounter, BillDetail.encounter_id == Encounter.id)
        .filter(BillDetail.created_at >= start_dt, BillDetail.created_at < end_dt)
        .group_by(Encounter.org_id)
        .all()
    )
    for oid, total, drug in detail_rows:
        if oid not in outpatient_total:
            continue
        outpatient_total[oid] = float(total or 0)
        outpatient_drug[oid] = float(drug or 0)

    # ---- 抗菌药物使用强度 ----
    # 同样从"整月 PrescriptionItem 全行进内存"改为库内聚合：抗菌药目录直接
    # JOIN 进来（drug_code 唯一），DDD>0 求 DDDs、DDD=0 计未覆盖数一次算完。
    ddd_sum: dict[int, float] = dict.fromkeys(org_ids, 0.0)
    uncovered: dict[int, int] = dict.fromkeys(org_ids, 0)
    item_rows = (
        db.query(
            Prescription.org_id,
            func.sum(
                sa.case(
                    (
                        DrugRule.ddd > 0,
                        func.coalesce(PrescriptionItem.daily_dose, 0)
                        * func.coalesce(PrescriptionItem.days, 0)
                        / DrugRule.ddd,
                    ),
                    else_=0.0,
                )
            ),
            # 目录未维护 DDD（ddd<=0）的抗菌药条目：计为未覆盖
            func.sum(sa.case((DrugRule.ddd <= 0, 1), else_=0)),
        )
        .select_from(PrescriptionItem)
        .join(Prescription, PrescriptionItem.prescription_id == Prescription.id)
        .join(
            DrugRule,
            sa.and_(
                DrugRule.drug_code == PrescriptionItem.drug_code,
                DrugRule.antibiotic.is_(True),
                DrugRule.active.is_(True),
            ),
        )
        .filter(Prescription.created_at >= start_dt, Prescription.created_at < end_dt)
        .group_by(Prescription.org_id)
        .all()
    )
    for oid, ddds, uncov in item_rows:
        if oid not in ddd_sum:
            continue
        ddd_sum[oid] = float(ddds or 0)
        uncovered[oid] = int(uncov or 0)

    # 分母：收治人天 = 期内出院者占用总床日（时刻级天数差，1 天下限）
    bed_days: dict[int, int] = dict.fromkeys(org_ids, 0)
    stay_days = _sql_stay_days(db, Admission.discharged_at, Admission.admitted_at)
    for oid, day_sum in (
        db.query(Admission.org_id, func.sum(_at_least_one_day(stay_days)))
        .filter(
            Admission.discharged_at.isnot(None),
            Admission.discharged_at >= start_dt,
            Admission.discharged_at < end_dt,
        )
        .group_by(Admission.org_id)
        .all()
    ):
        if oid in bed_days:
            bed_days[oid] = int(day_sum or 0)

    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for oid in org_ids:
        in_total, in_drug = inpatient.get(oid, (0.0, 0.0))
        out_total, out_drug = outpatient_total.get(oid, 0.0), outpatient_drug.get(oid, 0.0)
        days = bed_days.get(oid, 0)
        rows.append(
            {
                "org_id": oid,
                "org_name": org_names.get(oid, str(oid)),
                "inpatient_total": round(in_total, 2),
                "inpatient_drug": round(in_drug, 2),
                "inpatient_drug_ratio_pct": round(in_drug / in_total * 100, 2) if in_total else 0.0,
                "outpatient_total": round(out_total, 2),
                "outpatient_drug": round(out_drug, 2),
                "outpatient_drug_ratio_pct": (
                    round(out_drug / out_total * 100, 2) if out_total else 0.0
                ),
                "antibiotic_ddds": round(ddd_sum.get(oid, 0.0), 2),
                "bed_days": days,
                "antibiotic_intensity": (
                    round(ddd_sum.get(oid, 0.0) * 100 / days, 2) if days else 0.0
                ),
                "ddd_uncovered_items": uncovered.get(oid, 0),
                # 样本不足时强度不可比，客户端据此淡化展示而不是照着排名
                "intensity_unstable": 0 < days < MIN_BED_DAYS_FOR_INTENSITY,
            }
        )
        # 量纲告警：DDD 必须与 daily_dose 同单位。把 3g 的 DDD 填成 "3"（而处方
        # 按 mg 记）会让强度差三个数量级。程序无从校验单位，但结果的数量级是
        # 有常识的——国家控制目标是 40 DDDs/百人天，破 200 基本可以断定填错了。
        # 与其让一个天文数字被抄进上报表，不如在同一份响应里说清楚它可疑。
        if (
            float(rows[-1]["antibiotic_intensity"]) > INTENSITY_IMPLAUSIBLE
            and not rows[-1]["intensity_unstable"]
        ):
            warnings.append(
                f"{rows[-1]['org_name']}：抗菌药物使用强度 {rows[-1]['antibiotic_intensity']} "
                f"远超常规量级，请核对药品目录中 DDD 的单位是否与处方日剂量一致"
            )
    return {
        "period": period,
        "caliber": {
            "drug_ratio": "住院取病案首页药品费/总费用；门诊按收费目录 category=drug 归集明细",
            "antibiotic_intensity": "Σ(日剂量×天数÷DDD)×100 ÷ 收治人天（出院者占用总床日）",
        },
        "warnings": warnings,
        "orgs": rows,
    }
