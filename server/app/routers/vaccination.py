"""㉕疫苗接种业务协同：接种记录、禁忌管理、接种前综合评估。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import Organization, Patient, VaccinationRecord, VaccineContraindication

router = APIRouter(prefix="/api/vaccination", tags=["疫苗接种"], dependencies=[Depends(get_current_user)])


class RecordCreate(BaseModel):
    patient_id: int
    vaccine_code: str = Field(min_length=1)
    vaccine_name: str = Field(min_length=1)
    dose_no: int = Field(default=1, ge=1)
    vaccinated_date: str = ""
    org_id: int


class RecordOut(RecordCreate):
    id: int

    model_config = {"from_attributes": True}


@router.post(
    "/records",
    response_model=RecordOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # H2/L5: 疫苗接种登记
)
def vaccinate(body: RecordCreate, db: Session = Depends(get_db)):
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="受种者不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="接种机构不存在")
    # 接种禁忌硬拦截
    forbidden = (
        db.query(VaccineContraindication)
        .filter(
            VaccineContraindication.patient_id == body.patient_id,
            VaccineContraindication.vaccine_code == body.vaccine_code,
        )
        .first()
    )
    if forbidden:
        raise HTTPException(status_code=409, detail=f"存在接种禁忌：{forbidden.reason}")
    record = VaccinationRecord(**body.model_dump())
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.get("/records", response_model=list[RecordOut])
def vaccination_history(patient_id: int, db: Session = Depends(get_db)):
    """接种史查询：临床诊疗与接种场景共享调阅。"""
    return (
        db.query(VaccinationRecord)
        .filter(VaccinationRecord.patient_id == patient_id)
        .order_by(VaccinationRecord.id.desc())
        .all()
    )


class ContraCreate(BaseModel):
    patient_id: int
    vaccine_code: str = Field(min_length=1)
    reason: str = Field(min_length=1)


@router.post(
    "/contraindications",
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # H2/L5: 禁忌登记
)
def add_contraindication(body: ContraCreate, db: Session = Depends(get_db)):
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    contra = VaccineContraindication(**body.model_dump())
    db.add(contra)
    db.commit()
    return {"id": contra.id}


@router.get("/pre-check")
def pre_vaccination_check(patient_id: int, vaccine_code: str, db: Session = Depends(get_db)):
    """接种前综合评估：禁忌、既往剂次、近期就诊信息一屏返回。"""
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="受种者不存在")
    contras = (
        db.query(VaccineContraindication)
        .filter(
            VaccineContraindication.patient_id == patient_id,
            VaccineContraindication.vaccine_code == vaccine_code,
        )
        .all()
    )
    doses = (
        db.query(VaccinationRecord)
        .filter(VaccinationRecord.patient_id == patient_id, VaccinationRecord.vaccine_code == vaccine_code)
        .count()
    )
    return {
        "allowed": not contras,
        "contraindications": [c.reason for c in contras],
        "previous_doses": doses,
        "next_dose_no": doses + 1,
    }
