"""质量安全（浙江省指南 M9）：不良事件上报、病历质控、院感上报。

- AdverseEvent：全员可上报（可选匿名），管理层审核 → 整改留痕闭环；
- RecordQc：对就诊记录/病案首页抽检评分（0-100 自动定级甲/乙/丙），缺陷项记录；
- InfectionReport：院感病例上报与核实（确认/排除），确认例入区域安全提醒口径。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import (
    AdverseEvent,
    CaseSummary,
    Encounter,
    InfectionReport,
    Organization,
    Patient,
    RecordQc,
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
    status_code=201,
    # 病人安全：全部临床与经办角色均可上报（无惩罚上报文化）
    dependencies=[Depends(require_roles("doctor", "pharmacist", "public_health", "operator", "director"))],
)
def report_adverse_event(
    body: AdverseEventCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
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


@router.get("/adverse-events")
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
    if event.status != "reviewed":
        raise HTTPException(status_code=409, detail="须先审核后方可登记整改")
    event.status = "rectified"
    event.rectify_note = body.note
    event.rectified_by = user.full_name or user.username
    event.rectified_at = utcnow()
    db.commit()
    return _adverse_out(event)


@router.get("/adverse-events-stats")
def adverse_event_stats(db: Session = Depends(get_db)):
    """不良事件统计：按类型/等级分布与整改闭环率。"""
    total = db.query(func.count(AdverseEvent.id)).scalar() or 0
    rectified = (
        db.query(func.count(AdverseEvent.id)).filter(AdverseEvent.status == "rectified").scalar()
        or 0
    )
    by_type = dict(
        db.query(AdverseEvent.event_type, func.count(AdverseEvent.id))
        .group_by(AdverseEvent.event_type)
        .all()
    )
    by_level = dict(
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


@router.get("/record-qc")
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


@router.get("/record-qc-stats")
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
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # 院感上报=医师/公卫
)
def create_infection_report(
    body: InfectionReportCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
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


@router.get("/infection-reports")
def list_infection_reports(
    status: str | None = None, org_id: int | None = None, db: Session = Depends(get_db)
):
    q = db.query(InfectionReport)
    if status:
        q = q.filter(InfectionReport.status == status)
    if org_id is not None:
        q = q.filter(InfectionReport.org_id == org_id)
    return [_infection_out(r) for r in q.order_by(InfectionReport.id.desc()).limit(200).all()]


@router.post(
    "/infection-reports/{report_id}/verify",
    dependencies=[Depends(require_roles("public_health", "director"))],  # 核实=院感/公卫管理
)
def verify_infection_report(report_id: int, confirmed: bool, db: Session = Depends(get_db)):
    report = db.get(InfectionReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="院感报告不存在")
    if report.status != "reported":
        raise HTTPException(status_code=409, detail="该报告已核实")
    report.status = "confirmed" if confirmed else "excluded"
    db.commit()
    return _infection_out(report)


@router.get("/infection-stats")
def infection_stats(db: Session = Depends(get_db)):
    """院感统计：确认例数、按部位分布（区域安全提醒数据源，#70）。"""
    confirmed = (
        db.query(func.count(InfectionReport.id))
        .filter(InfectionReport.status == "confirmed")
        .scalar()
        or 0
    )
    by_site = dict(
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
