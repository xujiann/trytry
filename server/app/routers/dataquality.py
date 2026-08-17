"""数据质控规则引擎（块3）：规则驱动扫描存量数据，输出违规明细与汇总。

- 规则表 QcRule：code 唯一、target_table 指向被检表、rule_type 决定执行器、
  config 携带参数、severity 区分 error/warn、active 控制是否参与扫描
- 5 类执行器：required / range / enum / cross_ref / logic（命名逻辑校验）
- 启动时按 app/data/qc_rules_seed.py 幂等种子化 15 条规则（已存在编码不覆盖本地调整）
"""
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session

from ..concurrency import insert_or_conflict
from ..database import get_db
from ..deps import get_current_user, require_admin
from ..models import (
    Admission,
    ChronicDiseaseType,
    ChronicPatient,
    CodeEntry,
    CodeSystem,
    ConsortiumDrugCatalog,
    Encounter,
    ExamReport,
    ExamRequest,
    FollowUp,
    InfectiousCase,
    InsuranceAuditFlag,
    InsuranceSettlement,
    MedicalCert,
    Patient,
    PaymentCase,
    Prescription,
    PrescriptionItem,
    QcRule,
    RefillRequest,
    ReferralClinicalRef,
    User,
)
from ..visibility import visible_org_ids

router = APIRouter(
    prefix="/api/dataquality", tags=["数据质控"], dependencies=[Depends(get_current_user)]
)

RULE_TYPES = {
    "required": "必填项",
    "range": "数值区间",
    "enum": "取值枚举",
    "cross_ref": "引用校验",
    "logic": "逻辑校验",
}
SEVERITIES = {"error": "错误", "warn": "警告"}

# 可被质控规则引用的表（target_table → 模型）；新增被检表在此登记即可
_TABLE_MODELS = {
    m.__tablename__: m
    for m in (
        Patient,
        Encounter,
        ExamRequest,
        ExamReport,
        Prescription,
        PrescriptionItem,
        ChronicPatient,
        FollowUp,
        InfectiousCase,
        Admission,
        MedicalCert,
        ChronicDiseaseType,
    )
}
# 单条规则单次扫描的行数上限（防全表拉爆内存；超出部分下次整改后再扫）
SCAN_LIMIT = 5000


def _is_blank(value) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


# ---------------------------------------------------------------------------
# 命名逻辑校验（rule_type=logic）
# ---------------------------------------------------------------------------

_ID_CARD_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_ID_CARD_CHECK_CODES = "10X98765432"


def id_card_invalid_reason(value: str) -> str:
    """身份证号 GB 11643 校验：18 位、前17位数字、末位校验码正确。"""
    if _is_blank(value):
        return "身份证号为空"
    value = value.strip().upper()
    if len(value) != 18:
        return f"身份证号长度应为18位，实际 {len(value)} 位"
    if not value[:17].isdigit():
        return "身份证号前17位应全为数字"
    total = sum(int(value[i]) * _ID_CARD_WEIGHTS[i] for i in range(17))
    expected = _ID_CARD_CHECK_CODES[total % 11]
    if value[17] != expected:
        return f"身份证号校验位应为 {expected}，实际 {value[17]}"
    return ""


def _check_id_card(db: Session, rule: QcRule, model) -> list[tuple[int, str]]:
    field = rule.config.get("field", "id_card")
    hits = []
    for row in db.query(model).limit(SCAN_LIMIT).all():
        reason = id_card_invalid_reason(getattr(row, field, ""))
        if reason:
            hits.append((row.id, reason))
    return hits


def _check_critical_closed_loop(db: Session, rule: QcRule, model) -> list[tuple[int, str]]:
    """危急值报告未走到处置反馈（critical_status != resolved）。"""
    rows = (
        db.query(ExamReport)
        .filter(ExamReport.critical.is_(True), ExamReport.critical_status != "resolved")
        .limit(SCAN_LIMIT)
        .all()
    )
    return [
        (r.id, f"危急值闭环状态为 {r.critical_status or '未回填'}，未达处置反馈（resolved）")
        for r in rows
    ]


def _check_datetime_order(db: Session, rule: QcRule, model) -> list[tuple[int, str]]:
    """结束时间早于开始时间（结束为空视为进行中，不判违规）。"""
    start_field = rule.config.get("start_field", "")
    end_field = rule.config.get("end_field", "")
    hits = []
    for row in db.query(model).limit(SCAN_LIMIT).all():
        start, end = getattr(row, start_field, None), getattr(row, end_field, None)
        if start is None or end is None:
            continue
        if isinstance(start, str) or isinstance(end, str):
            if str(end) < str(start):
                hits.append((row.id, f"{end_field}（{end}）早于 {start_field}（{start}）"))
            continue
        if end < start:
            hits.append(
                (
                    row.id,
                    f"{end_field}（{end:%Y-%m-%d %H:%M}）早于 {start_field}（{start:%Y-%m-%d %H:%M}）",
                )
            )
    return hits


def _check_date_not_future(db: Session, rule: QcRule, model) -> list[tuple[int, str]]:
    field = rule.config.get("field", "")
    today = date.today().isoformat()
    hits = []
    for row in db.query(model).limit(SCAN_LIMIT).all():
        value = getattr(row, field, None)
        if isinstance(value, datetime):
            value = value.date().isoformat()
        elif isinstance(value, date):
            value = value.isoformat()
        if _is_blank(value):
            continue
        if str(value) > today:
            hits.append((row.id, f"{field}（{value}）晚于当前日期（{today}）"))
    return hits


def _check_chronic_followup_indicator(db: Session, rule: QcRule, model) -> list[tuple[int, str]]:
    """慢病随访须记录对应病种指标（按病种要求的指标字段判定）。"""
    mapping: dict[str, list[str]] = rule.config.get("disease_indicators", {})
    diseases = dict(db.query(ChronicPatient.id, ChronicPatient.disease).all())
    hits = []
    for row in db.query(FollowUp).limit(SCAN_LIMIT).all():
        disease = diseases.get(row.chronic_id, "")
        required = mapping.get(disease)
        if required:
            missing = [f for f in required if getattr(row, f, None) is None]
            if missing:
                hits.append((row.id, f"{disease} 随访缺少指标：{'、'.join(missing)}"))
            continue
        # 目录未列明指标要求的病种：至少记录一项指标（含通用 metrics）
        if row.sbp is None and row.dbp is None and row.glucose is None and not (row.metrics or {}):
            hits.append((row.id, f"{disease or '未知病种'} 随访未记录任何指标"))
    return hits


_LOGIC_CHECKS = {
    "id_card_checksum": _check_id_card,
    "critical_closed_loop": _check_critical_closed_loop,
    "datetime_order": _check_datetime_order,
    "date_not_future": _check_date_not_future,
    "chronic_followup_indicator": _check_chronic_followup_indicator,
}


# ---------------------------------------------------------------------------
# 通用执行器
# ---------------------------------------------------------------------------


def _filtered(db: Session, model, rule: QcRule):
    query = db.query(model)
    for key, value in (rule.config.get("filter") or {}).items():
        column = getattr(model, key, None)
        if column is not None:
            query = query.filter(column == value)
    return query.limit(SCAN_LIMIT)


def _run_required(db: Session, rule: QcRule, model) -> list[tuple[int, str]]:
    field = rule.config.get("field", "")
    return [
        (row.id, f"{field} 为空")
        for row in _filtered(db, model, rule).all()
        if _is_blank(getattr(row, field, None))
    ]


def _run_range(db: Session, rule: QcRule, model) -> list[tuple[int, str]]:
    field = rule.config.get("field", "")
    low, high = rule.config.get("min"), rule.config.get("max")
    ex_low, ex_high = rule.config.get("exclusive_min", False), rule.config.get("exclusive_max", False)
    hits = []
    for row in _filtered(db, model, rule).all():
        value = getattr(row, field, None)
        if value is None:
            hits.append((row.id, f"{field} 缺失，无法判定区间"))
            continue
        if low is not None and (value <= low if ex_low else value < low):
            hits.append((row.id, f"{field}={value} 低于下限 {low}{'（不含）' if ex_low else ''}"))
            continue
        if high is not None and (value >= high if ex_high else value > high):
            hits.append((row.id, f"{field}={value} 超出上限 {high}{'（不含）' if ex_high else ''}"))
    return hits


def _run_enum(db: Session, rule: QcRule, model) -> list[tuple[int, str]]:
    field = rule.config.get("field", "")
    allowed = set(rule.config.get("values", []))
    return [
        (row.id, f"{field}={getattr(row, field, None)} 不在允许取值 {sorted(allowed)} 内")
        for row in _filtered(db, model, rule).all()
        if getattr(row, field, None) not in allowed
    ]


def _run_cross_ref(db: Session, rule: QcRule, model) -> list[tuple[int, str]]:
    field = rule.config.get("field", "")
    skip_empty = rule.config.get("skip_empty", False)
    if rule.config.get("ref_code_system"):
        system_code = rule.config["ref_code_system"]
        system = db.query(CodeSystem).filter(CodeSystem.code == system_code).first()
        valid = (
            {c for (c,) in db.query(CodeEntry.code).filter(CodeEntry.system_id == system.id).all()}
            if system
            else set()
        )
        ref_desc = f"{system_code} 字典"
    else:
        ref_model = _TABLE_MODELS.get(rule.config.get("ref_table", ""))
        if ref_model is None:
            raise HTTPException(status_code=422, detail=f"规则 {rule.code} 的引用表未登记")
        ref_field = getattr(ref_model, rule.config.get("ref_field", "code"))
        valid = {v for (v,) in db.query(ref_field).all()}
        ref_desc = f"{rule.config['ref_table']} 目录"
    hits = []
    for row in _filtered(db, model, rule).all():
        value = getattr(row, field, None)
        if skip_empty and _is_blank(value):
            continue
        if value not in valid:
            hits.append((row.id, f"{field}={value or '空'} 不存在于 {ref_desc}"))
    return hits


def _run_logic(db: Session, rule: QcRule, model) -> list[tuple[int, str]]:
    check = _LOGIC_CHECKS.get(rule.config.get("check", ""))
    if check is None:
        raise HTTPException(
            status_code=422, detail=f"规则 {rule.code} 的逻辑校验 {rule.config.get('check')} 未实现"
        )
    return check(db, rule, model)


_EXECUTORS = {
    "required": _run_required,
    "range": _run_range,
    "enum": _run_enum,
    "cross_ref": _run_cross_ref,
    "logic": _run_logic,
}


def run_rule(db: Session, rule: QcRule) -> list[dict]:
    """执行单条规则，返回违规明细（规则/表/记录id/问题描述/严重度）。"""
    model = _TABLE_MODELS.get(rule.target_table)
    if model is None:
        raise HTTPException(
            status_code=422, detail=f"规则 {rule.code} 的被检表 {rule.target_table} 未登记"
        )
    executor = _EXECUTORS.get(rule.rule_type)
    if executor is None:
        raise HTTPException(status_code=422, detail=f"规则 {rule.code} 的类型 {rule.rule_type} 不支持")
    return [
        {
            "rule_code": rule.code,
            "rule_name": rule.name,
            "rule_type": rule.rule_type,
            "severity": rule.severity,
            "table": rule.target_table,
            "record_id": record_id,
            "message": message,
        }
        for record_id, message in executor(db, rule, model)
    ]


def _active_rules(db: Session, rule_code: str | None = None, target_table: str | None = None):
    query = db.query(QcRule).filter(QcRule.active.is_(True))
    if rule_code:
        query = query.filter(QcRule.code == rule_code)
    if target_table:
        query = query.filter(QcRule.target_table == target_table)
    return query.order_by(QcRule.code).all()


@router.get("/run")
def run_checks(
    response: Response,
    rule_code: str | None = None,
    target_table: str | None = None,
    severity: str | None = None,
    offset: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
):
    """按启用规则扫描现有数据，返回违规明细（停用规则不参与扫描）。"""
    violations: list[dict] = []
    for rule in _active_rules(db, rule_code, target_table):
        if severity and rule.severity != severity:
            continue
        violations.extend(run_rule(db, rule))
    total = len(violations)
    limit = min(max(limit, 1), 1000)
    response.headers["X-Total-Count"] = str(total)
    return {
        "total": total,
        "error_total": sum(1 for v in violations if v["severity"] == "error"),
        "warn_total": sum(1 for v in violations if v["severity"] == "warn"),
        "offset": max(offset, 0),
        "limit": limit,
        "items": violations[max(offset, 0) : max(offset, 0) + limit],
    }


@router.get("/summary")
def summary(db: Session = Depends(get_db)):
    """违规汇总：按规则、按严重度、按被检表三个维度。"""
    by_rule, by_severity, by_table = [], {"error": 0, "warn": 0}, {}
    for rule in _active_rules(db):
        hits = run_rule(db, rule)
        by_rule.append(
            {
                "rule_code": rule.code,
                "rule_name": rule.name,
                "rule_type": rule.rule_type,
                "rule_type_name": RULE_TYPES.get(rule.rule_type, rule.rule_type),
                "table": rule.target_table,
                "severity": rule.severity,
                "violations": len(hits),
            }
        )
        by_severity[rule.severity] = by_severity.get(rule.severity, 0) + len(hits)
        by_table[rule.target_table] = by_table.get(rule.target_table, 0) + len(hits)
    return {
        "rules_checked": len(by_rule),
        "total": sum(r["violations"] for r in by_rule),
        "by_severity": by_severity,
        "by_table": by_table,
        "by_rule": by_rule,
    }


# ---------- 规则维护（限管理员） ----------


class RuleCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1)
    target_table: str = Field(min_length=1)
    rule_type: str = Field(pattern="^(required|range|enum|cross_ref|logic)$")
    config: dict = Field(default_factory=dict)
    severity: str = Field(default="error", pattern="^(error|warn)$")
    active: bool = True


class RuleUpdate(BaseModel):
    name: str | None = None
    config: dict | None = None
    severity: str | None = Field(default=None, pattern="^(error|warn)$")
    active: bool | None = None


def _rule_out(r: QcRule) -> dict:
    return {
        "id": r.id,
        "code": r.code,
        "name": r.name,
        "target_table": r.target_table,
        "rule_type": r.rule_type,
        "rule_type_name": RULE_TYPES.get(r.rule_type, r.rule_type),
        "config": r.config,
        "severity": r.severity,
        "severity_name": SEVERITIES.get(r.severity, r.severity),
        "active": r.active,
    }


@router.get("/rules")
def list_rules(active: bool | None = None, db: Session = Depends(get_db)):
    query = db.query(QcRule)
    if active is not None:
        query = query.filter(QcRule.active.is_(active))
    return [_rule_out(r) for r in query.order_by(QcRule.code).all()]


@router.post("/rules", status_code=201, dependencies=[Depends(require_admin)])
def create_rule(body: RuleCreate, db: Session = Depends(get_db)):
    if db.query(QcRule).filter(QcRule.code == body.code).first():
        raise HTTPException(status_code=409, detail="规则编码已存在")
    if body.target_table not in _TABLE_MODELS:
        raise HTTPException(
            status_code=422,
            detail=f"被检表未登记（可选：{'、'.join(sorted(_TABLE_MODELS))}）",
        )
    rule = insert_or_conflict(db, QcRule(**body.model_dump()), "规则编码已存在")
    return _rule_out(rule)


@router.patch("/rules/{rule_id}", dependencies=[Depends(require_admin)])
def update_rule(rule_id: int, body: RuleUpdate, db: Session = Depends(get_db)):
    rule = db.get(QcRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    for key, value in body.model_dump(exclude_none=True).items():
        setattr(rule, key, value)
    db.commit()
    db.refresh(rule)
    return _rule_out(rule)


@router.delete("/rules/{rule_id}", dependencies=[Depends(require_admin)])
def delete_rule(rule_id: int, db: Session = Depends(get_db)):
    rule = db.get(QcRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    db.delete(rule)
    db.commit()
    return {"deleted": rule_id}


# ---------------------------------------------------------------------------
# S17 数据质量闭环扩展：新表裸外键完整性对账
# ---------------------------------------------------------------------------
#
# E1–E9 / S1–S16 引入了一批"松耦合"整数引用——用普通 Integer/字符串存对端 id，
# 刻意不建 DB 外键（不绑死表名、便于中央装配），代价是这些引用可能悬空：
#   · PaymentCase.admission_id / insurance_settlement_id
#   · InsuranceAuditFlag.settlement_id
#   · RefillRequest.prescription_id / drug_code
#   · ReferralClinicalRef.ref_id（按 ref_type 解析到不同目标表）
# 上面的规则引擎（QcRule）不覆盖这些新表，这里补一次"左连接取空"式对账，
# 用 ~col.in_(子查询) 一次性扫出指向不存在目标的孤儿引用，不逐行 db.get。
# 带 org_id 的表按调用者可见机构范围过滤（全域角色 visible_org_ids 返回 None → 看全部）；
# ReferralClinicalRef 无 org_id 列，全域对账。

# 每个检查项回填的样本 id 上限（只为定位，不回全量）
INTEGRITY_SAMPLE_LIMIT = 20


def _orphan_check(key: str, query) -> dict:
    """对一条已过滤好的孤儿引用查询，返回 {检查项, 孤儿数, 样本id}。"""
    orphan_count = query.count()
    sample_ids = [row.id for row in query.order_by(None).limit(INTEGRITY_SAMPLE_LIMIT).all()]
    return {"key": key, "orphan_count": orphan_count, "sample_ids": sample_ids}


def _scope(query, org_col, org_ids):
    """带 org_id 的表按可见机构过滤；org_ids 为 None（全域）时不加过滤。"""
    if org_ids is not None:
        query = query.filter(org_col.in_(org_ids))
    return query


@router.get("/integrity-scan")
def integrity_scan(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """新表裸外键完整性对账：扫出指向不存在目标的松耦合引用（孤儿引用）。

    带 org_id 的表按调用者可见机构范围过滤（全域角色看全部）；
    ReferralClinicalRef 无 org_id，做全域对账。summary 类引用无目标表，跳过。
    """
    org_ids = visible_org_ids(db, user)
    checks: list[dict] = []

    admission_ids = db.query(Admission.id)
    settlement_ids = db.query(InsuranceSettlement.id)
    prescription_ids = db.query(Prescription.id)
    catalog_codes = db.query(ConsortiumDrugCatalog.drug_code)

    # 1. PaymentCase.admission_id → admissions
    q = _scope(
        db.query(PaymentCase).filter(~PaymentCase.admission_id.in_(admission_ids)),
        PaymentCase.org_id,
        org_ids,
    )
    checks.append(_orphan_check("payment_case_admission", q))

    # 2. PaymentCase.insurance_settlement_id（非空）→ insurance_settlements
    q = _scope(
        db.query(PaymentCase).filter(
            PaymentCase.insurance_settlement_id.isnot(None),
            ~PaymentCase.insurance_settlement_id.in_(settlement_ids),
        ),
        PaymentCase.org_id,
        org_ids,
    )
    checks.append(_orphan_check("payment_case_settlement", q))

    # 3. InsuranceAuditFlag.settlement_id → insurance_settlements
    q = _scope(
        db.query(InsuranceAuditFlag).filter(~InsuranceAuditFlag.settlement_id.in_(settlement_ids)),
        InsuranceAuditFlag.org_id,
        org_ids,
    )
    checks.append(_orphan_check("audit_flag_settlement", q))

    # 4. RefillRequest.prescription_id（非空）→ prescriptions
    q = _scope(
        db.query(RefillRequest).filter(
            RefillRequest.prescription_id.isnot(None),
            ~RefillRequest.prescription_id.in_(prescription_ids),
        ),
        RefillRequest.org_id,
        org_ids,
    )
    checks.append(_orphan_check("refill_prescription", q))

    # 5. RefillRequest.drug_code（非空）→ consortium_drug_catalog.drug_code
    q = _scope(
        db.query(RefillRequest).filter(
            RefillRequest.drug_code != "",
            ~RefillRequest.drug_code.in_(catalog_codes),
        ),
        RefillRequest.org_id,
        org_ids,
    )
    checks.append(_orphan_check("refill_drug_code", q))

    # 6. ReferralClinicalRef.ref_id：按 ref_type 解析到不同目标表；summary 无目标，跳过
    ref_targets = {
        "exam_report": ExamReport,
        "encounter": Encounter,
        "prescription": Prescription,
    }
    ref_clauses = [
        and_(
            ReferralClinicalRef.ref_type == ref_type,
            ~ReferralClinicalRef.ref_id.in_(db.query(target.id)),
        )
        for ref_type, target in ref_targets.items()
    ]
    q = db.query(ReferralClinicalRef).filter(or_(*ref_clauses))
    checks.append(_orphan_check("referral_clinical_ref", q))

    return {"checks": checks, "total_orphans": sum(c["orphan_count"] for c in checks)}
