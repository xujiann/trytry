"""E2 医保支付方式改革落地：DRG/DIP 实际结算 + 医保智能审核（新建，不改存量）。

配置面（费率/病种目录/点值/审核规则）是全域主数据，限 admin，不做机构作用域；
结算与审核执行按 A 案隔离：写机构自有行 → assert_org_writable，按 id 改 →
assert_obj_org_writable，清单 → scope_org_list，统计 → stats_org_ids。

与存量表松耦合：admission 用 Admission 存在性校验；实际成本取该 admission 的
结算总额（Settlement.total_amount 之和，无则回退病案首页 CaseSummary.total_cost，
再无则 0）；DRG 权重取该 admission 的病案首页 CaseSummary.drg_weight；审核对象为
医保结算记录 InsuranceSettlement（org_id / total_amount / patient_id）。
"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_admin, require_roles
from ..models import (
    Admission,
    CaseSummary,
    DipCatalog,
    DipRate,
    DrgRate,
    InsuranceAuditFlag,
    InsuranceAuditRule,
    InsuranceSettlement,
    Organization,
    Patient,
    PaymentCase,
    Settlement,
    User,
)
from ..concurrency import insert_or_conflict, upsert_unique
from ..visibility import (
    assert_obj_org_writable,
    assert_org_writable,
    scope_org_list,
    stats_org_ids,
)

router = APIRouter(prefix="/api", tags=["医保支付改革"], dependencies=[Depends(get_current_user)])


# =========================================================================
# DRG 费率 & DIP 目录/点值（全域主数据，限 admin）
# =========================================================================


class DrgRateCreate(BaseModel):
    year: str = Field(min_length=4, max_length=4)
    region: str = Field(default="", max_length=16)
    rate_per_weight: float = Field(ge=0)
    note: str = Field(default="", max_length=128)
    active: bool = True


def _drg_rate_out(r: DrgRate) -> dict:
    return {
        "id": r.id, "year": r.year, "region": r.region,
        "rate_per_weight": r.rate_per_weight, "note": r.note, "active": r.active,
    }


@router.post("/drg-rates", status_code=201, dependencies=[Depends(require_admin)])
def create_drg_rate(body: DrgRateCreate, db: Session = Depends(get_db)):
    if (
        db.query(DrgRate)
        .filter(DrgRate.year == body.year, DrgRate.region == body.region)
        .first()
    ):
        raise HTTPException(status_code=409, detail="该年度该统筹区费率已存在")
    rate = insert_or_conflict(db, DrgRate(**body.model_dump()), "该年度该统筹区费率已存在")
    return _drg_rate_out(rate)


@router.get("/drg-rates")
def list_drg_rates(year: str | None = None, db: Session = Depends(get_db)):
    q = db.query(DrgRate)
    if year:
        q = q.filter(DrgRate.year == year)
    return [_drg_rate_out(r) for r in q.order_by(DrgRate.year.desc(), DrgRate.region).limit(500).all()]


class DipCatalogCreate(BaseModel):
    disease_code: str = Field(min_length=1, max_length=64)
    disease_name: str = Field(min_length=1, max_length=128)
    score: float = Field(ge=0)
    active: bool = True


class DipCatalogUpdate(BaseModel):
    score: float | None = Field(default=None, ge=0)
    active: bool | None = None


def _dip_catalog_out(c: DipCatalog) -> dict:
    return {
        "id": c.id, "disease_code": c.disease_code, "disease_name": c.disease_name,
        "score": c.score, "active": c.active,
    }


@router.post("/dip-catalog", status_code=201, dependencies=[Depends(require_admin)])
def create_dip_catalog(body: DipCatalogCreate, db: Session = Depends(get_db)):
    if db.query(DipCatalog).filter(DipCatalog.disease_code == body.disease_code).first():
        raise HTTPException(status_code=409, detail="该 DIP 病种码已存在")
    c = insert_or_conflict(db, DipCatalog(**body.model_dump()), "该 DIP 病种码已存在")
    return _dip_catalog_out(c)


@router.get("/dip-catalog")
def list_dip_catalog(active: bool | None = None, db: Session = Depends(get_db)):
    q = db.query(DipCatalog)
    if active is not None:
        q = q.filter(DipCatalog.active.is_(active))
    return [_dip_catalog_out(c) for c in q.order_by(DipCatalog.disease_code).limit(500).all()]


@router.patch("/dip-catalog/{catalog_id}", dependencies=[Depends(require_admin)])
def update_dip_catalog(catalog_id: int, body: DipCatalogUpdate, db: Session = Depends(get_db)):
    c = db.get(DipCatalog, catalog_id)
    if c is None:
        raise HTTPException(status_code=404, detail="病种目录不存在")
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(c, field, value)
    db.commit()
    return _dip_catalog_out(c)


class DipRateCreate(BaseModel):
    year: str = Field(min_length=4, max_length=4)
    point_value: float = Field(ge=0)
    active: bool = True


def _dip_rate_out(r: DipRate) -> dict:
    return {"id": r.id, "year": r.year, "point_value": r.point_value, "active": r.active}


@router.post("/dip-rates", status_code=201, dependencies=[Depends(require_admin)])
def create_dip_rate(body: DipRateCreate, db: Session = Depends(get_db)):
    if db.query(DipRate).filter(DipRate.year == body.year).first():
        raise HTTPException(status_code=409, detail="该年度 DIP 点值已存在")
    r = insert_or_conflict(db, DipRate(**body.model_dump()), "该年度 DIP 点值已存在")
    return _dip_rate_out(r)


@router.get("/dip-rates")
def list_dip_rates(db: Session = Depends(get_db)):
    return [_dip_rate_out(r) for r in db.query(DipRate).order_by(DipRate.year.desc()).limit(500).all()]


# =========================================================================
# 病组/病种实际结算（director/operator）
# =========================================================================


def _admission_actual_cost(db: Session, admission_id: int) -> float:
    """该 admission 的实际成本：优先取结算单总额之和，无则回退病案首页费用汇总。"""
    total = (
        db.query(func.coalesce(func.sum(Settlement.total_amount), 0.0))
        .filter(Settlement.admission_id == admission_id)
        .scalar()
    ) or 0.0
    if round(total, 2) > 0:
        return round(total, 2)
    summary = db.query(CaseSummary).filter(CaseSummary.admission_id == admission_id).first()
    if summary is not None and summary.total_cost:
        return round(summary.total_cost, 2)
    return 0.0


class SettleCaseIn(BaseModel):
    admission_id: int
    pay_mode: str = Field(pattern="^(drg|dip)$")
    year: str = Field(min_length=4, max_length=4)
    # DIP 结算须提供病种码（DRG 结算忽略此字段，走病案首页入组）
    disease_code: str | None = None
    # 费率所属统筹区（DRG），默认全域费率（region="")
    region: str = ""


def _payment_case_out(c: PaymentCase) -> dict:
    return {
        "id": c.id,
        "admission_id": c.admission_id,
        "org_id": c.org_id,
        "pay_mode": c.pay_mode,
        "group_code": c.group_code,
        "weight_or_score": c.weight_or_score,
        "standard_payment": c.standard_payment,
        "actual_cost": c.actual_cost,
        "profit": c.profit,
        "year": c.year,
        "created_at": c.created_at.isoformat(),
    }


@router.post(
    "/payment/settle-case",
    status_code=201,
    dependencies=[Depends(require_roles("director", "operator"))],
)
def settle_case(
    body: SettleCaseIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """按病组（DRG）或病种（DIP）测算应支付并落结算病例，算出盈亏。

    - DRG：读该住院病案首页的入组权重 drg_weight，×当年该统筹区费率；
    - DIP：按传入病种码取分值 ×当年点值；
    应支付 = 权重/分值 × 单价；实际成本 = 该住院结算总额；盈亏 = 应支付 − 实际。
    以 (admission_id, pay_mode) 幂等覆盖。
    """
    admission = db.get(Admission, body.admission_id)
    if admission is None:
        raise HTTPException(status_code=404, detail="住院记录不存在")
    # 归属校验：只能以本机构名义为本机构的住院做结算测算
    assert_org_writable(db, user, admission.org_id)

    if body.pay_mode == "drg":
        summary = (
            db.query(CaseSummary).filter(CaseSummary.admission_id == body.admission_id).first()
        )
        if summary is None or not summary.drg_code:
            raise HTTPException(status_code=409, detail="该住院尚未入组（病案首页无 DRG 组码），无法结算")
        rate = (
            db.query(DrgRate)
            .filter(
                DrgRate.year == body.year,
                DrgRate.region == body.region,
                DrgRate.active.is_(True),
            )
            .first()
        )
        if rate is None:
            raise HTTPException(status_code=409, detail="缺少该年度该统筹区的 DRG 费率")
        group_code = summary.drg_code
        weight_or_score = float(summary.drg_weight or 0)
        standard_payment = round(weight_or_score * rate.rate_per_weight, 2)
    else:  # dip
        if not body.disease_code:
            raise HTTPException(status_code=422, detail="DIP 结算须提供 disease_code")
        catalog = (
            db.query(DipCatalog).filter(DipCatalog.disease_code == body.disease_code).first()
        )
        if catalog is None:
            raise HTTPException(status_code=409, detail="DIP 病种目录中无该病种码")
        dip_rate = (
            db.query(DipRate)
            .filter(DipRate.year == body.year, DipRate.active.is_(True))
            .first()
        )
        if dip_rate is None:
            raise HTTPException(status_code=409, detail="缺少该年度的 DIP 点值")
        group_code = catalog.disease_code
        weight_or_score = float(catalog.score or 0)
        standard_payment = round(weight_or_score * dip_rate.point_value, 2)

    actual_cost = _admission_actual_cost(db, body.admission_id)
    profit = round(standard_payment - actual_cost, 2)

    case, _replaced = upsert_unique(
        db,
        PaymentCase,
        keys={"admission_id": body.admission_id, "pay_mode": body.pay_mode},
        values={
            "org_id": admission.org_id,
            "group_code": group_code,
            "weight_or_score": weight_or_score,
            "standard_payment": standard_payment,
            "actual_cost": actual_cost,
            "profit": profit,
            "year": body.year,
        },
    )
    return _payment_case_out(case)


@router.get("/payment/cases")
def list_payment_cases(
    pay_mode: str | None = None,
    year: str | None = None,
    org_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(PaymentCase)
    q = scope_org_list(db, user, q, PaymentCase, org_id)
    if pay_mode:
        q = q.filter(PaymentCase.pay_mode == pay_mode)
    if year:
        q = q.filter(PaymentCase.year == year)
    return [_payment_case_out(c) for c in q.order_by(PaymentCase.id.desc()).limit(500).all()]


@router.get("/payment/profit-stats", dependencies=[Depends(require_roles("director"))])
def payment_profit_stats(
    year: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """按支付方式汇总应支付/实际成本/盈亏（医共体统计范围 stats_org_ids）。"""
    allowed = stats_org_ids(db, user)
    q = db.query(
        PaymentCase.pay_mode,
        func.count(PaymentCase.id).label("n"),
        func.coalesce(func.sum(PaymentCase.standard_payment), 0.0).label("standard"),
        func.coalesce(func.sum(PaymentCase.actual_cost), 0.0).label("actual"),
        func.coalesce(func.sum(PaymentCase.profit), 0.0).label("profit"),
    )
    if allowed is not None:
        q = q.filter(PaymentCase.org_id.in_(allowed))
    if year:
        q = q.filter(PaymentCase.year == year)
    rows = q.group_by(PaymentCase.pay_mode).all()
    by_mode = [
        {
            "pay_mode": r.pay_mode,
            "count": r.n,
            "standard_payment": round(r.standard, 2),
            "actual_cost": round(r.actual, 2),
            "profit": round(r.profit, 2),
        }
        for r in rows
    ]
    return {
        "year": year,
        "by_pay_mode": by_mode,
        "total": {
            "count": sum(m["count"] for m in by_mode),
            "standard_payment": round(sum(m["standard_payment"] for m in by_mode), 2),
            "actual_cost": round(sum(m["actual_cost"] for m in by_mode), 2),
            "profit": round(sum(m["profit"] for m in by_mode), 2),
        },
    }


# =========================================================================
# 医保智能审核（规则限 admin；执行/疑点 director/operator）
# =========================================================================


_RULE_TYPES = {"over_amount", "over_frequency", "gender_contra", "age_contra", "decompose_admission"}


class AuditRuleCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    rule_type: str = Field(pattern="^(over_amount|over_frequency|gender_contra|age_contra|decompose_admission)$")
    params: dict = Field(default_factory=dict)
    severity: str = Field(default="warn", pattern="^(warn|block)$")
    active: bool = True


class AuditRuleUpdate(BaseModel):
    active: bool | None = None


def _audit_rule_out(r: InsuranceAuditRule) -> dict:
    return {
        "id": r.id, "code": r.code, "name": r.name, "rule_type": r.rule_type,
        "params": r.params, "severity": r.severity, "active": r.active,
    }


@router.post("/insurance/audit-rules", status_code=201, dependencies=[Depends(require_admin)])
def create_audit_rule(body: AuditRuleCreate, db: Session = Depends(get_db)):
    if db.query(InsuranceAuditRule).filter(InsuranceAuditRule.code == body.code).first():
        raise HTTPException(status_code=409, detail="该审核规则编码已存在")
    r = insert_or_conflict(db, InsuranceAuditRule(**body.model_dump()), "该审核规则编码已存在")
    return _audit_rule_out(r)


@router.get("/insurance/audit-rules")
def list_audit_rules(active: bool | None = None, db: Session = Depends(get_db)):
    q = db.query(InsuranceAuditRule)
    if active is not None:
        q = q.filter(InsuranceAuditRule.active.is_(active))
    return [_audit_rule_out(r) for r in q.order_by(InsuranceAuditRule.id).limit(500).all()]


@router.patch("/insurance/audit-rules/{rule_id}", dependencies=[Depends(require_admin)])
def update_audit_rule(rule_id: int, body: AuditRuleUpdate, db: Session = Depends(get_db)):
    r = db.get(InsuranceAuditRule, rule_id)
    if r is None:
        raise HTTPException(status_code=404, detail="审核规则不存在")
    if body.active is not None:
        r.active = body.active
    db.commit()
    return _audit_rule_out(r)


def _patient_age(patient: Patient | None) -> int | None:
    """按出生日期粗算年龄（YYYY-MM-DD）；无法解析返回 None。"""
    if patient is None or not patient.birth_date:
        return None
    try:
        y, m, d = (int(x) for x in patient.birth_date.split("-")[:3])
        born = date(y, m, d)
    except (ValueError, TypeError):
        return None
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def _apply_rule(rule: InsuranceAuditRule, settlement: InsuranceSettlement, patient: Patient | None):
    """对单条结算应用单条规则，命中返回疑点描述文本，否则 None。异常一律不命中（安全）。"""
    params = rule.params or {}
    try:
        if rule.rule_type == "over_amount":
            cap = params.get("max")
            if cap is not None and round(settlement.total_amount, 2) > round(float(cap), 2):
                return f"结算总额 {round(settlement.total_amount, 2)} 超过阈值 {cap}"
            return None
        if rule.rule_type == "gender_contra":
            # 性别禁忌：结算患者性别命中禁忌性别即疑点（如男性做妇科项目）
            forbid = params.get("forbid_gender") or params.get("gender")
            if forbid and patient is not None and patient.gender == forbid:
                return f"患者性别为{patient.gender}，命中性别禁忌规则"
            return None
        if rule.rule_type == "age_contra":
            age = _patient_age(patient)
            if age is None:
                return None
            lo, hi = params.get("min_age"), params.get("max_age")
            if lo is not None and age < int(lo):
                return f"患者年龄 {age} 小于下限 {lo}"
            if hi is not None and age > int(hi):
                return f"患者年龄 {age} 大于上限 {hi}"
            return None
        # over_frequency / decompose_admission：需跨结算聚合，本期留作安全空操作
        return None
    except Exception:  # pragma: no cover - 规则参数异常不得中断审核批次
        return None


class AuditRunIn(BaseModel):
    settlement_id: int


def _flag_out(f: InsuranceAuditFlag) -> dict:
    return {
        "id": f.id,
        "settlement_id": f.settlement_id,
        "org_id": f.org_id,
        "rule_code": f.rule_code,
        "detail": f.detail,
        "status": f.status,
        "created_at": f.created_at.isoformat(),
    }


@router.post(
    "/insurance/audit/run",
    status_code=201,
    dependencies=[Depends(require_roles("director", "operator"))],
)
def run_audit(
    body: AuditRunIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """对一条医保结算记录跑全部启用规则，命中即落审核疑点。"""
    settlement = db.get(InsuranceSettlement, body.settlement_id)
    if settlement is None:
        raise HTTPException(status_code=404, detail="医保结算记录不存在")
    assert_org_writable(db, user, settlement.org_id)
    patient = db.get(Patient, settlement.patient_id)
    rules = db.query(InsuranceAuditRule).filter(InsuranceAuditRule.active.is_(True)).all()
    created = []
    for rule in rules:
        detail = _apply_rule(rule, settlement, patient)
        if detail is None:
            continue
        flag = InsuranceAuditFlag(
            settlement_id=settlement.id,
            org_id=settlement.org_id,
            rule_code=rule.code,
            detail=detail[:512],
        )
        db.add(flag)
        created.append(flag)
    db.commit()
    for f in created:
        db.refresh(f)
    return {"settlement_id": settlement.id, "flags": [_flag_out(f) for f in created]}


@router.get("/insurance/audit/flags")
def list_audit_flags(
    status: str | None = None,
    org_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(InsuranceAuditFlag)
    q = scope_org_list(db, user, q, InsuranceAuditFlag, org_id)
    if status:
        q = q.filter(InsuranceAuditFlag.status == status)
    return [_flag_out(f) for f in q.order_by(InsuranceAuditFlag.id.desc()).limit(500).all()]


class AuditFlagUpdate(BaseModel):
    status: str = Field(pattern="^(confirmed|dismissed)$")


@router.patch(
    "/insurance/audit/flags/{flag_id}",
    dependencies=[Depends(require_roles("director", "operator"))],
)
def update_audit_flag(
    flag_id: int,
    body: AuditFlagUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    flag = db.get(InsuranceAuditFlag, flag_id)
    if flag is None:
        raise HTTPException(status_code=404, detail="审核疑点不存在")
    assert_obj_org_writable(db, user, flag)
    flag.status = body.status
    db.commit()
    return _flag_out(flag)
