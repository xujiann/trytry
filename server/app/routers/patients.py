"""患者主索引（EMPI）：以身份证号去重，生成全县唯一电子健康卡号。"""
import secrets

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import Patient
from ..schemas import PatientCreate, PatientOut

router = APIRouter(
    prefix="/api/patients", tags=["患者主索引"], dependencies=[Depends(get_current_user)]
)


def _generate_ehc_no(db: Session) -> str:
    while True:
        candidate = "EHC" + secrets.token_hex(6).upper()
        if db.query(Patient).filter(Patient.ehc_no == candidate).first() is None:
            return candidate


@router.post("", response_model=PatientOut, status_code=201)
def register_patient(body: PatientCreate, db: Session = Depends(get_db)):
    existing = db.query(Patient).filter(Patient.id_card == body.id_card).first()
    if existing:
        # 主索引幂等：同一身份证号返回既有档案，不重复建档
        return existing
    patient = Patient(ehc_no=_generate_ehc_no(db), **body.model_dump())
    db.add(patient)
    db.commit()
    db.refresh(patient)
    return patient


@router.get("", response_model=list[PatientOut])
def search_patients(keyword: str = "", db: Session = Depends(get_db)):
    query = db.query(Patient)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            (Patient.name.like(like)) | (Patient.id_card.like(like)) | (Patient.ehc_no.like(like))
        )
    return query.order_by(Patient.id).limit(100).all()


@router.get("/{ehc_no}", response_model=PatientOut)
def get_patient(ehc_no: str, db: Session = Depends(get_db)):
    patient = db.query(Patient).filter(Patient.ehc_no == ehc_no).first()
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    return patient
