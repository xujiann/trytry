"""公卫协同：㉖应急处置指挥、㉗医防协同提醒、㉘其他卫生业务监测。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import (
    ChronicPatient,
    HealthMonitorRecord,
    Organization,
    Patient,
    PhEventAction,
    PublicHealthEvent,
    VaccineContraindication,
)
from .chronic import GUIDANCE_POINTS

router = APIRouter(prefix="/api/publichealth", tags=["公卫协同"], dependencies=[Depends(get_current_user)])

# ---------- ㉖ 应急处置指挥 ----------


class EventCreate(BaseModel):
    title: str = Field(min_length=1)
    level: str = Field(default="IV", pattern="^(I|II|III|IV)$")
    disease_name: str = ""
    description: str = ""


class EventOut(EventCreate):
    id: int
    status: str

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
    action: str = Field(min_length=1)
    actor: str = ""


@router.post(
    "/events/{event_id}/actions",
    status_code=201,
    dependencies=[Depends(require_roles("public_health", "doctor"))],  # H2/L5: 事件处置
)
def add_action(event_id: int, body: ActionCreate, db: Session = Depends(get_db)):
    """处置动作留痕：应急值守、流调、资源调度等指挥记录。"""
    event = db.get(PublicHealthEvent, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    if event.status != "active":
        raise HTTPException(status_code=409, detail="事件已结案")
    action = PhEventAction(event_id=event_id, **body.model_dump())
    db.add(action)
    db.commit()
    return {"id": action.id}


@router.get("/events/{event_id}/actions")
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
    if event.status != "active":
        raise HTTPException(status_code=409, detail="事件已结案")
    event.status = "closed"
    db.commit()
    db.refresh(event)
    return event


# ---------- ㉗ 医防协同提醒（诊间提醒） ----------


@router.get("/reminders/{patient_id}")
def clinic_reminders(patient_id: int, today: str | None = None, db: Session = Depends(get_db)):
    """诊间医防协同提醒：接诊时汇聚该患者的公卫待办与风险提示。

    L-2：默认取服务端当前日期；today 覆盖参数仅限测试/管理排查用途（YYYY-MM-DD）。
    """
    from ..deps import resolve_business_date

    if db.get(Patient, patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    cutoff = resolve_business_date(today).isoformat()
    reminders: list[dict] = []
    for c in db.query(ChronicPatient).filter(ChronicPatient.patient_id == patient_id).all():
        if c.next_due and c.next_due < cutoff:
            reminders.append({"type": "chronic_followup_overdue", "detail": f"{c.disease} 随访已超期（应访日期 {c.next_due}）"})
        if c.level == 3:
            reminders.append({"type": "chronic_high_risk", "detail": f"{c.disease} 分级3级，建议上转评估"})
        if c.disease in GUIDANCE_POINTS:
            reminders.append({"type": "lifestyle_guidance", "detail": GUIDANCE_POINTS[c.disease]})
    for v in db.query(VaccineContraindication).filter(VaccineContraindication.patient_id == patient_id).all():
        reminders.append({"type": "vaccine_contraindication", "detail": f"疫苗 {v.vaccine_code} 禁忌：{v.reason}"})
    active_events = db.query(PublicHealthEvent).filter(PublicHealthEvent.status == "active").count()
    if active_events:
        reminders.append({"type": "active_ph_event", "detail": f"当前有 {active_events} 起突发公卫事件处置中，注意相关症状问诊"})
    return {"patient_id": patient_id, "reminders": reminders}


# ---------- ㉘ 其他卫生业务监测 ----------

_DOMAINS = {"nutrition", "environment", "occupational", "radiation", "school"}


class MonitorCreate(BaseModel):
    domain: str
    org_id: int
    indicator: str = Field(min_length=1)
    value: float
    threshold: float
    record_date: str = ""


class MonitorOut(MonitorCreate):
    id: int
    exceeded: bool

    model_config = {"from_attributes": True}


@router.post(
    "/monitors",
    response_model=MonitorOut,
    status_code=201,
    dependencies=[Depends(require_roles("public_health", "doctor"))],  # H2/L5: 监测指标上报
)
def add_monitor(body: MonitorCreate, db: Session = Depends(get_db)):
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
