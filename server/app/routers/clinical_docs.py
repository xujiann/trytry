"""住院临床文书（T2.1 / T2.2）：病程记录、护理记录、体温单、交接班。

此前 `/api/inpatient` 只有入出转、医嘱、病案首页——住院期间的连续文书是空的。
门诊那套 `MedicalRecord` 顶不上：它是"一次就诊一份"，而住院病程是同一次住院内
的连续记录流，因此这里全部挂在 admission 上。

角色约定沿用 deps.py 矩阵：病程记录限医师（诊疗性质），护理记录与体温单
放开给医师与经办（护士在本平台的角色映射为 operator），交接班同护理。
"""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, paginate, require_roles
from ..models import (
    Admission,
    InpatientOrder,
    NursingRecord,
    ProgressNote,
    ShiftHandover,
    User,
    VitalSignRecord,
    Ward,
)
from ..visibility import assert_obj_org_writable

router = APIRouter(prefix="/api/inpatient", tags=["住院临床文书"], dependencies=[Depends(get_current_user)])

NOTE_TYPES = ("first", "daily", "ward_round", "rescue", "consultation", "discharge")


# ---------------------------------------------------------------- 响应契约
#
# 模型集中放在所有端点之前（`response_model=` 是装饰器参数，导入时求值）。


class ProgressNoteOut(BaseModel):
    id: int
    admission_id: int
    note_type: str
    content: str
    doctor_name: str
    # 没填记录时刻就落创建时刻（"%Y-%m-%d %H:%M"，与 created_at 的 isoformat 不同格式）
    recorded_at: str
    created_at: str


class DocumentCompletenessOut(BaseModel):
    admission_id: int
    note_types: list[str]
    nursing_records: int
    vital_records: int
    missing: list[str]
    complete: bool


class NursingRecordCreatedOut(BaseModel):
    """新建护理记录**带** `admission_id`，列表里不带——两处形状不同，
    故是两个模型。（列表是按 admission 查的，再回一遍 id 是冗余。）"""

    id: int
    admission_id: int
    inpatient_order_id: int | None
    nursing_level: str
    content: str
    nurse_name: str
    recorded_at: str


class NursingRecordOut(BaseModel):
    id: int
    inpatient_order_id: int | None
    nursing_level: str
    content: str
    nurse_name: str
    recorded_at: str


class VitalCreatedOut(BaseModel):
    id: int
    measured_at: str


class VitalSignOut(BaseModel):
    """体温单一行。八项体征**全部可空**：一次测量未必测全，
    用 0 冒充"未测"会污染趋势曲线（见 VitalIn 的注释）。"""

    id: int
    measured_at: str
    temperature: float | None
    pulse: int | None
    respiration: int | None
    sbp: int | None
    dbp: int | None
    intake_ml: int | None
    output_ml: int | None
    weight_kg: float | None
    recorder: str


class HandoverCreatedOut(BaseModel):
    """交接班新建只回六个键（没有 from_staff/to_staff/content），
    与列表的九个键不同形。"""

    id: int
    ward_id: int
    shift: str
    handover_date: str
    patient_count: int
    critical_count: int


class HandoverOut(BaseModel):
    id: int
    ward_id: int
    shift: str
    handover_date: str
    from_staff: str
    to_staff: str
    patient_count: int
    critical_count: int
    content: str


def _admission_or_404(db: Session, admission_id: int) -> Admission:
    admission = db.get(Admission, admission_id)
    if admission is None:
        raise HTTPException(status_code=404, detail="住院记录不存在")
    return admission


def _admission_for_write(db: Session, user: User, admission_id: int) -> Admission:
    """写文书前的取件 + 归属校验。

    本模块的文书全部挂在 admission 上，而 `NursingRecord`/`ProgressNote`/
    `VitalSignRecord` 自己都不带 org_id——归属隔一跳在 `admissions.org_id` 上。
    此前四个写接口一个守卫都没有：任一成员单位的医师或经办按 admission_id
    就能往**别家医院的住院病历**里写病程记录、护理记录和体温单。病历是法律
    文书，别家写进来的内容既删不掉也说不清是谁的责任。

    单独包一层而不是把校验塞进 `_admission_or_404`：那个还被四个读接口用着，
    读侧的可见性口径与"能不能以这家机构名义写"不是一回事，混在一起会误伤读。
    """
    admission = _admission_or_404(db, admission_id)
    assert_obj_org_writable(db, user, admission)
    return admission


# ---------------------------------------------------------------- 病程记录


class ProgressNoteIn(BaseModel):
    note_type: str = Field(pattern="^(first|daily|ward_round|rescue|consultation|discharge)$")
    content: str = Field(min_length=1, max_length=4096)
    doctor_name: str = ""
    recorded_at: str = ""


@router.post(
    "/admissions/{admission_id}/progress-notes",
    response_model=ProgressNoteOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor"))],  # 病程记录=医师
)
def create_progress_note(
    admission_id: int,
    body: ProgressNoteIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """书写病程记录。

    两条规则：出院后不得再补录（病历应在住院期间形成）；首次病程每次住院唯一。
    """
    admission = _admission_for_write(db, user, admission_id)
    if admission.status != "admitted":
        raise HTTPException(status_code=409, detail="患者已出院，不可再书写病程记录")
    if body.note_type == "first":
        exists = (
            db.query(ProgressNote)
            .filter(ProgressNote.admission_id == admission_id, ProgressNote.note_type == "first")
            .first()
        )
        if exists is not None:
            raise HTTPException(status_code=409, detail="首次病程记录已存在")
    note = ProgressNote(
        admission_id=admission_id,
        note_type=body.note_type,
        content=body.content,
        doctor_name=body.doctor_name or user.full_name,
        recorded_at=body.recorded_at,
        created_by=user.id,
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return _note_out(note)


def _note_out(n: ProgressNote) -> dict:
    return {
        "id": n.id,
        "admission_id": n.admission_id,
        "note_type": n.note_type,
        "content": n.content,
        "doctor_name": n.doctor_name,
        "recorded_at": n.recorded_at or n.created_at.strftime("%Y-%m-%d %H:%M"),
        "created_at": n.created_at.isoformat(),
    }


@router.get("/admissions/{admission_id}/progress-notes",
            response_model=list[ProgressNoteOut])
def list_progress_notes(
    admission_id: int,
    response: Response,
    note_type: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    _admission_or_404(db, admission_id)
    query = db.query(ProgressNote).filter(ProgressNote.admission_id == admission_id)
    if note_type:
        query = query.filter(ProgressNote.note_type == note_type)
    return [_note_out(n) for n in paginate(query.order_by(ProgressNote.id), response, offset, limit)]


@router.get("/admissions/{admission_id}/document-completeness",
            response_model=DocumentCompletenessOut)
def document_completeness(admission_id: int, db: Session = Depends(get_db)):
    """文书完整性检查：住院病历该有而没有的部分。

    出院前的自查工具，也是病历质控的抓手——缺首次病程、无护理记录、
    无体征记录都是终末质控里最常见的扣分项。
    """
    _admission_or_404(db, admission_id)
    types = {
        t
        for (t,) in db.query(ProgressNote.note_type)
        .filter(ProgressNote.admission_id == admission_id)
        .distinct()
        .all()
    }
    nursing = db.query(NursingRecord).filter(NursingRecord.admission_id == admission_id).count()
    vitals = db.query(VitalSignRecord).filter(VitalSignRecord.admission_id == admission_id).count()
    missing = []
    if "first" not in types:
        missing.append("缺首次病程记录")
    if "daily" not in types:
        missing.append("缺日常病程记录")
    if not nursing:
        missing.append("缺护理记录")
    if not vitals:
        missing.append("缺体征记录")
    return {
        "admission_id": admission_id,
        "note_types": sorted(types),
        "nursing_records": nursing,
        "vital_records": vitals,
        "missing": missing,
        "complete": not missing,
    }


# ---------------------------------------------------------------- 护理记录


class NursingIn(BaseModel):
    nursing_level: str = Field(default="level2", pattern="^(special|level1|level2|level3)$")
    content: str = Field(default="", max_length=2048)
    nurse_name: str = ""
    recorded_at: str = ""
    # 护理执行联动（P1-24a）：本条护理记录若由执行某条医嘱产生，传该医嘱 id。
    # 医嘱必须存在且属于同一次住院——挂错住院的联动比不联动更糟（质控会拿它下结论）。
    inpatient_order_id: int | None = None


@router.post(
    "/admissions/{admission_id}/nursing-records",
    response_model=NursingRecordCreatedOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "operator"))],  # 护士在本平台映射为 operator
)
def create_nursing_record(
    admission_id: int,
    body: NursingIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    admission = _admission_for_write(db, user, admission_id)
    if admission.status != "admitted":
        raise HTTPException(status_code=409, detail="患者已出院，不可再书写护理记录")
    if body.inpatient_order_id is not None:
        order = db.get(InpatientOrder, body.inpatient_order_id)
        if order is None or order.admission_id != admission_id:
            # 422 而非 404：这是请求体里的关联字段不合法，与"路径资源不存在"区分开
            raise HTTPException(status_code=422, detail="关联医嘱不存在或不属于本次住院")
    record = NursingRecord(
        admission_id=admission_id,
        inpatient_order_id=body.inpatient_order_id,
        nursing_level=body.nursing_level,
        content=body.content,
        nurse_name=body.nurse_name or user.full_name,
        recorded_at=body.recorded_at,
        created_by=user.id,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return {
        "id": record.id,
        "admission_id": admission_id,
        "inpatient_order_id": record.inpatient_order_id,
        "nursing_level": record.nursing_level,
        "content": record.content,
        "nurse_name": record.nurse_name,
        "recorded_at": record.recorded_at or record.created_at.strftime("%Y-%m-%d %H:%M"),
    }


@router.get("/admissions/{admission_id}/nursing-records",
            response_model=list[NursingRecordOut])
def list_nursing_records(
    admission_id: int, response: Response, offset: int = 0, limit: int = 100,
    db: Session = Depends(get_db),
):
    _admission_or_404(db, admission_id)
    query = db.query(NursingRecord).filter(NursingRecord.admission_id == admission_id)
    return [
        {
            "id": r.id,
            "inpatient_order_id": r.inpatient_order_id,
            "nursing_level": r.nursing_level,
            "content": r.content,
            "nurse_name": r.nurse_name,
            "recorded_at": r.recorded_at or r.created_at.strftime("%Y-%m-%d %H:%M"),
        }
        for r in paginate(query.order_by(NursingRecord.id), response, offset, limit)
    ]


# ---------------------------------------------------------------- 体温单


class VitalIn(BaseModel):
    measured_at: str = Field(min_length=1, max_length=16)
    # 全部可空：一次测量未必测全，用 0 冒充"未测"会污染趋势曲线
    temperature: float | None = Field(default=None, ge=30, le=45)
    pulse: int | None = Field(default=None, ge=0, le=300)
    respiration: int | None = Field(default=None, ge=0, le=100)
    sbp: int | None = Field(default=None, ge=0, le=300)
    dbp: int | None = Field(default=None, ge=0, le=200)
    intake_ml: int | None = Field(default=None, ge=0)
    output_ml: int | None = Field(default=None, ge=0)
    weight_kg: float | None = Field(default=None, ge=0, le=500)
    recorder: str = ""


@router.post(
    "/admissions/{admission_id}/vitals",
    response_model=VitalCreatedOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "operator"))],
)
def create_vital(
    admission_id: int,
    body: VitalIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    admission = _admission_for_write(db, user, admission_id)
    if admission.status != "admitted":
        raise HTTPException(status_code=409, detail="患者已出院，不可再记录体征")
    record = VitalSignRecord(
        admission_id=admission_id, created_by=user.id,
        **body.model_dump(exclude={"recorder"}), recorder=body.recorder or user.full_name,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return {"id": record.id, "measured_at": record.measured_at}


@router.get("/admissions/{admission_id}/vitals", response_model=list[VitalSignOut])
def list_vitals(admission_id: int, db: Session = Depends(get_db)):
    """体温单数据：按测量时刻升序，供前端画趋势曲线。"""
    _admission_or_404(db, admission_id)
    rows = (
        db.query(VitalSignRecord)
        .filter(VitalSignRecord.admission_id == admission_id)
        .order_by(VitalSignRecord.measured_at, VitalSignRecord.id)
        .limit(500)
        .all()
    )
    return [
        {
            "id": r.id,
            "measured_at": r.measured_at,
            "temperature": r.temperature,
            "pulse": r.pulse,
            "respiration": r.respiration,
            "sbp": r.sbp,
            "dbp": r.dbp,
            "intake_ml": r.intake_ml,
            "output_ml": r.output_ml,
            "weight_kg": r.weight_kg,
            "recorder": r.recorder,
        }
        for r in rows
    ]


# ---------------------------------------------------------------- 交接班


class HandoverIn(BaseModel):
    ward_id: int
    shift: str = Field(pattern="^(day|evening|night)$")
    handover_date: str = Field(min_length=10, max_length=10)
    from_staff: str = ""
    to_staff: str = ""
    critical_count: int = Field(default=0, ge=0)
    content: str = Field(default="", max_length=2048)


@router.post(
    "/handovers", response_model=HandoverCreatedOut, status_code=201,
    dependencies=[Depends(require_roles("doctor", "operator"))]
)
def create_handover(
    body: HandoverIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """交接班：在院人数由系统按当前住院数据快照，不让人工填——这个数填错就没意义了。"""
    ward = db.get(Ward, body.ward_id)
    if ward is None:
        raise HTTPException(status_code=404, detail="病区不存在")
    # 归属校验：与同模块另外三个写接口同一族，只是归属由 body 里的 ward_id 定
    # 而非路径参数——`Ward` 自带 org_id，直接按对象校验即可。
    # 顺带说明它为何不在闸门的名单里：闸门只扫路径参数型（`/{id}`）写接口，
    # 归属走 body 的这一类它一个都看不见。这是**第三个**结构性盲区，已登记。
    assert_obj_org_writable(db, user, ward)
    patient_count = (
        db.query(Admission)
        .filter(Admission.ward_id == body.ward_id, Admission.status == "admitted")
        .count()
    )
    handover = ShiftHandover(
        **body.model_dump(), patient_count=patient_count, created_by=user.id
    )
    db.add(handover)
    db.commit()
    db.refresh(handover)
    return {
        "id": handover.id,
        "ward_id": handover.ward_id,
        "shift": handover.shift,
        "handover_date": handover.handover_date,
        "patient_count": handover.patient_count,
        "critical_count": handover.critical_count,
    }


@router.get("/handovers", response_model=list[HandoverOut])
def list_handovers(
    response: Response,
    ward_id: int | None = None,
    handover_date: str | None = None,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    query = db.query(ShiftHandover)
    if ward_id is not None:
        query = query.filter(ShiftHandover.ward_id == ward_id)
    if handover_date:
        query = query.filter(ShiftHandover.handover_date == handover_date)
    return [
        {
            "id": h.id,
            "ward_id": h.ward_id,
            "shift": h.shift,
            "handover_date": h.handover_date,
            "from_staff": h.from_staff,
            "to_staff": h.to_staff,
            "patient_count": h.patient_count,
            "critical_count": h.critical_count,
            "content": h.content,
        }
        for h in paginate(query.order_by(ShiftHandover.id.desc()), response, offset, limit)
    ]
