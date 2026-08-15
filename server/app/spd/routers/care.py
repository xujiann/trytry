"""全域慢专病 · 服务域：监测数据、评估、干预、宣教、复诊、上报、健康处方、在线咨询。

对应招标文件：成员端 #7/#12/#14/#15/#16、个案管理师端 #6/#9/#14、
医生移动端 #8/#9/#12/#13、患者端 #4/#7/#10/#12。
"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ...clock import now_naive
from ...database import get_db
from ...datetypes import DateStr, OptionalDateStr
from ...deps import get_current_user, paginate, require_roles, resolve_business_date
from ..platform import Patient, User
from ..models import (
    SpdAssessment,
    SpdCaseReport,
    SpdCaseReportTask,
    SpdConsult,
    SpdConsultMessage,
    SpdDevice,
    SpdEduMaterial,
    SpdEduPush,
    SpdEnrollment,
    SpdHealthPrescription,
    SpdIntervention,
    SpdInterventionTemplate,
    SpdMeasurement,
    SpdRevisit,
    SpdScale,
)
from ..rules import score_scale
from ..service import award_points, judge_measurement, spawn_task
from ...visibility import assert_org_writable, assert_patient_visible, visible_org_ids

router = APIRouter(
    prefix="/api/spd",
    tags=["全域慢专病·服务"],
    dependencies=[Depends(get_current_user)],
)

SERVICE_ROLES = ("doctor", "public_health", "director")


def _enrollment_of(db: Session, patient_id: int, program_code: str) -> SpdEnrollment | None:
    if not program_code:
        return None
    return (
        db.query(SpdEnrollment)
        .filter(
            SpdEnrollment.patient_id == patient_id,
            SpdEnrollment.program_code == program_code,
        )
        .first()
    )


# ============================================================ 监测数据


class MeasurementIn(BaseModel):
    patient_id: int
    metric: str = Field(min_length=1, max_length=32)
    value: float
    unit: str = Field(default="", max_length=16)
    program_code: str = Field(default="", max_length=32)
    source: str = Field(default="manual", pattern="^(manual|device|his|poct)$")
    device_sn: str = Field(default="", max_length=64)
    measured_at: str = Field(default="", max_length=19)
    note: str = Field(default="", max_length=256)


class MeasurementBatchIn(BaseModel):
    items: list[MeasurementIn] = Field(min_length=1, max_length=200)


def _measure_out(m: SpdMeasurement) -> dict:
    return {
        "id": m.id, "patient_id": m.patient_id, "program_code": m.program_code,
        "metric": m.metric, "value": m.value, "unit": m.unit, "level": m.level,
        "source": m.source, "device_sn": m.device_sn, "note": m.note,
        "measured_at": m.measured_at.isoformat(),
    }


def _record_measurement(db: Session, body: MeasurementIn, user_id: int | None) -> SpdMeasurement:
    enrollment = _enrollment_of(db, body.patient_id, body.program_code)
    stage = enrollment.stage if enrollment else ""
    level = judge_measurement(db, body.program_code, stage, body.metric, body.value)
    measured_at = now_naive()
    if body.measured_at:
        try:
            from datetime import datetime

            measured_at = datetime.fromisoformat(body.measured_at)
        except ValueError:
            raise HTTPException(status_code=422, detail="measured_at 格式须为 ISO 日期时间") from None
    record = SpdMeasurement(
        patient_id=body.patient_id, program_code=body.program_code, metric=body.metric,
        value=body.value, unit=body.unit, level=level, source=body.source,
        device_sn=body.device_sn, measured_at=measured_at, operator_id=user_id,
        note=body.note,
    )
    db.add(record)
    db.flush()
    if body.device_sn:
        device = db.query(SpdDevice).filter(SpdDevice.sn == body.device_sn).first()
        if device is not None:
            device.last_sync_at = now_naive()
    return record


@router.post("/measurements", status_code=201,
             dependencies=[Depends(require_roles(*SERVICE_ROLES, "operator"))])
def create_measurement(
    body: MeasurementIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """录入一次监测值，按管理目标即时判定等级并在异常时生成处置任务。

    异常自动建任务的门槛设在 `high`/`low` 而不是"任何非正常"：
    等级只有这三种，设成"非 normal 即建任务"是同一件事，写成范围只会
    让后来加第四种等级的人以为这里有别的讲究。
    """
    assert_patient_visible(db, user, body.patient_id, resource="spd_measurement")
    record = _record_measurement(db, body, user.id)
    enrollment = _enrollment_of(db, body.patient_id, body.program_code)
    if record.level in ("high", "low") and enrollment is not None:
        spawn_task(
            db,
            patient_id=body.patient_id,
            title=f"指标异常处置：{body.metric} {body.value}{body.unit}",
            task_type="intervention",
            enrollment=enrollment,
            due_days=3,
            priority=2,
            source="rule",
        )
    db.commit()
    return _measure_out(record)


@router.post("/measurements/batch",
             dependencies=[Depends(require_roles(*SERVICE_ROLES, "operator"))])
def batch_measurements(
    body: MeasurementBatchIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """设备批量上传（蓝牙/物联网一次回传多条）。"""
    created, abnormal = 0, 0
    for item in body.items:
        assert_patient_visible(db, user, item.patient_id, resource="spd_measurement")
        record = _record_measurement(db, item, user.id)
        created += 1
        if record.level in ("high", "low"):
            abnormal += 1
    db.commit()
    return {"created": created, "abnormal": abnormal}


@router.get("/measurements")
def list_measurements(
    response: Response,
    patient_id: int,
    metric: str | None = None,
    program_code: str | None = None,
    level: str | None = None,
    since: str = "",
    offset: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_patient_visible(db, user, patient_id, resource="spd_measurement")
    query = db.query(SpdMeasurement).filter(SpdMeasurement.patient_id == patient_id)
    for column, value in (
        (SpdMeasurement.metric, metric), (SpdMeasurement.program_code, program_code),
        (SpdMeasurement.level, level),
    ):
        if value:
            query = query.filter(column == value)
    if since:
        query = query.filter(SpdMeasurement.measured_at >= f"{since} 00:00:00")
    rows = paginate(query.order_by(SpdMeasurement.measured_at.desc()), response, offset, limit)
    return [_measure_out(m) for m in rows]


@router.get("/measurements/trend")
def measurement_trend(
    patient_id: int,
    metric: str,
    granularity: str = "day",
    days: int = 90,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """指标趋势（患者端 #7）：按日/周/月聚合，给出均值、极值与等级分布。

    聚合在 Python 侧做而不是库侧 `date_trunc`：那个函数 SQLite 没有，
    而平台要同时跑在 SQLite（开发/演示）与 PostgreSQL/国产库（生产）上。
    单个患者单个指标的量级（百条）在应用层聚合毫无压力。
    """
    assert_patient_visible(db, user, patient_id, resource="spd_measurement")
    since = now_naive() - timedelta(days=max(min(days, 730), 1))
    rows = (
        db.query(SpdMeasurement)
        .filter(
            SpdMeasurement.patient_id == patient_id,
            SpdMeasurement.metric == metric,
            SpdMeasurement.measured_at >= since,
        )
        .order_by(SpdMeasurement.measured_at)
        .all()
    )
    buckets: dict[str, list[float]] = {}
    for row in rows:
        day = row.measured_at.date()
        if granularity == "month":
            key = day.strftime("%Y-%m")
        elif granularity == "week":
            key = f"{day.isocalendar().year}-W{day.isocalendar().week:02d}"
        else:
            key = day.isoformat()
        buckets.setdefault(key, []).append(row.value)
    levels: dict[str, int] = {}
    for row in rows:
        levels[row.level] = levels.get(row.level, 0) + 1
    return {
        "metric": metric,
        "granularity": granularity,
        "points": [
            {"label": key, "avg": round(sum(vals) / len(vals), 2),
             "min": min(vals), "max": max(vals), "count": len(vals)}
            for key, vals in sorted(buckets.items())
        ],
        "level_distribution": levels,
        "total": len(rows),
        "latest": _measure_out(rows[-1]) if rows else None,
    }


# ============================================================ 评估


class AssessIn(BaseModel):
    patient_id: int
    scale_code: str = Field(min_length=1, max_length=32)
    answers: dict = Field(default_factory=dict)
    program_code: str = Field(default="", max_length=32)
    channel: str = Field(default="doctor", pattern="^(doctor|self)$")


def _assess_out(a: SpdAssessment, patient_name: str = "") -> dict:
    return {
        "id": a.id, "patient_id": a.patient_id, "patient_name": patient_name,
        "scale_id": a.scale_id, "scale_code": a.scale_code,
        "scale_version": a.scale_version, "program_code": a.program_code,
        "answers": a.answers or {}, "score": a.score, "risk_level": a.risk_level,
        "advice": a.advice, "channel": a.channel, "created_at": a.created_at.isoformat(),
    }


@router.post("/assessments", status_code=201,
             dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def create_assessment(
    body: AssessIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """开展一次评估：自动评分、给出风险等级，并回写纳管档案的风险分级。

    风险等级回写是有意的联动：招标文件成员端 #4 要求风险等级"作为管理目标、
    路径、随访和转诊策略调整依据"，评估完还要人工去改一次档案，
    这一步一定会有人忘记，忘记的结果是高危患者仍按低危频次随访。
    """
    assert_patient_visible(db, user, body.patient_id, resource="spd_assessment")
    scale = (
        db.query(SpdScale)
        .filter(SpdScale.code == body.scale_code, SpdScale.status == "published")
        .order_by(SpdScale.id.desc())
        .first()
    )
    if scale is None:
        raise HTTPException(status_code=404, detail="量表不存在或未发布")
    graded = score_scale(scale.items or [], body.answers, scale.scoring or {})
    record = SpdAssessment(
        patient_id=body.patient_id, scale_id=scale.id, scale_code=scale.code,
        scale_version=scale.version, program_code=body.program_code or scale.program_code,
        answers=body.answers, score=graded["score"], risk_level=graded["risk_level"],
        advice=graded["advice"], channel=body.channel, operator_id=user.id,
    )
    db.add(record)
    enrollment = _enrollment_of(db, body.patient_id, record.program_code)
    if enrollment is not None and graded["risk_level"] in ("low", "mid", "high", "very_high"):
        enrollment.risk_level = graded["risk_level"]
        if graded["risk_level"] in ("high", "very_high"):
            _auto_intervene(db, enrollment, graded["risk_level"])
    db.commit()
    patient = db.get(Patient, body.patient_id)
    return _assess_out(record, patient.name if patient else "")


def _auto_intervene(db: Session, enrollment: SpdEnrollment, risk_level: str) -> None:
    """高危自动触发干预与复诊（成员端 #9、智能随访端 #8）。

    模板按 `auto_risk_level` 匹配；没有配模板就只建复诊，不静默什么都不做——
    "高危了但系统没动静"是这套系统最不该出现的状态。
    """
    template = (
        db.query(SpdInterventionTemplate)
        .filter(
            SpdInterventionTemplate.program_code == enrollment.program_code,
            SpdInterventionTemplate.auto_risk_level == risk_level,
            SpdInterventionTemplate.active.is_(True),
        )
        .first()
    )
    if template is not None:
        exists = (
            db.query(SpdIntervention)
            .filter(
                SpdIntervention.enrollment_id == enrollment.id,
                SpdIntervention.template_id == template.id,
                SpdIntervention.status.in_(["planned", "doing"]),
            )
            .first()
        )
        if exists is None:
            db.add(
                SpdIntervention(
                    patient_id=enrollment.patient_id, enrollment_id=enrollment.id,
                    program_code=enrollment.program_code, template_id=template.id,
                    goal=f"{risk_level}风险自动干预", content=template.content,
                    measures=template.measures, frequency=template.frequency,
                    next_at=(date.today() + timedelta(days=7)).isoformat(),
                    owner_id=enrollment.doctor_user_id, status="planned",
                )
            )
    already = (
        db.query(SpdRevisit)
        .filter(
            SpdRevisit.patient_id == enrollment.patient_id,
            SpdRevisit.program_code == enrollment.program_code,
            SpdRevisit.source == "high_risk",
            SpdRevisit.status == "planned",
        )
        .first()
    )
    if already is None:
        db.add(
            SpdRevisit(
                patient_id=enrollment.patient_id, program_code=enrollment.program_code,
                plan_date=(date.today() + timedelta(days=14)).isoformat(),
                doctor_user_id=enrollment.doctor_user_id, items="高危复诊评估",
                source="high_risk", status="planned",
            )
        )


@router.get("/assessments")
def list_assessments(
    response: Response,
    patient_id: int | None = None,
    scale_code: str | None = None,
    risk_level: str | None = None,
    program_code: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(SpdAssessment)
    if patient_id is not None:
        assert_patient_visible(db, user, patient_id, resource="spd_assessment")
        query = query.filter(SpdAssessment.patient_id == patient_id)
    for column, value in (
        (SpdAssessment.scale_code, scale_code), (SpdAssessment.risk_level, risk_level),
        (SpdAssessment.program_code, program_code),
    ):
        if value:
            query = query.filter(column == value)
    rows = paginate(query.order_by(SpdAssessment.id.desc()), response, offset, limit)
    names = {
        p.id: p.name
        for p in db.query(Patient).filter(Patient.id.in_([r.patient_id for r in rows] or [0]))
    }
    return [_assess_out(r, names.get(r.patient_id, "")) for r in rows]


@router.get("/assessments/stats")
def assessment_stats(
    scale_code: str | None = None,
    program_code: str | None = None,
    db: Session = Depends(get_db),
):
    """评估统计（成员端 #8）：人数、人次、结果分布与逐题分布。"""
    query = db.query(SpdAssessment)
    if scale_code:
        query = query.filter(SpdAssessment.scale_code == scale_code)
    if program_code:
        query = query.filter(SpdAssessment.program_code == program_code)
    rows = query.order_by(SpdAssessment.id.desc()).limit(5000).all()
    by_risk: dict[str, int] = {}
    by_item: dict[str, dict[str, int]] = {}
    for row in rows:
        by_risk[row.risk_level or "未分级"] = by_risk.get(row.risk_level or "未分级", 0) + 1
        for key, value in (row.answers or {}).items():
            options = by_item.setdefault(key, {})
            for one in value if isinstance(value, list) else [value]:
                options[str(one)] = options.get(str(one), 0) + 1
    return {
        "persons": len({r.patient_id for r in rows}),
        "times": len(rows),
        "by_risk": by_risk,
        "by_item": by_item,
    }


# ============================================================ 干预


class InterventionTemplateIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    program_code: str = Field(default="", max_length=32)
    category: str = Field(default="diet", pattern="^(diet|exercise|drug|psych|other)$")
    content: str = Field(default="", max_length=2048)
    measures: str = Field(default="", max_length=1024)
    frequency: str = Field(default="", max_length=32)
    cycle_days: int = Field(default=30, ge=1, le=3650)
    auto_risk_level: str = Field(default="", pattern="^(|low|mid|high|very_high)$")


@router.post("/intervention-templates", status_code=201,
             dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def create_intervention_template(
    body: InterventionTemplateIn, db: Session = Depends(get_db)
):
    from sqlalchemy.exc import IntegrityError

    template = SpdInterventionTemplate(**body.model_dump())
    db.add(template)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该干预模板编码已存在") from None
    return {"id": template.id, "code": template.code, "name": template.name}


@router.get("/intervention-templates")
def list_intervention_templates(
    program_code: str | None = None, category: str | None = None, db: Session = Depends(get_db)
):
    query = db.query(SpdInterventionTemplate).filter(SpdInterventionTemplate.active.is_(True))
    if program_code:
        query = query.filter(SpdInterventionTemplate.program_code == program_code)
    if category:
        query = query.filter(SpdInterventionTemplate.category == category)
    return [
        {"id": t.id, "code": t.code, "name": t.name, "program_code": t.program_code,
         "category": t.category, "content": t.content, "measures": t.measures,
         "frequency": t.frequency, "cycle_days": t.cycle_days,
         "auto_risk_level": t.auto_risk_level}
        for t in query.order_by(SpdInterventionTemplate.id).limit(300).all()
    ]


class InterventionIn(BaseModel):
    patient_ids: list[int] = Field(min_length=1, max_length=500)
    program_code: str = Field(default="", max_length=32)
    template_id: int | None = None
    goal: str = Field(default="", max_length=256)
    content: str = Field(default="", max_length=2048)
    measures: str = Field(default="", max_length=1024)
    frequency: str = Field(default="", max_length=32)
    next_at: OptionalDateStr = ""
    create_task: bool = True


def _intervention_out(i: SpdIntervention, patient_name: str = "") -> dict:
    return {
        "id": i.id, "patient_id": i.patient_id, "patient_name": patient_name,
        "enrollment_id": i.enrollment_id, "program_code": i.program_code,
        "template_id": i.template_id, "goal": i.goal, "content": i.content,
        "measures": i.measures, "frequency": i.frequency, "next_at": i.next_at,
        "owner_id": i.owner_id, "status": i.status, "feedback": i.feedback,
        "read_at": i.read_at.isoformat() if i.read_at else "",
        "created_at": i.created_at.isoformat(),
    }


@router.post("/interventions", status_code=201,
             dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def create_interventions(
    body: InterventionIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """新增干预：支持批量选人、引用模板，并自动生成定时任务（成员端 #15）。"""
    template = (
        db.get(SpdInterventionTemplate, body.template_id)
        if body.template_id is not None else None
    )
    if body.template_id is not None and template is None:
        raise HTTPException(status_code=404, detail="干预模板不存在")
    content = body.content or (template.content if template else "")
    if not content:
        raise HTTPException(status_code=422, detail="干预内容不能为空")
    next_at = body.next_at or (
        date.today() + timedelta(days=template.cycle_days if template else 30)
    ).isoformat()

    created = []
    for patient_id in dict.fromkeys(body.patient_ids):
        assert_patient_visible(db, user, patient_id, resource="spd_intervention")
        enrollment = _enrollment_of(db, patient_id, body.program_code)
        record = SpdIntervention(
            patient_id=patient_id,
            enrollment_id=enrollment.id if enrollment else None,
            program_code=body.program_code,
            template_id=body.template_id,
            goal=body.goal or (template.name if template else ""),
            content=content,
            measures=body.measures or (template.measures if template else ""),
            frequency=body.frequency or (template.frequency if template else ""),
            next_at=next_at, owner_id=user.id, status="planned",
        )
        db.add(record)
        db.flush()
        created.append(record.id)
        if body.create_task:
            spawn_task(
                db, patient_id=patient_id, title=f"干预执行：{record.goal or '健康干预'}",
                task_type="intervention", program_code=body.program_code,
                enrollment=enrollment, assignee_id=user.id, org_id=user.org_id,
                due_days=7, source="manual",
            )
    db.commit()
    return {"created": len(created), "ids": created}


@router.get("/interventions")
def list_interventions(
    response: Response,
    patient_id: int | None = None,
    program_code: str | None = None,
    status: str | None = None,
    owner_id: int | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(SpdIntervention)
    if patient_id is not None:
        assert_patient_visible(db, user, patient_id, resource="spd_intervention")
        query = query.filter(SpdIntervention.patient_id == patient_id)
    for column, value in (
        (SpdIntervention.program_code, program_code), (SpdIntervention.status, status),
        (SpdIntervention.owner_id, owner_id),
    ):
        if value is not None and value != "":
            query = query.filter(column == value)
    rows = paginate(query.order_by(SpdIntervention.id.desc()), response, offset, limit)
    names = {
        p.id: p.name
        for p in db.query(Patient).filter(Patient.id.in_([r.patient_id for r in rows] or [0]))
    }
    return [_intervention_out(r, names.get(r.patient_id, "")) for r in rows]


class InterventionUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(planned|doing|done|removed)$")
    feedback: str = Field(default="", max_length=512)
    next_at: OptionalDateStr | None = None


@router.patch("/interventions/{intervention_id}",
              dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def update_intervention(
    intervention_id: int, body: InterventionUpdate, db: Session = Depends(get_db)
):
    """办理 / 移除 / 恢复干预任务，并可记录患者反馈。"""
    record = db.get(SpdIntervention, intervention_id)
    if record is None:
        raise HTTPException(status_code=404, detail="干预记录不存在")
    for key, value in body.model_dump(exclude_unset=True).items():
        if key == "feedback" and not value:
            continue
        setattr(record, key, value)
    db.commit()
    return _intervention_out(record)


# ============================================================ 宣教推送


class EduPushIn(BaseModel):
    material_id: int
    patient_ids: list[int] = Field(min_length=1, max_length=1000)
    channel: str = Field(default="sms", pattern="^(sms|wechat|app)$")
    send_at: str = Field(default="", max_length=19)
    frequency: str = Field(default="once", max_length=32)


@router.post("/edu-pushes", status_code=201,
             dependencies=[Depends(require_roles(*SERVICE_ROLES, "operator"))])
def push_education(
    body: EduPushIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """向纳管患者推送宣教内容，可设置发送时间与频率（成员端 #7）。"""
    material = db.get(SpdEduMaterial, body.material_id)
    if material is None or not material.active:
        raise HTTPException(status_code=404, detail="宣教素材不存在或已停用")
    created = 0
    for patient_id in dict.fromkeys(body.patient_ids):
        db.add(
            SpdEduPush(
                material_id=body.material_id, patient_id=patient_id, channel=body.channel,
                send_at=body.send_at or now_naive().strftime("%Y-%m-%d %H:%M:%S"),
                frequency=body.frequency,
                status="pending" if body.send_at else "sent",
                operator_id=user.id,
            )
        )
        created += 1
    db.commit()
    return {"pushed": created, "material": material.title}


@router.get("/edu-pushes")
def list_edu_pushes(
    response: Response,
    patient_id: int | None = None,
    material_id: int | None = None,
    status: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdEduPush)
    for column, value in (
        (SpdEduPush.patient_id, patient_id), (SpdEduPush.material_id, material_id),
        (SpdEduPush.status, status),
    ):
        if value is not None and value != "":
            query = query.filter(column == value)
    rows = paginate(query.order_by(SpdEduPush.id.desc()), response, offset, limit)
    titles = {
        m.id: m.title
        for m in db.query(SpdEduMaterial)
        .filter(SpdEduMaterial.id.in_([r.material_id for r in rows] or [0]))
    }
    return [
        {"id": r.id, "material_id": r.material_id, "title": titles.get(r.material_id, ""),
         "patient_id": r.patient_id, "channel": r.channel, "send_at": r.send_at,
         "frequency": r.frequency, "status": r.status,
         "read_at": r.read_at.isoformat() if r.read_at else "",
         "created_at": r.created_at.isoformat()}
        for r in rows
    ]


@router.get("/edu-pushes/stats")
def edu_stats(program_code: str | None = None, db: Session = Depends(get_db)):
    """宣教成效统计（成员端 #16）：覆盖人数、执行次数、阅读完成率。"""
    query = db.query(SpdEduPush)
    if program_code:
        ids = [
            m.id
            for m in db.query(SpdEduMaterial)
            .filter(SpdEduMaterial.program_code == program_code)
            .all()
        ]
        query = query.filter(SpdEduPush.material_id.in_(ids or [0]))
    rows = query.limit(20000).all()
    read = sum(1 for r in rows if r.status == "read")
    sent = sum(1 for r in rows if r.status in ("sent", "read"))
    return {
        "covered_patients": len({r.patient_id for r in rows}),
        "push_times": len(rows),
        "sent": sent,
        "read": read,
        "read_rate": round(read / sent * 100, 1) if sent else 0.0,
        "by_channel": {
            channel: sum(1 for r in rows if r.channel == channel)
            for channel in {r.channel for r in rows}
        },
    }


# ============================================================ 复诊计划


class RevisitIn(BaseModel):
    patient_id: int
    program_code: str = Field(default="", max_length=32)
    plan_date: DateStr
    dept: str = Field(default="", max_length=64)
    doctor_user_id: int | None = None
    items: str = Field(default="", max_length=512)
    source: str = Field(default="manual", pattern="^(path|discharge|high_risk|manual)$")


def _revisit_out(r: SpdRevisit, patient_name: str = "") -> dict:
    return {
        "id": r.id, "patient_id": r.patient_id, "patient_name": patient_name,
        "program_code": r.program_code, "plan_date": r.plan_date, "dept": r.dept,
        "doctor_user_id": r.doctor_user_id, "items": r.items, "source": r.source,
        "status": r.status, "remind_status": r.remind_status,
        "actual_date": r.actual_date, "log": r.log or [],
    }


@router.post("/revisits", status_code=201, dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def create_revisit(
    body: RevisitIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    assert_patient_visible(db, user, body.patient_id, resource="spd_revisit")
    record = SpdRevisit(**body.model_dump(), status="planned")
    db.add(record)
    db.commit()
    return _revisit_out(record)


@router.get("/revisits")
def list_revisits(
    response: Response,
    patient_id: int | None = None,
    status: str | None = None,
    dept: str | None = None,
    doctor_user_id: int | None = None,
    date_from: str = "",
    date_to: str = "",
    overdue: bool = False,
    today: str | None = None,
    offset: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """复诊日历看板（个案管理师端 #9、医生移动端 #12）。

    `overdue=true` 取"计划日期已过且仍是 planned"的——逾期未复诊人员清单，
    这批人正是要电话/微信邀约的对象。
    """
    business_day = resolve_business_date(today)
    query = db.query(SpdRevisit)
    if patient_id is not None:
        assert_patient_visible(db, user, patient_id, resource="spd_revisit")
        query = query.filter(SpdRevisit.patient_id == patient_id)
    for column, value in (
        (SpdRevisit.status, status), (SpdRevisit.dept, dept),
        (SpdRevisit.doctor_user_id, doctor_user_id),
    ):
        if value is not None and value != "":
            query = query.filter(column == value)
    if date_from:
        query = query.filter(SpdRevisit.plan_date >= date_from)
    if date_to:
        query = query.filter(SpdRevisit.plan_date <= date_to)
    if overdue:
        query = query.filter(
            SpdRevisit.status == "planned",
            SpdRevisit.plan_date < business_day.isoformat(),
        )
    rows = paginate(query.order_by(SpdRevisit.plan_date), response, offset, limit)
    names = {
        p.id: p.name
        for p in db.query(Patient).filter(Patient.id.in_([r.patient_id for r in rows] or [0]))
    }
    return [_revisit_out(r, names.get(r.patient_id, "")) for r in rows]


class RevisitUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(planned|done|overdue|removed)$")
    plan_date: OptionalDateStr | None = None
    actual_date: OptionalDateStr | None = None
    remind_status: str | None = Field(default=None, pattern="^(none|sent|contacted)$")
    note: str = Field(default="", max_length=256)


@router.patch("/revisits/{revisit_id}", dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def update_revisit(revisit_id: int, body: RevisitUpdate, db: Session = Depends(get_db)):
    """编辑 / 移除 / 恢复复诊计划，并留日志（医生移动端 #12 要求日志记录能力）。"""
    record = db.get(SpdRevisit, revisit_id)
    if record is None:
        raise HTTPException(status_code=404, detail="复诊计划不存在")
    data = body.model_dump(exclude_unset=True)
    note = data.pop("note", "")
    for key, value in data.items():
        setattr(record, key, value)
    record.log = (record.log or []) + [{
        "at": date.today().isoformat(),
        "note": note or f"状态变更为{record.status}",
    }]
    db.commit()
    return _revisit_out(record)


# ============================================================ 上报任务与记录


class ReportTaskIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    program_code: str = Field(default="", max_length=32)
    dept: str = Field(default="", max_length=64)
    manager_user_id: int | None = None
    assignee_ids: list[int] = Field(default_factory=list)
    org_ids: list[int] = Field(default_factory=list)


@router.post("/case-report-tasks", status_code=201,
             dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def create_case_report_task(body: ReportTaskIn, db: Session = Depends(get_db)):
    from sqlalchemy.exc import IntegrityError

    task = SpdCaseReportTask(**body.model_dump())
    db.add(task)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该上报任务编码已存在") from None
    return {"id": task.id, "code": task.code, "name": task.name, "active": task.active}


@router.get("/case-report-tasks")
def list_case_report_tasks(active: bool | None = None, db: Session = Depends(get_db)):
    query = db.query(SpdCaseReportTask)
    if active is not None:
        query = query.filter(SpdCaseReportTask.active.is_(active))
    return [
        {"id": t.id, "code": t.code, "name": t.name, "program_code": t.program_code,
         "dept": t.dept, "manager_user_id": t.manager_user_id,
         "assignee_ids": t.assignee_ids or [], "org_ids": t.org_ids or [], "active": t.active}
        for t in query.order_by(SpdCaseReportTask.id).limit(200).all()
    ]


@router.patch("/case-report-tasks/{task_id}",
              dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def update_case_report_task(task_id: int, body: dict, db: Session = Depends(get_db)):
    task = db.get(SpdCaseReportTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="上报任务不存在")
    for key in ("name", "program_code", "dept", "manager_user_id", "assignee_ids",
                "org_ids", "active"):
        if key in body:
            setattr(task, key, body[key])
    db.commit()
    return {"id": task.id, "name": task.name, "active": task.active}


class CaseReportIn(BaseModel):
    patient_id: int
    task_id: int | None = None
    program_code: str = Field(default="", max_length=32)
    report_type: str = Field(default="review", pattern="^(review|referral|followup|dispose)$")
    content: str = Field(default="", max_length=1024)
    trigger_rule: str = Field(default="", max_length=128)


@router.post("/case-reports", status_code=201,
             dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def create_case_report(
    body: CaseReportIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """把规则触发的异常患者上报为复核/转诊/随访/处置任务（成员端 #11）。

    上报同时生成一条统一任务，这样"上报了"和"有人在办"是同一件事的两面，
    不会出现上报单堆着、任务中心却什么都没有。
    """
    assert_patient_visible(db, user, body.patient_id, resource="spd_case_report")
    report = SpdCaseReport(
        **body.model_dump(), reporter_id=user.id, org_id=user.org_id, status="pending",
    )
    db.add(report)
    db.flush()
    enrollment = _enrollment_of(db, body.patient_id, body.program_code)
    spawn_task(
        db, patient_id=body.patient_id,
        title=f"异常上报处置：{body.content[:40] or body.report_type}",
        task_type="report", program_code=body.program_code, enrollment=enrollment,
        org_id=user.org_id, due_days=3, priority=2, source="report",
    )
    if enrollment is not None:
        award_points(
            db, enrollment.village_doctor_id or user.id, "abnormal_report",
            ref_type="case_report", ref_id=report.id, note="异常上报",
            org_id=enrollment.org_id,
        )
    db.commit()
    return {"id": report.id, "status": report.status}


@router.get("/case-reports")
def list_case_reports(
    response: Response,
    patient_id: int | None = None,
    task_id: int | None = None,
    status: str | None = None,
    report_type: str | None = None,
    id_card: str = "",
    date_from: str = "",
    date_to: str = "",
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """上报明细：按患者、证件号和上报时间筛选（成员端 #11、中心端 #11）。"""
    query = db.query(SpdCaseReport)
    orgs = visible_org_ids(db, user)
    if orgs is not None:
        query = query.filter(SpdCaseReport.org_id.in_(orgs))
    for column, value in (
        (SpdCaseReport.patient_id, patient_id), (SpdCaseReport.task_id, task_id),
        (SpdCaseReport.status, status), (SpdCaseReport.report_type, report_type),
    ):
        if value is not None and value != "":
            query = query.filter(column == value)
    if id_card:
        ids = [
            p.id
            for p in db.query(Patient).filter(Patient.id_card.contains(id_card)).limit(200)
        ]
        query = query.filter(SpdCaseReport.patient_id.in_(ids or [0]))
    if date_from:
        query = query.filter(SpdCaseReport.created_at >= f"{date_from} 00:00:00")
    if date_to:
        query = query.filter(SpdCaseReport.created_at <= f"{date_to} 23:59:59")
    rows = paginate(query.order_by(SpdCaseReport.id.desc()), response, offset, limit)
    names = {
        p.id: p.name
        for p in db.query(Patient).filter(Patient.id.in_([r.patient_id for r in rows] or [0]))
    }
    return [
        {"id": r.id, "task_id": r.task_id, "patient_id": r.patient_id,
         "patient_name": names.get(r.patient_id, ""), "program_code": r.program_code,
         "report_type": r.report_type, "content": r.content,
         "trigger_rule": r.trigger_rule, "status": r.status,
         "handle_note": r.handle_note, "created_at": r.created_at.isoformat()}
        for r in rows
    ]


class HandleReportIn(BaseModel):
    status: str = Field(pattern="^(handling|done|closed)$")
    handle_note: str = Field(default="", max_length=512)


@router.post("/case-reports/{report_id}/handle",
             dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def handle_case_report(
    report_id: int, body: HandleReportIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    report = db.get(SpdCaseReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="上报记录不存在")
    assert_org_writable(db, user, report.org_id)
    report.status = body.status
    report.handle_note = body.handle_note
    report.handled_by = user.id
    if body.status in ("done", "closed"):
        report.handled_at = now_naive()
    db.commit()
    return {"id": report.id, "status": report.status}


# ============================================================ 健康处方


class PrescriptionIn(BaseModel):
    patient_id: int
    program_code: str = Field(default="", max_length=32)
    drug_advice: str = Field(default="", max_length=1024)
    rehab_advice: str = Field(default="", max_length=1024)
    life_advice: str = Field(default="", max_length=1024)
    target_note: str = Field(default="", max_length=512)


@router.post("/health-prescriptions", status_code=201,
             dependencies=[Depends(require_roles("doctor", "director"))])
def create_health_prescription(
    body: PrescriptionIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    assert_patient_visible(db, user, body.patient_id, resource="spd_health_rx")
    if not (body.drug_advice or body.rehab_advice or body.life_advice):
        raise HTTPException(status_code=422, detail="健康处方至少要有一项指导内容")
    record = SpdHealthPrescription(**body.model_dump(), doctor_id=user.id)
    db.add(record)
    db.commit()
    return {"id": record.id, "created_at": record.created_at.isoformat()}


@router.get("/health-prescriptions")
def list_health_prescriptions(
    response: Response, patient_id: int, offset: int = 0, limit: int = 50,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    assert_patient_visible(db, user, patient_id, resource="spd_health_rx")
    query = db.query(SpdHealthPrescription).filter(
        SpdHealthPrescription.patient_id == patient_id
    )
    rows = paginate(query.order_by(SpdHealthPrescription.id.desc()), response, offset, limit)
    return [
        {"id": r.id, "program_code": r.program_code, "drug_advice": r.drug_advice,
         "rehab_advice": r.rehab_advice, "life_advice": r.life_advice,
         "target_note": r.target_note, "doctor_id": r.doctor_id,
         "created_at": r.created_at.isoformat()}
        for r in rows
    ]


# ============================================================ 在线咨询


class ConsultReplyIn(BaseModel):
    content: str = Field(min_length=1, max_length=2048)


@router.get("/consults")
def list_consults(
    response: Response,
    status: str | None = None,
    doctor_id: int | None = None,
    mine: bool = False,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(SpdConsult)
    if status:
        query = query.filter(SpdConsult.status == status)
    if mine:
        query = query.filter(SpdConsult.doctor_id == user.id)
    elif doctor_id is not None:
        query = query.filter(SpdConsult.doctor_id == doctor_id)
    rows = paginate(query.order_by(SpdConsult.id.desc()), response, offset, limit)
    names = {
        p.id: p.name
        for p in db.query(Patient).filter(Patient.id.in_([r.patient_id for r in rows] or [0]))
    }
    counts = dict(
        db.query(SpdConsultMessage.consult_id, func.count(SpdConsultMessage.id))
        .filter(SpdConsultMessage.consult_id.in_([r.id for r in rows] or [0]))
        .group_by(SpdConsultMessage.consult_id)
        .all()
    )
    return [
        {"id": r.id, "patient_id": r.patient_id, "patient_name": names.get(r.patient_id, ""),
         "program_code": r.program_code, "doctor_id": r.doctor_id, "status": r.status,
         "messages": counts.get(r.id, 0), "created_at": r.created_at.isoformat()}
        for r in rows
    ]


@router.get("/consults/{consult_id}/messages")
def consult_messages(
    consult_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    consult = db.get(SpdConsult, consult_id)
    if consult is None:
        raise HTTPException(status_code=404, detail="咨询会话不存在")
    assert_patient_visible(db, user, consult.patient_id, resource="spd_consult")
    rows = (
        db.query(SpdConsultMessage)
        .filter(SpdConsultMessage.consult_id == consult_id)
        .order_by(SpdConsultMessage.id)
        .limit(500)
        .all()
    )
    return [
        {"id": m.id, "sender": m.sender, "sender_id": m.sender_id, "content": m.content,
         "created_at": m.created_at.isoformat()}
        for m in rows
    ]


@router.post("/consults/{consult_id}/reply",
             dependencies=[Depends(require_roles("doctor", "director"))])
def reply_consult(
    consult_id: int, body: ConsultReplyIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    consult = db.get(SpdConsult, consult_id)
    if consult is None:
        raise HTTPException(status_code=404, detail="咨询会话不存在")
    if consult.status != "open":
        raise HTTPException(status_code=409, detail="该会话已结束")
    assert_patient_visible(db, user, consult.patient_id, resource="spd_consult")
    if consult.doctor_id is None:
        consult.doctor_id = user.id
    message = SpdConsultMessage(
        consult_id=consult_id, sender="doctor", sender_id=user.id, content=body.content
    )
    db.add(message)
    db.commit()
    return {"id": message.id, "created_at": message.created_at.isoformat()}


@router.post("/consults/{consult_id}/close",
             dependencies=[Depends(require_roles("doctor", "director"))])
def close_consult(consult_id: int, db: Session = Depends(get_db)):
    consult = db.get(SpdConsult, consult_id)
    if consult is None:
        raise HTTPException(status_code=404, detail="咨询会话不存在")
    consult.status = "closed"
    consult.closed_at = now_naive()
    db.commit()
    return {"id": consult.id, "status": consult.status}


class ConsultFollowupIn(BaseModel):
    program_code: str = Field(default="", max_length=32)
    title: str = Field(default="咨询转随访", max_length=128)
    due_days: int = Field(default=7, ge=0, le=365)


@router.post("/consults/{consult_id}/to-followup",
             dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def consult_to_followup(
    consult_id: int, body: ConsultFollowupIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """依据咨询记录发起随访（个案管理师端 #6）。"""
    consult = db.get(SpdConsult, consult_id)
    if consult is None:
        raise HTTPException(status_code=404, detail="咨询会话不存在")
    enrollment = _enrollment_of(
        db, consult.patient_id, body.program_code or consult.program_code
    )
    task = spawn_task(
        db, patient_id=consult.patient_id, title=body.title, task_type="followup",
        program_code=body.program_code or consult.program_code, enrollment=enrollment,
        assignee_id=user.id, org_id=user.org_id, due_days=body.due_days, source="manual",
    )
    db.commit()
    return {"task_id": task.id, "due_date": task.due_date}
