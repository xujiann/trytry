"""预约诊疗：机构发布分时段号源，居民一站式预约（挂号/检查/检验）。"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import clock
from ..numtypes import INT4_MAX
from ..texttypes import NON_BLANK
from ..visibility import assert_org_writable, scope_org_list, scope_patient_list
from ..concurrency import insert_or_conflict
from ..database import get_db
from ..datetypes import DateStr
from ..deps import (
    get_current_user,
    paginate,
    require_admin,
    require_date,
    require_roles,
    resolve_business_date,
    keyword_like,
)
from ..models import (
    Appointment,
    AppointmentSlot,
    Department,
    Employee,
    Organization,
    Patient,
    ServiceBlacklist,
    User,
)
from ..schemas import AppointmentCreate, AppointmentOut, SlotCreate, SlotOut

router = APIRouter(prefix="/api/appointments", tags=["预约诊疗"], dependencies=[Depends(get_current_user)])

# 状态文案（措辞照抄模型列注释；报错文案用它，别把英文码直接拼给窗口人员看——P2-74）
APPOINTMENT_STATUS_NAMES = {"booked": "已预约", "cancelled": "已取消", "fulfilled": "已就诊"}


class DoctorNextSlotOut(BaseModel):
    """近期可约号源（最多 5 枚）。`remaining` = capacity - booked，恒 int。"""

    slot_id: int
    slot_date: str
    slot_time: str
    remaining: int
    resource_name: str


class DoctorCandidateOut(BaseModel):
    """便捷寻医行。没号的医师也在列（`bookable=false`、`next_slots=[]`），
    见 handler docstring——契约不许把"没号"建成"行消失"。"""

    employee_id: int
    name: str
    title: str
    title_level: str
    position: str
    org_id: int
    org_name: str
    available_slots: int
    next_slots: list[DoctorNextSlotOut]
    bookable: bool
    # 所在科室（P2-821）：职工挂的科室（`Employee.dept_id`，浙#9 科室信息库），没挂是空串。加在末尾，前面的键一字不动
    dept_name: str


@router.get("/doctors", response_model=list[DoctorCandidateOut])
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
    # 下界不早于业务日（P2-1301）：原先直接用 `from_date`——起始日期填过去，过去的号也算「可约」、`bookable` 为真，
    # 照着 `next_slots` 给的第一个号去约只得 409「该号源日期已过」（P2-64）。与管理端号源清单（P2-882）同一口径
    today = max(resolve_business_date(from_date, field="from_date"), clock.today()).isoformat()
    query = db.query(Employee).outerjoin(Department, Department.id == Employee.dept_id)
    if org_id is not None:
        query = query.filter(Employee.org_id == org_id)
    if keyword:
        # 科室也比（P2-821）：说明与页面占位都写「按姓名 / 科室 / 职称」，原先只比姓名、职称、岗位——搜「心内科」空表，
        # 挂在心内科的医师查不到。科室按职工挂的科室（dept_id）的名称比；没挂科室的照旧只按另外三项
        query = query.filter(
            keyword_like(Employee.name, keyword) | keyword_like(Employee.title, keyword)
            | keyword_like(Employee.position, keyword) | keyword_like(Department.name, keyword)
        )
    # 有余号的先排、再取前 200 位（P2-178）：原先按编号取前 200 位、再在这 200 位里把有号的排前——全县职工过 200 位
    # （不带关键字、不限机构时必然），编号靠后的医师有号也不在清单里，编号靠前、没号的倒占着位置。
    # 余号数与下面逐位算的 available_slots 同一个口径：今天及以后、没约满的号源
    open_slots = (
        db.query(AppointmentSlot.employee_id.label("employee_id"), func.count(AppointmentSlot.id).label("n"))
        .filter(AppointmentSlot.employee_id.isnot(None), AppointmentSlot.slot_date >= today,
                AppointmentSlot.booked < AppointmentSlot.capacity)
        .group_by(AppointmentSlot.employee_id)
        .subquery()
    )
    employees = (
        query.outerjoin(open_slots, open_slots.c.employee_id == Employee.id)
        .order_by(func.coalesce(open_slots.c.n, 0).desc(), Employee.id)
        .limit(200)
        .all()
    )
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
    dept_names = {
        d.id: d.name
        for d in db.query(Department).filter(Department.id.in_({e.dept_id for e in employees if e.dept_id} or {0}))
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
            "dept_name": dept_names.get(e.dept_id, "") if e.dept_id else "",
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
        if employee.status == "left":  # 离职的医师不再放号（挂上了也没人坐诊），同批量生成、预约同一句
            raise HTTPException(status_code=409, detail="该医师已离职，不能放号")
    # 日期早于业务日不放号（P2-1301）：原先只校验形状，年份敲成去年也 201——建出来的号清单不列（P2-882）、谁也约不上
    # （P2-64），等于白放。与约号那句同一个口径、同一个比法；批量生成跳过已过的日期，见下
    if body.slot_date < clock.today().isoformat():
        raise HTTPException(status_code=422, detail="该号源日期已过，不能放号")
    slot = AppointmentSlot(**body.model_dump())
    # 同机构+医师+资源+日期+时段唯一（uq_slot_with_employee /
    # uq_slot_without_employee 两条部分索引，NULL != NULL 故拆两条）：
    # 重复号源各带 capacity，放号量会凭空翻倍。
    insert_or_conflict(db, slot, "该时段号源已存在（同机构/医师/资源/日期/时段）")
    return slot


# ---------------------------------------------------------------- 号源批量生成（D1 开办工具）

# 单次批量生成上限：日期数 × 模板数。开办期一个科室一个季度的号源
# （90 天 × 数个时段模板）在此限内；更大的量分多次调用，防止误填
# 日期区间一次生成数十万行把库写爆。
MAX_BATCH_SLOTS = 1000


class SlotTemplate(BaseModel):
    """时段模板：与 SlotCreate 相同的号源属性，唯独日期由区间展开。"""

    resource_type: str = Field(pattern="^(outpatient|exam|lab)$")
    resource_name: str = Field(min_length=1, max_length=128, pattern=NON_BLANK)
    employee_id: int | None = None
    slot_time: str = Field(default="", max_length=16)
    capacity: int = Field(default=1, ge=1, le=INT4_MAX)


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
    # 区间里早于业务日、没有生成的日期数（P2-1301）。加在末尾，前两个键语义不变：`skipped` 仍只数「已有号源」的跳过
    skipped_past_dates: int


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
    早于业务日的日期不生成，回执 `skipped_past_dates` 报有几天（P2-1301）。
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
            if employee.status == "left":
                raise HTTPException(status_code=409, detail=f"该医师已离职，不能放号: {employee_id}")
    start, end = date.fromisoformat(body.date_from), date.fromisoformat(body.date_to)
    if start > end:
        raise HTTPException(status_code=422, detail="date_from 不得晚于 date_to")
    skip = set(body.skip_dates)
    days: list[str] = []
    # 早于业务日的日期不生成、只计数回执（P2-1301）：原先照生成、照计进 created——这样的号清单不列（P2-882）、谁也约不上
    # （P2-64）。区间起点填早了，今天及以后的照常生成；上限只按真要生成的日期算
    today = clock.today()
    skipped_past_dates = 0
    cursor = start
    while True:
        day = cursor.isoformat()
        if day not in skip and not (body.skip_weekends and cursor.weekday() >= 5):
            if cursor < today:
                skipped_past_dates += 1
            else:
                days.append(day)
        if cursor == end:   # 先判再加（P2-410）：区间止于 9999-12-31 时再加一天就越界，原先整个请求 500
            break
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
    return {"created": created, "skipped": skipped, "skipped_past_dates": skipped_past_dates}


@router.get("/slots", response_model=list[SlotOut])
def list_slots(
    response: Response,
    org_id: int | None = None,
    slot_date: str | None = None,
    offset: int = 0,
    limit: int = 500,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(AppointmentSlot)
    query = scope_org_list(db, user, query, AppointmentSlot, org_id)
    if slot_date:
        # 等值匹配：`2026-9-1` 会让"这天没有号源"，不报错（P1-58）
        slot_date = require_date(slot_date, field="slot_date")
        query = query.filter(AppointmentSlot.slot_date == slot_date)
    else:
        # 不带日期只列今天及以后（P2-882，与资源目录 P2-164、居民端 P2-64、寻医同一句）：原先没有日期下界、按日期正序取
        # 最早的 500 个——开诊一两个月后管理端「号源」面板全是过去的号，今天以后的一个都不列，照表抄号源 ID 去约只得 409
        query = query.filter(AppointmentSlot.slot_date >= clock.today().isoformat())
    # 补 id 尾键：`(slot_date, slot_time)` 不是全序——号源表的唯一索引是
    # (org_id, employee_id, resource_type, resource_name, slot_date, slot_time)，
    # 同一个「日期+时段」上按设计并排着各机构各资源的号源。居民端同一张表的
    # `/me/slots` 在第二批已经补过，这里是它的业务端孪生。
    return paginate(
        query.order_by(
            AppointmentSlot.slot_date, AppointmentSlot.slot_time, AppointmentSlot.id
        ),
        response, offset, limit,
    )


def book_slot(db: Session, slot_id: int, patient_id: int) -> Appointment:
    """号源预约核心逻辑：管理端代约与居民端自助预约共用。

    黑名单拦截、原子占号、重复预约判定都在这里，两条入口不会走出不同的行为。
    """
    slot = db.get(AppointmentSlot, slot_id)
    if slot is None:
        raise HTTPException(status_code=404, detail="号源不存在")
    # 日期已过的号源不再接受预约（P2-64）：原先照约不误——居民端「可约号源」按日期正序列出，头几页全是过去的号，
    # 约上的是一个已经过去的时段。按业务日期比（与寻医列表 `slot_date >= today` 同一口径），两条入口同一句
    if slot.slot_date < clock.today().isoformat():
        raise HTTPException(status_code=409, detail="该号源日期已过，不能再预约")
    if slot.employee_id is not None:
        # 医师离职前放出的号源还挂在清单上：登记离职不会回收号源，照样约得上就是约了一个没人坐诊的号
        doctor = db.get(Employee, slot.employee_id)
        if doctor is not None and doctor.status == "left":
            raise HTTPException(status_code=409, detail="该医师已离职，此号源不再接受预约")
        # 调往别院同理（P2-497）：登记调动只改医师的机构，旧机构放出的号还挂着——约上的是一个医师已经不在那家坐诊的号，
        # 寻医清单还把它算在医师**新**机构名下。放号时就判「医师不属于该机构」（单条与批量），这里是同一句的约号那一半
        if doctor is not None and doctor.org_id != slot.org_id:
            raise HTTPException(status_code=409, detail="该医师已调离放号机构，此号源不再接受预约")
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
    if existing:
        # 仅 cancelled 记录允许复用重约。条件 UPDATE（`WHERE status = 'cancelled'`，P2-109）：原先读到 cancelled 就占号、
        # 改 booked，两路并发重约都读到 cancelled、都占了号——一条预约，号源已约数加了两次，白白少放出一个号。
        # 先转预约、再占号：与取消（先转预约、再放号）同一个加锁顺序，PG 上两路不会互等成死锁
        revived = (
            db.query(Appointment)
            .filter(Appointment.id == existing.id, Appointment.status == "cancelled")
            .update({Appointment.status: "booked"}, synchronize_session=False)
        )
        if not revived:   # 并发抢输：另一路已经把它重约上了
            db.rollback()
            raise HTTPException(status_code=409, detail="请勿重复预约")

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
        db.rollback()   # 重约的那一步（转回 booked）一并退回
        raise HTTPException(status_code=409, detail="号源已约满")

    if existing:
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


def _leave_booked(db: Session, appointment: Appointment, to_status: str, action: str) -> None:
    """把一条预约从 booked 转到 `to_status`：条件 UPDATE（`WHERE status = 'booked'`），影响 0 行即 409（P2-109）。

    原先「读状态 → 判 booked → 改」：PG 的 READ COMMITTED 下两路并发都读到 booked、都往下走——取消连点两下，号源
    已约数被多扣一次（之后能多约出一个号）；一路取消、一路核销，已就诊的人占着的号被放出去。号源一侧早是条件 UPDATE
    （H3），状态这一侧补上同一个写法。先按内存里的状态判一次，给顺序重复的请求一句准确的话。"""
    if appointment.status != "booked":
        # 状态机：仅 booked 可取消 / 核销；fulfilled/cancelled 均拒绝（M1）
        raise HTTPException(status_code=409, detail=f"当前状态 {APPOINTMENT_STATUS_NAMES.get(appointment.status, appointment.status)} 不可{action}")
    moved = (
        db.query(Appointment)
        .filter(Appointment.id == appointment.id, Appointment.status == "booked")
        .update({Appointment.status: to_status}, synchronize_session=False)
    )
    if not moved:   # 并发抢输：另一路已经把它转走了
        db.rollback()
        db.refresh(appointment)
        raise HTTPException(status_code=409, detail=f"当前状态 {APPOINTMENT_STATUS_NAMES.get(appointment.status, appointment.status)} 不可{action}")
    appointment.status = to_status


def release_appointment(db: Session, appointment: Appointment) -> Appointment:
    """取消预约并释放号源：管理端与居民端共用。"""
    _leave_booked(db, appointment, "cancelled", "取消")
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
def list_appointments(
    response: Response,
    patient_id: int | None = None,
    # 取值照预约状态列注释（booked / cancelled / fulfilled），写错 422，不静默当成「不筛」
    status: str | None = Query(default=None, pattern="^(booked|cancelled|fulfilled)$"),
    slot_date: str | None = None,
    offset: int = 0,
    limit: int = 500,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """预约清单：管理端「预约记录」表，到诊核销与取消的唯一入口。

    按状态、按号源日期筛（P2-1300）：原先只收 `patient_id`、按编号倒序缺省 500 条，`?status=` / `?slot_date=` 被静默
    忽略——约号过 500 条以后，一周前约、今天就诊的那条已被后约的挤出这一页，页面上找不到这一行去核销 / 取消。页面现在
    先按 `status=booked` 取待办排在最前（同 P2-408 / P2-456）。两个筛选都叠在可见范围（`scope_patient_list`）之后，
    只收窄、不绕过它；出参与分页不变。
    """
    query = db.query(Appointment)
    query = scope_patient_list(db, user, query, Appointment, patient_id, "appointment")
    if status:
        query = query.filter(Appointment.status == status)
    if slot_date:
        # 日期在号源上：join 回号源表等值比；校验与本文件 `list_slots` 同一句（P1-58），留空等于不筛
        slot_date = require_date(slot_date, field="slot_date")
        query = query.join(AppointmentSlot, AppointmentSlot.id == Appointment.slot_id).filter(
            AppointmentSlot.slot_date == slot_date
        )
    return paginate(query.order_by(Appointment.id.desc()), response, offset, limit)


@router.post(
    "/{appointment_id}/cancel",
    response_model=AppointmentOut,
    dependencies=[Depends(require_roles("operator", "doctor"))],  # H2
)
def cancel(
    appointment_id: int, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """经办端取消预约。

    归属隔着一跳：`appointments` 没有机构列，经 `slot_id` 回到
    `appointment_slots.org_id` 才判得了——号源属于哪家医院，谁家的前台就能动它。

    ⚠️ **守卫写在端点里而不是 `release_appointment` 里，这是有意的**：
    那个函数是**管理端与居民端共用**的（`portal.py:1381` 也调它），
    居民取消自己的预约走的是门户令牌那套口径，不该被员工的机构规则判。
    """
    appointment = db.get(Appointment, appointment_id)
    if appointment is None:
        raise HTTPException(status_code=404, detail="预约不存在")
    slot = db.get(AppointmentSlot, appointment.slot_id)
    assert_org_writable(db, user, slot.org_id if slot else None)
    return release_appointment(db, appointment)


@router.post(
    "/{appointment_id}/fulfill",
    response_model=AppointmentOut,
    dependencies=[Depends(require_roles("operator", "doctor"))],  # H2: 到诊核销
)
def fulfill(
    appointment_id: int, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """到诊核销。归属同 `cancel`：经 `slot_id` 回到号源所属机构。

    归属判定排在状态机之前：先 403，免得用"当前状态 X 不可核销"把别家号源的
    状态探出去。
    """
    appointment = db.get(Appointment, appointment_id)
    if appointment is None:
        raise HTTPException(status_code=404, detail="预约不存在")
    slot = db.get(AppointmentSlot, appointment.slot_id)
    assert_org_writable(db, user, slot.org_id if slot else None)
    _leave_booked(db, appointment, "fulfilled", "核销")   # 与并发的取消互斥（P2-109）
    db.commit()
    db.refresh(appointment)
    return appointment

# ---------------------------------------------------------------- ADR-0006 搬家
#
# 以下自 `service_extras.py`（倾倒场）搬入：服务黑名单。
# 路径一字未改（`/api/appointments...` 原样），两边 router 的鉴权本就一致
# （都是 `dependencies=[Depends(get_current_user)]`），故可直接并入本模块的
# router——不像 ADR-0006 第一批的 `/api/performance` 那样存在鉴权分裂。


class BlacklistAddedOut(BaseModel):
    id: int
    domain: str


class BlacklistOut(BaseModel):
    id: int
    domain: str
    # 中文名服务端折算，前端不该自己再维护一份 appointment→"预约爽约" 的映射
    domain_name: str
    patient_id: int
    reason: str


class BlacklistRemovedOut(BaseModel):
    removed: bool
    domain: str


# ---- ⑫ 预约黑名单 ----


class BlacklistCreate(BaseModel):
    patient_id: int
    reason: str = Field(default="", max_length=256)
    domain: str = Field(default="appointment", pattern="^(appointment|shortage)$")


BLACKLIST_DOMAINS = {"appointment": "预约爽约", "shortage": "缺药登记后不取药"}


@router.post("/blacklist", response_model=BlacklistAddedOut, status_code=201,
             dependencies=[Depends(require_admin)])
def add_blacklist(body: BlacklistCreate, db: Session = Depends(get_db)):
    """加入服务黑名单。

    路径保留 `/appointments/blacklist` 不改——已有对接方在用，为了内部模型
    泛化去破坏外部契约不值当。业务域由 `domain` 参数给出，默认预约。
    """
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if (
        db.query(ServiceBlacklist)
        .filter(
            ServiceBlacklist.domain == body.domain,
            ServiceBlacklist.patient_id == body.patient_id,
        )
        .first()
    ):
        raise HTTPException(status_code=409, detail=f"已在{BLACKLIST_DOMAINS[body.domain]}黑名单")
    entry = ServiceBlacklist(**body.model_dump())
    db.add(entry)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="已在黑名单") from None
    return {"id": entry.id, "domain": entry.domain}


@router.get("/blacklist", response_model=list[BlacklistOut],
            dependencies=[Depends(get_current_user)])
def list_blacklist(domain: str | None = None, db: Session = Depends(get_db)):
    query = db.query(ServiceBlacklist)
    if domain:
        query = query.filter(ServiceBlacklist.domain == domain)
    return [
        {"id": b.id, "domain": b.domain, "domain_name": BLACKLIST_DOMAINS.get(b.domain, b.domain),
         "patient_id": b.patient_id, "reason": b.reason}
        for b in query.order_by(ServiceBlacklist.id.desc()).limit(500).all()
    ]


@router.delete("/blacklist/{patient_id}", response_model=BlacklistRemovedOut,
               dependencies=[Depends(require_admin)])
def remove_blacklist(
    patient_id: int, domain: str = "appointment", db: Session = Depends(get_db)
):
    entry = (
        db.query(ServiceBlacklist)
        .filter(ServiceBlacklist.domain == domain, ServiceBlacklist.patient_id == patient_id)
        .first()
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="不在黑名单")
    db.delete(entry)
    db.commit()
    return {"removed": True, "domain": domain}