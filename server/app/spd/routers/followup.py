"""全域慢专病 · 智能随访服务端（通用随访能力应用端）+ 智能辅助应用端。

对应招标文件最后两个端：智能随访服务端 #1~#13、智能辅助应用端 #1~#3。

这两个端是**通用能力**而不是慢专病专属：出院随访、术后随访、体检随访对全院
所有科室开放，报告推送也服务于非慢专病业务。所以随访方案 / 问卷 / 呼叫任务
不挂在 `SpdEnrollment` 上——一个刚做完阑尾切除的患者不该为了被随访而先被
"纳管"成慢病患者。它们只认患者与场景。
"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...clock import now_naive
from ...database import get_db
from ...datetypes import OptionalDateStr
from ...deps import get_current_user, paginate, require_roles, resolve_business_date, row_dict
from ..platform import Admission, Encounter, Patient, User
from ..models import (
    SpdCallTask,
    SpdEnrollment,
    SpdFollowupRecord,
    SpdFollowupRule,
    SpdQcSample,
    SpdQuestionnaire,
    SpdReportInstance,
    SpdReportTask,
    SpdReportTemplate,
    SpdRevisit,
    SpdTask,
)
from ..reporting import compose_section, default_period_label
from ..rules import RuleError, grade_abnormal, validate_conditions
from ...visibility import assert_org_writable, assert_patient_visible, visible_org_ids

router = APIRouter(
    prefix="/api/spd",
    tags=["全域慢专病·智能随访与辅助"],
    dependencies=[Depends(get_current_user)],
)

FOLLOWUP_ROLES = ("doctor", "public_health", "director", "operator")


# ============================================================ 随访方案规则


class FollowupRuleIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    scene: str = Field(default="inpatient", pattern="^(inpatient|outpatient|surgery|checkup)$")
    dept: str = Field(default="", max_length=64)
    program_code: str = Field(default="", max_length=32)
    diagnosis_keywords: list[str] = Field(default_factory=list)
    surgery_keywords: list[str] = Field(default_factory=list)
    order_keywords: list[str] = Field(default_factory=list)
    points: list[int] = Field(default_factory=list)
    questionnaire_code: str = Field(default="", max_length=32)
    executor_role: str = Field(default="nurse", max_length=32)
    allow_depts: list[str] = Field(default_factory=list)
    allow_roles: list[str] = Field(default_factory=list)


def _rule_out(r: SpdFollowupRule) -> dict:
    return {
        "id": r.id, "code": r.code, "name": r.name, "scene": r.scene, "dept": r.dept,
        "program_code": r.program_code,
        "diagnosis_keywords": r.diagnosis_keywords or [],
        "surgery_keywords": r.surgery_keywords or [],
        "order_keywords": r.order_keywords or [],
        "points": r.points or [], "questionnaire_code": r.questionnaire_code,
        "executor_role": r.executor_role, "allow_depts": r.allow_depts or [],
        "allow_roles": r.allow_roles or [], "preset": r.preset, "active": r.active,
    }


@router.post("/followup-rules", status_code=201,
             dependencies=[Depends(require_roles("director", "doctor"))])
def create_followup_rule(body: FollowupRuleIn, db: Session = Depends(get_db)):
    if not body.points:
        raise HTTPException(status_code=422, detail="随访方案至少要有一个随访时间点")
    if any(p < 0 or p > 3650 for p in body.points):
        raise HTTPException(status_code=422, detail="随访时间点须在 0~3650 天之间")
    rule = SpdFollowupRule(**body.model_dump())
    db.add(rule)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该随访方案编码已存在") from None
    return _rule_out(rule)


@router.get("/followup-rules")
def list_followup_rules(
    scene: str | None = None, dept: str | None = None, active: bool | None = None,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    """方案清单。带 `allow_depts` / `allow_roles` 的方案只对被授权的科室与角色可见。"""
    query = db.query(SpdFollowupRule)
    if scene:
        query = query.filter(SpdFollowupRule.scene == scene)
    if dept:
        query = query.filter(SpdFollowupRule.dept == dept)
    if active is not None:
        query = query.filter(SpdFollowupRule.active.is_(active))
    rows = query.order_by(SpdFollowupRule.id).limit(300).all()
    if user.role not in ("admin", "director"):
        rows = [
            r for r in rows
            if not (r.allow_roles or []) or user.role in (r.allow_roles or [])
        ]
    return [_rule_out(r) for r in rows]


@router.patch("/followup-rules/{rule_id}",
              dependencies=[Depends(require_roles("director", "doctor"))])
def update_followup_rule(rule_id: int, body: dict, db: Session = Depends(get_db)):
    rule = db.get(SpdFollowupRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="随访方案不存在")
    for key in ("name", "dept", "program_code", "diagnosis_keywords", "surgery_keywords",
                "order_keywords", "points", "questionnaire_code", "executor_role",
                "allow_depts", "allow_roles", "active"):
        if key in body:
            setattr(rule, key, body[key])
    db.commit()
    return _rule_out(rule)


# ============================================================ 随访问卷


class QuestionnaireIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    scene: str = Field(default="inpatient", max_length=16)
    items: list[dict] = Field(default_factory=list)
    abnormal_rules: list[dict] = Field(default_factory=list)
    track_dept: str = Field(default="", max_length=64)
    handle_role: str = Field(default="doctor", max_length=32)


def _q_out(q: SpdQuestionnaire) -> dict:
    return {
        "id": q.id, "code": q.code, "name": q.name, "scene": q.scene,
        "items": q.items or [], "abnormal_rules": q.abnormal_rules or [],
        "track_dept": q.track_dept, "handle_role": q.handle_role,
        "preset": q.preset, "active": q.active,
    }


@router.post("/questionnaires", status_code=201,
             dependencies=[Depends(require_roles("director", "doctor"))])
def create_questionnaire(body: QuestionnaireIn, db: Session = Depends(get_db)):
    for rule in body.abnormal_rules:
        try:
            validate_conditions([rule.get("when", {})])
        except RuleError as exc:
            raise HTTPException(status_code=422, detail=f"异常分级规则非法：{exc}") from None
    questionnaire = SpdQuestionnaire(**body.model_dump())
    db.add(questionnaire)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该问卷编码已存在") from None
    return _q_out(questionnaire)


@router.get("/questionnaires")
def list_questionnaires(scene: str | None = None, db: Session = Depends(get_db)):
    query = db.query(SpdQuestionnaire).filter(SpdQuestionnaire.active.is_(True))
    if scene:
        query = query.filter(SpdQuestionnaire.scene == scene)
    return [_q_out(q) for q in query.order_by(SpdQuestionnaire.id).limit(200).all()]


@router.patch("/questionnaires/{q_id}",
              dependencies=[Depends(require_roles("director", "doctor"))])
def update_questionnaire(q_id: int, body: dict, db: Session = Depends(get_db)):
    questionnaire = db.get(SpdQuestionnaire, q_id)
    if questionnaire is None:
        raise HTTPException(status_code=404, detail="问卷不存在")
    for key in ("name", "items", "abnormal_rules", "track_dept", "handle_role", "active"):
        if key in body:
            setattr(questionnaire, key, body[key])
    db.commit()
    return _q_out(questionnaire)


# ============================================================ 随访任务生成与执行


class GeneratePlanIn(BaseModel):
    patient_id: int
    rule_id: int
    base_date: OptionalDateStr = ""
    org_id: int | None = None
    dept: str = Field(default="", max_length=64)
    executor_id: int | None = None
    channel: str = Field(default="phone", pattern="^(phone|wechat|sms|self|visit)$")


def _record_out(r: SpdFollowupRecord, patient_name: str = "") -> dict:
    return {
        "id": r.id, "patient_id": r.patient_id, "patient_name": patient_name,
        "program_code": r.program_code, "rule_id": r.rule_id,
        "questionnaire_code": r.questionnaire_code, "scene": r.scene,
        "org_id": r.org_id, "dept": r.dept, "planned_at": r.planned_at,
        "executed_at": r.executed_at, "channel": r.channel,
        "executor_id": r.executor_id, "answers": r.answers or {},
        "abnormal_level": r.abnormal_level, "result": r.result,
        "evidence": r.evidence or [], "status": r.status,
        "created_at": r.created_at.isoformat(),
    }


@router.post("/followup-plans", status_code=201,
             dependencies=[Depends(require_roles(*FOLLOWUP_ROLES))])
def generate_followup_plan(
    body: GeneratePlanIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """按方案的多个时间点一次性生成随访任务（智能随访端 #4）。

    基准日缺省取今天；出院随访应当传出院日，术后随访传手术日——
    接口不去猜是哪一天，因为猜错的后果是整条随访计划全部错位。
    """
    assert_patient_visible(db, user, body.patient_id, resource="spd_followup")
    rule = db.get(SpdFollowupRule, body.rule_id)
    if rule is None or not rule.active:
        raise HTTPException(status_code=404, detail="随访方案不存在或已停用")
    base = date.fromisoformat(body.base_date) if body.base_date else date.today()
    created = []
    for offset in rule.points or []:
        record = SpdFollowupRecord(
            patient_id=body.patient_id, program_code=rule.program_code, rule_id=rule.id,
            questionnaire_code=rule.questionnaire_code, scene=rule.scene,
            org_id=body.org_id if body.org_id is not None else user.org_id,
            dept=body.dept or rule.dept,
            planned_at=(base + timedelta(days=int(offset))).isoformat(),
            channel=body.channel, executor_id=body.executor_id, status="planned",
        )
        db.add(record)
        created.append(record)
    db.commit()
    return {"created": len(created), "items": [_record_out(r) for r in created]}


class AutoMatchIn(BaseModel):
    scene: str = Field(default="inpatient", pattern="^(inpatient|outpatient|surgery|checkup)$")
    org_id: int | None = None
    days: int = Field(default=7, ge=1, le=90)
    limit: int = Field(default=200, ge=1, le=1000)


@router.post("/followup-plans/auto-match",
             dependencies=[Depends(require_roles(*FOLLOWUP_ROLES))])
def auto_match_plans(
    body: AutoMatchIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """依据患者特征自动匹配方案并生成随访任务（智能随访端 #5）。

    匹配靠诊断关键词命中：方案没配任何关键词就是**不匹配任何人**（与纳入规则
    同一口径），否则一个空方案会给全院每个出院患者都排上随访。
    """
    org_id = body.org_id if body.org_id is not None else user.org_id
    rules = (
        db.query(SpdFollowupRule)
        .filter(SpdFollowupRule.scene == body.scene, SpdFollowupRule.active.is_(True))
        .all()
    )
    rules = [r for r in rules if (r.diagnosis_keywords or [])]
    if not rules:
        return {"matched": 0, "created": 0, "note": "没有配置了诊断关键词的可用方案"}

    since = now_naive() - timedelta(days=body.days)
    if body.scene == "inpatient":
        rows = (
            db.query(Admission)
            .filter(Admission.org_id == org_id, Admission.status == "discharged")
            .order_by(Admission.id.desc())
            .limit(body.limit)
            .all()
        )
        candidates = [
            (
                a.patient_id,
                (a.discharged_at or a.admitted_at).date().isoformat(),
                a.diagnosis_name or "",
            )
            for a in rows
        ]
    else:
        rows = (
            db.query(Encounter)
            .filter(Encounter.org_id == org_id, Encounter.created_at >= since)
            .order_by(Encounter.id.desc())
            .limit(body.limit)
            .all()
        )
        candidates = [
            (e.patient_id, e.created_at.date().isoformat(),
             f"{e.diagnosis_name or ''}{e.diagnosis_code or ''}")
            for e in rows
        ]

    matched, created = 0, 0
    for patient_id, base_date, text in candidates:
        rule = next(
            (r for r in rules if any(k and k in text for k in r.diagnosis_keywords or [])), None
        )
        if rule is None:
            continue
        matched += 1
        exists = (
            db.query(SpdFollowupRecord.id)
            .filter(
                SpdFollowupRecord.patient_id == patient_id,
                SpdFollowupRecord.rule_id == rule.id,
            )
            .first()
        )
        if exists is not None:
            continue
        try:
            base = date.fromisoformat(base_date)
        except (ValueError, TypeError):
            base = date.today()
        for offset in rule.points or []:
            db.add(
                SpdFollowupRecord(
                    patient_id=patient_id, program_code=rule.program_code, rule_id=rule.id,
                    questionnaire_code=rule.questionnaire_code, scene=rule.scene,
                    org_id=org_id, dept=rule.dept,
                    planned_at=(base + timedelta(days=int(offset))).isoformat(),
                    channel="phone", status="planned",
                )
            )
            created += 1
    db.commit()
    return {"scanned": len(candidates), "matched": matched, "created": created}


@router.get("/followup-records")
def list_followup_records(
    response: Response,
    patient_id: int | None = None,
    scene: str | None = None,
    status: str | None = None,
    dept: str | None = None,
    executor_id: int | None = None,
    abnormal_level: str | None = None,
    mine: bool = False,
    date_from: str = "",
    date_to: str = "",
    overdue: bool = False,
    today: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """随访看板：全院 / 科室 / 个人三个口径由参数组合而成，不做三个接口。

    `overdue=true` 按 status 过滤（进接口先跑一次超期扫描）——与督办、考核同一口径。
    """
    business_day = resolve_business_date(today)
    if overdue:
        from ..service import sweep_overdue

        sweep_overdue(db, business_day)
        db.commit()
    query = db.query(SpdFollowupRecord)
    if patient_id is not None:
        assert_patient_visible(db, user, patient_id, resource="spd_followup")
        query = query.filter(SpdFollowupRecord.patient_id == patient_id)
    else:
        orgs = visible_org_ids(db, user)
        if orgs is not None:
            query = query.filter(SpdFollowupRecord.org_id.in_(orgs))
    if mine:
        query = query.filter(SpdFollowupRecord.executor_id == user.id)
    elif executor_id is not None:
        query = query.filter(SpdFollowupRecord.executor_id == executor_id)
    for column, value in (
        (SpdFollowupRecord.scene, scene), (SpdFollowupRecord.status, status),
        (SpdFollowupRecord.dept, dept),
        (SpdFollowupRecord.abnormal_level, abnormal_level),
    ):
        if value is not None and value != "":
            query = query.filter(column == value)
    if date_from:
        query = query.filter(SpdFollowupRecord.planned_at >= date_from)
    if date_to:
        query = query.filter(SpdFollowupRecord.planned_at <= date_to)
    if overdue:
        query = query.filter(SpdFollowupRecord.status == "overdue")
    rows = paginate(query.order_by(SpdFollowupRecord.planned_at), response, offset, limit)
    names = {
        p.id: p.name
        for p in db.query(Patient).filter(Patient.id.in_([r.patient_id for r in rows] or [0]))
    }
    return [_record_out(r, names.get(r.patient_id, "")) for r in rows]


@router.get("/followup-records/{record_id}/context")
def followup_context(
    record_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """随访前置资料（智能随访端 #1）：基本资料 + 就诊 + 住院 + 历史随访一屏聚合。"""
    record = db.get(SpdFollowupRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="随访记录不存在")
    assert_patient_visible(db, user, record.patient_id, resource="spd_followup")
    patient = db.get(Patient, record.patient_id)
    encounters = (
        db.query(Encounter)
        .filter(Encounter.patient_id == record.patient_id)
        .order_by(Encounter.id.desc())
        .limit(10)
        .all()
    )
    admissions = (
        db.query(Admission)
        .filter(Admission.patient_id == record.patient_id)
        .order_by(Admission.id.desc())
        .limit(5)
        .all()
    )
    history = (
        db.query(SpdFollowupRecord)
        .filter(
            SpdFollowupRecord.patient_id == record.patient_id,
            SpdFollowupRecord.status == "done",
        )
        .order_by(SpdFollowupRecord.id.desc())
        .limit(10)
        .all()
    )
    questionnaire = (
        db.query(SpdQuestionnaire)
        .filter(SpdQuestionnaire.code == record.questionnaire_code)
        .first()
    )
    return {
        "record": _record_out(record, patient.name if patient else ""),
        "patient": {
            "id": patient.id, "name": patient.name, "gender": patient.gender,
            "birth_date": patient.birth_date, "phone": patient.phone,
        } if patient else None,
        "encounters": [
            {"id": e.id, "encounter_type": e.encounter_type,
             "diagnosis_name": e.diagnosis_name, "doctor_name": e.doctor_name,
             "created_at": e.created_at.isoformat()}
            for e in encounters
        ],
        "admissions": [
            {"id": a.id, "admitted_at": a.admitted_at.isoformat(),
             "discharged_at": a.discharged_at.isoformat() if a.discharged_at else "",
             "diagnosis_name": a.diagnosis_name, "doctor_name": a.doctor_name,
             "status": a.status}
            for a in admissions
        ],
        "history": [_record_out(h) for h in history],
        "questionnaire": _q_out(questionnaire) if questionnaire else None,
    }


class ExecuteIn(BaseModel):
    answers: dict = Field(default_factory=dict)
    channel: str = Field(default="phone", pattern="^(phone|wechat|sms|self|visit)$")
    result: str = Field(default="", max_length=512)
    evidence: list[str] = Field(default_factory=list)
    unreachable: bool = False


@router.post("/followup-records/{record_id}/execute",
             dependencies=[Depends(require_roles(*FOLLOWUP_ROLES))])
def execute_followup(
    record_id: int, body: ExecuteIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """执行随访：填问卷 → 自动异常分级 → 重度异常自动派处置任务。

    失访（`unreachable`）单独一个状态而不是"完成但没答案"：随访完成率的分母
    应该含失访、分子不含，两者混在一起会把完成率算高。
    """
    record = db.get(SpdFollowupRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="随访记录不存在")
    assert_org_writable(db, user, record.org_id)
    if record.status in ("done", "removed"):
        raise HTTPException(status_code=409, detail="该随访已结束")
    record.channel = body.channel
    record.executor_id = user.id
    record.executed_at = date.today().isoformat()
    record.result = body.result
    if body.evidence:
        record.evidence = body.evidence
    if body.unreachable:
        record.status = "unreachable"
        db.commit()
        return _record_out(record)

    record.answers = body.answers
    record.status = "done"
    questionnaire = (
        db.query(SpdQuestionnaire)
        .filter(SpdQuestionnaire.code == record.questionnaire_code)
        .first()
    )
    action = ""
    if questionnaire is not None:
        level, action = grade_abnormal(questionnaire.abnormal_rules or [], body.answers)
        record.abnormal_level = level
        if level in ("mid", "high"):
            enrollment = (
                db.query(SpdEnrollment)
                .filter(
                    SpdEnrollment.patient_id == record.patient_id,
                    SpdEnrollment.program_code == record.program_code,
                )
                .first()
                if record.program_code else None
            )
            db.add(
                SpdTask(
                    program_code=record.program_code, patient_id=record.patient_id,
                    enrollment_id=enrollment.id if enrollment else None,
                    task_type="report", title=f"随访异常处置：{action or level}",
                    org_id=record.org_id, status="pending",
                    priority=3 if level == "high" else 2,
                    due_date=(date.today() + timedelta(days=1 if level == "high" else 3))
                    .isoformat(),
                    source="followup",
                )
            )
    db.commit()
    out = _record_out(record)
    out["action"] = action
    return out


class RecordPatchIn(BaseModel):
    status: str | None = Field(default=None, pattern="^(planned|removed)$")
    planned_at: OptionalDateStr | None = None
    executor_id: int | None = None
    channel: str | None = None


@router.patch("/followup-records/{record_id}",
              dependencies=[Depends(require_roles(*FOLLOWUP_ROLES))])
def update_followup_record(
    record_id: int,
    body: RecordPatchIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """移除 / 恢复随访任务、改期、改执行人（智能随访端 #4"任务移除恢复"）。"""
    record = db.get(SpdFollowupRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="随访记录不存在")
    assert_org_writable(db, user, record.org_id)
    if record.status == "done":
        raise HTTPException(status_code=409, detail="已完成的随访不可修改")
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(record, key, value)
    db.commit()
    return _record_out(record)


@router.get("/followup-stats")
def followup_stats(
    dept: str | None = None,
    scene: str | None = None,
    executor_id: int | None = None,
    date_from: str = "",
    date_to: str = "",
    today: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """随访质量与工作量统计（智能随访端 #13）：完成率、超期、异常分布、人员工作量。"""
    business_day = resolve_business_date(today)
    query = db.query(SpdFollowupRecord)
    orgs = visible_org_ids(db, user)
    if orgs is not None:
        query = query.filter(SpdFollowupRecord.org_id.in_(orgs))
    if dept:
        query = query.filter(SpdFollowupRecord.dept == dept)
    if scene:
        query = query.filter(SpdFollowupRecord.scene == scene)
    if executor_id is not None:
        query = query.filter(SpdFollowupRecord.executor_id == executor_id)
    if date_from:
        query = query.filter(SpdFollowupRecord.planned_at >= date_from)
    if date_to:
        query = query.filter(SpdFollowupRecord.planned_at <= date_to)

    by_status = row_dict(
        query.with_entities(SpdFollowupRecord.status, func.count(SpdFollowupRecord.id))
        .group_by(SpdFollowupRecord.status).all()
    )
    total = sum(by_status.values())
    done = by_status.get("done", 0)
    # planned 且已过期的算上（扫描间隙里的）+ 已标 overdue 的，两者都是超期
    overdue = query.filter(
        (SpdFollowupRecord.status == "overdue")
        | (
            (SpdFollowupRecord.status == "planned")
            & (SpdFollowupRecord.planned_at != "")
            & (SpdFollowupRecord.planned_at < business_day.isoformat())
        )
    ).count()
    by_abnormal = row_dict(
        query.filter(SpdFollowupRecord.status == "done")
        .with_entities(SpdFollowupRecord.abnormal_level, func.count(SpdFollowupRecord.id))
        .group_by(SpdFollowupRecord.abnormal_level).all()
    )
    by_executor = (
        query.filter(SpdFollowupRecord.status == "done")
        .with_entities(SpdFollowupRecord.executor_id, func.count(SpdFollowupRecord.id))
        .group_by(SpdFollowupRecord.executor_id).all()
    )
    names = {
        u.id: u.full_name or u.username
        for u in db.query(User).filter(User.id.in_([e for e, _ in by_executor if e] or [0]))
    }
    return {
        "total": total,
        "done": done,
        "completion_rate": round(done / total * 100, 1) if total else 0.0,
        "overdue": overdue,
        "by_status": by_status,
        "by_abnormal": by_abnormal,
        "by_channel": dict(
            query.filter(SpdFollowupRecord.status == "done")
            .with_entities(SpdFollowupRecord.channel, func.count(SpdFollowupRecord.id))
            .group_by(SpdFollowupRecord.channel).all()
        ),
        "by_executor": [
            {"executor_id": eid, "executor_name": names.get(eid, ""), "done": count}
            for eid, count in by_executor if eid
        ],
    }


# ============================================================ 呼叫任务与录音


class CallTaskIn(BaseModel):
    patient_id: int
    phone: str = Field(default="", max_length=20)
    ref_type: str = Field(default="followup", max_length=24)
    ref_id: int | None = None


@router.post("/call-tasks", status_code=201,
             dependencies=[Depends(require_roles(*FOLLOWUP_ROLES))])
def create_call_task(
    body: CallTaskIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """把随访 / 复诊 / 宣教 / 异常处置转为呼叫任务（智能随访端 #10）。"""
    assert_patient_visible(db, user, body.patient_id, resource="spd_call")
    phone = body.phone
    if not phone:
        patient = db.get(Patient, body.patient_id)
        phone = patient.phone if patient else ""
    task = SpdCallTask(
        patient_id=body.patient_id, phone=phone, ref_type=body.ref_type,
        ref_id=body.ref_id, operator_id=user.id, status="pending",
    )
    db.add(task)
    db.flush()
    # 经呼叫通道派发（manual=等人工外呼，http=推给呼叫中心）。
    # 派发失败不报错：任务留在 pending、结果里记原因——通道抖一下
    # 不该让"发起随访"这个动作失败。
    from ..callcenter import get_call_provider

    accepted, note = get_call_provider().dispatch(task.id, phone, body.ref_type)
    if not accepted:
        task.result = note
    db.commit()
    return {"id": task.id, "phone": task.phone, "status": task.status,
            "dispatch": {"accepted": accepted, "note": note}}


class CallResultIn(BaseModel):
    status: str = Field(pattern="^(connected|failed|cancelled)$")
    duration_s: int = Field(default=0, ge=0, le=36000)
    record_url: str = Field(default="", max_length=256)
    result: str = Field(default="", max_length=512)


@router.post("/call-tasks/{task_id}/result",
             dependencies=[Depends(require_roles(*FOLLOWUP_ROLES))])
def record_call_result(
    task_id: int, body: CallResultIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """回写通话结果与录音地址；接通结果同步写回关联的随访记录。"""
    task = db.get(SpdCallTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="呼叫任务不存在")
    task.status = body.status
    task.duration_s = body.duration_s
    task.record_url = body.record_url
    task.result = body.result
    task.started_at = task.started_at or now_naive()
    task.operator_id = task.operator_id or user.id
    if body.status == "connected" and task.ref_type == "followup" and task.ref_id:
        record = db.get(SpdFollowupRecord, task.ref_id)
        if record is not None:
            assert_org_writable(db, user, record.org_id)
        if record is not None and record.status in ("planned", "overdue"):
            record.result = (record.result + " " + body.result).strip()[:500]
            record.evidence = (record.evidence or []) + (
                [body.record_url] if body.record_url else []
            )
    db.commit()
    return {"id": task.id, "status": task.status, "duration_s": task.duration_s}


@router.get("/call-tasks")
def list_call_tasks(
    response: Response,
    status: str | None = None,
    phone: str = "",
    patient_name: str = "",
    date_from: str = "",
    date_to: str = "",
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdCallTask)
    if status:
        query = query.filter(SpdCallTask.status == status)
    if phone:
        query = query.filter(SpdCallTask.phone.contains(phone))
    if patient_name:
        ids = [
            p.id for p in db.query(Patient).filter(Patient.name.contains(patient_name)).limit(200)
        ]
        query = query.filter(SpdCallTask.patient_id.in_(ids or [0]))
    if date_from:
        query = query.filter(SpdCallTask.created_at >= f"{date_from} 00:00:00")
    if date_to:
        query = query.filter(SpdCallTask.created_at <= f"{date_to} 23:59:59")
    rows = paginate(query.order_by(SpdCallTask.id.desc()), response, offset, limit)
    names = {
        p.id: p.name
        for p in db.query(Patient).filter(Patient.id.in_([r.patient_id for r in rows] or [0]))
    }
    return [
        {"id": r.id, "patient_id": r.patient_id, "patient_name": names.get(r.patient_id, ""),
         "phone": r.phone, "ref_type": r.ref_type, "ref_id": r.ref_id,
         "status": r.status, "duration_s": r.duration_s, "record_url": r.record_url,
         "result": r.result, "created_at": r.created_at.isoformat()}
        for r in rows
    ]


# ============================================================ 随访抽查质控


class QcPlanIn(BaseModel):
    dept: str = Field(default="", max_length=64)
    ratio: float = Field(default=0.1, gt=0, le=1)
    count: int = Field(default=0, ge=0, le=500)
    batch: str = Field(default="", max_length=32)


@router.post("/qc-samples/plan", dependencies=[Depends(require_roles("director", "doctor"))])
def plan_qc(
    body: QcPlanIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """按比例或数量制定抽查计划（智能随访端 #11）。

    抽样按 id 取模而不是随机：同一批次重复调用要抽到同一批人，
    否则质控员刷新一次页面，待抽查清单就换了一批。
    """
    query = db.query(SpdFollowupRecord).filter(SpdFollowupRecord.status == "done")
    orgs = visible_org_ids(db, user)
    if orgs is not None:
        query = query.filter(SpdFollowupRecord.org_id.in_(orgs))
    if body.dept:
        query = query.filter(SpdFollowupRecord.dept == body.dept)
    rows = query.order_by(SpdFollowupRecord.id.desc()).limit(2000).all()
    if body.count:
        picked = rows[: body.count]
    else:
        step = max(int(1 / body.ratio), 1)
        picked = [r for index, r in enumerate(rows) if index % step == 0]
    batch = body.batch or f"QC{date.today().strftime('%Y%m%d')}"
    existing = {
        rid
        for (rid,) in db.query(SpdQcSample.record_id).filter(SpdQcSample.batch == batch).all()
    }
    created = 0
    for record in picked:
        if record.id in existing:
            continue
        db.add(
            SpdQcSample(
                record_id=record.id, batch=batch, dept=record.dept, sampler_id=user.id
            )
        )
        created += 1
    db.commit()
    return {"batch": batch, "pool": len(rows), "planned": len(picked), "created": created}


class QcResultIn(BaseModel):
    result: str = Field(pattern="^(pass|warn|fail)$")
    method: str = Field(default="record", pattern="^(record|phone|wechat)$")
    note: str = Field(default="", max_length=512)


@router.post("/qc-samples/{sample_id}/result",
             dependencies=[Depends(require_roles("director", "doctor"))])
def record_qc_result(sample_id: int, body: QcResultIn, db: Session = Depends(get_db)):
    sample = db.get(SpdQcSample, sample_id)
    if sample is None:
        raise HTTPException(status_code=404, detail="抽查记录不存在")
    sample.result = body.result
    sample.method = body.method
    sample.note = body.note
    db.commit()
    return {"id": sample.id, "result": sample.result}


@router.get("/qc-samples")
def list_qc_samples(
    response: Response, batch: str = "", result: str | None = None,
    offset: int = 0, limit: int = 100, db: Session = Depends(get_db),
):
    query = db.query(SpdQcSample)
    if batch:
        query = query.filter(SpdQcSample.batch == batch)
    if result:
        query = query.filter(SpdQcSample.result == result)
    rows = paginate(query.order_by(SpdQcSample.id.desc()), response, offset, limit)
    records = {
        r.id: r
        for r in db.query(SpdFollowupRecord)
        .filter(SpdFollowupRecord.id.in_([s.record_id for s in rows] or [0]))
    }
    return [
        {"id": s.id, "record_id": s.record_id, "batch": s.batch, "dept": s.dept,
         "result": s.result, "method": s.method, "note": s.note,
         "record": _record_out(records[s.record_id]) if s.record_id in records else None,
         "created_at": s.created_at.isoformat()}
        for s in rows
    ]


# ============================================================ 智能辅助 · 报告模板与推送


class ReportTemplateIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    period: str = Field(default="daily", pattern="^(daily|weekly|monthly|custom)$")
    scope_level: str = Field(default="center", pattern="^(center|dept|grassroots|personal)$")
    sections: list[dict] = Field(default_factory=list)
    variables: dict = Field(default_factory=dict)


def _template_out(t: SpdReportTemplate) -> dict:
    return {
        "id": t.id, "code": t.code, "name": t.name, "period": t.period,
        "scope_level": t.scope_level, "sections": t.sections or [],
        "variables": t.variables or {}, "active": t.active,
    }


@router.post("/report-templates", status_code=201,
             dependencies=[Depends(require_roles("director"))])
def create_report_template(body: ReportTemplateIn, db: Session = Depends(get_db)):
    if not body.sections:
        raise HTTPException(status_code=422, detail="报告模板至少要有一个内容段落")
    template = SpdReportTemplate(**body.model_dump())
    db.add(template)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该报告模板编码已存在") from None
    return _template_out(template)


@router.get("/report-templates")
def list_report_templates(period: str | None = None, db: Session = Depends(get_db)):
    query = db.query(SpdReportTemplate)
    if period:
        query = query.filter(SpdReportTemplate.period == period)
    return [_template_out(t) for t in query.order_by(SpdReportTemplate.id).limit(100).all()]


@router.patch("/report-templates/{template_id}",
              dependencies=[Depends(require_roles("director"))])
def update_report_template(template_id: int, body: dict, db: Session = Depends(get_db)):
    template = db.get(SpdReportTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="报告模板不存在")
    for key in ("name", "period", "scope_level", "sections", "variables", "active"):
        if key in body:
            setattr(template, key, body[key])
    db.commit()
    return _template_out(template)


class ReportTaskIn(BaseModel):
    template_id: int
    name: str = Field(min_length=1, max_length=64)
    frequency: str = Field(default="daily", pattern="^(daily|weekly|monthly|custom)$")
    push_time: str = Field(default="08:00", max_length=5)
    subscriber_ids: list[int] = Field(default_factory=list)
    org_ids: list[int] = Field(default_factory=list)
    valid_from: OptionalDateStr = ""
    valid_to: OptionalDateStr = ""
    priority: int = Field(default=1, ge=1, le=9)


def _task_out(t: SpdReportTask) -> dict:
    return {
        "id": t.id, "template_id": t.template_id, "name": t.name,
        "frequency": t.frequency, "push_time": t.push_time,
        "subscriber_ids": t.subscriber_ids or [], "org_ids": t.org_ids or [],
        "valid_from": t.valid_from, "valid_to": t.valid_to, "priority": t.priority,
        "status": t.status,
        "last_run_at": t.last_run_at.isoformat() if t.last_run_at else "",
    }


@router.post("/report-tasks", status_code=201, dependencies=[Depends(require_roles("director"))])
def create_report_task(body: ReportTaskIn, db: Session = Depends(get_db)):
    if db.get(SpdReportTemplate, body.template_id) is None:
        raise HTTPException(status_code=404, detail="报告模板不存在")
    task = SpdReportTask(**body.model_dump())
    db.add(task)
    db.commit()
    return _task_out(task)


@router.get("/report-tasks")
def list_report_tasks(status: str | None = None, db: Session = Depends(get_db)):
    query = db.query(SpdReportTask)
    if status:
        query = query.filter(SpdReportTask.status == status)
    return [
        _task_out(t)
        for t in query.order_by(SpdReportTask.priority, SpdReportTask.id).limit(200).all()
    ]


@router.patch("/report-tasks/{task_id}", dependencies=[Depends(require_roles("director"))])
def update_report_task(task_id: int, body: dict, db: Session = Depends(get_db)):
    """启用 / 暂停 / 改频率 / 调优先级。删除也走这里（status=deleted 由前端不再展示）。"""
    task = db.get(SpdReportTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="报告推送任务不存在")
    for key in ("name", "frequency", "push_time", "subscriber_ids", "org_ids",
                "valid_from", "valid_to", "priority", "status"):
        if key in body:
            setattr(task, key, body[key])
    db.commit()
    return _task_out(task)


@router.delete("/report-tasks/{task_id}", status_code=204,
               dependencies=[Depends(require_roles("director"))])
def delete_report_task(task_id: int, db: Session = Depends(get_db)):
    task = db.get(SpdReportTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="报告推送任务不存在")
    db.delete(task)
    db.commit()
    return Response(status_code=204)


class GenerateReportIn(BaseModel):
    task_id: int | None = None
    template_code: str = Field(default="", max_length=32)
    org_id: int | None = None
    period_label: str = Field(default="", max_length=32)


@router.post("/report-instances", status_code=201,
             dependencies=[Depends(require_roles("director", "doctor"))])
def generate_report(
    body: GenerateReportIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """按模板生成一份报告实例（智能辅助端 #1/#3）。

    段落内容经 `spd/reporting.py` 的注册表从本平台各业务表实时聚合——报告不是
    另存一份统计结果，而是**同一批数字的另一种排版**；指标段落（key=indicator）
    直接复用考核指标库的取数与公式，从结构上保证报表与考核同源。
    """
    task = db.get(SpdReportTask, body.task_id) if body.task_id is not None else None
    if body.task_id is not None and task is None:
        raise HTTPException(status_code=404, detail="报告推送任务不存在")
    template = (
        db.get(SpdReportTemplate, task.template_id) if task is not None
        else db.query(SpdReportTemplate)
        .filter(SpdReportTemplate.code == body.template_code).first()
    )
    if template is None:
        raise HTTPException(status_code=404, detail="报告模板不存在")

    org_id = body.org_id if body.org_id is not None else user.org_id
    period_label = body.period_label or default_period_label(template.period)
    content = {
        "period_label": period_label,
        "sections": [
            compose_section(db, section, org_id, template.period)
            for section in template.sections or []
        ],
    }
    instance = SpdReportInstance(
        task_id=task.id if task else None, template_code=template.code,
        title=f"{template.name}（{period_label}）", period_label=period_label,
        scope_level=template.scope_level, org_id=org_id, content=content,
        subscriber_ids=(task.subscriber_ids if task else []) or [],
    )
    db.add(instance)
    if task is not None:
        task.last_run_at = now_naive()
    db.commit()
    return {
        "id": instance.id, "title": instance.title, "period_label": period_label,
        "content": content,
    }


@router.get("/report-instances")
def list_report_instances(
    response: Response,
    template_code: str | None = None,
    mine: bool = False,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(SpdReportInstance)
    if template_code:
        query = query.filter(SpdReportInstance.template_code == template_code)
    rows = paginate(query.order_by(SpdReportInstance.id.desc()), response, offset, limit)
    if mine:
        rows = [
            r for r in rows
            if not (r.subscriber_ids or []) or user.id in (r.subscriber_ids or [])
        ]
    return [
        {"id": r.id, "title": r.title, "template_code": r.template_code,
         "period_label": r.period_label, "scope_level": r.scope_level,
         "org_id": r.org_id, "created_at": r.created_at.isoformat()}
        for r in rows
    ]


@router.get("/report-instances/{instance_id}")
def get_report_instance(instance_id: int, db: Session = Depends(get_db)):
    instance = db.get(SpdReportInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    return {
        "id": instance.id, "title": instance.title,
        "template_code": instance.template_code, "period_label": instance.period_label,
        "scope_level": instance.scope_level, "org_id": instance.org_id,
        "content": instance.content or {},
        "subscriber_ids": instance.subscriber_ids or [],
        "created_at": instance.created_at.isoformat(),
    }


@router.get("/health-calendar")
def health_calendar(
    patient_id: int, day: str = "", db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """患者健康日历（智能随访端 #12）：某天的随访、宣教与复诊安排。"""
    assert_patient_visible(db, user, patient_id, resource="spd_calendar")
    target = day or date.today().isoformat()
    followups = (
        db.query(SpdFollowupRecord)
        .filter(
            SpdFollowupRecord.patient_id == patient_id,
            SpdFollowupRecord.planned_at == target,
        )
        .all()
    )
    revisits = (
        db.query(SpdRevisit)
        .filter(SpdRevisit.patient_id == patient_id, SpdRevisit.plan_date == target)
        .all()
    )
    tasks = (
        db.query(SpdTask)
        .filter(SpdTask.patient_id == patient_id, SpdTask.due_date == target)
        .all()
    )
    return {
        "day": target,
        "followups": [_record_out(f) for f in followups],
        "revisits": [
            {"id": r.id, "plan_date": r.plan_date, "dept": r.dept, "items": r.items,
             "status": r.status}
            for r in revisits
        ],
        "tasks": [
            {"id": t.id, "title": t.title, "task_type": t.task_type, "status": t.status}
            for t in tasks
        ],
    }
