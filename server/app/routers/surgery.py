"""手术麻醉管理（T2.3）。

此前全平台关于手术只有病案首页里的一个 `operation` 文本字段和 DRG 分组的
"主手术关键词"——外科组能不能正确入组，全看医生在首页那一栏怎么写。这里补齐
申请→审批→排班→术中记录的完整链路；填病案首页时手术栏留空，就从本次住院已完成的术中记录取值
（只在建首页那一刻带一次，首页建好之后再完成的手术不会补进去——P2-182 订正此前「自动取术式」的说法，改不改见待裁定）。

角色：申请与术中记录限医师；审批限管理层（职责分离，申请人不能自己批）；
排班限经办与管理层（手术室排班是护士长/手术室的工作）。
"""
from collections.abc import Iterable
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, exists, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query, Session

from .. import clock
from ..concurrency import move_row, serialized_on
from ..visibility import assert_obj_org_writable, assert_org_writable, assert_patient_visible, scope_org_list
from ..database import get_db
from ..numtypes import INT4_MAX
from ..texttypes import NON_BLANK
from ..datetypes import DateStr, OptionalDateStr, OptionalDateTimeStr, TimeStr, legacy_date, legacy_time
from ..deps import get_current_user, paginate, require_admin, require_date, require_roles
from ..notify import notify_patient
from .followups import FOLLOWUP_TITLE_MAX
from ..models import (
    Admission,
    FollowupTask,
    HighValueConsumable,
    OperatingRoom,
    Organization,
    Patient,
    SurgeryRecord,
    SurgeryRequest,
    SurgerySchedule,
    User,
    utcnow,
)

router = APIRouter(prefix="/api/surgery", tags=["手术麻醉"], dependencies=[Depends(get_current_user)])

# 状态文案（措辞照抄模型列注释；报错文案用它，别把英文码直接拼给窗口人员看——P2-74）
SURGERY_STATUS_NAMES = {"requested": "待审批", "approved": "已审批", "scheduled": "已排班", "completed": "已完成", "cancelled": "已取消"}

# 术后随访默认间隔
SURGERY_FOLLOWUP_DAYS = 14


# ---------------------------------------------------------------- 手术间


class RoomIn(BaseModel):
    org_id: int
    name: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)


# ---------------------------------------------------------------- 响应契约
#
# 模型集中放在所有端点之前（`response_model=` 是装饰器参数，导入时求值）。


class OperatingRoomOut(BaseModel):
    id: int
    org_id: int
    name: str
    active: bool


class SurgeryRequestOut(BaseModel):
    id: int
    admission_id: int
    patient_id: int
    org_id: int
    surgery_name: str
    surgery_code: str
    incision_level: str
    anesthesia_type: str
    surgeon_name: str
    urgency: str
    planned_date: str
    status: str
    #: 非计划重返手术室（P2-172 起出参带上：申请清单上要看得见勾没勾）
    unplanned_return: bool


class SurgeryStatusOut(BaseModel):
    id: int
    status: str


class SurgeryScheduledOut(BaseModel):
    id: int
    request_id: int
    room_id: int
    scheduled_date: str
    start_time: str
    end_time: str


class SurgeryScheduleOut(BaseModel):
    """排班列表连带申请单的字段一起出——排班表要直接看得到术式与术者，
    否则每行都得再拉一次申请单。与 `SurgeryScheduledOut`（新建时的回执）
    不是同一组键，是两个模型。"""

    id: int
    request_id: int
    room_name: str
    scheduled_date: str
    start_time: str
    end_time: str
    surgery_name: str
    surgeon_name: str
    anesthesia_type: str
    urgency: str
    status: str


class SurgeryRecordCreatedOut(BaseModel):
    id: int
    request_id: int
    outcome: str


class SurgeryRecordOut(BaseModel):
    """手术记录全文。注意这**不是**居民端会看到的内容——出血量、术中所见、
    并发症是给医生看的专业文书（见 portal 的 my_surgeries docstring）。"""

    id: int
    request_id: int
    actual_surgery_name: str
    surgeon_name: str
    assistants: str
    anesthetist_name: str
    anesthesia_type: str
    incision_level: str
    start_at: str
    end_at: str
    blood_loss_ml: int
    findings: str
    procedure: str
    complications: str
    outcome: str
    preop_diagnosis: str
    postop_diagnosis: str


class SurgeryStatsOut(BaseModel):
    """按机构的手术量。`by_incision`/`by_anesthesia` 的键是实际出现过的
    切口等级与麻醉方式，没出现的不该硬塞一个 0，故是 dict 而非固定字段。"""

    org_id: int
    org_name: str
    total: int
    by_incision: dict[str, int]
    by_anesthesia: dict[str, int]
    complications: int


@router.post("/rooms", response_model=OperatingRoomOut, status_code=201,
             dependencies=[Depends(require_admin)])
def create_room(body: RoomIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
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


@router.get("/rooms", response_model=list[OperatingRoomOut])
def list_rooms(
    response: Response,
    org_id: int | None = None,
    offset: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(OperatingRoom)
    query = scope_org_list(db, user, query, OperatingRoom, org_id)
    return [
        {"id": r.id, "org_id": r.org_id, "name": r.name, "active": r.active}
        for r in paginate(query.order_by(OperatingRoom.id), response, offset, limit)
    ]


def room_labels(db: Session, room_ids: Iterable[int]) -> dict[int, str]:
    """手术间给患者、经办看的叫法 `{手术间号: 「所属医院 · 手术间名」}`（P2-1402），按手术间号一次连表取齐，空集合不打库。
    「手术已安排」站内信、居民端「我的手术」与管理端排班下拉（`pages-mgmt.js` 的 `roomLabel`）同一个写法。

    手术间名只在一家医院里唯一（同一机构下不重名）。跨机构排台（基层把病人排进县医院的空台，撮合的用途）之后，站内信只写
    「2号手术间」、居民端「医院」一栏是申请方，手术间却是另一家的——照着去的是申请的那家。本院的同样带上：同一间手术间
    不管谁排的叫法一致，站内信原先连医院都不写。
    """
    wanted = set(room_ids)
    if not wanted:
        return {}
    rows = (db.query(OperatingRoom.id, OperatingRoom.name, Organization.name)
            .join(Organization, Organization.id == OperatingRoom.org_id)
            .filter(OperatingRoom.id.in_(wanted)).all())
    return {room_id: f"{org_name} · {room_name}" for room_id, room_name, org_name in rows}


# ---------------------------------------------------------------- 手术申请


class SurgeryRequestIn(BaseModel):
    admission_id: int
    surgery_name: str = Field(min_length=1, max_length=256, pattern=NON_BLANK)
    surgery_code: str = Field(default="", max_length=32)
    incision_level: str = Field(default="II", pattern="^(I|II|III|IV)$")
    anesthesia_type: str = Field(default="general", pattern="^(general|spinal|local|nerve_block)$")
    surgeon_name: str = Field(default="", max_length=64)   # `surgeon_name=body.surgeon_name or …` 原先判据认不出来
    urgency: str = Field(default="elective", pattern="^(elective|urgent|emergency)$")
    # 非计划重返手术室：由医师在提出申请时显式标记。不做推断——分期手术、
    # 计划内二次探查都是正常的，"同一住院有第二台手术"这种规则只会冤枉人。
    # 反过来本次住院此前没有手术的勾不上（422，见 create_request，P2-1398）
    unplanned_return: bool = False
    planned_date: OptionalDateStr = ""


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
        "unplanned_return": r.unplanned_return,
    }


# 术中死亡之后，同一次住院不再提新申请、不再批准、不再排班（P2-1396）。三处共用这半句，各自接上被拦的动作
_DIED_IN_SURGERY = "本次住院患者已于手术中死亡（术中记录转归「死亡」）"


def _died_in_surgery(db: Session, admission_id: int) -> bool:
    """这次住院是否已有转归「死亡」的术中记录（P2-1396）。

    转归「死亡」原先只跳过术后随访与「术后随访已安排」（P2-499，出院的 P2-878 同一句）；住院仍是「在院」（出院要先写
    病案首页），同住院另一张已审批的申请照排 201、家属在居民端收到「手术已安排……请遵医嘱做好术前准备」，死后还能再提
    新申请，那一台挂在排班表与「待填术中记录」里占着手术间。判定按住院：平台主索引没有「死亡」（P1-112 待裁定），不扩到
    患者。驳回不经过这里——驳回是在途单收尾的出口，别堵死（已审批 / 已排班的在途单怎么收尾随 P2-182）。
    """
    return db.query(SurgeryRecord.id).join(SurgeryRequest, SurgeryRecord.request_id == SurgeryRequest.id).filter(
        SurgeryRequest.admission_id == admission_id, SurgeryRecord.outcome == "死亡").first() is not None


@router.post("/requests", response_model=SurgeryRequestOut, status_code=201,
             dependencies=[Depends(require_roles("doctor"))])
def create_request(
    body: SurgeryRequestIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """提出手术申请。患者与机构从住院记录带出，不让客户端自报，避免张冠李戴。"""
    admission = db.get(Admission, body.admission_id)
    if admission is None:
        raise HTTPException(status_code=404, detail="住院记录不存在")
    if admission.status != "admitted":
        raise HTTPException(status_code=409, detail="患者已出院，不可申请手术")
    # 手术排在这次住院所属医院。docstring 说"患者与机构从住院记录带出，不让客户端
    # 自报，避免张冠李戴"——带出来了，但没校验调用方是不是那家。
    # 实测未修前：乙院 doctor 能给甲院的住院病人申请手术（201，org_id 是甲院）。
    assert_obj_org_writable(db, user, admission)
    if _died_in_surgery(db, admission.id):   # P2-1396
        raise HTTPException(status_code=409, detail=f"{_DIED_IN_SURGERY}，不可再申请手术")
    # 「非计划重返手术室」是同一次住院内的**再次**手术（模型列注释、申请表单的勾选说明都这么写）：本次住院此前连一张未取消的手术
    # 申请都没有，就无「重返」可言（P2-1398）。原先住院里唯一一台手术勾了也 201，「非计划重返手术室率」算成 1/2。与上面
    # `SurgeryRequestIn` 的「不做推断」不矛盾：那句说的是不能由「同一住院有第二台」推断出重返（分期、计划内二次探查都不算），
    # 这里只拦连第一台都没有的勾错，有前一台的勾不勾仍以医师标记为准。勾错、漏勾的事后更正是业务口径，不在此列
    if body.unplanned_return and db.query(SurgeryRequest.id).filter(
            SurgeryRequest.admission_id == admission.id, SurgeryRequest.status != "cancelled").first() is None:
        raise HTTPException(status_code=422, detail="本次住院此前没有手术，不能标为非计划重返手术室")
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


@router.get("/requests", response_model=list[SurgeryRequestOut])
def list_requests(
    response: Response,
    status: str | None = None,
    org_id: int | None = None,
    admission_id: int | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    query = db.query(SurgeryRequest)
    if status:
        query = query.filter(SurgeryRequest.status == status)
    query = scope_org_list(db, user, query, SurgeryRequest, org_id)
    if admission_id is not None:
        query = query.filter(SurgeryRequest.admission_id == admission_id)
    return [
        _request_out(r)
        for r in paginate(query.order_by(SurgeryRequest.id.desc()), response, offset, limit)
    ]


class ApproveIn(BaseModel):
    approved: bool = True
    note: str = ""


#: 否决被拦时文案里列出的耗材条码个数上限（P2-1399）：再多的写「等 N 件」，不让一句报错拖成一屏
IMPLANT_BARCODES_SHOWN = 5


def _refuse_if_implanted(db: Session, request_id: int) -> None:
    """已按这张申请登记了高值耗材（耗材的 `used_surgery_id` 指向它）的，不可否决：409 并列出条码（P2-1399）。

    急诊先植入、后被否决，追溯链原先记成「植入于已取消的手术」（申请已是已取消），想改挂到重提的申请又因耗材「已使用」
    409——登记没有逆操作（P2-752），登错了改不回。与 `materials.use_consumable` 里「不让登记到已取消的手术」（P2-763）是
    同一条规矩的两侧：那边拦「往已取消的手术上登记」，这边拦「把已登记了耗材的手术取消」。
    """
    barcodes = [code for (code,) in db.query(HighValueConsumable.barcode).filter(
        HighValueConsumable.used_surgery_id == request_id).order_by(HighValueConsumable.id)]
    if barcodes:
        more = f" 等 {len(barcodes)} 件" if len(barcodes) > IMPLANT_BARCODES_SHOWN else ""
        raise HTTPException(status_code=409, detail=f"这张申请已登记了高值耗材（条码 "
                                                    f"{'、'.join(barcodes[:IMPLANT_BARCODES_SHOWN])}{more}），"
                                                    "不能否决——否决后耗材追溯会记成植入于一台已取消的手术")


@router.post("/requests/{request_id}/approve", response_model=SurgeryStatusOut,
             dependencies=[Depends(require_roles("director"))])
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
    assert_obj_org_writable(db, user, request)
    if request.status != "requested":
        raise HTTPException(status_code=409, detail=f"当前状态 {SURGERY_STATUS_NAMES.get(request.status, request.status)} 不可审批")
    if request.created_by == user.id:
        raise HTTPException(status_code=403, detail="不得审批本人提出的手术申请")
    if body.approved and _died_in_surgery(db, request.admission_id):   # 只拦批准，驳回照旧放行（P2-1396）
        raise HTTPException(status_code=409, detail=f"{_DIED_IN_SURGERY}，不可审批通过")
    expect = SurgeryRequest.status == "requested"
    if not body.approved:
        # 已按这张申请登记了高值耗材的不可否决（P2-1399）。先锁外看一眼（报错要列出条码），「没有耗材指向它」再压进下面
        # 那条带状态条件的 UPDATE：看完到翻转之间提交的登记也拦得住。剩下的窗口在登记那一侧——`use_consumable` 锁外读申请
        # 状态，它读到「待审批」、登记却在这条 UPDATE 开始执行之后才提交的，两路都成，耗材照样挂到已取消的申请上（两条
        # UPDATE 改的是两张表，这条看不见那边还没提交的行）；要收口得两侧都先锁申请那一行再判，属登记链路的改动，不在此列
        _refuse_if_implanted(db, request.id)
        expect = and_(expect, ~exists().where(HighValueConsumable.used_surgery_id == request.id))
    # 审批与「还待审批」压进同一条 UPDATE（P2-1184，与物资采购 P2-403、药品采购 P2-759、用血 P2-110 同一个写法）：上面那道
    # 预检是锁外读的——两位主任一个批准、一个驳回同时到，原先整行写回、后提交的把先提交的结论改掉，两路都 200：驳回的
    # 主任以为已经否决，申请却成了「已审批」，经办照常排进手术间、患者收到「手术已安排」
    if not move_row(db, SurgeryRequest, request.id, expect,
                    status="approved" if body.approved else "cancelled", approved_by=user.id,
                    approved_at=utcnow()):
        db.rollback()
        db.refresh(request)  # 抢输了就按库里的现状措辞
        if request.status == "requested" and not body.approved:
            _refuse_if_implanted(db, request.id)   # 还待审批却没改到：输给了刚提交的耗材登记（P2-1399）
        raise HTTPException(status_code=409, detail=f"当前状态 {SURGERY_STATUS_NAMES.get(request.status, request.status)} 不可审批")
    db.commit()
    return {"id": request.id, "status": request.status}


# ---------------------------------------------------------------- 排班


def _canonical_date(model):
    """日期列是规范的 `YYYY-MM-DD`（十个字符、第 5 / 8 位是 '-'）：按字符串比、等值查都可信的那些行。"""
    return model.scheduled_date.like("____-__-__")


def room_occupancy(query: Query[SurgerySchedule], day: str) -> list[tuple[SurgerySchedule, str, str]]:
    """手术间在 `day`（规范的 `YYYY-MM-DD`）占着的时段：`query` 是调用方按手术间筛好的排班查询（排班判同一患者重叠时传按
    患者筛好的，P2-1397），返回 `[(排班行, 起, 止), …]`，起止读成 `HH:MM`、按时刻先后排。

    排班判冲突（`schedule_surgery`）与手术间撮合（`resources.match_operating_rooms`）共用这一份（P2-1401）：撮合原先自己按
    `scheduled_date == 当天` 等值查、时刻按字符串比，存量「2026-10-6 08:00-10:00」占着的手术间撮合说可用，照着排却 409——
    撮合说能排、排班说冲突，比没有撮合更糟。

    同手术间当天的，加上日期不是规范写法的存量行，逐条按日历读了再筛（P2-894）：P1-61 / P2-46 之前日期、时刻都不卡形状，
    「2026-10-5」等值比不上「2026-10-05」、「０８:００」按字符串比排在一切半角时刻之后——同一手术间同一时段又排进一台（与
    P1-117 同一个后果）。时刻认不出的按占满当天算，起止记成 00:00 / 24:00（24:00 按字符串比大过一天里的任何时刻）：宁可拦下
    让人核对，也不把两台排进同一手术间。
    """
    taken = []
    for row in (query.filter(or_(SurgerySchedule.scheduled_date == day, ~_canonical_date(SurgerySchedule)))
                .order_by(SurgerySchedule.start_time, SurgerySchedule.id).all()):
        if legacy_date(row.scheduled_date) != day:
            continue
        start, end = legacy_time(row.start_time), legacy_time(row.end_time)
        if start is None or end is None:
            start, end = "00:00", "24:00"
        taken.append((row, start, end))
    taken.sort(key=lambda item: (item[1], item[0].id))
    return taken


class ScheduleIn(BaseModel):
    room_id: int
    scheduled_date: DateStr
    # 只认半角、且是一天里真有的时刻（P2-46）：全角「０８:００」按字符串比排在一切半角时刻之后，
    # 冲突判定判不出它与半角时段重叠，同一手术间同一时段排得进两台
    start_time: TimeStr
    end_time: TimeStr


@router.post(
    "/requests/{request_id}/schedule",
    response_model=SurgeryScheduledOut,
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
    判定读的是别的排班行、写的是一条 INSERT——INSERT 不给任何既有行加锁，并发下两路都读到「没有重叠」
    就都排进去；(room_id, date, start_time) 唯一约束只挡得住起点完全相同的那种。原先把起点不同的情形
    记作「极窄竞态由排班人复核，不上悲观锁」，真 PG 实测八路同时排起点错开的重叠时段，八台全排进同一
    手术间（P1-117）。故判定与写入圈在手术间这一行的临界区里（`serialized_on`，PG 上 `SELECT … FOR UPDATE`），
    不同手术间之间互不阻塞。同一患者同日的时段也不许重叠（P2-1397），外面再圈一层患者那一行（先患者、后手术间）。
    """
    request = db.get(SurgeryRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="手术申请不存在")
    assert_obj_org_writable(db, user, request)
    if request.status != "approved":
        raise HTTPException(status_code=409, detail=f"当前状态 {SURGERY_STATUS_NAMES.get(request.status, request.status)} 不可排班")
    if _died_in_surgery(db, request.admission_id):   # 不再给死者排台、不再发「手术已安排」（P2-1396）
        raise HTTPException(status_code=409, detail=f"{_DIED_IN_SURGERY}，不可排班")
    room = db.get(OperatingRoom, body.room_id)
    if room is None or not room.active:
        raise HTTPException(status_code=404, detail="手术间不存在或已停用")
    if body.end_time <= body.start_time:
        raise HTTPException(status_code=422, detail="结束时间须晚于开始时间")

    # 先锁患者、后锁手术间（P2-1397）：下面还判同一患者有没有时段重叠的另一台——同一患者的两台同时排进两个手术间，两路锁的
    # 是不同的手术间行、互不阻塞，判定读的又是别的排班行（与 P1-117 同一个形状），两路都读到「没有重叠」、各插一条；故再在
    # 患者那一行上圈一层。两把锁只在这里一起拿、次序恒为患者在前，PG 上不会两路各握一把互等成死锁；以后别处要同时拿这两把
    # （如改期换台），也照这个次序
    with serialized_on(db, Patient, request.patient_id), serialized_on(db, OperatingRoom, room.id):
        # 当天的占用按 `room_occupancy` 读（存量的非规范日期、时刻照读，认不出的按占满当天，P2-894），与撮合同一份（P2-1401）
        for taken, start, end in room_occupancy(
                db.query(SurgerySchedule).filter(SurgerySchedule.room_id == body.room_id), body.scheduled_date):
            if start < body.end_time and end > body.start_time:
                raise HTTPException(
                    status_code=409,
                    detail=f"手术间在 {taken.start_time}-{taken.end_time} 已被占用",
                )
        # 同一患者同一天时段重叠的另一台（P2-1397）：上面只按手术间判，同一患者的两台排进两个手术间的重叠时段原先都 201，
        # 居民收到两条时段重叠的「手术已安排」。已取消的不算；当天的占用同样按 `room_occupancy` 读（存量非规范写法照读，时刻
        # 认不出的按占满当天，P2-1401）。术者、麻醉医师撞台是业务口径（术者是自由文本，按姓名判会误伤同名），不在此列
        for taken, start, end in room_occupancy(
                db.query(SurgerySchedule)
                .join(SurgeryRequest, SurgerySchedule.request_id == SurgeryRequest.id)
                .filter(SurgeryRequest.patient_id == request.patient_id, SurgeryRequest.status != "cancelled",
                        SurgerySchedule.request_id != request.id),
                body.scheduled_date):
            if start < body.end_time and end > body.start_time:
                raise HTTPException(
                    status_code=409,
                    detail=f"该患者在 {taken.start_time}-{taken.end_time} 已排有另一台手术，同一患者的手术时段不能重叠",
                )

        # D-1：排班记录、申请状态、患者通知必须同进同出。此处原先分两次 commit，
        # 第二次之前中断就会留下"排班已落库、申请仍是 approved"的死结——重排被唯一
        # 约束挡回 409，填术中记录又要求 scheduled，只能改数据库才能救。
        # `notify_*` 本就设计成只 add 不 commit，正是为了让业务事务统一决定提交时机。
        # commit 必须在临界区里：PG 上行锁随提交释放，提前出块就把窗口放回去了。
        schedule = SurgerySchedule(request_id=request_id, created_by=user.id, **body.model_dump())
        db.add(schedule)
        request.status = "scheduled"
        # 手术间带所属医院（P2-1402）：排进别家手术间的，原先只写「2号手术间」，患者照着去的是申请的那家
        notify_patient(
            db,
            request.patient_id,
            category="surgery",
            title=f"手术已安排：{request.surgery_name}",
            body=f"{body.scheduled_date} {body.start_time}-{body.end_time}，"
                 f"{room_labels(db, [room.id]).get(room.id, room.name)}。请遵医嘱做好术前准备。",
            link_type="surgery_request",
            link_id=request.id,
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=409, detail="该手术已排班或时段被并发占用") from None
    db.refresh(schedule)
    return {
        "id": schedule.id,
        "request_id": request_id,
        "room_id": schedule.room_id,
        "scheduled_date": schedule.scheduled_date,
        "start_time": schedule.start_time,
        "end_time": schedule.end_time,
    }


@router.get("/schedules", response_model=list[SurgeryScheduleOut])
def list_schedules(
    scheduled_date: str | None = None, room_id: int | None = None, db: Session = Depends(get_db)
):
    """手术排班表：按手术间与时段排序，就是手术室墙上那张表。

    不指定日期时给**今天及以后**的排班（P2-155）：原先按日期升序取前 300 条，排班一多（一天十来台，不到一个月），
    桌面与移动端的「手术排班」只剩最早那 300 条历史，明天的手术哪儿都看不见。查某一天的照旧按日期等值查。
    """
    query = db.query(SurgerySchedule, SurgeryRequest, OperatingRoom).join(
        SurgeryRequest, SurgerySchedule.request_id == SurgeryRequest.id
    ).join(OperatingRoom, SurgerySchedule.room_id == OperatingRoom.id)
    # 日期不是规范写法的存量行（P1-61 之前存下的「2026-9-5」「2026/10/05」）按日历读了再筛再排（P2-894）：原先按字符串比，
    # 「2026-9-5」同一年里比任何规范日期都「晚」，早过去的旧排班一直挂在「今天及以后」；查某一天也漏掉同一天的旧写法
    today = clock.today().isoformat()
    if room_id is not None:
        query = query.filter(SurgerySchedule.room_id == room_id)
    # 规范写法的按字符串筛、排、取前 300；日期不是规范写法的存量行（P1-61 之前）数量有限，另取出来按日历读了再并进来
    # 重排（最终的前 300 里规范写法的那些必在规范写法的前 300 里，结果不变）
    if scheduled_date:
        # 等值匹配：`2026-9-1` 会让"这天没有手术排班"，不报错（P1-58）
        scheduled_date = require_date(scheduled_date, field="scheduled_date")
        canonical = query.filter(SurgerySchedule.scheduled_date == scheduled_date)
    else:
        canonical = query.filter(SurgerySchedule.scheduled_date >= today)
    found = [(s.scheduled_date, s, r, room) for s, r, room in canonical.filter(_canonical_date(SurgerySchedule)).order_by(
        SurgerySchedule.scheduled_date, SurgerySchedule.room_id, SurgerySchedule.start_time
    ).limit(300).all()]
    for s, r, room in query.filter(~_canonical_date(SurgerySchedule)).order_by(SurgerySchedule.id).all():
        day = legacy_date(s.scheduled_date)
        if day is not None and (day == scheduled_date if scheduled_date else day >= today):
            found.append((day, s, r, room))
    found.sort(key=lambda row: (row[0], row[1].room_id, legacy_time(row[1].start_time) or row[1].start_time))
    rows = [(s, r, room) for _, s, r, room in found[:300]]
    # 做完的手术按术中记录的实际术式与术者（P2-1400），取法同居民端「我的手术」（P2-556）：中转开腹、换了主刀的，
    # 「查看记录」与居民端写实际的，排班表原先还印申请单上的——手术记录署的术者（P2-1307）在这张表上对不上。只取这两项；
    # 没有术中记录的照旧取申请单
    records = {
        rec.request_id: rec
        for rec in db.query(SurgeryRecord.request_id, SurgeryRecord.actual_surgery_name, SurgeryRecord.surgeon_name)
        .filter(SurgeryRecord.request_id.in_([r.id for _, r, _ in rows] or [0]))
        .all()
    }
    return [
        {
            "id": s.id,
            "request_id": r.id,
            "room_name": room.name,
            "scheduled_date": s.scheduled_date,
            "start_time": s.start_time,
            "end_time": s.end_time,
            "surgery_name": records[r.id].actual_surgery_name if r.id in records else r.surgery_name,
            "surgeon_name": (records[r.id].surgeon_name if r.id in records else "") or r.surgeon_name,
            "anesthesia_type": r.anesthesia_type,
            "urgency": r.urgency,
            "status": r.status,
        }
        for s, r, room in rows
    ]


# ---------------------------------------------------------------- 术中记录


class SurgeryRecordIn(BaseModel):
    actual_surgery_name: str = Field(min_length=1, max_length=256, pattern=NON_BLANK)
    # 以下补列长 / 列容量（P1-91 / P1-93 第四层）：`SurgeryRecord(**{**body.model_dump(), …})` 这种字典字面量写法
    # 原先判据认不出来，PG 上超长即 500
    surgeon_name: str = Field(default="", max_length=64)
    assistants: str = Field(default="", max_length=256)
    anesthetist_name: str = Field(default="", max_length=64)
    anesthesia_type: str = Field(default="general", pattern="^(general|spinal|local|nerve_block)$")
    incision_level: str = Field(default="II", pattern="^(I|II|III|IV)$")
    start_at: OptionalDateTimeStr = ""  # 时间戳真源（P1-100）：形状不对 422，合法值原样落库
    end_at: OptionalDateTimeStr = ""
    blood_loss_ml: int = Field(default=0, ge=0, le=INT4_MAX)
    findings: str = Field(default="", max_length=2048)
    procedure: str = Field(default="", max_length=4096)
    complications: str = Field(default="", max_length=1024)
    outcome: str = Field(default="好转", pattern="^(治愈|好转|未愈|死亡)$")
    # 术前/术后诊断：留空即"未采集"，不进诊断符合率的分母（见 quality 模块口径）
    preop_diagnosis: str = Field(default="", max_length=256)
    postop_diagnosis: str = Field(default="", max_length=256)


def operation_day(start_at: str | None, scheduled_date: str | None) -> str | None:
    """做手术的那天（`YYYY-MM-DD`）：手术开始时刻的日期，没填取排班日；都读不成返回 None，由调用方按录入时刻兜底。

    术后随访起算（`_operation_day`，P2-896）与手术质量指标归月（`quality.clinical_indicators`，P2-1075）共用这一条。
    新录的开始时刻已按 `check_datetime` 校验过（`YYYY-MM-DD` 开头），存量的开始时刻与排班日可能是 P1-61 之前的写法，
    一律按日历读（`legacy_date`）。
    """
    for raw in ((start_at or "").replace("T", " ").split(" ")[0], scheduled_date or ""):
        day = legacy_date(raw)
        if day:
            return day
    return None


def _operation_day(db: Session, request_id: int, start_at: str) -> date:
    """术后随访从手术那天起算（P2-896）：取手术开始时刻的日期，没填取排班日，再没有才取今天（取法见 `operation_day`）。

    原先按术中记录的录入日（今天）起算——9-20 夜里做的手术 9-24 补录（开始时刻写的是 9-20），随访到期 10-08，应为
    10-04。与出院随访按实际出院日起算（P2-545）、生命周期补登按发生日期（P2-864）同一句。
    """
    scheduled = db.query(SurgerySchedule.scheduled_date).filter(SurgerySchedule.request_id == request_id).first()
    day = operation_day(start_at, scheduled[0] if scheduled else None)
    return date.fromisoformat(day) if day else clock.today()


@router.post(
    "/requests/{request_id}/record", response_model=SurgeryRecordCreatedOut,
    status_code=201, dependencies=[Depends(require_roles("doctor"))]
)
def create_record(
    request_id: int,
    body: SurgeryRecordIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """填写术中记录并结案；同时自动生成术后随访任务（转归「死亡」的不生成，见下）。"""
    # 起止顺序（区间起止顺序）：`T` 写法与空格写法先换成同一种再比——时间戳原样落库（P1-100）
    if body.start_at and body.end_at and body.end_at.replace("T", " ") < body.start_at.replace("T", " "):
        raise HTTPException(status_code=422, detail="手术结束时间不得早于开始时间")
    request = db.get(SurgeryRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="手术申请不存在")
    assert_obj_org_writable(db, user, request)
    if request.status != "scheduled":
        raise HTTPException(status_code=409, detail=f"当前状态 {SURGERY_STATUS_NAMES.get(request.status, request.status)} 不可填写术中记录")
    record = SurgeryRecord(
        request_id=request_id,
        created_by=user.id,
        **{**body.model_dump(), "surgeon_name": body.surgeon_name or request.surgeon_name},
    )
    db.add(record)
    request.status = "completed"
    # 转归「死亡」不排术后随访、不发「术后随访已安排」（P2-499）：原先照排照发——「您的XX已完成，我们将在 N 天内与您联系
    # 随访」发到死者名下（家属在居民端看得到），术后随访任务到期被扫成超期、挂进随访督办。转归与记录同一个请求，
    # 不存在出院随访（P2-385）那种「转归还没写」的先后问题
    if body.outcome != "死亡":
        db.add(
            FollowupTask(
                patient_id=request.patient_id,
                org_id=request.org_id,
                category="surgery",
                source_id=request.id,
                title=f"术后随访：{body.actual_surgery_name}"[:FOLLOWUP_TITLE_MAX],   # 超列宽截断（P1-164）
                due_date=(_operation_day(db, request_id, body.start_at)
                          + timedelta(days=SURGERY_FOLLOWUP_DAYS)).isoformat(),
            )
        )
        notify_patient(
            db,
            request.patient_id,
            category="followup",
            title="术后随访已安排",
            body=f"您的{body.actual_surgery_name}已完成，我们将在 {SURGERY_FOLLOWUP_DAYS} 天内与您联系随访。",
            link_type="surgery_request",
            link_id=request.id,
        )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该手术已有术中记录") from None
    db.refresh(record)
    return {"id": record.id, "request_id": request_id, "outcome": record.outcome}


@router.get("/requests/{request_id}/record", response_model=SurgeryRecordOut)
def get_record(request_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """术中记录。按申请所属患者做可见性判定并留痕（P0-20）。

    原先连调用方都不收：乙院医生按申请号就能读甲院的术式、术中所见、转归与术前术后诊断
    （实测 200）。本文件的申请清单按机构收口、写接口都校验归属，唯独这条读接口什么都不问——
    与 P0-10 / P0-19 同一个形状，判定照抄 `assert_patient_visible`（判定与留痕绑在一起）。
    """
    request = db.get(SurgeryRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="术中记录不存在")
    assert_patient_visible(db, user, request.patient_id, resource="surgery_record")
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
        "preop_diagnosis": record.preop_diagnosis,
        "postop_diagnosis": record.postop_diagnosis,
    }


@router.get("/stats", response_model=list[SurgeryStatsOut],
            dependencies=[Depends(require_roles("director"))])
def surgery_stats(db: Session = Depends(get_db)):
    # 第十轮 P2：手术量按机构汇总，属管理聚合，限 director/admin。
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
