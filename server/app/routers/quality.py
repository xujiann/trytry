"""质量安全（浙江省指南 M9）：不良事件上报、病历质控、院感上报。

- AdverseEvent：全员可上报（可选匿名），管理层审核 → 整改留痕闭环；
- RecordQc：对就诊记录/病案首页抽检评分（0-100 自动定级甲/乙/丙），缺陷项记录；
- InfectionReport：院感病例上报与核实（确认/排除），确认例入区域安全提醒口径。

块2 深化：结构化电子病历 MedicalRecord + 环节质控规则 RecordQcRule
（提交即评分，缺陷清单实时返回；qc-summary 按机构/医师统计甲乙丙分布）。
"""
import re
from datetime import datetime, timedelta

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..concurrency import ensure_present, insert_if_absent
from ..visibility import assert_obj_org_writable, assert_org_writable, scope_org_list
from ..database import get_db
from ..deps import get_current_user, require_admin, require_roles, resolve_org_scope, row_dict
from ..models import (
    Admission,
    AdverseEvent,
    CaseSummary,
    EmergencyCase,
    Encounter,
    ExamReport,
    ExamRequest,
    InfectionReport,
    MedicalRecord,
    Organization,
    Patient,
    RecordQc,
    RecordQcRule,
    SurgeryRecord,
    SurgeryRequest,
    User,
    utcnow,
)

router = APIRouter(prefix="/api/quality", tags=["质量安全"], dependencies=[Depends(get_current_user)])


# ---------- 不良事件 ----------

_EVENT_TYPES = {"medication", "device", "fall", "pressure_sore", "transfusion", "identification", "other"}


class AdverseEventCreate(BaseModel):
    org_id: int
    event_type: str
    level: str = Field(pattern="^(I|II|III|IV)$")
    description: str = Field(min_length=1, max_length=2048)
    anonymous: bool = False


# ============================================================ 响应契约
#
# 这个模块的分数**全是 INTEGER 列**（`MedicalRecord.qc_score` /
# `RecordQc.score` / `RecordQcRule.deduct_points`），`score = max(0, 100 -
# deducted)` 也是整数运算——声明 int 才是原样。
# 均值类（`avg_score` = `round(x, 1)`、各种 `*_pct` = `round(x, 2)`）才是 float，
# 且它们的兜底字面量也写成 `0.0`，两条分支同型。


class AdverseEventOut(BaseModel):
    id: int
    org_id: int | None
    event_type: str
    level: str
    anonymous: bool
    reporter_name: str
    description: str
    status: str
    review_note: str
    # 存的是**姓名**（VARCHAR(64)），不是用户 id 外键；未审时是空串不是 null
    reviewed_by: str
    rectify_note: str
    rectified_by: str
    created_at: str


class AdverseStatsOut(BaseModel):
    total: int
    rectified: int
    closed_loop_pct: float
    by_type: dict[str, int]
    by_level: dict[str, int]


class RecordQcOut(BaseModel):
    id: int
    target_type: str
    target_id: int
    score: int
    grade: str
    # `RecordQc.defects` 是 **VARCHAR(1024)**——存的是缺陷描述文本，不是数组。
    # （与 `MedicalRecord.qc_defects` 那个 JSON 列同名不同型，别混。）
    defects: str
    qc_by: str


class RecordQcStatsOut(BaseModel):
    total: int
    avg_score: float
    grade_a_pct: float
    with_defects: int


class InfectionReportOut(BaseModel):
    id: int
    org_id: int | None
    patient_id: int
    infection_site: str
    pathogen: str
    note: str
    status: str
    reported_by: str
    report_date: str


class InfectionStatsOut(BaseModel):
    confirmed: int
    pending_verify: int
    by_site: dict[str, int]


class MedicalRecordOut(BaseModel):
    """病历。中间六个字段来自 `**{name: getattr(r, name) for name in
    RECORD_FIELDS}` 展开——`RECORD_FIELDS` 是模块级常量（六项，固定顺序），
    不是运行期数据，所以能逐字段建模而不是宽字典。"""

    id: int
    encounter_id: int
    org_id: int | None
    doctor_name: str
    chief_complaint: str
    present_illness: str
    past_history: str
    physical_exam: str
    diagnosis_basis: str
    treatment_plan: str
    qc_score: int
    qc_grade: str
    created_at: str
    updated_at: str


class QcDefectOut(BaseModel):
    rule_code: str
    rule_name: str
    field: str
    field_name: str
    rule_type: str
    rule_type_name: str
    message: str
    deduct_points: int


class QcResultOut(BaseModel):
    score: int
    grade: str
    deducted: int
    rules_checked: int
    defects: list[QcDefectOut]


class RecordUpsertOut(BaseModel):
    created: bool
    record: MedicalRecordOut
    qc: QcResultOut


class RecordDetailOut(BaseModel):
    record: MedicalRecordOut
    defects: list[QcDefectOut]


class RecordRescoreOut(BaseModel):
    """重评分。handler 写的是 `{"record_id": …, **result}`——`record_id` 在
    **最前**，`result` 的五个键跟在后面。

    所以这里**刻意不继承** `QcResultOut`：继承会把 `record_id` 排到末尾，
    那是另一串字节。（继承对不对，取决于新字段在 handler 里是不是真的在最后。）
    """

    record_id: int
    score: int
    grade: str
    deducted: int
    rules_checked: int
    defects: list[QcDefectOut]


class RecordQcRuleOut(BaseModel):
    id: int
    code: str
    name: str
    check_field: str
    field_name: str
    rule: str
    rule_name: str
    # 规则参数，形状由 rule 类型决定
    config: dict[str, Any]
    deduct_points: int
    active: bool


class QcBucketOut(BaseModel):
    """分组桶。`key` **两种类型**：`by_org` 的是 `org_id`（int），
    `by_doctor` 的是医师姓名（str）——同一个 `group()` 建出来的两组，
    分组字段不同类型。声明成 str 会让机构分组 500。"""

    key: int | str
    name: str
    total: int
    avg_score: float
    grade_a: int
    grade_b: int
    grade_c: int
    grade_a_pct: float


class RecordQcSummaryOut(BaseModel):
    period: str
    total: int
    avg_score: float
    grade_distribution: dict[str, int]
    grade_a_pct: float
    by_org: list[QcBucketOut]
    by_doctor: list[QcBucketOut]


class ClinicalIndicatorOut(BaseModel):
    key: str
    name: str
    dimension: str
    numerator: int
    denominator: int
    rate_pct: float
    # **条件键**：只有"术前术后诊断符合率"带它（未采集台次），且位置夹在
    # rate_pct 与 caliber **中间**——去掉它剩下的顺序恰好是其余指标的顺序，
    # 故一个模型 + exclude_unset 能同时满足。
    uncollected: int | None = None
    caliber: str


class ClinicalIndicatorsOut(BaseModel):
    period: str
    org_id: int | None
    group_id: int | None
    indicators: list[ClinicalIndicatorOut]


def _adverse_out(e: AdverseEvent) -> dict:
    return {
        "id": e.id,
        "org_id": e.org_id,
        "event_type": e.event_type,
        "level": e.level,
        "anonymous": e.anonymous,
        "reporter_name": e.reporter_name,
        "description": e.description,
        "status": e.status,
        "review_note": e.review_note,
        "reviewed_by": e.reviewed_by,
        "rectify_note": e.rectify_note,
        "rectified_by": e.rectified_by,
        "created_at": e.created_at.isoformat(),
    }


@router.post(
    "/adverse-events",
    response_model=AdverseEventOut,
    status_code=201,
    # 病人安全：全部临床与经办角色均可上报（无惩罚上报文化）
    dependencies=[Depends(require_roles("doctor", "pharmacist", "public_health", "operator", "director"))],
)
def report_adverse_event(
    body: AdverseEventCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    assert_org_writable(db, user, body.org_id)
    if body.event_type not in _EVENT_TYPES:
        raise HTTPException(status_code=422, detail="未知不良事件类型")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    event = AdverseEvent(
        **body.model_dump(),
        # 匿名上报不落报告人（匿名可选：鼓励上报）
        reporter_name="" if body.anonymous else (user.full_name or user.username),
    )
    db.add(event)
    db.commit()
    return _adverse_out(event)


@router.get("/adverse-events", response_model=list[AdverseEventOut])
def list_adverse_events(
    status: str | None = None, event_type: str | None = None, db: Session = Depends(get_db)
):
    q = db.query(AdverseEvent)
    if status:
        q = q.filter(AdverseEvent.status == status)
    if event_type:
        q = q.filter(AdverseEvent.event_type == event_type)
    return [_adverse_out(e) for e in q.order_by(AdverseEvent.id.desc()).limit(200).all()]


class NoteBody(BaseModel):
    note: str = Field(min_length=1, max_length=1024)


@router.post(
    "/adverse-events/{event_id}/review",
    response_model=AdverseEventOut,
    dependencies=[Depends(require_roles("director"))],  # 审核=管理层
)
def review_adverse_event(
    event_id: int,
    body: NoteBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    event = db.get(AdverseEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="不良事件不存在")
    assert_obj_org_writable(db, user, event)
    if event.status != "reported":
        raise HTTPException(status_code=409, detail=f"当前状态 {event.status} 不可审核")
    event.status = "reviewed"
    event.review_note = body.note
    event.reviewed_by = user.full_name or user.username
    event.reviewed_at = utcnow()
    db.commit()
    return _adverse_out(event)


@router.post(
    "/adverse-events/{event_id}/rectify",
    response_model=AdverseEventOut,
    dependencies=[Depends(require_roles("director", "operator"))],  # 整改登记=管理层/经办
)
def rectify_adverse_event(
    event_id: int,
    body: NoteBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    event = db.get(AdverseEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="不良事件不存在")
    assert_obj_org_writable(db, user, event)
    if event.status != "reviewed":
        raise HTTPException(status_code=409, detail="须先审核后方可登记整改")
    event.status = "rectified"
    event.rectify_note = body.note
    event.rectified_by = user.full_name or user.username
    event.rectified_at = utcnow()
    db.commit()
    return _adverse_out(event)


@router.get("/adverse-events-stats", response_model=AdverseStatsOut)
def adverse_event_stats(db: Session = Depends(get_db)):
    """不良事件统计：按类型/等级分布与整改闭环率。"""
    total = db.query(func.count(AdverseEvent.id)).scalar() or 0
    rectified = (
        db.query(func.count(AdverseEvent.id)).filter(AdverseEvent.status == "rectified").scalar()
        or 0
    )
    by_type = row_dict(
        db.query(AdverseEvent.event_type, func.count(AdverseEvent.id))
        .group_by(AdverseEvent.event_type)
        .all()
    )
    by_level = row_dict(
        db.query(AdverseEvent.level, func.count(AdverseEvent.id)).group_by(AdverseEvent.level).all()
    )
    return {
        "total": total,
        "rectified": rectified,
        "closed_loop_pct": round(rectified * 100.0 / total, 2) if total else 0.0,
        "by_type": by_type,
        "by_level": by_level,
    }


# ---------- 病历质控 ----------


class RecordQcCreate(BaseModel):
    target_type: str = Field(pattern="^(encounter|case_summary)$")
    target_id: int
    score: int = Field(ge=0, le=100)
    defects: str = ""


def _grade(score: int) -> str:
    return "甲" if score >= 90 else ("乙" if score >= 80 else "丙")


@router.post(
    "/record-qc",
    response_model=RecordQcOut,
    status_code=201,
    dependencies=[Depends(require_roles("director", "doctor"))],  # 病历质控=管理层/医师质控员
)
def create_record_qc(
    body: RecordQcCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    target_model = Encounter if body.target_type == "encounter" else CaseSummary
    if db.get(target_model, body.target_id) is None:
        raise HTTPException(status_code=404, detail="抽检对象不存在")
    qc = RecordQc(
        **body.model_dump(), grade=_grade(body.score), qc_by=user.full_name or user.username
    )
    db.add(qc)
    db.commit()
    return {
        "id": qc.id,
        "target_type": qc.target_type,
        "target_id": qc.target_id,
        "score": qc.score,
        "grade": qc.grade,
        "defects": qc.defects,
        "qc_by": qc.qc_by,
    }


@router.get("/record-qc", response_model=list[RecordQcOut])
def list_record_qc(
    target_type: str | None = None, grade: str | None = None, db: Session = Depends(get_db)
):
    q = db.query(RecordQc)
    if target_type:
        q = q.filter(RecordQc.target_type == target_type)
    if grade:
        q = q.filter(RecordQc.grade == grade)
    return [
        {
            "id": r.id,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "score": r.score,
            "grade": r.grade,
            "defects": r.defects,
            "qc_by": r.qc_by,
        }
        for r in q.order_by(RecordQc.id.desc()).limit(200).all()
    ]


@router.get("/record-qc-stats", response_model=RecordQcStatsOut)
def record_qc_stats(db: Session = Depends(get_db)):
    """病历质控统计：抽检量、均分、甲级率、缺陷病历数。"""
    total = db.query(func.count(RecordQc.id)).scalar() or 0
    avg_score = db.query(func.avg(RecordQc.score)).scalar()
    grade_a = db.query(func.count(RecordQc.id)).filter(RecordQc.grade == "甲").scalar() or 0
    with_defects = (
        db.query(func.count(RecordQc.id)).filter(RecordQc.defects != "").scalar() or 0
    )
    return {
        "total": total,
        "avg_score": round(avg_score, 1) if avg_score is not None else 0.0,
        "grade_a_pct": round(grade_a * 100.0 / total, 2) if total else 0.0,
        "with_defects": with_defects,
    }


# ---------- 院感上报 ----------

_INFECTION_SITES = {"respiratory", "surgical_site", "urinary", "bloodstream", "gastrointestinal", "other"}


class InfectionReportCreate(BaseModel):
    org_id: int
    patient_id: int
    infection_site: str
    pathogen: str = ""
    note: str = ""
    report_date: str = ""


def _infection_out(r: InfectionReport) -> dict:
    return {
        "id": r.id,
        "org_id": r.org_id,
        "patient_id": r.patient_id,
        "infection_site": r.infection_site,
        "pathogen": r.pathogen,
        "note": r.note,
        "status": r.status,
        "reported_by": r.reported_by,
        "report_date": r.report_date,
    }


@router.post(
    "/infection-reports",
    response_model=InfectionReportOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # 院感上报=医师/公卫
)
def create_infection_report(
    body: InfectionReportCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_org_writable(db, user, body.org_id)
    if body.infection_site not in _INFECTION_SITES:
        raise HTTPException(status_code=422, detail="未知感染部位")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    report = InfectionReport(**body.model_dump(), reported_by=user.full_name or user.username)
    db.add(report)
    db.commit()
    return _infection_out(report)


@router.get("/infection-reports", response_model=list[InfectionReportOut])
def list_infection_reports(
    status: str | None = None, org_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    q = db.query(InfectionReport)
    if status:
        q = q.filter(InfectionReport.status == status)
    q = scope_org_list(db, user, q, InfectionReport, org_id)
    return [_infection_out(r) for r in q.order_by(InfectionReport.id.desc()).limit(200).all()]


@router.post(
    "/infection-reports/{report_id}/verify",
    response_model=InfectionReportOut,
    dependencies=[Depends(require_roles("public_health", "director"))],  # 核实=院感/公卫管理
)
def verify_infection_report(report_id: int, confirmed: bool, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    report = db.get(InfectionReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="院感报告不存在")
    assert_obj_org_writable(db, user, report)
    if report.status != "reported":
        raise HTTPException(status_code=409, detail="该报告已核实")
    report.status = "confirmed" if confirmed else "excluded"
    db.commit()
    return _infection_out(report)


@router.get("/infection-stats", response_model=InfectionStatsOut)
def infection_stats(db: Session = Depends(get_db)):
    """院感统计：确认例数、按部位分布（区域安全提醒数据源，#70）。"""
    confirmed = (
        db.query(func.count(InfectionReport.id))
        .filter(InfectionReport.status == "confirmed")
        .scalar()
        or 0
    )
    by_site = row_dict(
        db.query(InfectionReport.infection_site, func.count(InfectionReport.id))
        .filter(InfectionReport.status == "confirmed")
        .group_by(InfectionReport.infection_site)
        .all()
    )
    pending = (
        db.query(func.count(InfectionReport.id))
        .filter(InfectionReport.status == "reported")
        .scalar()
        or 0
    )
    return {"confirmed": confirmed, "pending_verify": pending, "by_site": by_site}


# ---------------------------------------------------------------------------
# 块2：结构化电子病历与环节质控
#
# 与上方「病历质控」（RecordQc）的区别：上者为事后**抽检**打分（人工给分），
# 本节为病历书写**环节**的实时规则质控（提交即评分，缺陷清单同步返回）。
# 分级口径按环节质控要求：≥90 甲、≥75 乙、其余丙。
# ---------------------------------------------------------------------------

# 结构化病历字段 → 中文名（缺陷清单展示用）
RECORD_FIELDS = {
    "chief_complaint": "主诉",
    "present_illness": "现病史",
    "past_history": "既往史",
    "physical_exam": "体格检查",
    "diagnosis_basis": "诊断依据",
    "treatment_plan": "治疗方案",
}
# 引擎派生字段（非病历表列，由就诊上下文计算）
DERIVED_FIELDS = {"critical_disposal": "危急值处置记录", "case_summary": "病案首页"}
RECORD_RULE_TYPES = {
    "required": "必填",
    "min_length": "字数下限",
    "max_length": "字数上限",
    "keyword_present": "要点关键词",
}


def record_grade(score: int) -> str:
    """环节质控分级：≥90 甲级、≥75 乙级、其余丙级。"""
    return "甲" if score >= 90 else ("乙" if score >= 75 else "丙")


def _record_context(db: Session, record: MedicalRecord) -> tuple[dict, dict]:
    """汇集被检字段（病历字段 + 派生字段）与规则触发条件。"""
    fields = {name: (getattr(record, name) or "") for name in RECORD_FIELDS}
    # 危急值处置可写于治疗方案或现病史，合并后统一检索关键词
    fields["critical_disposal"] = f"{record.treatment_plan or ''} {record.present_illness or ''}"

    encounter = db.get(Encounter, record.encounter_id)
    patient_id = encounter.patient_id if encounter else None
    has_critical = (
        db.query(ExamReport.id)
        .join(ExamRequest, ExamReport.request_id == ExamRequest.id)
        .filter(ExamRequest.patient_id == patient_id, ExamReport.critical.is_(True))
        .first()
        is not None
    )
    admission = (
        db.query(Admission)
        .filter(Admission.patient_id == patient_id, Admission.status == "discharged")
        .order_by(Admission.id.desc())
        .first()
    )
    summary = (
        db.query(CaseSummary).filter(CaseSummary.admission_id == admission.id).first()
        if admission
        else None
    )
    fields["case_summary"] = summary.discharge_diagnosis if summary else ""
    conditions = {
        "has_critical_report": has_critical,
        "inpatient_discharged": admission is not None,
    }
    return fields, conditions


def _check_record_rule(rule: RecordQcRule, value: str) -> str:
    """单条规则判定：返回缺陷说明，合规返回空串。"""
    text = (value or "").strip()
    if rule.rule == "required":
        return "未填写" if not text else ""
    if rule.rule == "min_length":
        minimum = int(rule.config.get("min", 0))
        return f"仅 {len(text)} 字，少于要求的 {minimum} 字" if len(text) < minimum else ""
    if rule.rule == "max_length":
        maximum = int(rule.config.get("max", 0))
        return f"共 {len(text)} 字，超出上限 {maximum} 字" if len(text) > maximum else ""
    if rule.rule == "keyword_present":
        keywords = rule.config.get("keywords", [])
        if any(k in text for k in keywords):
            return ""
        return f"未体现要点（应含以下之一：{'、'.join(keywords)}）"
    return ""  # 未知规则类型不判缺陷（规则库演进期兼容）


def evaluate_record(db: Session, record: MedicalRecord) -> dict:
    """环节质控评分：逐条启用规则判定，命中即扣分，返回缺陷清单与等级。"""
    fields, conditions = _record_context(db, record)
    rules = (
        db.query(RecordQcRule)
        .filter(RecordQcRule.active.is_(True))
        .order_by(RecordQcRule.code)
        .all()
    )
    defects, deducted = [], 0
    for rule in rules:
        condition = (rule.config or {}).get("condition")
        if condition and not conditions.get(condition, False):
            continue  # 条件未触发：本次不参与评分（不计分母也不扣分）
        message = _check_record_rule(rule, fields.get(rule.check_field, ""))
        if message:
            deducted += rule.deduct_points
            defects.append(
                {
                    "rule_code": rule.code,
                    "rule_name": rule.name,
                    "field": rule.check_field,
                    "field_name": RECORD_FIELDS.get(rule.check_field)
                    or DERIVED_FIELDS.get(rule.check_field, rule.check_field),
                    "rule_type": rule.rule,
                    "rule_type_name": RECORD_RULE_TYPES.get(rule.rule, rule.rule),
                    "message": message,
                    "deduct_points": rule.deduct_points,
                }
            )
    score = max(0, 100 - deducted)
    return {
        "score": score,
        "grade": record_grade(score),
        "deducted": deducted,
        "rules_checked": len(rules),
        "defects": defects,
    }


def _apply_qc(db: Session, record: MedicalRecord) -> dict:
    """评分并把结果快照回写病历（统计口径唯一来源，调用方负责 commit）。"""
    result = evaluate_record(db, record)
    record.qc_score = result["score"]
    record.qc_grade = result["grade"]
    record.qc_defects = result["defects"]
    return result


class MedicalRecordIn(BaseModel):
    encounter_id: int
    chief_complaint: str = Field(default="", max_length=256)
    present_illness: str = Field(default="", max_length=2048)
    past_history: str = Field(default="", max_length=1024)
    physical_exam: str = Field(default="", max_length=1024)
    diagnosis_basis: str = Field(default="", max_length=1024)
    treatment_plan: str = Field(default="", max_length=1024)


def _record_out(r: MedicalRecord) -> dict:
    return {
        "id": r.id,
        "encounter_id": r.encounter_id,
        "org_id": r.org_id,
        "doctor_name": r.doctor_name,
        **{name: getattr(r, name) for name in RECORD_FIELDS},
        "qc_score": r.qc_score,
        "qc_grade": r.qc_grade,
        "created_at": r.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat(),
    }


@router.post(
    "/records",
    response_model=RecordUpsertOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor"))],  # 病历书写=医师
)
def upsert_medical_record(
    body: MedicalRecordIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """提交/更新结构化病历：同一就诊一份，**同步返回缺陷清单与扣分**。"""
    encounter = db.get(Encounter, body.encounter_id)
    if encounter is None:
        raise HTTPException(status_code=404, detail="就诊记录不存在")
    record = (
        db.query(MedicalRecord).filter(MedicalRecord.encounter_id == body.encounter_id).first()
    )
    created = record is None
    if created:
        # 插入后还要跑质控、再与病历同一次提交，故不用会自己 commit 的 upsert_unique。
        # 两个请求同时提交同一次就诊的病历，都查不到就都去插——撞 encounter_id 唯一约束。
        created = insert_if_absent(
            db,
            MedicalRecord(
                encounter_id=body.encounter_id,
                org_id=encounter.org_id,
                doctor_name=user.full_name or user.username,
                created_by=user.id,
            ),
        )
        record = (
            db.query(MedicalRecord)
            .filter(MedicalRecord.encounter_id == body.encounter_id)
            .first()
        )
    record = ensure_present(record, "病历")
    for name in RECORD_FIELDS:
        setattr(record, name, getattr(body, name))
    record.updated_at = utcnow()
    db.flush()
    result = _apply_qc(db, record)
    db.commit()
    db.refresh(record)
    return {"created": created, "record": _record_out(record), "qc": result}


@router.get("/records", response_model=list[MedicalRecordOut])
def list_medical_records(
    encounter_id: int | None = None,
    org_id: int | None = None,
    grade: str | None = None,
    doctor_name: str | None = None,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    q = db.query(MedicalRecord)
    if encounter_id is not None:
        q = q.filter(MedicalRecord.encounter_id == encounter_id)
    q = scope_org_list(db, user, q, MedicalRecord, org_id)
    if grade:
        q = q.filter(MedicalRecord.qc_grade == grade)
    if doctor_name:
        q = q.filter(MedicalRecord.doctor_name == doctor_name)
    return [_record_out(r) for r in q.order_by(MedicalRecord.id.desc()).limit(200).all()]


def _grade_bucket() -> dict:
    return {"甲": 0, "乙": 0, "丙": 0}


@router.get("/records/qc-summary", response_model=RecordQcSummaryOut)
def record_qc_summary(
    period: str | None = None,
    org_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """全量环节质控统计：按机构/医师的甲乙丙分布与平均分（period=YYYY-MM 过滤）。"""
    if period is not None and not re.fullmatch(r"\d{4}-\d{2}", period):
        raise HTTPException(status_code=422, detail="period 格式须为 YYYY-MM")
    q = db.query(MedicalRecord)
    # 质控统计是管理口径：牵头医院要看得到片区的病历质量分布
    q = scope_org_list(db, user, q, MedicalRecord, org_id, stats=True)
    records = [
        r
        for r in q.order_by(MedicalRecord.id.desc()).limit(5000).all()
        # 月份口径与运营月报一致：按病历创建月份归属
        if period is None or (r.created_at and r.created_at.strftime("%Y-%m") == period)
    ]
    org_names = row_dict(db.query(Organization.id, Organization.name).all())

    def group(key_fn, label_fn) -> list[dict]:
        buckets: dict = {}
        for r in records:
            bucket = buckets.setdefault(
                key_fn(r), {"grades": _grade_bucket(), "total": 0, "score_sum": 0}
            )
            bucket["grades"][r.qc_grade] = bucket["grades"].get(r.qc_grade, 0) + 1
            bucket["total"] += 1
            bucket["score_sum"] += r.qc_score
        return [
            {
                "key": key,
                "name": label_fn(key),
                "total": b["total"],
                "avg_score": round(b["score_sum"] / b["total"], 1) if b["total"] else 0.0,
                "grade_a": b["grades"]["甲"],
                "grade_b": b["grades"]["乙"],
                "grade_c": b["grades"]["丙"],
                "grade_a_pct": round(b["grades"]["甲"] * 100.0 / b["total"], 2) if b["total"] else 0.0,
            }
            for key, b in sorted(buckets.items(), key=lambda kv: -kv[1]["total"])
        ]

    grades = _grade_bucket()
    for r in records:
        grades[r.qc_grade] = grades.get(r.qc_grade, 0) + 1
    total = len(records)
    return {
        "period": period or "累计",
        "total": total,
        "avg_score": round(sum(r.qc_score for r in records) / total, 1) if total else 0.0,
        "grade_distribution": grades,
        "grade_a_pct": round(grades["甲"] * 100.0 / total, 2) if total else 0.0,
        "by_org": group(lambda r: r.org_id, lambda k: org_names.get(k, "")),
        "by_doctor": group(lambda r: r.doctor_name, lambda k: k),
    }


@router.get("/records/{record_id}", response_model=RecordDetailOut)
def get_medical_record(record_id: int, db: Session = Depends(get_db)):
    record = db.get(MedicalRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="病历不存在")
    return {"record": _record_out(record), "defects": record.qc_defects or []}


@router.get("/records/{record_id}/qc", response_model=RecordRescoreOut)
def rescore_medical_record(record_id: int, db: Session = Depends(get_db)):
    """按当前规则库重新评分（规则调整或病历修正后复评），结果回写快照。"""
    record = db.get(MedicalRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="病历不存在")
    result = _apply_qc(db, record)
    db.commit()
    return {"record_id": record.id, **result}


# ---------- 环节质控规则库 ----------


class RecordQcRuleUpdate(BaseModel):
    active: bool | None = None
    deduct_points: int | None = Field(default=None, ge=0, le=100)


def _record_rule_out(r: RecordQcRule) -> dict:
    return {
        "id": r.id,
        "code": r.code,
        "name": r.name,
        "check_field": r.check_field,
        "field_name": RECORD_FIELDS.get(r.check_field)
        or DERIVED_FIELDS.get(r.check_field, r.check_field),
        "rule": r.rule,
        "rule_name": RECORD_RULE_TYPES.get(r.rule, r.rule),
        "config": r.config,
        "deduct_points": r.deduct_points,
        "active": r.active,
    }


@router.get("/record-qc-rules", response_model=list[RecordQcRuleOut])
def list_record_qc_rules(active: bool | None = None, db: Session = Depends(get_db)):
    q = db.query(RecordQcRule)
    if active is not None:
        q = q.filter(RecordQcRule.active.is_(active))
    return [_record_rule_out(r) for r in q.order_by(RecordQcRule.code).all()]


@router.patch("/record-qc-rules/{rule_id}", response_model=RecordQcRuleOut, dependencies=[Depends(require_admin)])
def update_record_qc_rule(rule_id: int, body: RecordQcRuleUpdate, db: Session = Depends(get_db)):
    rule = db.get(RecordQcRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="质控规则不存在")
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(rule, field, value)
    db.commit()
    return _record_rule_out(rule)


# ============================================================================
# 医疗质量三维指标（指南 #14 / #52）
#
# 指南把质量评价拆成"诊断准确 / 治疗有效 / 痛苦最小"三维。本模块只做**能从
# 已有业务数据算出来的那部分**，算不了的宁可不出数：一个靠猜的治愈率比没有
# 治愈率更危险，它会被写进考核。
# ============================================================================

# 转归取值来自 CaseSummary.outcome / SurgeryRecord.outcome，四档固定
CURED_OUTCOMES = ("治愈", "好转")
DEATH_OUTCOMES = ("死亡",)


def _normalize_diagnosis(text: str) -> str:
    """诊断名归一化：去空白、去括号补充说明、统一大小写。

    "急性心肌梗死（PCI术后）" 与 "急性心肌梗死" 应判为符合——括号里通常是
    治疗方式或分期补充，不是另一个诊断。这是**粗口径**，够用于院内监测，
    不能拿去做病案首页上报；真要精确必须走 ICD 编码比对，而门诊侧目前
    只有诊断名没有编码。
    """
    cleaned = re.sub(r"[（(].*?[)）]", "", text or "")
    return re.sub(r"\s+", "", cleaned).strip().lower()


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator * 100, 2) if denominator else 0.0


@router.get("/clinical-indicators", response_model=ClinicalIndicatorsOut,
            response_model_exclude_unset=True)
def clinical_indicators(
    period: str | None = None,
    org_id: int | None = None,
    group_id: int | None = None,
    db: Session = Depends(get_db),
):
    """医疗质量指标：诊断符合率、治愈好转率、死亡率、抢救成功率。

    `period` 为 YYYY-MM，省略即全期。每项都同时返回分子分母，
    只给一个百分比没法核对，也没法判断样本量小到不该看。
    """
    start_dt = end_dt = None
    if period:
        try:
            start = datetime.strptime(period + "-01", "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="period 须为 YYYY-MM 格式") from None
        start_dt = start
        end_dt = (start.replace(day=28) + timedelta(days=4)).replace(day=1)

    # ---- 入出院诊断符合率 + 治愈好转率 + 死亡率（数据源：病案首页）----
    summaries = db.query(CaseSummary, Admission).join(
        Admission, CaseSummary.admission_id == Admission.id
    )
    scope = resolve_org_scope(db, group_id, org_id)
    if scope is not None:
        summaries = summaries.filter(Admission.org_id.in_(scope))
    if start_dt is not None:
        summaries = summaries.filter(
            CaseSummary.created_at >= start_dt, CaseSummary.created_at < end_dt
        )
    rows = summaries.all()
    admit_match = sum(
        1
        for cs, adm in rows
        if _normalize_diagnosis(adm.diagnosis_name) == _normalize_diagnosis(cs.discharge_diagnosis)
    )
    cured = sum(1 for cs, _ in rows if cs.outcome in CURED_OUTCOMES)
    died = sum(1 for cs, _ in rows if cs.outcome in DEATH_OUTCOMES)

    # ---- 术前术后诊断符合率（数据源：手术记录）----
    surgeries = db.query(SurgeryRecord).join(
        SurgeryRequest, SurgeryRecord.request_id == SurgeryRequest.id
    )
    if scope is not None:
        surgeries = surgeries.filter(SurgeryRequest.org_id.in_(scope))
    if start_dt is not None:
        surgeries = surgeries.filter(
            SurgeryRecord.created_at >= start_dt, SurgeryRecord.created_at < end_dt
        )
    # 一次取回，下面的诊断符合率与手术质量两组指标共用，不重复打库
    all_surgeries = surgeries.all()
    surgery_total = len(all_surgeries)
    # 两项诊断都填了才纳入分母——没填的是"未采集"，不是"不符合"
    surgery_rows = [
        r for r in all_surgeries if r.preop_diagnosis.strip() and r.postop_diagnosis.strip()
    ]
    surgery_match = sum(
        1
        for r in surgery_rows
        if _normalize_diagnosis(r.preop_diagnosis) == _normalize_diagnosis(r.postop_diagnosis)
    )

    # ---- 抢救成功率（数据源：急救病例转归）----
    rescues = db.query(EmergencyCase).filter(EmergencyCase.rescue_outcome != "")
    if scope is not None:
        rescues = rescues.filter(EmergencyCase.dest_org_id.in_(scope))
    if start_dt is not None:
        rescues = rescues.filter(
            EmergencyCase.created_at >= start_dt, EmergencyCase.created_at < end_dt
        )
    rescue_rows = rescues.all()
    rescue_success = sum(1 for r in rescue_rows if r.rescue_outcome == "success")

    # ---- 手术质量（数据源：手术申请标记 + 术中记录）----
    complication_cases = sum(1 for r in all_surgeries if r.complications.strip())
    unplanned = (
        db.query(SurgeryRequest)
        .filter(SurgeryRequest.unplanned_return.is_(True), SurgeryRequest.status == "completed")
    )
    if scope is not None:
        unplanned = unplanned.filter(SurgeryRequest.org_id.in_(scope))
    if start_dt is not None:
        unplanned = unplanned.filter(
            SurgeryRequest.created_at >= start_dt, SurgeryRequest.created_at < end_dt
        )
    unplanned_count = unplanned.count()

    return {
        "period": period or "全期",
        "org_id": org_id,
        "group_id": group_id,
        "indicators": [
            {
                "key": "admit_discharge_match",
                "name": "入出院诊断符合率",
                "dimension": "诊断准确",
                "numerator": admit_match,
                "denominator": len(rows),
                "rate_pct": _rate(admit_match, len(rows)),
                "caliber": "出院诊断与入院诊断一致的出院人次 ÷ 出院人次（诊断名归一化比对）",
            },
            {
                "key": "preop_postop_match",
                "name": "术前术后诊断符合率",
                "dimension": "诊断准确",
                "numerator": surgery_match,
                "denominator": len(surgery_rows),
                "rate_pct": _rate(surgery_match, len(surgery_rows)),
                "uncollected": surgery_total - len(surgery_rows),
                "caliber": "术后诊断与术前诊断一致的手术台次 ÷ 已填两项诊断的手术台次",
            },
            {
                "key": "cure_improve",
                "name": "治愈好转率",
                "dimension": "治疗有效",
                "numerator": cured,
                "denominator": len(rows),
                "rate_pct": _rate(cured, len(rows)),
                "caliber": "转归为治愈或好转的出院人次 ÷ 出院人次",
            },
            {
                "key": "mortality",
                "name": "住院死亡率",
                "dimension": "治疗有效",
                "numerator": died,
                "denominator": len(rows),
                "rate_pct": _rate(died, len(rows)),
                "caliber": "转归为死亡的出院人次 ÷ 出院人次",
            },
            {
                "key": "rescue_success",
                "name": "抢救成功率",
                "dimension": "治疗有效",
                "numerator": rescue_success,
                "denominator": len(rescue_rows),
                "rate_pct": _rate(rescue_success, len(rescue_rows)),
                "caliber": "抢救成功例数 ÷ 已判定转归的抢救例数（未判定的不计入分母）",
            },
            {
                "key": "surgery_complication",
                "name": "手术并发症发生率",
                "dimension": "手术质量",
                "numerator": complication_cases,
                "denominator": surgery_total,
                "rate_pct": _rate(complication_cases, surgery_total),
                # 这里与诊断符合率的口径**不同**，必须写清楚：并发症栏留空按"无"
                # 计，因为绝大多数手术确实没有并发症，医师不会专门去填"无"。
                # 代价是漏填与真无区分不开，所以这个数只能低估、不会高估。
                "caliber": "术中记录填写了并发症的台次 ÷ 已出术中记录台次"
                           "（并发症栏留空按无计，故本指标只会低估）",
            },
            {
                "key": "unplanned_return",
                "name": "非计划重返手术室率",
                "dimension": "手术质量",
                "numerator": unplanned_count,
                "denominator": surgery_total,
                "rate_pct": _rate(unplanned_count, surgery_total),
                "caliber": "医师标记为非计划重返的已完成手术 ÷ 已出术中记录台次"
                           "（不做推断：分期手术与计划内二次探查不算重返）",
            },
        ],
    }
