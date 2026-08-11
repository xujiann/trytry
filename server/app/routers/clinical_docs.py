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
    NursingRecord,
    ProgressNote,
    ShiftHandover,
    User,
    VitalSignRecord,
    Ward,
)

router = APIRouter(prefix="/api/inpatient", tags=["住院临床文书"], dependencies=[Depends(get_current_user)])

NOTE_TYPES = ("first", "daily", "ward_round", "rescue", "consultation", "discharge")


def _admission_or_404(db: Session, admission_id: int) -> Admission:
    admission = db.get(Admission, admission_id)
    if admission is None:
        raise HTTPException(status_code=404, detail="住院记录不存在")
    return admission


# ---------------------------------------------------------------- 病程记录


class ProgressNoteIn(BaseModel):
    note_type: str = Field(pattern="^(first|daily|ward_round|rescue|consultation|discharge)$")
    content: str = Field(min_length=1, max_length=4096)
    doctor_name: str = ""
    recorded_at: str = ""


@router.post(
    "/admissions/{admission_id}/progress-notes",
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
    admission = _admission_or_404(db, admission_id)
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


@router.get("/admissions/{admission_id}/progress-notes")
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


@router.get("/admissions/{admission_id}/document-completeness")
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


@router.post(
    "/admissions/{admission_id}/nursing-records",
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "operator"))],  # 护士在本平台映射为 operator
)
def create_nursing_record(
    admission_id: int,
    body: NursingIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    admission = _admission_or_404(db, admission_id)
    if admission.status != "admitted":
        raise HTTPException(status_code=409, detail="患者已出院，不可再书写护理记录")
    record = NursingRecord(
        admission_id=admission_id,
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
        "nursing_level": record.nursing_level,
        "content": record.content,
        "nurse_name": record.nurse_name,
        "recorded_at": record.recorded_at or record.created_at.strftime("%Y-%m-%d %H:%M"),
    }


@router.get("/admissions/{admission_id}/nursing-records")
def list_nursing_records(
    admission_id: int, response: Response, offset: int = 0, limit: int = 100,
    db: Session = Depends(get_db),
):
    _admission_or_404(db, admission_id)
    query = db.query(NursingRecord).filter(NursingRecord.admission_id == admission_id)
    return [
        {
            "id": r.id,
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
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "operator"))],
)
def create_vital(
    admission_id: int,
    body: VitalIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    admission = _admission_or_404(db, admission_id)
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


@router.get("/admissions/{admission_id}/vitals")
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
    "/handovers", status_code=201, dependencies=[Depends(require_roles("doctor", "operator"))]
)
def create_handover(
    body: HandoverIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """交接班：在院人数由系统按当前住院数据快照，不让人工填——这个数填错就没意义了。"""
    if db.get(Ward, body.ward_id) is None:
        raise HTTPException(status_code=404, detail="病区不存在")
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


@router.get("/handovers")
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
