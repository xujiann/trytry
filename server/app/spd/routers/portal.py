"""全域慢专病 · 患者移动端（自主服务与健康管理端）。

对应招标文件患者移动端 #1~#20。挂在 `/api/portal/spd` 下而不是 `/api/spd`：
居民令牌带 `scope=portal`，`deps.get_current_user` 明确拒绝它进入业务接口。
这条边界是平台既有的横向隔离设计，慢专病不该在它上面开口子——
居民端的每个接口都必须经 `accessible_patient` 解析"这次要看谁的档案"，
包括为家人代管的那份。
"""
from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ...clock import now_naive
from ...concurrency import ensure_present, insert_if_absent, insert_or_conflict
from ...database import get_db
from ...deps import paginate
from ..platform import Encounter, Patient, ResidentAccount
from ..models import (
    SpdAssessment,
    SpdConsult,
    SpdConsultMessage,
    SpdEduMaterial,
    SpdEduPush,
    SpdEnrollment,
    SpdFollowupRecord,
    SpdIntervention,
    SpdMeasurement,
    SpdPackageBinding,
    SpdPathInstance,
    SpdProgram,
    SpdReferralCase,
    SpdReferralStep,
    SpdRevisit,
    SpdScale,
    SpdScreening,
    SpdServiceApply,
    SpdServicePackage,
    SpdTask,
    SpdTeam,
)
from ..rules import is_suspect_risk, score_scale
from ..service import close_followup_record, judge_measurement
from fastapi import File, Form, UploadFile

from ..platform import (
    accessible_patient,
    current_resident,
    store_attachment,
    valid_task_evidence,
)

router = APIRouter(prefix="/api/portal/spd", tags=["慢专病·患者移动端"])


def _patient(
    db: Session, account: ResidentAccount, patient_id: int | None, *, resource: str | None
) -> Patient:
    """解析并校验"这次要看谁的档案"，读接口顺带落调阅留痕（AccessLog）。

    与平台 `accessible_patient` 同一契约（经 platform 适配层，边界不破）：
    **读接口**给一个 `spd_` 前缀的资源词（沿用业务端 spd 路由的既有词表），
    留痕主体 `resident:{account_id}`、依据 self/delegate；**写接口**传 ``None``
    （写操作由审计中间件按同一居民主体落 AuditLog）。
    """
    return accessible_patient(db, account, patient_id, resource=resource)


def _program_names(db: Session, codes: list[str]) -> dict[str, str]:
    return {
        p.code: p.name
        for p in db.query(SpdProgram).filter(SpdProgram.code.in_(codes or [""])).all()
    }


# ============================================================ 首页与档案


class SpdPatientBriefOut(BaseModel):
    id: int
    name: str
    gender: str
    birth_date: str


class SpdHomeProgramOut(BaseModel):
    program_code: str
    program_name: str
    stage: str
    risk_level: str
    # 尚未分派团队/主管医生时为 null（外键可空），照实建模
    team_id: int | None
    team_name: str
    doctor_user_id: int | None
    next_followup_at: str


class SpdLatestMetricOut(BaseModel):
    value: float
    unit: str
    level: str
    measured_at: str


class SpdHomeTodoOut(BaseModel):
    followups: int
    tasks: int
    revisits: int
    interventions: int
    unread_edu: int


class SpdHomePackageOut(BaseModel):
    binding_id: int
    name: str
    program_code: str
    total: int
    used: int
    # 恒为 float：`used / total * 100` 走真除法，无服务项时字面量 0.0
    progress: float
    period_end: str


class SpdHomeOut(BaseModel):
    """居民首页。

    `latest_metrics` 的键是**指标名**，且只有测过的指标才在里面——固定字段
    的模型会给没测过的指标注入 `null`，故用 dict。取值范围是代码里的五项
    （bp_sys/bp_dia/glucose_fasting/bmi/spo2），不是任意键。
    """

    patient: SpdPatientBriefOut
    programs: list[SpdHomeProgramOut]
    latest_metrics: dict[str, SpdLatestMetricOut]
    todo: SpdHomeTodoOut
    packages: list[SpdHomePackageOut]
    enrolled: bool


@router.get("/home", response_model=SpdHomeOut)
def home(
    patient_id: int | None = None,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """居民首页（#1/#2）：签约团队、主管医生、纳管病种、最新指标、待办与服务包进度。"""
    patient = _patient(db, account, patient_id, resource="spd_home")
    enrollments = (
        db.query(SpdEnrollment)
        .filter(SpdEnrollment.patient_id == patient.id, SpdEnrollment.status == "active")
        .all()
    )
    names = _program_names(db, [e.program_code for e in enrollments])
    teams: dict[int | None, str] = {
        t.id: t.name
        for t in db.query(SpdTeam).filter(
            SpdTeam.id.in_([e.team_id for e in enrollments if e.team_id] or [0])
        )
    }
    latest: dict[str, dict] = {}
    for metric in ("bp_sys", "bp_dia", "glucose_fasting", "bmi", "spo2"):
        row = (
            db.query(SpdMeasurement)
            .filter(SpdMeasurement.patient_id == patient.id, SpdMeasurement.metric == metric)
            .order_by(SpdMeasurement.measured_at.desc())
            .first()
        )
        if row is not None:
            latest[metric] = {"value": row.value, "unit": row.unit, "level": row.level,
                              "measured_at": row.measured_at.isoformat()}

    today = date.today().isoformat()
    packages = []
    for enrollment in enrollments:
        for binding in (
            db.query(SpdPackageBinding)
            .filter(
                SpdPackageBinding.enrollment_id == enrollment.id,
                SpdPackageBinding.status == "bound",
            )
            .all()
        ):
            items = binding.items or []
            total = sum(int(i.get("total", 0)) for i in items)
            used = sum(int(i.get("used", 0)) for i in items)
            package = db.get(SpdServicePackage, binding.package_id)
            packages.append({
                "binding_id": binding.id,
                "name": package.name if package else "",
                "program_code": enrollment.program_code,
                "total": total, "used": used,
                "progress": round(used / total * 100, 1) if total else 0.0,
                "period_end": binding.period_end,
            })

    return {
        "patient": {"id": patient.id, "name": patient.name, "gender": patient.gender,
                    "birth_date": patient.birth_date},
        "programs": [
            {"program_code": e.program_code, "program_name": names.get(e.program_code, ""),
             "stage": e.stage, "risk_level": e.risk_level,
             "team_id": e.team_id, "team_name": teams.get(e.team_id, ""),
             "doctor_user_id": e.doctor_user_id,
             "next_followup_at": e.next_followup_at}
            for e in enrollments
        ],
        "latest_metrics": latest,
        "todo": {
            "followups": db.query(SpdFollowupRecord).filter(
                SpdFollowupRecord.patient_id == patient.id,
                SpdFollowupRecord.status == "planned",
            ).count(),
            "tasks": db.query(SpdTask).filter(
                SpdTask.patient_id == patient.id,
                SpdTask.status.in_(["pending", "claimed", "doing", "overdue"]),
            ).count(),
            "revisits": db.query(SpdRevisit).filter(
                SpdRevisit.patient_id == patient.id, SpdRevisit.status == "planned",
                SpdRevisit.plan_date >= today,
            ).count(),
            "interventions": db.query(SpdIntervention).filter(
                SpdIntervention.patient_id == patient.id,
                SpdIntervention.status.in_(["planned", "doing"]),
            ).count(),
            "unread_edu": db.query(SpdEduPush).filter(
                SpdEduPush.patient_id == patient.id, SpdEduPush.status == "sent",
            ).count(),
        },
        "packages": packages,
        "enrolled": bool(enrollments),
    }


class SpdArchivePatientOut(SpdPatientBriefOut):
    phone: str
    ehc_no: str


class SpdArchiveProfileOut(BaseModel):
    program_code: str
    program_name: str
    status: str
    stage: str
    risk_level: str
    # 四个都是 JSON 列，内容由业务配置决定，宽类型如实反映
    habits: dict[str, Any]
    risk_factors: list[Any]
    complications: list[Any]
    tags: list[Any]


class SpdTimelineItemOut(BaseModel):
    """时间轴条目。就诊与随访两个来源**同形**（合并后还要一起排序），
    故只建一个模型——两种 kind 的键集合必须一致，否则排序键都对不齐。"""

    kind: str
    at: str
    title: str
    detail: str


class SpdArchiveOut(BaseModel):
    patient: SpdArchivePatientOut
    profiles: list[SpdArchiveProfileOut]
    timeline: list[SpdTimelineItemOut]


@router.get("/archive", response_model=SpdArchiveOut)
def archive(
    patient_id: int | None = None,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """个人全周期档案（#3）：基本资料、生活习惯、危险因素 + 就诊时间线。"""
    patient = _patient(db, account, patient_id, resource="spd_archive")
    enrollments = (
        db.query(SpdEnrollment).filter(SpdEnrollment.patient_id == patient.id).all()
    )
    names = _program_names(db, [e.program_code for e in enrollments])
    encounters = (
        db.query(Encounter)
        .filter(Encounter.patient_id == patient.id)
        .order_by(Encounter.id.desc())
        .limit(30)
        .all()
    )
    followups = (
        db.query(SpdFollowupRecord)
        .filter(
            SpdFollowupRecord.patient_id == patient.id,
            SpdFollowupRecord.status == "done",
        )
        .order_by(SpdFollowupRecord.id.desc())
        .limit(20)
        .all()
    )
    timeline = [
        {"kind": "encounter", "at": e.created_at.isoformat(),
         "title": e.diagnosis_name or "就诊", "detail": e.summary or ""}
        for e in encounters
    ] + [
        {"kind": "followup", "at": f.executed_at or f.planned_at,
         "title": f"{f.scene}随访", "detail": f.result or ""}
        for f in followups
    ]
    timeline.sort(key=lambda item: item["at"], reverse=True)
    return {
        "patient": {"id": patient.id, "name": patient.name, "gender": patient.gender,
                    "birth_date": patient.birth_date, "phone": patient.phone,
                    "ehc_no": patient.ehc_no},
        "profiles": [
            {"program_code": e.program_code, "program_name": names.get(e.program_code, ""),
             "status": e.status, "stage": e.stage, "risk_level": e.risk_level,
             "habits": e.habits or {}, "risk_factors": e.risk_factors or [],
             "complications": e.complications or [], "tags": e.tags or []}
            for e in enrollments
        ],
        "timeline": timeline[:50],
    }


# ============================================================ 居家监测


class SelfMeasureIn(BaseModel):
    patient_id: int | None = None
    metric: str = Field(min_length=1, max_length=32)
    value: float
    unit: str = Field(default="", max_length=16)
    program_code: str = Field(default="", max_length=32)
    source: str = Field(default="manual", pattern="^(manual|device)$")
    device_sn: str = Field(default="", max_length=64)
    note: str = Field(default="", max_length=256)


class SpdMeasurementCreatedOut(BaseModel):
    id: int
    level: str
    measured_at: str


@router.post("/measurements", response_model=SpdMeasurementCreatedOut, status_code=201)
def add_measurement(
    body: SelfMeasureIn,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """居家健康台账（#4）：手工记录或设备回传，落库即按管理目标判定等级。"""
    patient = _patient(db, account, body.patient_id, resource=None)  # 写走 AuditLog
    enrollment = (
        db.query(SpdEnrollment)
        .filter(
            SpdEnrollment.patient_id == patient.id,
            SpdEnrollment.program_code == body.program_code,
        )
        .first()
        if body.program_code else None
    )
    level = judge_measurement(
        db, body.program_code, enrollment.stage if enrollment else "", body.metric, body.value
    )
    record = SpdMeasurement(
        patient_id=patient.id, program_code=body.program_code, metric=body.metric,
        value=body.value, unit=body.unit, level=level, source=body.source,
        device_sn=body.device_sn, note=body.note,
    )
    db.add(record)
    db.commit()
    return {"id": record.id, "level": level, "measured_at": record.measured_at.isoformat()}


class SpdMeasurementOut(BaseModel):
    id: int
    metric: str
    # Float 列：整数读回来也是 float（140 → 140.0），与 Money 列的陷阱相反，
    # 这里声明 float 才是原样
    value: float
    unit: str
    level: str
    source: str
    measured_at: str


@router.get("/measurements", response_model=list[SpdMeasurementOut])
def list_measurements(
    response: Response,
    metric: str = "",
    patient_id: int | None = None,
    days: int = 90,
    offset: int = 0,
    limit: int = 200,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """指标历史与趋势（#7）。按日返回原始点，前端自行按日/周/月切换展示。"""
    patient = _patient(db, account, patient_id, resource="spd_measurement")
    since = now_naive() - timedelta(days=max(min(days, 730), 1))
    query = db.query(SpdMeasurement).filter(
        SpdMeasurement.patient_id == patient.id, SpdMeasurement.measured_at >= since
    )
    if metric:
        query = query.filter(SpdMeasurement.metric == metric)
    rows = paginate(query.order_by(SpdMeasurement.measured_at.desc()), response, offset, limit)
    return [
        {"id": r.id, "metric": r.metric, "value": r.value, "unit": r.unit,
         "level": r.level, "source": r.source, "measured_at": r.measured_at.isoformat()}
        for r in rows
    ]


# ============================================================ 风险自查与服务申请


class SpdScaleOut(BaseModel):
    """量表。`items` 是题目数组（key/type/options 由 rules.score_scale 消费），
    题目字段随题型而变，故用宽字典——但元素必须是对象，否则评分函数本身会炸。"""

    id: int
    code: str
    name: str
    program_code: str
    items: list[dict[str, Any]]


@router.get("/scales", response_model=list[SpdScaleOut])
def list_screen_scales(program_code: str = "", db: Session = Depends(get_db)):
    """可自查的筛查量表清单（#11/#13）。只暴露已发布的筛查类量表。"""
    query = db.query(SpdScale).filter(
        SpdScale.status == "published", SpdScale.category == "screen"
    )
    if program_code:
        query = query.filter(SpdScale.program_code == program_code)
    return [
        {"id": s.id, "code": s.code, "name": s.name, "program_code": s.program_code,
         "items": s.items or []}
        for s in query.order_by(SpdScale.id).limit(50).all()
    ]


class SpdScaleByTokenOut(BaseModel):
    id: int
    code: str
    name: str
    category: str
    items: list[dict[str, Any]]


@router.get("/scales/by-token/{qr_token}", response_model=SpdScaleByTokenOut)
def scale_by_token(qr_token: str, db: Session = Depends(get_db)):
    """扫码进入评估问卷（#5）。令牌无效一律 404，不透露量表是否存在。"""
    scale = (
        db.query(SpdScale)
        .filter(SpdScale.qr_token == qr_token, SpdScale.status == "published")
        .first()
    )
    if scale is None:
        raise HTTPException(status_code=404, detail="二维码无效或已失效")
    return {"id": scale.id, "code": scale.code, "name": scale.name,
            "category": scale.category, "items": scale.items or []}


class SelfScreenIn(BaseModel):
    patient_id: int | None = None
    program_code: str = Field(min_length=1, max_length=32)
    scale_code: str = Field(default="", max_length=32)
    answers: dict = Field(default_factory=dict)
    draft: bool = False


class SpdScreeningOut(BaseModel):
    """自查结果。这个端点有**三种形状**，全靠条件键区分：

    1. 草稿 + 有量表 → `draft/score/risk_level/advice/answered/total_items`
       （后两个来自 `score_scale`，答了几题、共几题）；
    2. 草稿 + 无量表 → `draft/score/risk_level/advice`（无量表时是固定兜底值，
       没有 answered/total_items 可言）；
    3. 落库 → `id/score/risk_level/result/advice/can_apply`。

    逐字段建模会把三种形状的字段互相注入 `null`（草稿响应里冒出 `"id": null`、
    落库响应里冒出 `"answered": null`），故带 `response_model_exclude_unset=True`。

    `score` 是 `int | float` 而不是 float：有量表时是 `round(total, 2)`（float），
    无量表时是字面量 `0`（int）。声明成 float 会把兜底分从 `0` 变成 `0.0`。

    字段顺序必须与 handler 的出键顺序一致（`result` 在 `advice` 之前）——
    序列化按模型声明顺序走，顺序不同即改字节。写这个模型时就排错过一次，
    逐字节比对当场抓到。
    """

    draft: bool | None = None
    id: int | None = None
    score: int | float | None = None
    risk_level: str | None = None
    result: str | None = None
    advice: str | None = None
    answered: int | None = None
    total_items: int | None = None
    can_apply: bool | None = None


@router.post("/screenings", response_model=SpdScreeningOut,
             response_model_exclude_unset=True, status_code=201)
def self_screening(
    body: SelfScreenIn,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """居民高危自查（#11/#13）：生成风险分级与健康建议，中高风险可申请专病服务。

    自查结果**不直接进目标池**——居民自填的问卷不足以作为纳管依据，
    要经基层复核。这里只落一条 `source='self'` 的筛查记录，
    在中心端的"待复核"清单里等人看。
    """
    patient = _patient(db, account, body.patient_id, resource=None)  # 写走 AuditLog
    scale = None
    if body.scale_code:
        scale = (
            db.query(SpdScale)
            .filter(SpdScale.code == body.scale_code, SpdScale.status == "published")
            .order_by(SpdScale.id.desc())
            .first()
        )
        if scale is None:
            raise HTTPException(status_code=404, detail="量表不存在或未发布")
    graded = (
        score_scale(scale.items or [], body.answers, scale.scoring or {})
        if scale else {"score": 0, "risk_level": "low", "advice": ""}
    )
    if body.draft:
        return {"draft": True, **graded}
    # graded 的两个来源（量表评分 / 无量表默认）拼出来是宽类型，
    # 这里先收敛成 str，风险等级判定与入库用同一个值。
    risk: str = str(graded["risk_level"] or "low")
    record = SpdScreening(
        patient_id=patient.id, program_code=body.program_code, source="self",
        scale_code=body.scale_code, answers=body.answers, score=graded["score"],
        risk_level=risk,
        result="suspect" if is_suspect_risk(risk) else "normal",
        advice=graded["advice"],
    )
    db.add(record)
    db.commit()
    return {
        "id": record.id, "score": record.score, "risk_level": record.risk_level,
        "result": record.result, "advice": record.advice,
        "can_apply": is_suspect_risk(record.risk_level),
    }


class ApplyIn(BaseModel):
    patient_id: int | None = None
    program_code: str = Field(min_length=1, max_length=32)
    screening_id: int | None = None
    note: str = Field(default="", max_length=512)


class SpdServiceApplyCreatedOut(BaseModel):
    id: int
    status: str


@router.post("/service-applies", response_model=SpdServiceApplyCreatedOut, status_code=201)
def apply_service(
    body: ApplyIn,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """提交专病服务申请（#2）。同一病种已有待受理申请时不重复提交。"""
    patient = _patient(db, account, body.patient_id, resource=None)  # 写走 AuditLog
    pending = (
        db.query(SpdServiceApply)
        .filter(
            SpdServiceApply.patient_id == patient.id,
            SpdServiceApply.program_code == body.program_code,
            SpdServiceApply.status == "pending",
        )
        .first()
    )
    if pending is not None:
        raise HTTPException(status_code=409, detail="该病种已有待受理的服务申请")
    apply = SpdServiceApply(
        patient_id=patient.id, program_code=body.program_code,
        screening_id=body.screening_id, note=body.note, status="pending",
    )
    # 上面的"有没有待受理"是 check-then-act：并发/连点两路都查不到就都建，多出来的
    # 那条待受理会一直挂在团队收件箱里等人拒绝。部分唯一索引
    # uq_spd_apply_pending_patient_program 兜底，抢输者拿到的 409 与顺序请求一字不差。
    insert_or_conflict(db, apply, "该病种已有待受理的服务申请")
    return {"id": apply.id, "status": apply.status}


class SpdServiceApplyOut(BaseModel):
    id: int
    program_code: str
    status: str
    note: str
    handle_note: str
    created_at: str


@router.get("/service-applies", response_model=list[SpdServiceApplyOut])
def my_applies(
    patient_id: int | None = None,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    patient = _patient(db, account, patient_id, resource="spd_apply")
    rows = (
        db.query(SpdServiceApply)
        .filter(SpdServiceApply.patient_id == patient.id)
        .order_by(SpdServiceApply.id.desc())
        .limit(50)
        .all()
    )
    return [
        {"id": r.id, "program_code": r.program_code, "status": r.status,
         "note": r.note, "handle_note": r.handle_note,
         "created_at": r.created_at.isoformat()}
        for r in rows
    ]


# ============================================================ 全流程进度与任务


class SpdJourneyPathOut(BaseModel):
    id: int
    template_code: str
    current_node_key: str
    progress: int
    status: str


class SpdJourneyTaskOut(BaseModel):
    id: int
    title: str
    task_type: str
    status: str
    due_date: str


class SpdJourneyReferralOut(BaseModel):
    id: int
    direction: str
    status: str
    created_at: str


class SpdJourneyProgramOut(BaseModel):
    program_code: str
    program_name: str
    stage: str
    risk_level: str
    status: str
    paths: list[SpdJourneyPathOut]
    tasks: list[SpdJourneyTaskOut]
    referrals: list[SpdJourneyReferralOut]


class SpdJourneyOut(BaseModel):
    programs: list[SpdJourneyProgramOut]


@router.get("/journey", response_model=SpdJourneyOut)
def journey(
    program_code: str = "",
    patient_id: int | None = None,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """居民专病全流程视图（#14）：病种、团队、风险、阶段、节点进度与时间轴。"""
    patient = _patient(db, account, patient_id, resource="spd_journey")
    query = db.query(SpdEnrollment).filter(SpdEnrollment.patient_id == patient.id)
    if program_code:
        query = query.filter(SpdEnrollment.program_code == program_code)
    enrollments = query.all()
    names = _program_names(db, [e.program_code for e in enrollments])
    out = []
    for enrollment in enrollments:
        instances = (
            db.query(SpdPathInstance)
            .filter(SpdPathInstance.enrollment_id == enrollment.id)
            .all()
        )
        tasks = (
            db.query(SpdTask)
            .filter(SpdTask.enrollment_id == enrollment.id)
            .order_by(SpdTask.id.desc())
            .limit(30)
            .all()
        )
        referrals = (
            db.query(SpdReferralCase)
            .filter(
                SpdReferralCase.patient_id == patient.id,
                SpdReferralCase.program_code == enrollment.program_code,
            )
            .order_by(SpdReferralCase.id.desc())
            .limit(10)
            .all()
        )
        out.append({
            "program_code": enrollment.program_code,
            "program_name": names.get(enrollment.program_code, ""),
            "stage": enrollment.stage, "risk_level": enrollment.risk_level,
            "status": enrollment.status,
            "paths": [
                {"id": i.id, "template_code": i.template_code,
                 "current_node_key": i.current_node_key, "progress": i.progress,
                 "status": i.status}
                for i in instances
            ],
            "tasks": [
                {"id": t.id, "title": t.title, "task_type": t.task_type,
                 "status": t.status, "due_date": t.due_date}
                for t in tasks
            ],
            "referrals": [
                {"id": r.id, "direction": r.direction, "status": r.status,
                 "created_at": r.created_at.isoformat()}
                for r in referrals
            ],
        })
    return {"programs": out}


class TaskSubmitIn(BaseModel):
    patient_id: int | None = None
    result: dict = Field(default_factory=dict)
    # 附件 id 列表（经 POST /api/portal/spd/tasks/{id}/attachments 上传后获得）
    evidence: list[int | str] = Field(default_factory=list)


class SpdTaskOut(BaseModel):
    id: int
    title: str
    task_type: str
    status: str
    due_date: str
    form_code: str
    require_evidence: bool
    # 任务填报结果：字段由 form_code 指定的表单决定，故宽字典
    result: dict[str, Any]
    review_note: str


class SpdTaskStatusOut(BaseModel):
    id: int
    status: str


@router.get("/tasks", response_model=list[SpdTaskOut])
def my_tasks(
    patient_id: int | None = None,
    status: str = "",
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """居民健康任务（#15）：需要本人填报的任务清单。"""
    patient = _patient(db, account, patient_id, resource="spd_task")
    query = db.query(SpdTask).filter(SpdTask.patient_id == patient.id)
    if status:
        query = query.filter(SpdTask.status == status)
    rows = query.order_by(SpdTask.due_date).limit(100).all()
    return [
        {"id": r.id, "title": r.title, "task_type": r.task_type, "status": r.status,
         "due_date": r.due_date, "form_code": r.form_code,
         "require_evidence": r.require_evidence, "result": r.result or {},
         "review_note": r.review_note}
        for r in rows
    ]


@router.post("/tasks/{task_id}/submit", response_model=SpdTaskStatusOut)
def submit_task(
    task_id: int,
    body: TaskSubmitIn,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """居民提交任务（#15）：填体征/服药/症状、上传凭证，提交后转为待审核。

    居民提交一律落 `submitted` 而不是 `done`——居民填的内容要由医护确认，
    否则"任务完成率"会变成"居民点了几次提交"。
    """
    patient = _patient(db, account, body.patient_id, resource=None)  # 写走 AuditLog
    task = db.get(SpdTask, task_id)
    if task is None or task.patient_id != patient.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status in ("done", "cancelled"):
        raise HTTPException(status_code=409, detail="该任务已结束")
    if body.evidence:
        problems = valid_task_evidence(db, task.id, body.evidence)
        if problems:
            raise HTTPException(status_code=422, detail="；".join(problems))
        task.evidence = body.evidence
    if task.require_evidence and not (task.evidence or []):
        raise HTTPException(status_code=422, detail="该任务需要上传照片或报告等凭证")
    task.result = body.result
    task.status = "submitted"
    db.commit()
    return {"id": task.id, "status": task.status}


# ============================================================ 随访 / 干预 / 宣教 / 复诊


class SpdTaskAttachmentOut(BaseModel):
    attachment_id: int
    filename: str
    size: int


@router.post("/tasks/{task_id}/attachments", response_model=SpdTaskAttachmentOut,
             status_code=201)
async def upload_task_evidence(
    task_id: int,
    file: UploadFile = File(...),
    patient_id: int | None = Form(default=None),
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """居民为自己的任务上传佐证材料（患者端 #15 的"上传照片或报告等凭证"）。

    平台附件路由只认业务令牌，居民走不进去，所以这里开一条居民通道——
    但存储、白名单、限额与去重经 `platform.store_attachment` 复用**同一份**实现，
    只有鉴权（居民令牌 + 本人任务）是这里自己的。`uploaded_by` 记 NULL：
    居民不在 users 表内，伪造一个工作人员 id 会在 PG 上撞外键、在审计上撒谎。
    """
    patient = _patient(db, account, patient_id, resource=None)  # 写走 AuditLog
    task = db.get(SpdTask, task_id)
    if task is None or task.patient_id != patient.id:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status in ("done", "cancelled"):
        raise HTTPException(status_code=409, detail="该任务已结束")
    data = await file.read(10 * 1024 * 1024 + 1)
    attachment = store_attachment(
        db, data=data, filename=file.filename or "unnamed",
        content_type=file.content_type or "", owner_type="spd_task",
        owner_id=task.id, uploaded_by=None,
    )
    db.commit()
    return {"attachment_id": attachment.id, "filename": attachment.filename,
            "size": attachment.size}


class SpdFollowupOut(BaseModel):
    id: int
    scene: str
    planned_at: str
    # 未执行时为空串（不是 null）
    executed_at: str
    channel: str
    status: str
    result: str
    abnormal_level: str


@router.get("/followups", response_model=list[SpdFollowupOut])
def my_followups(
    patient_id: int | None = None,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """随访计划与历史记录（#9/#12）。"""
    patient = _patient(db, account, patient_id, resource="spd_followup")
    rows = (
        db.query(SpdFollowupRecord)
        .filter(SpdFollowupRecord.patient_id == patient.id)
        .order_by(SpdFollowupRecord.planned_at.desc())
        .limit(100)
        .all()
    )
    return [
        {"id": r.id, "scene": r.scene, "planned_at": r.planned_at,
         "executed_at": r.executed_at, "channel": r.channel, "status": r.status,
         "result": r.result, "abnormal_level": r.abnormal_level}
        for r in rows
    ]


class SelfFollowupIn(BaseModel):
    patient_id: int | None = None
    answers: dict = Field(default_factory=dict)


class SpdSelfAnswerOut(BaseModel):
    id: int
    abnormal_level: str
    # 无问卷或无异常规则命中时为空串
    action: str


@router.post("/followups/{record_id}/self-answer", response_model=SpdSelfAnswerOut)
def self_answer_followup(
    record_id: int,
    body: SelfFollowupIn,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """线上自助随访作答。异常分级与派单逻辑与医护执行时完全一致。"""
    from ..models import SpdQuestionnaire
    from ..rules import grade_abnormal

    patient = _patient(db, account, body.patient_id, resource=None)  # 写走 AuditLog
    record = db.get(SpdFollowupRecord, record_id)
    if record is None or record.patient_id != patient.id:
        raise HTTPException(status_code=404, detail="随访任务不存在")
    if record.status not in ("planned", "overdue"):
        # 超期的更该让居民补答，只挡已完成/已移除/失访
        raise HTTPException(status_code=409, detail="该随访已结束")
    record.answers = body.answers
    record.channel = "self"
    # 与医护执行同一道闸：办结的判定与写入压在同一条 UPDATE 里（见
    # service.close_followup_record）。居民连点两次、或居民与医护同时办同一条随访时，
    # 抢输的一路 rowcount 为 0，拿到与预检一致的 409，异常处置任务只派一次。
    if not close_followup_record(db, record.id, "done", allowed_from=("planned", "overdue")):
        db.rollback()  # 先退掉写事务再抛，避免后续审计落库撞写锁
        raise HTTPException(status_code=409, detail="该随访已结束")
    record.executed_at = date.today().isoformat()
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
            db.add(
                SpdTask(
                    program_code=record.program_code, patient_id=patient.id,
                    task_type="report", title=f"自助随访异常处置：{action or level}",
                    org_id=record.org_id, status="pending",
                    priority=3 if level == "high" else 2,
                    due_date=(date.today() + timedelta(days=1)).isoformat(),
                    source="followup",
                )
            )
    db.commit()
    return {"id": record.id, "abnormal_level": record.abnormal_level, "action": action}


class SpdInterventionOut(BaseModel):
    id: int
    goal: str
    content: str
    measures: str
    frequency: str
    next_at: str
    status: str
    feedback: str
    read: bool
    created_at: str


@router.get("/interventions", response_model=list[SpdInterventionOut])
def my_interventions(
    patient_id: int | None = None,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """医生推送的干预方案（#10）。"""
    patient = _patient(db, account, patient_id, resource="spd_intervention")
    rows = (
        db.query(SpdIntervention)
        .filter(SpdIntervention.patient_id == patient.id)
        .order_by(SpdIntervention.id.desc())
        .limit(50)
        .all()
    )
    return [
        {"id": r.id, "goal": r.goal, "content": r.content, "measures": r.measures,
         "frequency": r.frequency, "next_at": r.next_at, "status": r.status,
         "feedback": r.feedback, "read": r.read_at is not None,
         "created_at": r.created_at.isoformat()}
        for r in rows
    ]


class FeedbackIn(BaseModel):
    patient_id: int | None = None
    feedback: str = Field(default="", max_length=512)
    done: bool = False


class SpdInterventionFeedbackOut(BaseModel):
    id: int
    status: str
    read: bool


@router.post("/interventions/{intervention_id}/feedback",
             response_model=SpdInterventionFeedbackOut)
def feedback_intervention(
    intervention_id: int,
    body: FeedbackIn,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """标记已读并反馈执行情况（#10 的患者端闭环）。"""
    patient = _patient(db, account, body.patient_id, resource=None)  # 写走 AuditLog
    record = db.get(SpdIntervention, intervention_id)
    if record is None or record.patient_id != patient.id:
        raise HTTPException(status_code=404, detail="干预方案不存在")
    record.read_at = record.read_at or now_naive()
    if body.feedback:
        record.feedback = body.feedback
    if body.done:
        record.status = "done"
    db.commit()
    return {"id": record.id, "status": record.status, "read": True}


class SpdEduPushOut(BaseModel):
    """宣教推送。素材被删时四个素材字段回落为空串（不是 null）——
    推送记录还在，只是内容找不回来了。"""

    id: int
    material_id: int
    title: str
    media_type: str
    content: str
    media_url: str
    status: str
    created_at: str


@router.get("/edu", response_model=list[SpdEduPushOut])
def my_education(
    patient_id: int | None = None,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    patient = _patient(db, account, patient_id, resource="spd_edu")
    rows = (
        db.query(SpdEduPush)
        .filter(SpdEduPush.patient_id == patient.id)
        .order_by(SpdEduPush.id.desc())
        .limit(50)
        .all()
    )
    materials = {
        m.id: m
        for m in db.query(SpdEduMaterial)
        .filter(SpdEduMaterial.id.in_([r.material_id for r in rows] or [0]))
    }
    return [
        {"id": r.id, "material_id": r.material_id,
         "title": materials[r.material_id].title if r.material_id in materials else "",
         "media_type": materials[r.material_id].media_type if r.material_id in materials else "",
         "content": materials[r.material_id].content if r.material_id in materials else "",
         "media_url": materials[r.material_id].media_url if r.material_id in materials else "",
         "status": r.status, "created_at": r.created_at.isoformat()}
        for r in rows
    ]


class SpdEduReadOut(BaseModel):
    id: int
    status: str


@router.post("/edu/{push_id}/read", response_model=SpdEduReadOut)
def read_education(
    push_id: int,
    patient_id: int | None = None,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    patient = _patient(db, account, patient_id, resource=None)  # 写走 AuditLog
    push = db.get(SpdEduPush, push_id)
    if push is None or push.patient_id != patient.id:
        raise HTTPException(status_code=404, detail="宣教记录不存在")
    push.status = "read"
    push.read_at = push.read_at or now_naive()
    db.commit()
    return {"id": push.id, "status": push.status}


class SpdRevisitOut(BaseModel):
    id: int
    plan_date: str
    dept: str
    # items 是 String 列（顿号分隔的复诊项目文本），不是 JSON 数组
    items: str
    status: str
    actual_date: str


@router.get("/revisits", response_model=list[SpdRevisitOut])
def my_revisits(
    patient_id: int | None = None,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    patient = _patient(db, account, patient_id, resource="spd_revisit")
    rows = (
        db.query(SpdRevisit)
        .filter(SpdRevisit.patient_id == patient.id)
        .order_by(SpdRevisit.plan_date.desc())
        .limit(50)
        .all()
    )
    return [
        {"id": r.id, "plan_date": r.plan_date, "dept": r.dept, "items": r.items,
         "status": r.status, "actual_date": r.actual_date}
        for r in rows
    ]


class SpdAssessmentOut(BaseModel):
    id: int
    scale_code: str
    # Float 列：整数分读回来是 2.0
    score: float
    risk_level: str
    advice: str
    created_at: str


@router.get("/assessments", response_model=list[SpdAssessmentOut])
def my_assessments(
    patient_id: int | None = None,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    patient = _patient(db, account, patient_id, resource="spd_assessment")
    rows = (
        db.query(SpdAssessment)
        .filter(SpdAssessment.patient_id == patient.id)
        .order_by(SpdAssessment.id.desc())
        .limit(50)
        .all()
    )
    return [
        {"id": r.id, "scale_code": r.scale_code, "score": r.score,
         "risk_level": r.risk_level, "advice": r.advice,
         "created_at": r.created_at.isoformat()}
        for r in rows
    ]


# ============================================================ 转诊与咨询


class SpdReferralOut(BaseModel):
    id: int
    direction: str
    status: str
    current_level: str
    reason: str
    # 触发证据由触发规则决定字段（血压值、化验项…），故宽字典
    trigger_evidence: dict[str, Any]
    created_at: str


@router.get("/referrals", response_model=list[SpdReferralOut])
def my_referrals(
    patient_id: int | None = None,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """转诊记录与进度（#16/#17）。"""
    patient = _patient(db, account, patient_id, resource="spd_referral")
    rows = (
        db.query(SpdReferralCase)
        .filter(SpdReferralCase.patient_id == patient.id)
        .order_by(SpdReferralCase.id.desc())
        .limit(50)
        .all()
    )
    return [
        {"id": r.id, "direction": r.direction, "status": r.status,
         "current_level": r.current_level, "reason": r.reason,
         "trigger_evidence": r.trigger_evidence or {},
         "created_at": r.created_at.isoformat()}
        for r in rows
    ]


class SpdReferralStepOut(BaseModel):
    step: str
    action: str
    opinion: str
    created_at: str


class SpdReferralDetailOut(BaseModel):
    """转诊详情。**不继承** `SpdReferralOut`——列表有 `created_at` 而详情没有，
    继承会凭空要求一个详情不返回的字段（写这行时就是这么错的，被响应校验当场拦下）。
    字段顺序也与 handler 一致：序列化按模型声明顺序走，顺序不同即改字节。"""

    id: int
    direction: str
    status: str
    current_level: str
    reason: str
    trigger_evidence: dict[str, Any]
    materials: list[Any]
    steps: list[SpdReferralStepOut]


@router.get("/referrals/{case_id}", response_model=SpdReferralDetailOut)
def my_referral_detail(
    case_id: int,
    patient_id: int | None = None,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    patient = _patient(db, account, patient_id, resource="spd_referral")
    case = db.get(SpdReferralCase, case_id)
    if case is None or case.patient_id != patient.id:
        raise HTTPException(status_code=404, detail="转诊记录不存在")
    steps = (
        db.query(SpdReferralStep)
        .filter(SpdReferralStep.case_id == case_id)
        .order_by(SpdReferralStep.id)
        .all()
    )
    return {
        "id": case.id, "direction": case.direction, "status": case.status,
        "current_level": case.current_level, "reason": case.reason,
        "trigger_evidence": case.trigger_evidence or {},
        "materials": case.materials or [],
        "steps": [
            {"step": s.step, "action": s.action, "opinion": s.opinion,
             "created_at": s.created_at.isoformat()}
            for s in steps
        ],
    }


class ConsultIn(BaseModel):
    patient_id: int | None = None
    program_code: str = Field(default="", max_length=32)
    content: str = Field(min_length=1, max_length=2048)


class SpdConsultStartedOut(BaseModel):
    consult_id: int
    status: str


@router.post("/consults", response_model=SpdConsultStartedOut, status_code=201)
def start_consult(
    body: ConsultIn,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """发起或继续专病在线咨询（#18）。同病种已有开放会话时复用，不新开一条。"""
    patient = _patient(db, account, body.patient_id, resource=None)  # 写走 AuditLog
    consult = (
        db.query(SpdConsult)
        .filter(
            SpdConsult.patient_id == patient.id,
            SpdConsult.program_code == body.program_code,
            SpdConsult.status == "open",
        )
        .first()
    )
    if consult is None:
        enrollment = (
            db.query(SpdEnrollment)
            .filter(
                SpdEnrollment.patient_id == patient.id,
                SpdEnrollment.program_code == body.program_code,
            )
            .first()
        )
        consult = SpdConsult(
            patient_id=patient.id, program_code=body.program_code,
            doctor_id=enrollment.doctor_user_id if enrollment else None, status="open",
        )
        if not insert_if_absent(db, consult):
            # 并发抢输：另一路刚建出同病种的开放会话（撞 uq_spd_consult_open_patient_program）。
            # 与顺序第二次请求同一语义——复用那条，不新开也不 409，本条消息照样落进去；
            # 否则两条线程各显一半消息，医生列表与工作台也会各看到两条会话。
            opened = (
                db.query(SpdConsult)
                .filter(
                    SpdConsult.patient_id == patient.id,
                    SpdConsult.program_code == body.program_code,
                    SpdConsult.status == "open",
                )
                .first()
            )
            if opened is None:
                # 病态窗口：赢家刚提交、医生又立刻把会话关了。抛之前先退事务——
                # SAVEPOINT 回滚只退掉那一行，外层写事务还开着（见 insert_if_absent 文档）
                db.rollback()
            consult = ensure_present(opened, "开放咨询会话")
    db.add(
        SpdConsultMessage(
            consult_id=consult.id, sender="patient", sender_id=account.id,
            content=body.content,
        )
    )
    db.commit()
    return {"consult_id": consult.id, "status": consult.status}


class SpdConsultOut(BaseModel):
    id: int
    program_code: str
    # 未纳管（查不到 enrollment）时无主管医生，为 null
    doctor_id: int | None
    status: str
    created_at: str


@router.get("/consults", response_model=list[SpdConsultOut])
def my_consults(
    patient_id: int | None = None,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    patient = _patient(db, account, patient_id, resource="spd_consult")
    rows = (
        db.query(SpdConsult)
        .filter(SpdConsult.patient_id == patient.id)
        .order_by(SpdConsult.id.desc())
        .limit(30)
        .all()
    )
    return [
        {"id": r.id, "program_code": r.program_code, "doctor_id": r.doctor_id,
         "status": r.status, "created_at": r.created_at.isoformat()}
        for r in rows
    ]


class SpdConsultMessageOut(BaseModel):
    id: int
    sender: str
    content: str
    created_at: str


@router.get("/consults/{consult_id}/messages",
            response_model=list[SpdConsultMessageOut])
def my_consult_messages(
    consult_id: int,
    patient_id: int | None = None,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    patient = _patient(db, account, patient_id, resource="spd_consult")
    consult = db.get(SpdConsult, consult_id)
    if consult is None or consult.patient_id != patient.id:
        raise HTTPException(status_code=404, detail="咨询会话不存在")
    rows = (
        db.query(SpdConsultMessage)
        .filter(SpdConsultMessage.consult_id == consult_id)
        .order_by(SpdConsultMessage.id)
        .limit(500)
        .all()
    )
    return [
        {"id": m.id, "sender": m.sender, "content": m.content,
         "created_at": m.created_at.isoformat()}
        for m in rows
    ]
