"""⑦县域智慧医疗急救：呼救调度→转运（生命体征实时回传）→到院→收治，"上车即入院"。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import EmergencyCase, EmergencyVital, Organization
from ..schemas import PatientOut  # noqa: F401  (保持 schemas 导入路径一致性)

router = APIRouter(prefix="/api/emergency", tags=["智慧急救"], dependencies=[Depends(get_current_user)])

_FLOW = {"dispatched": "en_route", "en_route": "arrived", "arrived": "admitted"}


class CaseCreate(BaseModel):
    caller_phone: str = ""
    location: str = Field(min_length=1)
    symptom: str = ""
    ambulance_no: str = ""
    dest_org_id: int | None = None
    patient_id: int | None = None


class CaseOut(CaseCreate):
    id: int
    status: str

    model_config = {"from_attributes": True}


class VitalCreate(BaseModel):
    heart_rate: float | None = None
    sbp: float | None = None
    dbp: float | None = None
    spo2: float | None = None
    note: str = ""


class VitalOut(VitalCreate):
    id: int
    case_id: int

    model_config = {"from_attributes": True}


@router.post("/cases", response_model=CaseOut, status_code=201)
def dispatch(body: CaseCreate, db: Session = Depends(get_db)):
    if body.dest_org_id is not None and db.get(Organization, body.dest_org_id) is None:
        raise HTTPException(status_code=404, detail="目标医院不存在")
    case = EmergencyCase(**body.model_dump())
    db.add(case)
    db.commit()
    db.refresh(case)
    return case


@router.get("/cases", response_model=list[CaseOut])
def list_cases(status: str | None = None, db: Session = Depends(get_db)):
    query = db.query(EmergencyCase)
    if status:
        query = query.filter(EmergencyCase.status == status)
    return query.order_by(EmergencyCase.id.desc()).limit(200).all()


@router.post("/cases/{case_id}/advance", response_model=CaseOut)
def advance(case_id: int, db: Session = Depends(get_db)):
    case = db.get(EmergencyCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="急救事件不存在")
    next_status = _FLOW.get(case.status)
    if next_status is None:
        raise HTTPException(status_code=409, detail=f"状态 {case.status} 已是终态")
    case.status = next_status
    db.commit()
    db.refresh(case)
    return case


@router.post("/cases/{case_id}/vitals", response_model=VitalOut, status_code=201)
def report_vitals(case_id: int, body: VitalCreate, db: Session = Depends(get_db)):
    """车载终端回传生命体征——院内可实时调阅，实现院前院内无缝对接。"""
    case = db.get(EmergencyCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="急救事件不存在")
    if case.status == "admitted":
        raise HTTPException(status_code=409, detail="已收治，转由院内记录")
    vital = EmergencyVital(case_id=case_id, **body.model_dump())
    db.add(vital)
    db.commit()
    db.refresh(vital)
    return vital


@router.get("/cases/{case_id}/vitals", response_model=list[VitalOut])
def list_vitals(case_id: int, db: Session = Depends(get_db)):
    if db.get(EmergencyCase, case_id) is None:
        raise HTTPException(status_code=404, detail="急救事件不存在")
    return db.query(EmergencyVital).filter(EmergencyVital.case_id == case_id).order_by(EmergencyVital.id).all()
