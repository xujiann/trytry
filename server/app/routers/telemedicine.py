"""⑨互联网+诊疗：在线咨询、复诊续方（医师回复，续方联动集中审方）。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import OnlineConsult, Organization, Patient, Prescription
from ..schemas import PrescriptionOut  # noqa: F401

router = APIRouter(prefix="/api/telemedicine", tags=["互联网+诊疗"], dependencies=[Depends(get_current_user)])


class ConsultCreate(BaseModel):
    patient_id: int
    org_id: int
    consult_type: str = Field(default="consult", pattern="^(consult|repeat_rx)$")
    question: str = Field(min_length=1, max_length=1024)


class ConsultOut(ConsultCreate):
    id: int
    reply: str
    doctor_name: str
    status: str
    prescription_id: int | None

    model_config = {"from_attributes": True}


class ReplyBody(BaseModel):
    reply: str = Field(min_length=1)
    doctor_name: str = Field(min_length=1)
    # 复诊续方：医师回复时可关联已开具处方（须先经集中审方）
    prescription_id: int | None = None


@router.post("/consults", response_model=ConsultOut, status_code=201)
def create_consult(body: ConsultCreate, db: Session = Depends(get_db)):
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    consult = OnlineConsult(**body.model_dump())
    db.add(consult)
    db.commit()
    db.refresh(consult)
    return consult


@router.get("/consults", response_model=list[ConsultOut])
def list_consults(status: str | None = None, db: Session = Depends(get_db)):
    query = db.query(OnlineConsult)
    if status:
        query = query.filter(OnlineConsult.status == status)
    return query.order_by(OnlineConsult.id.desc()).limit(200).all()


@router.post("/consults/{consult_id}/reply", response_model=ConsultOut, dependencies=[Depends(require_roles("doctor"))])
def reply(consult_id: int, body: ReplyBody, db: Session = Depends(get_db)):
    consult = db.get(OnlineConsult, consult_id)
    if consult is None:
        raise HTTPException(status_code=404, detail="咨询不存在")
    if consult.status != "open":
        raise HTTPException(status_code=409, detail=f"当前状态 {consult.status} 不可回复")
    if body.prescription_id is not None:
        prescription = db.get(Prescription, body.prescription_id)
        if prescription is None:
            raise HTTPException(status_code=404, detail="关联处方不存在")
        if prescription.patient_id != consult.patient_id:
            raise HTTPException(status_code=422, detail="处方与咨询患者不一致")
        if prescription.status not in ("auto_passed", "approved"):
            raise HTTPException(status_code=409, detail="处方未通过审方，不可用于续方")
        consult.prescription_id = body.prescription_id
    consult.reply = body.reply
    consult.doctor_name = body.doctor_name
    consult.status = "replied"
    db.commit()
    db.refresh(consult)
    return consult


@router.post("/consults/{consult_id}/close", response_model=ConsultOut)
def close(consult_id: int, db: Session = Depends(get_db)):
    consult = db.get(OnlineConsult, consult_id)
    if consult is None:
        raise HTTPException(status_code=404, detail="咨询不存在")
    if consult.status != "replied":
        raise HTTPException(status_code=409, detail=f"当前状态 {consult.status} 不可结束")
    consult.status = "closed"
    db.commit()
    db.refresh(consult)
    return consult
