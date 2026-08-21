"""预约诊疗：机构发布分时段号源，居民一站式预约（挂号/检查/检验）。"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..visibility import scope_org_list, scope_patient_list
from ..database import get_db
from ..datetypes import DateStr
from ..deps import get_current_user, require_admin, require_roles, resolve_business_date
from ..models import (
    Appointment,
    AppointmentSlot,
    Employee,
    Organization,
    Patient,
    ServiceBlacklist,
    User,
)
from ..schemas import AppointmentCreate, AppointmentOut, SlotCreate, SlotOut

router = APIRouter(prefix="/api/appointments", tags=["预约诊疗"], dependencies=[Depends(get_current_user)])


@router.get("/doctors")
def find_doctors(
    keyword: str | None = None,
    org_id: int | None = None,
    from_date: str | None = None,
    db: Session = Depends(get_db),
):
    """便捷寻医（指引⑨"便捷寻医"）：按姓名/科室/职称找医师，带出可约号源。

    第九轮横向隔离**明确不设限**：这是面向居民的寻医目录，跨机构找医师
    正是它的用途（在卫生院帮患者约县医院的号）。医师姓名与号源本就是
    公开挂出来的信息，不是管理数据。

    只列**还有余号**的医师排在前面，但没号的也一并返回并标注——
    只给有号的，居民会以为这位医师不存在，转头去问"你们医院不是有王主任吗"。

    号源与医师靠 `employee_id` 关联，不靠姓名字符串匹配：同名与写法不一
    都会漏，而漏掉的表现是"这位医师查不到号"，几乎无法自查。
    """
    today = resolve_business_date(from_date).isoformat()
    query = db.query(Employee)
    if org_id is not None:
        query = query.filter(Employee.org_id == org_id)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            (Employee.name.like(like)) | (Employee.title.like(like)) | (Employee.position.like(like))
        )
    employees = query.order_by(Employee.id).limit(200).all()
    if not employees:
        return []
    slots = (
        db.query(AppointmentSlot)
        .filter(
            AppointmentSlot.employee_id.in_([e.id for e in employees]),
            AppointmentSlot.slot_date >= today,
        )
        .order_by(AppointmentSlot.slot_date, AppointmentSlot.slot_time)
        .all()
    )
    # employee_id 可空（未指派的号源），键类型要承认这一点
    by_employee: dict[int | None, list[AppointmentSlot]] = {}
    for s in slots:
        by_employee.setdefault(s.employee_id, []).append(s)
    org_names = {
        o.id: o.name
        for o in db.query(Organization)
        .filter(Organization.id.in_({e.org_id for e in employees}))
        .all()
    }
    rows = []
    for e in employees:
        mine = by_employee.get(e.id, [])
        available = [s for s in mine if s.booked < s.capacity]
        rows.append({
            "employee_id": e.id,
            "name": e.name,
            "title": e.title,
            "title_level": e.title_level,
            "position": e.position,
            "org_id": e.org_id,
            "org_name": org_names.get(e.org_id, ""),
            "available_slots": len(available),
            "next_slots": [
                {"slot_id": s.id, "slot_date": s.slot_date, "slot_time": s.slot_time,
                 "remaining": s.capacity - s.booked, "resource_name": s.resource_name}
                for s in available[:5]
            ],
            # 没号的也返回并标注，见 docstring
            "bookable": bool(available),
        })
    return sorted(rows, key=lambda r: (-r["available_slots"], r["employee_id"]))


@router.post("/slots", response_model=SlotOut, status_code=201, dependencies=[Depends(require_admin)])
def create_slot(body: SlotCreate, db: Session = Depends(get_db)):
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    if body.employee_id is not None:
        employee = db.get(Employee, body.employee_id)
        if employee is None:
            raise HTTPException(status_code=404, detail="医师不存在")
        if employee.org_id != body.org_id:
            raise HTTPException(status_code=422, detail="医师不属于该机构")
    slot = AppointmentSlot(**body.model_dump())
    db.add(slot)
    db.commit()
    db.refresh(slot)
    return slot


# ---------------------------------------------------------------- 号源批量生成（D1 开办工具）

# 单次批量生成上限：日期数 × 模板数。开办期一个科室一个季度的号源
# （90 天 × 数个时段模板）在此限内；更大的量分多次调用，防止误填
# 日期区间一次生成数十万行把库写爆。
MAX_BATCH_SLOTS = 1000


class SlotTemplate(BaseModel):
    """时段模板：与 SlotCreate 相同的号源属性，唯独日期由区间展开。"""

    resource_type: str = Field(pattern="^(outpatient|exam|lab)$")
    resource_name: str = Field(min_length=1)
    employee_id: int | None = None
    slot_time: str = ""
    capacity: int = Field(default=1, ge=1)


class SlotBatchCreate(BaseModel):
    org_id: int
    templates: list[SlotTemplate] = Field(min_length=1)
    date_from: DateStr
    date_to: DateStr
    # 节假日/停诊日跳过（YYYY-MM-DD），可选
    skip_dates: list[DateStr] = Field(default_factory=list)
    skip_weekends: bool = False


class SlotBatchOut(BaseModel):
    created: int
    skipped: int


@router.post(
    "/slots/batch",
    response_model=SlotBatchOut,
    status_code=201,
    dependencies=[Depends(require_admin)],
)
def batch_create_slots(body: SlotBatchCreate, db: Session = Depends(get_db)):
    """模板×日期区间批量生成号源。

    幂等：机构+医师+资源+日期+时段 已有号源的日期跳过（skipped 计数），
    重复调用不产生重复号源——开办期常常要"补生成"某几天，重跑安全比报错友好。
    """
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    employee_ids = {t.employee_id for t in body.templates if t.employee_id is not None}
    if employee_ids:
        employees = {
            e.id: e for e in db.query(Employee).filter(Employee.id.in_(employee_ids)).all()
        }
        for employee_id in employee_ids:
            employee = employees.get(employee_id)
            if employee is None:
                raise HTTPException(status_code=404, detail=f"医师不存在: {employee_id}")
            if employee.org_id != body.org_id:
                raise HTTPException(status_code=422, detail=f"医师不属于该机构: {employee_id}")
    start, end = date.fromisoformat(body.date_from), date.fromisoformat(body.date_to)
    if start > end:
        raise HTTPException(status_code=422, detail="date_from 不得晚于 date_to")
    skip = set(body.skip_dates)
    days: list[str] = []
    cursor = start
    while cursor <= end:
        day = cursor.isoformat()
        if day not in skip and not (body.skip_weekends and cursor.weekday() >= 5):
            days.append(day)
        cursor += timedelta(days=1)
    total = len(days) * len(body.templates)
    if total > MAX_BATCH_SLOTS:
        raise HTTPException(
            status_code=422,
            detail=f"单次生成量超上限: {total} > {MAX_BATCH_SLOTS}（请缩小日期区间或分批生成）",
        )
    # 幂等键集合预载（区间内一次查询），生成时按键跳过已有号源
    existing = {
        (s.employee_id, s.resource_type, s.resource_name, s.slot_date, s.slot_time)
        for s in db.query(AppointmentSlot)
        .filter(
            AppointmentSlot.org_id == body.org_id,
            AppointmentSlot.slot_date >= body.date_from,
            AppointmentSlot.slot_date <= body.date_to,
        )
        .all()
    }
    created = skipped = 0
    for day in days:
        for template in body.templates:
            key = (
                template.employee_id, template.resource_type,
                template.resource_name, day, template.slot_time,
            )
            if key in existing:
                skipped += 1
                continue
            db.add(AppointmentSlot(org_id=body.org_id, slot_date=day, **template.model_dump()))
            existing.add(key)
            created += 1
    try:
        db.commit()
    except IntegrityError:
        # appointment_slots 当前无唯一约束，此处为防御：若后续加约束，
        # 并发重复生成应得 409 而非 500
        db.rollback()
        raise HTTPException(status_code=409, detail="号源生成冲突，请重试")
    return {"created": created, "skipped": skipped}


@router.get("/slots", response_model=list[SlotOut])
def list_slots(org_id: int | None = None, slot_date: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user),):
    query = db.query(AppointmentSlot)
    query = scope_org_list(db, user, query, AppointmentSlot, org_id)
    if slot_date:
        query = query.filter(AppointmentSlot.slot_date == slot_date)
    return query.order_by(AppointmentSlot.slot_date, AppointmentSlot.slot_time).limit(500).all()


def book_slot(db: Session, slot_id: int, patient_id: int) -> Appointment:
    """号源预约核心逻辑：管理端代约与居民端自助预约共用。

    黑名单拦截、原子占号、重复预约判定都在这里，两条入口不会走出不同的行为。
    """
    slot = db.get(AppointmentSlot, slot_id)
    if slot is None:
        raise HTTPException(status_code=404, detail="号源不存在")
    if db.get(Patient, patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    banned = (
        db.query(ServiceBlacklist)
        .filter(
            ServiceBlacklist.domain == "appointment",
            ServiceBlacklist.patient_id == patient_id,
        )
        .first()
    )
    if banned:
        raise HTTPException(status_code=403, detail=f"该患者在预约黑名单中：{banned.reason}")
    existing = (
        db.query(Appointment)
        .filter(Appointment.slot_id == slot_id, Appointment.patient_id == patient_id)
        .first()
    )
    if existing and existing.status == "booked":
        raise HTTPException(status_code=409, detail="请勿重复预约")
    if existing and existing.status == "fulfilled":
        # M1 整改：已就诊记录不可静默回退复用，防止状态机回退与号源虚占
        raise HTTPException(status_code=409, detail="该预约已就诊，不可重复预约")

    # H3 整改：条件 UPDATE 原子占号（WHERE booked < capacity），杜绝并发超卖
    claimed = (
        db.query(AppointmentSlot)
        .filter(
            AppointmentSlot.id == slot_id,
            AppointmentSlot.booked < AppointmentSlot.capacity,
        )
        .update(
            {AppointmentSlot.booked: AppointmentSlot.booked + 1},
            synchronize_session=False,
        )
    )
    if not claimed:
        db.rollback()
        raise HTTPException(status_code=409, detail="号源已约满")

    if existing:
        # 仅 cancelled 记录允许复用重约
        existing.status = "booked"
        db.commit()
        db.refresh(existing)
        return existing
    appointment = Appointment(slot_id=slot_id, patient_id=patient_id)
    db.add(appointment)
    try:
        db.commit()
    except IntegrityError:
        # L-7 整改：并发同患者同号源双 book 触发唯一约束 → 409（而非500）
        db.rollback()
        raise HTTPException(status_code=409, detail="该患者已预约此号源")
    db.refresh(appointment)
    return appointment


def release_appointment(db: Session, appointment: Appointment) -> Appointment:
    """取消预约并释放号源：管理端与居民端共用。"""
    if appointment.status != "booked":
        # 状态机：仅 booked 可取消；fulfilled/cancelled 均拒绝（M1）
        raise HTTPException(status_code=409, detail=f"当前状态 {appointment.status} 不可取消")
    appointment.status = "cancelled"
    # H3 整改：条件 UPDATE 释放号源（WHERE booked > 0），防止并发释放扣成负数
    db.query(AppointmentSlot).filter(
        AppointmentSlot.id == appointment.slot_id, AppointmentSlot.booked > 0
    ).update({AppointmentSlot.booked: AppointmentSlot.booked - 1}, synchronize_session=False)
    db.commit()
    db.refresh(appointment)
    return appointment


@router.post(
    "",
    response_model=AppointmentOut,
    status_code=201,
    dependencies=[Depends(require_roles("operator", "doctor"))],  # H2: 预约经办
)
def book(body: AppointmentCreate, db: Session = Depends(get_db)):
    return book_slot(db, body.slot_id, body.patient_id)


@router.get("", response_model=list[AppointmentOut])
def list_appointments(patient_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user),):
    query = db.query(Appointment)
    query = scope_patient_list(db, user, query, Appointment, patient_id, "appointment")
    return query.order_by(Appointment.id.desc()).limit(500).all()


@router.post(
    "/{appointment_id}/cancel",
    response_model=AppointmentOut,
    dependencies=[Depends(require_roles("operator", "doctor"))],  # H2
)
def cancel(appointment_id: int, db: Session = Depends(get_db)):
    appointment = db.get(Appointment, appointment_id)
    if appointment is None:
        raise HTTPException(status_code=404, detail="预约不存在")
    return release_appointment(db, appointment)


@router.post(
    "/{appointment_id}/fulfill",
    response_model=AppointmentOut,
    dependencies=[Depends(require_roles("operator", "doctor"))],  # H2: 到诊核销
)
def fulfill(appointment_id: int, db: Session = Depends(get_db)):
    appointment = db.get(Appointment, appointment_id)
    if appointment is None:
        raise HTTPException(status_code=404, detail="预约不存在")
    if appointment.status != "booked":
        raise HTTPException(status_code=409, detail=f"当前状态 {appointment.status} 不可核销")
    appointment.status = "fulfilled"
    db.commit()
    db.refresh(appointment)
    return appointment
