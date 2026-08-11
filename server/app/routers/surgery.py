"""手术麻醉管理（T2.3）。

此前全平台关于手术只有病案首页里的一个 `operation` 文本字段和 DRG 分组的
"主手术关键词"——外科组能不能正确入组，全看医生在首页那一栏怎么写。这里补齐
申请→审批→排班→术中记录的完整链路，并让病案首页从术中记录取值。

角色：申请与术中记录限医师；审批限管理层（职责分离，申请人不能自己批）；
排班限经办与管理层（手术室排班是护士长/手术室的工作）。
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, paginate, require_admin, require_roles
from ..models import (
    Admission,
    FollowupTask,
    OperatingRoom,
    Organization,
    SurgeryRecord,
    SurgeryRequest,
    SurgerySchedule,
    User,
    utcnow,
)

router = APIRouter(prefix="/api/surgery", tags=["手术麻醉"], dependencies=[Depends(get_current_user)])

# 术后随访默认间隔
SURGERY_FOLLOWUP_DAYS = 14


# ---------------------------------------------------------------- 手术间


class RoomIn(BaseModel):
    org_id: int
    name: str = Field(min_length=1, max_length=64)


@router.post("/rooms", status_code=201, dependencies=[Depends(require_admin)])
def create_room(body: RoomIn, db: Session = Depends(get_db)):
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    room = OperatingRoom(**body.model_dump())
    db.add(room)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该机构已有同名手术间") from None
    db.refresh(room)
    return {"id": room.id, "org_id": room.org_id, "name": room.name, "active": room.active}


@router.get("/rooms")
def list_rooms(org_id: int | None = None, db: Session = Depends(get_db)):
    query = db.query(OperatingRoom)
    if org_id is not None:
        query = query.filter(OperatingRoom.org_id == org_id)
    return [
        {"id": r.id, "org_id": r.org_id, "name": r.name, "active": r.active}
        for r in query.order_by(OperatingRoom.id).limit(200).all()
    ]


# ---------------------------------------------------------------- 手术申请


class SurgeryRequestIn(BaseModel):
    admission_id: int
    surgery_name: str = Field(min_length=1, max_length=256)
    surgery_code: str = ""
    incision_level: str = Field(default="II", pattern="^(I|II|III|IV)$")
    anesthesia_type: str = Field(default="general", pattern="^(general|spinal|local|nerve_block)$")
    surgeon_name: str = ""
    urgency: str = Field(default="elective", pattern="^(elective|urgent|emergency)$")
    planned_date: str = ""


def _request_out(r: SurgeryRequest) -> dict:
    return {
        "id": r.id,
        "admission_id": r.admission_id,
        "patient_id": r.patient_id,
        "org_id": r.org_id,
        "surgery_name": r.surgery_name,
        "surgery_code": r.surgery_code,
        "incision_level": r.incision_level,
        "anesthesia_type": r.anesthesia_type,
        "surgeon_name": r.surgeon_name,
        "urgency": r.urgency,
        "planned_date": r.planned_date,
        "status": r.status,
    }


@router.post("/requests", status_code=201, dependencies=[Depends(require_roles("doctor"))])
def create_request(
    body: SurgeryRequestIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """提出手术申请。患者与机构从住院记录带出，不让客户端自报，避免张冠李戴。"""
    admission = db.get(Admission, body.admission_id)
    if admission is None:
        raise HTTPException(status_code=404, detail="住院记录不存在")
    if admission.status != "admitted":
        raise HTTPException(status_code=409, detail="患者已出院，不可申请手术")
    request = SurgeryRequest(
        patient_id=admission.patient_id,
        org_id=admission.org_id,
        created_by=user.id,
        surgeon_name=body.surgeon_name or user.full_name,
        **body.model_dump(exclude={"surgeon_name"}),
    )
    db.add(request)
    db.commit()
    db.refresh(request)
    return _request_out(request)


@router.get("/requests")
def list_requests(
    response: Response,
    status: str | None = None,
    org_id: int | None = None,
    admission_id: int | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SurgeryRequest)
    if status:
        query = query.filter(SurgeryRequest.status == status)
    if org_id is not None:
        query = query.filter(SurgeryRequest.org_id == org_id)
    if admission_id is not None:
        query = query.filter(SurgeryRequest.admission_id == admission_id)
    return [
        _request_out(r)
        for r in paginate(query.order_by(SurgeryRequest.id.desc()), response, offset, limit)
    ]


class ApproveIn(BaseModel):
    approved: bool = True
    note: str = ""


@router.post("/requests/{request_id}/approve", dependencies=[Depends(require_roles("director"))])
def approve_request(
    request_id: int,
    body: ApproveIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """审批（限管理层）。申请人不得自批——职责分离，与双通道申报同一口径。"""
    request = db.get(SurgeryRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="手术申请不存在")
    if request.status != "requested":
        raise HTTPException(status_code=409, detail=f"当前状态 {request.status} 不可审批")
    if request.created_by == user.id:
        raise HTTPException(status_code=403, detail="不得审批本人提出的手术申请")
    request.status = "approved" if body.approved else "cancelled"
    request.approved_by = user.id
    request.approved_at = utcnow()
    db.commit()
    return {"id": request.id, "status": request.status}


# ---------------------------------------------------------------- 排班


class ScheduleIn(BaseModel):
    room_id: int
    scheduled_date: str = Field(min_length=10, max_length=10)
    start_time: str = Field(pattern=r"^\d{2}:\d{2}$")
    end_time: str = Field(pattern=r"^\d{2}:\d{2}$")


@router.post(
    "/requests/{request_id}/schedule",
    status_code=201,
    dependencies=[Depends(require_roles("operator", "director"))],
)
def schedule_surgery(
    request_id: int,
    body: ScheduleIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """排手术间时段。

    重叠判定：同手术间同日，`已排 start < 新 end` 且 `已排 end > 新 start` 即冲突。
    时间是 "HH:MM" 定长字符串，字典序与时序一致，可直接比较。
    并发下两个请求可能同时通过检查，(room_id, date, start_time) 唯一约束兜住
    起点相同的情形；起点不同的极窄竞态由排班人复核，不上悲观锁。
    """
    request = db.get(SurgeryRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="手术申请不存在")
    if request.status != "approved":
        raise HTTPException(status_code=409, detail=f"当前状态 {request.status} 不可排班")
    room = db.get(OperatingRoom, body.room_id)
    if room is None or not room.active:
        raise HTTPException(status_code=404, detail="手术间不存在或已停用")
    if body.end_time <= body.start_time:
        raise HTTPException(status_code=422, detail="结束时间须晚于开始时间")

    conflict = (
        db.query(SurgerySchedule)
        .filter(
            SurgerySchedule.room_id == body.room_id,
            SurgerySchedule.scheduled_date == body.scheduled_date,
            SurgerySchedule.start_time < body.end_time,
            SurgerySchedule.end_time > body.start_time,
        )
        .first()
    )
    if conflict is not None:
        raise HTTPException(
            status_code=409,
            detail=f"手术间在 {conflict.start_time}-{conflict.end_time} 已被占用",
        )

    schedule = SurgerySchedule(request_id=request_id, created_by=user.id, **body.model_dump())
    db.add(schedule)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该手术已排班或时段被并发占用") from None
    request.status = "scheduled"
    db.commit()
    db.refresh(schedule)
    return {
        "id": schedule.id,
        "request_id": request_id,
        "room_id": schedule.room_id,
        "scheduled_date": schedule.scheduled_date,
        "start_time": schedule.start_time,
        "end_time": schedule.end_time,
    }


@router.get("/schedules")
def list_schedules(
    scheduled_date: str | None = None, room_id: int | None = None, db: Session = Depends(get_db)
):
    """手术排班表：按手术间与时段排序，就是手术室墙上那张表。"""
    query = db.query(SurgerySchedule, SurgeryRequest, OperatingRoom).join(
        SurgeryRequest, SurgerySchedule.request_id == SurgeryRequest.id
    ).join(OperatingRoom, SurgerySchedule.room_id == OperatingRoom.id)
    if scheduled_date:
        query = query.filter(SurgerySchedule.scheduled_date == scheduled_date)
    if room_id is not None:
        query = query.filter(SurgerySchedule.room_id == room_id)
    rows = query.order_by(
        SurgerySchedule.scheduled_date, SurgerySchedule.room_id, SurgerySchedule.start_time
    ).limit(300).all()
    return [
        {
            "id": s.id,
            "request_id": r.id,
            "room_name": room.name,
            "scheduled_date": s.scheduled_date,
            "start_time": s.start_time,
            "end_time": s.end_time,
            "surgery_name": r.surgery_name,
            "surgeon_name": r.surgeon_name,
            "anesthesia_type": r.anesthesia_type,
            "urgency": r.urgency,
            "status": r.status,
        }
        for s, r, room in rows
    ]


# ---------------------------------------------------------------- 术中记录


class SurgeryRecordIn(BaseModel):
    actual_surgery_name: str = Field(min_length=1, max_length=256)
    surgeon_name: str = ""
    assistants: str = ""
    anesthetist_name: str = ""
    anesthesia_type: str = Field(default="general", pattern="^(general|spinal|local|nerve_block)$")
    incision_level: str = Field(default="II", pattern="^(I|II|III|IV)$")
    start_at: str = ""
    end_at: str = ""
    blood_loss_ml: int = Field(default=0, ge=0)
    findings: str = ""
    procedure: str = ""
    complications: str = ""
    outcome: str = Field(default="好转", pattern="^(治愈|好转|未愈|死亡)$")


@router.post(
    "/requests/{request_id}/record", status_code=201, dependencies=[Depends(require_roles("doctor"))]
)
def create_record(
    request_id: int,
    body: SurgeryRecordIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """填写术中记录并结案；同时自动生成术后随访任务。"""
    request = db.get(SurgeryRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="手术申请不存在")
    if request.status != "scheduled":
        raise HTTPException(status_code=409, detail=f"当前状态 {request.status} 不可填写术中记录")
    record = SurgeryRecord(
        request_id=request_id,
        created_by=user.id,
        **{**body.model_dump(), "surgeon_name": body.surgeon_name or request.surgeon_name},
    )
    db.add(record)
    request.status = "completed"
    db.add(
        FollowupTask(
            patient_id=request.patient_id,
            org_id=request.org_id,
            category="surgery",
            source_id=request.id,
            title=f"术后随访：{body.actual_surgery_name}",
            due_date=(datetime.now().date() + timedelta(days=SURGERY_FOLLOWUP_DAYS)).isoformat(),
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该手术已有术中记录") from None
    db.refresh(record)
    return {"id": record.id, "request_id": request_id, "outcome": record.outcome}


@router.get("/requests/{request_id}/record")
def get_record(request_id: int, db: Session = Depends(get_db)):
    record = db.query(SurgeryRecord).filter(SurgeryRecord.request_id == request_id).first()
    if record is None:
        raise HTTPException(status_code=404, detail="术中记录不存在")
    return {
        "id": record.id,
        "request_id": record.request_id,
        "actual_surgery_name": record.actual_surgery_name,
        "surgeon_name": record.surgeon_name,
        "assistants": record.assistants,
        "anesthetist_name": record.anesthetist_name,
        "anesthesia_type": record.anesthesia_type,
        "incision_level": record.incision_level,
        "start_at": record.start_at,
        "end_at": record.end_at,
        "blood_loss_ml": record.blood_loss_ml,
        "findings": record.findings,
        "procedure": record.procedure,
        "complications": record.complications,
        "outcome": record.outcome,
    }


@router.get("/stats")
def surgery_stats(db: Session = Depends(get_db)):
    """手术量统计：按机构分总台次、切口等级构成、麻醉方式构成。"""
    org_names = {o.id: o.name for o in db.query(Organization).all()}
    rows = (
        db.query(SurgeryRequest, SurgeryRecord)
        .join(SurgeryRecord, SurgeryRecord.request_id == SurgeryRequest.id)
        .all()
    )
    stats: dict[int, dict] = {}
    for request, record in rows:
        entry = stats.setdefault(
            request.org_id,
            {
                "org_id": request.org_id,
                "org_name": org_names.get(request.org_id, ""),
                "total": 0,
                "by_incision": {},
                "by_anesthesia": {},
                "complications": 0,
            },
        )
        entry["total"] += 1
        entry["by_incision"][record.incision_level] = entry["by_incision"].get(record.incision_level, 0) + 1
        entry["by_anesthesia"][record.anesthesia_type] = (
            entry["by_anesthesia"].get(record.anesthesia_type, 0) + 1
        )
        if record.complications:
            entry["complications"] += 1
    return sorted(stats.values(), key=lambda x: -x["total"])
