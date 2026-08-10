"""㉔妇幼保健业务协同：孕产妇建册/高危管理/产检/产后访视，儿童保健档案与访视。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import ChildRecord, ChildVisit, MaternalRecord, MaternalVisit, Patient

router = APIRouter(prefix="/api/maternal", tags=["妇幼保健"], dependencies=[Depends(get_current_user)])


class MaternalCreate(BaseModel):
    patient_id: int
    lmp: str = ""
    edc: str = ""
    gravidity: int = Field(default=1, ge=1)
    parity: int = Field(default=0, ge=0)
    high_risk: bool = False
    risk_factors: str = ""


class MaternalOut(MaternalCreate):
    id: int
    status: str

    model_config = {"from_attributes": True}


@router.post(
    "/records",
    response_model=MaternalOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # H2/L5: 妇幼建档
)
def register(body: MaternalCreate, db: Session = Depends(get_db)):
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    existing = db.query(MaternalRecord).filter(MaternalRecord.patient_id == body.patient_id).first()
    if existing:
        return existing
    record = MaternalRecord(**body.model_dump())
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.get("/records", response_model=list[MaternalOut])
def list_records(high_risk: bool | None = None, db: Session = Depends(get_db)):
    query = db.query(MaternalRecord)
    if high_risk is not None:
        query = query.filter(MaternalRecord.high_risk.is_(high_risk))
    return query.order_by(MaternalRecord.high_risk.desc(), MaternalRecord.id.desc()).limit(200).all()


class VisitCreate(BaseModel):
    visit_type: str = Field(pattern="^(prenatal|postpartum)$")
    gest_week: int | None = Field(default=None, ge=4, le=45)
    bp: str = ""
    note: str = ""
    visit_date: str = ""


@router.post(
    "/records/{record_id}/visits",
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # H2/L5: 产检随访
)
def add_visit(record_id: int, body: VisitCreate, db: Session = Depends(get_db)):
    record = db.get(MaternalRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="孕产妇档案不存在")
    if body.visit_type == "postpartum" and record.status == "registered":
        record.status = "delivered"
    visit = MaternalVisit(record_id=record_id, **body.model_dump())
    # 产检时收缩压≥140 自动标记高危
    if body.bp:
        try:
            sbp = float(body.bp.split("/")[0])
            if sbp >= 140 and not record.high_risk:
                record.high_risk = True
                record.risk_factors = (record.risk_factors + "；" if record.risk_factors else "") + "妊娠期高血压可能"
        except ValueError:
            pass
    db.add(visit)
    db.commit()
    return {"id": visit.id, "record_id": record_id, "high_risk": record.high_risk, "status": record.status}


@router.post(
    "/records/{record_id}/close",
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # H2/L5
)
def close_record(record_id: int, db: Session = Depends(get_db)):
    record = db.get(MaternalRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="孕产妇档案不存在")
    if record.status != "delivered":
        raise HTTPException(status_code=409, detail="须完成产后访视（分娩后）方可结案")
    record.status = "closed"
    db.commit()
    return {"id": record_id, "status": "closed"}


class ChildCreate(BaseModel):
    name: str = Field(min_length=1)
    gender: str = "未知"
    birth_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    guardian_patient_id: int | None = None


class ChildOut(ChildCreate):
    id: int

    model_config = {"from_attributes": True}


@router.post(
    "/children",
    response_model=ChildOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # H2/L5: 儿童建档
)
def register_child(body: ChildCreate, db: Session = Depends(get_db)):
    child = ChildRecord(**body.model_dump())
    db.add(child)
    db.commit()
    db.refresh(child)
    return child


@router.get("/children", response_model=list[ChildOut])
def list_children(db: Session = Depends(get_db)):
    return db.query(ChildRecord).order_by(ChildRecord.id.desc()).limit(200).all()


class ChildVisitCreate(BaseModel):
    visit_type: str = Field(pattern="^(newborn|checkup)$")
    height_cm: float | None = None
    weight_kg: float | None = None
    note: str = ""
    visit_date: str = ""


@router.post(
    "/children/{child_id}/visits",
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # H2/L5: 儿童随访
)
def add_child_visit(child_id: int, body: ChildVisitCreate, db: Session = Depends(get_db)):
    if db.get(ChildRecord, child_id) is None:
        raise HTTPException(status_code=404, detail="儿童档案不存在")
    visit = ChildVisit(child_id=child_id, **body.model_dump())
    db.add(visit)
    db.commit()
    return {"id": visit.id, "child_id": child_id}
