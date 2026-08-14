"""阶段十 统一资源与排程撮合中心。

延续 `/api/service-requests` 已经定下的那条原则：**聚合而非替代**。

五类资源（号源、检查资源、手术间、血制品、通用资源）各有必要的领域字段与
状态机，再造一张通用资源表去覆盖它们，只会得到一张没人写入的空表——
统一申请单中心当初就是这么判的，这里没有理由反过来。

所以这个模块做两件事，都是既有模型答不了的：

1. **通用资源登记（`resources`）**：只收那些**没有领域表**的资源——工勤、
   后勤、通用设备、会议室。有领域表的一律不进这张表。
2. **统一资源视图与排程撮合**：把五类资源在一处列出，并回答"这张申请单
   能排到哪里、什么时候"。这是从"看得见"升级到"排得上"的那一步。

发布/撤回状态只对通用资源生效——号源有 capacity、手术间有 active、
血制品有库存量，各自已经表达了"能不能用"，再压一层发布状态只会打架。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..visibility import assert_org_writable
from ..database import get_db
from ..datetypes import OptionalDateStr
from ..deps import get_current_user, require_roles, resolve_business_date, resolve_org_scope
from ..models import (
    AppointmentSlot,
    BloodStock,
    ExamResource,
    OperatingRoom,
    Organization,
    Resource,
    SurgerySchedule,
    User,
)

router = APIRouter(
    prefix="/api/resources", tags=["统一资源与排程"], dependencies=[Depends(get_current_user)]
)

RESOURCE_TYPES = {
    "logistics": "工勤服务",
    "facility": "后勤设施",
    "equipment": "通用设备",
    "meeting_room": "会议室",
}
RESOURCE_STATUS = {"draft": "草稿", "published": "已发布", "withdrawn": "已撤回"}


# ============================================================ 通用资源登记


class ResourceIn(BaseModel):
    org_id: int
    resource_type: str = Field(pattern="^(logistics|facility|equipment|meeting_room)$")
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    capacity: int = Field(default=1, ge=1)
    unit: str = Field(default="", max_length=16)
    location: str = Field(default="", max_length=256)
    contact: str = Field(default="", max_length=64)
    note: str = Field(default="", max_length=512)


class ResourceUpdate(BaseModel):
    name: str | None = None
    capacity: int | None = Field(default=None, ge=1)
    location: str | None = None
    contact: str | None = None
    note: str | None = None


def _resource_out(r: Resource) -> dict:
    return {
        "id": r.id,
        "org_id": r.org_id,
        "resource_type": r.resource_type,
        "resource_type_name": RESOURCE_TYPES.get(r.resource_type, r.resource_type),
        "code": r.code,
        "name": r.name,
        "capacity": r.capacity,
        "unit": r.unit,
        "location": r.location,
        "contact": r.contact,
        "status": r.status,
        "status_name": RESOURCE_STATUS.get(r.status, r.status),
        "withdraw_reason": r.withdraw_reason,
        "note": r.note,
    }


@router.post("", status_code=201, dependencies=[Depends(require_roles("operator", "director"))])
def register_resource(body: ResourceIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """登记通用资源。**新登记的一律是草稿**——直接发布意味着还没核对完
    就已经能被申请到，而资源信息填错的代价是有人白跑一趟。"""
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    resource = Resource(**body.model_dump())
    db.add(resource)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该机构下资源编码已存在") from None
    db.refresh(resource)
    return _resource_out(resource)


@router.get("")
def list_resources(
    org_id: int | None = None,
    resource_type: str | None = None,
    status: str | None = None,
    group_id: int | None = None,
    db: Session = Depends(get_db),
):
    query = db.query(Resource)
    scope = resolve_org_scope(db, group_id, org_id)
    if scope is not None:
        query = query.filter(Resource.org_id.in_(scope))
    if resource_type:
        query = query.filter(Resource.resource_type == resource_type)
    if status:
        query = query.filter(Resource.status == status)
    return [_resource_out(r) for r in query.order_by(Resource.id.desc()).limit(500).all()]


@router.patch("/{resource_id}", dependencies=[Depends(require_roles("operator", "director"))])
def update_resource(resource_id: int, body: ResourceUpdate, db: Session = Depends(get_db)):
    resource = _resource(db, resource_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(resource, field, value)
    db.commit()
    db.refresh(resource)
    return _resource_out(resource)


@router.post(
    "/{resource_id}/publish", dependencies=[Depends(require_roles("operator", "director"))]
)
def publish_resource(resource_id: int, db: Session = Depends(get_db)):
    resource = _resource(db, resource_id)
    if resource.status == "published":
        raise HTTPException(status_code=409, detail="该资源已发布")
    resource.status = "published"
    resource.withdraw_reason = ""
    db.commit()
    db.refresh(resource)
    return _resource_out(resource)


class WithdrawIn(BaseModel):
    reason: str = Field(min_length=1, max_length=256)


@router.post(
    "/{resource_id}/withdraw", dependencies=[Depends(require_roles("operator", "director"))]
)
def withdraw_resource(resource_id: int, body: WithdrawIn, db: Session = Depends(get_db)):
    """撤回（不删行）。撤回理由必填——"这台设备为什么不能约了"是使用方
    一定会问的，答不上来就会被反复问。"""
    resource = _resource(db, resource_id)
    if resource.status == "withdrawn":
        raise HTTPException(status_code=409, detail="该资源已撤回")
    resource.status = "withdrawn"
    resource.withdraw_reason = body.reason
    db.commit()
    db.refresh(resource)
    return _resource_out(resource)


def _resource(db: Session, resource_id: int) -> Resource:
    resource = db.get(Resource, resource_id)
    if resource is None:
        raise HTTPException(status_code=404, detail="资源不存在")
    return resource


# ============================================================ 统一资源视图


@router.get("/catalog")
def resource_catalog(
    org_id: int | None = None,
    group_id: int | None = None,
    resource_kind: str | None = Query(default=None, description="slot/exam/or_room/blood/general"),
    db: Session = Depends(get_db),
):
    """五类资源一处看全。

    与统一申请单中心同一个做法：**聚合而非替代**。各类资源保留自己的领域表，
    这里只统一"有什么、在哪、还能用多少"这三个问题的答案格式。

    `available` 的含义按各类资源自己的规则算，不强行统一成一个数——
    号源看余量、手术间看启停、血制品看库存、通用资源看发布状态。
    把它们压成同一个数字，只会让每一类都被表达错。
    """
    scope = resolve_org_scope(db, group_id, org_id)
    items: list[dict] = []

    def in_scope(query, column):
        return query.filter(column.in_(scope)) if scope is not None else query

    if resource_kind in (None, "slot"):
        rows = in_scope(db.query(AppointmentSlot), AppointmentSlot.org_id).limit(500).all()
        items += [
            {"kind": "slot", "kind_name": "号源", "id": s.id, "org_id": s.org_id,
             "name": s.resource_name, "detail": f"{s.slot_date} {s.slot_time}",
             "available": s.capacity - s.booked, "unit": "个",
             "usable": s.capacity > s.booked}
            for s in rows
        ]
    if resource_kind in (None, "exam"):
        rows = in_scope(db.query(ExamResource), ExamResource.org_id).limit(500).all()
        items += [
            {"kind": "exam", "kind_name": "检查资源", "id": r.id, "org_id": r.org_id,
             "name": r.item_name, "detail": f"{r.device} {r.duration_min}分钟",
             "available": None, "unit": "", "usable": r.active}
            for r in rows
        ]
    if resource_kind in (None, "or_room"):
        rows = in_scope(db.query(OperatingRoom), OperatingRoom.org_id).limit(500).all()
        items += [
            {"kind": "or_room", "kind_name": "手术间", "id": r.id, "org_id": r.org_id,
             "name": r.name, "detail": "", "available": None, "unit": "",
             "usable": r.active}
            for r in rows
        ]
    if resource_kind in (None, "blood"):
        # 血库是全县一本账（`blood_stocks` 没有 org_id），故不参与机构范围筛选，
        # 且只在未指定机构/分组时列出——按机构筛还把全县血库列出来会误导人。
        if scope is None:
            rows = db.query(BloodStock).limit(500).all()
            items += [
                {"kind": "blood", "kind_name": "血制品", "id": r.id, "org_id": None,
                 "name": f"{r.blood_type} {r.component}", "detail": "全县共用血库",
                 "available": r.quantity_ml, "unit": "ml", "usable": r.quantity_ml > 0}
                for r in rows
            ]
    if resource_kind in (None, "general"):
        rows = in_scope(db.query(Resource), Resource.org_id).limit(500).all()
        items += [
            {"kind": "general", "kind_name": RESOURCE_TYPES.get(r.resource_type, "通用资源"),
             "id": r.id, "org_id": r.org_id, "name": r.name, "detail": r.location,
             "available": r.capacity, "unit": r.unit,
             "usable": r.status == "published"}
            for r in rows
        ]
    by_kind: dict[str, dict] = {}
    for item in items:
        entry = by_kind.setdefault(item["kind"], {"total": 0, "usable": 0})
        entry["total"] += 1
        if item["usable"]:
            entry["usable"] += 1
    return {
        "total": len(items),
        "by_kind": by_kind,
        "items": items,
        "caliber": "available 按各类资源自己的规则算，不强行统一成一个数——"
                   "号源看余量、检查资源与手术间看启停、血制品看库存、通用资源看发布状态",
    }


# ============================================================ 排程撮合


@router.get("/match/slots")
def match_slots(
    resource_type: str = Query(default="outpatient", pattern="^(outpatient|exam|lab)$"),
    keyword: str | None = None,
    org_id: int | None = None,
    group_id: int | None = None,
    from_date: str | None = None,
    days: int = Query(default=14, ge=1, le=90),
    db: Session = Depends(get_db),
):
    """号源撮合：给定诉求，回答"能排到哪里、最早什么时候"。

    这是"聚合视图"与"撮合中心"的分界：前者回答"有什么"，后者回答"排哪个"。
    按最早可用时间排序，同时给出每家机构的余量——**不自动选**，
    因为"最早"与"最近"经常不是同一家，选哪个是患者的事。
    """
    from datetime import timedelta

    start = resolve_business_date(from_date)
    end = (start + timedelta(days=days - 1)).isoformat()
    query = db.query(AppointmentSlot).filter(
        AppointmentSlot.resource_type == resource_type,
        AppointmentSlot.slot_date >= start.isoformat(),
        AppointmentSlot.slot_date <= end,
        AppointmentSlot.booked < AppointmentSlot.capacity,
    )
    scope = resolve_org_scope(db, group_id, org_id)
    if scope is not None:
        query = query.filter(AppointmentSlot.org_id.in_(scope))
    if keyword:
        query = query.filter(AppointmentSlot.resource_name.like(f"%{keyword}%"))
    slots = query.order_by(AppointmentSlot.slot_date, AppointmentSlot.slot_time).limit(500).all()
    org_names = {
        o.id: o.name
        for o in db.query(Organization).filter(
            Organization.id.in_({s.org_id for s in slots})
        ).all()
    } if slots else {}

    by_org: dict[int, dict] = {}
    for s in slots:
        entry = by_org.setdefault(
            s.org_id,
            {"org_id": s.org_id, "org_name": org_names.get(s.org_id, ""),
             "earliest": s.slot_date, "remaining_total": 0, "slots": []},
        )
        entry["remaining_total"] += s.capacity - s.booked
        if len(entry["slots"]) < 5:
            entry["slots"].append({
                "slot_id": s.id, "resource_name": s.resource_name,
                "slot_date": s.slot_date, "slot_time": s.slot_time,
                "remaining": s.capacity - s.booked,
            })
    return {
        "window": {"start": start.isoformat(), "end": end},
        "candidates": sorted(by_org.values(), key=lambda x: (x["earliest"], -x["remaining_total"])),
        "caliber": "按最早可用日期排序；不自动选——最早与最近常不是同一家，"
                   "选哪个是患者的事",
    }


@router.get("/match/or-rooms")
def match_operating_rooms(
    org_id: int,
    scheduled_date: OptionalDateStr = Query(default=""),
    start_time: str = Query(default="08:00", pattern=r"^\d{2}:\d{2}$"),
    end_time: str = Query(default="18:00", pattern=r"^\d{2}:\d{2}$"),
    db: Session = Depends(get_db),
):
    """手术间撮合：给定日期与时间窗，列出**没有冲突**的手术间及其空档。

    第九轮横向隔离**明确不设限**：跨机构撮合正是这条接口的用途——基层把
    手术病人转上来，要先看得到县医院哪天有空台。空档时间不是敏感数据。

    冲突判定与排班接口用的是同一套（已排 start < 新 end 且 已排 end > 新 start），
    刻意不另写一份——两处判定不一致，撮合说能排、排班说冲突，比没有撮合更糟。
    """
    if end_time <= start_time:
        raise HTTPException(status_code=422, detail="结束时间须晚于开始时间")
    day = scheduled_date or resolve_business_date(None).isoformat()
    rooms = (
        db.query(OperatingRoom)
        .filter(OperatingRoom.org_id == org_id, OperatingRoom.active.is_(True))
        .all()
    )
    if not rooms:
        return {"scheduled_date": day, "rooms": [], "hint": "该机构没有启用中的手术间"}
    schedules = (
        db.query(SurgerySchedule)
        .filter(
            SurgerySchedule.room_id.in_([r.id for r in rooms]),
            SurgerySchedule.scheduled_date == day,
        )
        .order_by(SurgerySchedule.start_time)
        .all()
    )
    by_room: dict[int, list[SurgerySchedule]] = {}
    for s in schedules:
        by_room.setdefault(s.room_id, []).append(s)

    result = []
    for room in rooms:
        booked = by_room.get(room.id, [])
        conflicts = [
            {"start_time": s.start_time, "end_time": s.end_time}
            for s in booked
            if s.start_time < end_time and s.end_time > start_time
        ]
        # 空档：在请求窗口内，被已排时段切剩下的连续区间
        gaps, cursor = [], start_time
        for s in sorted(booked, key=lambda x: x.start_time):
            if s.end_time <= start_time or s.start_time >= end_time:
                continue
            if s.start_time > cursor:
                gaps.append({"start_time": cursor, "end_time": s.start_time})
            cursor = max(cursor, s.end_time)
        if cursor < end_time:
            gaps.append({"start_time": cursor, "end_time": end_time})
        result.append({
            "room_id": room.id,
            "room_name": room.name,
            "available": not conflicts,
            "conflicts": conflicts,
            "gaps": gaps,
        })
    return {
        "scheduled_date": day,
        "window": {"start_time": start_time, "end_time": end_time},
        "rooms": sorted(result, key=lambda r: (not r["available"], r["room_id"])),
        "caliber": "冲突判定与 /api/surgery 排班接口同一套规则；空档为请求窗口内"
                   "被已排时段切剩的连续区间",
    }
