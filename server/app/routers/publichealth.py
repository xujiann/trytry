"""公卫协同：㉖应急处置指挥、㉗医防协同提醒、㉘其他卫生业务监测。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, FiniteFloat
from sqlalchemy.orm import Session

from ..concurrency import serialized_on
from ..visibility import assert_org_writable, assert_patient_visible
from ..database import get_db
from ..datetypes import OptionalDateStr
from ..texttypes import NON_BLANK
from ..deps import get_current_user, require_roles
from ..models import (
    ChronicDiseaseType,
    ChronicPatient,
    HealthMonitorRecord,
    Organization,
    Patient,
    PhEventAction,
    PublicHealthEvent,
    User,
)
from .chronic import guidance_for
from .vaccination import _effective_contraindications

router = APIRouter(prefix="/api/publichealth", tags=["公卫协同"], dependencies=[Depends(get_current_user)])

# ---------- ㉖ 应急处置指挥 ----------


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=256, pattern=NON_BLANK)
    level: str = Field(default="IV", pattern="^(I|II|III|IV)$")
    disease_name: str = Field(default="", max_length=128)
    description: str = Field(default="", max_length=1024)


class EventOut(EventCreate):
    id: int
    status: str
    # 出参不带「不能只填空格」（P1-109）：修之前存进去的纯空白行要原样读出来，而不是让整个清单 500
    title: str = Field(min_length=1, max_length=256)

    model_config = {"from_attributes": True}


@router.post(
    "/events",
    response_model=EventOut,
    status_code=201,
    dependencies=[Depends(require_roles("public_health", "doctor"))],  # H2/L5: 公卫事件
)
def create_event(body: EventCreate, db: Session = Depends(get_db)):
    event = PublicHealthEvent(**body.model_dump())
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


@router.get("/events", response_model=list[EventOut])
def list_events(status: str | None = None, db: Session = Depends(get_db)):
    query = db.query(PublicHealthEvent)
    if status:
        query = query.filter(PublicHealthEvent.status == status)
    return query.order_by(PublicHealthEvent.id.desc()).limit(100).all()


class ActionCreate(BaseModel):
    action: str = Field(min_length=1, max_length=512, pattern=NON_BLANK)
    actor: str = Field(default="", max_length=64)


class EventActionCreatedOut(BaseModel):
    """处置动作回执只回一个 id——与清单行四键不同形，两个模型。"""

    id: int


class EventActionOut(BaseModel):
    """处置留痕行。`at` 是 handler 里 `created_at.isoformat()` 过的字符串。"""

    id: int
    action: str
    actor: str
    at: str


@router.post(
    "/events/{event_id}/actions",
    response_model=EventActionCreatedOut,
    status_code=201,
    dependencies=[Depends(require_roles("public_health", "doctor"))],  # H2/L5: 事件处置
)
def add_action(event_id: int, body: ActionCreate, db: Session = Depends(get_db)):
    """处置动作留痕：应急值守、流调、资源调度等指挥记录。

    「处置中」在这起事件那一行的临界区里、刷新之后再判（P2-464）：结案也在同一个临界区里改状态。原先锁外判了
    「处置中」就插——判完、插入之前结案先提交，这条处置动作照样落库，已结案的事件上多出一条结案之后的处置记录
    （按顺序在结案之后记是 409）。INSERT 不给事件那一行加锁，判定与写入压不进一条 SQL，故用 `serialized_on`。
    """
    event = db.get(PublicHealthEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    with serialized_on(db, PublicHealthEvent, event.id):
        db.refresh(event)
        if event.status != "active":
            db.rollback()
            raise HTTPException(status_code=409, detail="事件已结案")
        action = PhEventAction(event_id=event_id, **body.model_dump())
        db.add(action)
        db.commit()
    return {"id": action.id}


@router.get("/events/{event_id}/actions", response_model=list[EventActionOut])
def list_actions(event_id: int, db: Session = Depends(get_db)):
    if db.get(PublicHealthEvent, event_id) is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    return [
        {"id": a.id, "action": a.action, "actor": a.actor, "at": a.created_at.isoformat()}
        for a in db.query(PhEventAction).filter(PhEventAction.event_id == event_id).order_by(PhEventAction.id).all()
    ]


@router.post(
    "/events/{event_id}/close",
    response_model=EventOut,
    dependencies=[Depends(require_roles("public_health", "doctor"))],  # H2/L5
)
def close_event(event_id: int, db: Session = Depends(get_db)):
    event = db.get(PublicHealthEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    # 与记处置动作同一个临界区（P2-464）：结案要么排在一条处置动作整个提交之后，要么让它随后判到「已结案」
    with serialized_on(db, PublicHealthEvent, event.id):
        db.refresh(event)
        if event.status != "active":
            db.rollback()
            raise HTTPException(status_code=409, detail="事件已结案")
        event.status = "closed"
        db.commit()
    db.refresh(event)
    return event


# ---------- ㉗ 医防协同提醒（诊间提醒） ----------


class ClinicReminderOut(BaseModel):
    """诊间提醒行：五种来源（慢病随访超期/慢病高危/生活方式指导/疫苗禁忌/
    处置中公卫事件）同形两键，`detail` 是服务端拼好的整句。"""

    type: str
    detail: str


class ClinicRemindersOut(BaseModel):
    patient_id: int
    reminders: list[ClinicReminderOut]


@router.get("/reminders/{patient_id}", response_model=ClinicRemindersOut)
def clinic_reminders(
    patient_id: int,
    today: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """诊间医防协同提醒：接诊时汇聚该患者的公卫待办与风险提示。

    L-2：默认取服务端当前日期；today 覆盖参数仅限测试/管理排查用途（YYYY-MM-DD）。
    """
    assert_patient_visible(db, user, patient_id, resource="publichealth")
    from ..deps import resolve_business_date

    if db.get(Patient, patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    cutoff = resolve_business_date(today).isoformat()
    reminders: list[dict] = []
    chronic = db.query(ChronicPatient).filter(ChronicPatient.patient_id == patient_id).all()
    # 整句里写病种的中文名（P2-880，与 P2-767 / P2-73 同一句：不把英文码拼进给人看的文字）：原先印「hypertension 分级3级，
    # 建议上转评估」「diabetes 随访已超期」；目录里没有的照旧印编码
    disease_names = {t.code: t.name for t in db.query(ChronicDiseaseType).filter(
        ChronicDiseaseType.code.in_([c.disease for c in chronic] or [""]))}
    for c in chronic:
        disease = disease_names.get(c.disease, c.disease)
        if c.next_due and c.next_due < cutoff:
            reminders.append({"type": "chronic_followup_overdue", "detail": f"{disease} 随访已超期（应访日期 {c.next_due}）"})
        if c.level == 3:
            reminders.append({"type": "chronic_high_risk", "detail": f"{disease} 分级3级，建议上转评估"})
        guidance = guidance_for(db, c.disease)
        if guidance:
            reminders.append({"type": "lifestyle_guidance", "detail": guidance})
    # 只提示生效中的禁忌（P2-132）：原先已解除、已过期的也一条不落地提示，同一时刻接种台放行这支疫苗
    for v in _effective_contraindications(db, patient_id, None, cutoff):
        reminders.append({"type": "vaccine_contraindication", "detail": f"疫苗 {v.vaccine_code} 禁忌：{v.reason}"})
    # 处置中的事件说出是哪起（P2-1435）：原先只给个数——「当前有 1 起突发公卫事件处置中」，处置中的是诺如病毒感染还是流感，
    # 医生不知道该问腹泻还是发热。列病种与级别，级别照事件列表的写法印「IV级」，没填病种的印事件名称；多起用顿号隔开、
    # 按立案先后倒序（同事件列表），超过 3 起只列最新 3 起、写「等 N 起」。仍是一条 active_ph_event，只改 detail 文字。
    # 处置中的事件同一时刻不过寥寥几起，整批取回、在这里取前 3 起：句子里要写总数，库里只取 3 起也还得另数一遍
    active = (db.query(PublicHealthEvent).filter(PublicHealthEvent.status == "active")
              .order_by(PublicHealthEvent.id.desc()).all())
    if active:
        shown = "、".join(f"{(e.disease_name or '').strip() or e.title}（{e.level}级）" for e in active[:3])
        more = f"等 {len(active)} 起" if len(active) > 3 else ""
        reminders.append({"type": "active_ph_event", "detail": f"突发公卫事件处置中：{shown}{more}，注意相关症状问诊"})
    return {"patient_id": patient_id, "reminders": reminders}


# ---------- ㉘ 其他卫生业务监测 ----------

_DOMAINS = {"nutrition", "environment", "occupational", "radiation", "school"}


class MonitorCreate(BaseModel):
    domain: str = Field(max_length=16)
    org_id: int
    indicator: str = Field(min_length=1, max_length=128, pattern=NON_BLANK)
    value: FiniteFloat
    threshold: FiniteFloat
    record_date: OptionalDateStr = ""


class MonitorOut(MonitorCreate):
    id: int
    exceeded: bool
    # 出参不要求有限值（P1-92）：PG 的浮点/金额列存得下 NaN，存量坏值要读成 null，而不是让整个响应 500
    value: float
    threshold: float
    # 出参不带入参的日历校验（P1-63）：库里的存量坏日期要原样读出来，而不是让响应 500
    record_date: str = ""
    # 出参不带「不能只填空格」（P1-109）：修之前存进去的纯空白行要原样读出来，而不是让整个清单 500
    indicator: str = Field(min_length=1, max_length=128)

    model_config = {"from_attributes": True}


@router.post(
    "/monitors",
    response_model=MonitorOut,
    status_code=201,
    dependencies=[Depends(require_roles("public_health", "doctor"))],  # H2/L5: 监测指标上报
)
def add_monitor(body: MonitorCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    if body.domain not in _DOMAINS:
        raise HTTPException(status_code=422, detail=f"未知监测领域: {body.domain}")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    record = HealthMonitorRecord(**body.model_dump(), exceeded=body.value > body.threshold)
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.get("/monitors", response_model=list[MonitorOut])
def list_monitors(domain: str | None = None, exceeded: bool | None = None, db: Session = Depends(get_db)):
    query = db.query(HealthMonitorRecord)
    if domain:
        query = query.filter(HealthMonitorRecord.domain == domain)
    if exceeded is not None:
        query = query.filter(HealthMonitorRecord.exceeded.is_(exceeded))
    return query.order_by(HealthMonitorRecord.id.desc()).limit(200).all()
