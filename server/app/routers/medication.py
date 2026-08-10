"""⑮基层缺药登记 + ⑯居民用药监测。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import DrugShortage, Organization, Patient, Prescription, PrescriptionItem

router = APIRouter(prefix="/api/medication", tags=["药事监测"], dependencies=[Depends(get_current_user)])

# 同时在用药品达到该数即提示多重用药风险
POLYPHARMACY_THRESHOLD = 5

_SHORTAGE_FLOW = {"registered": "purchasing", "purchasing": "delivered"}


class ShortageCreate(BaseModel):
    org_id: int
    drug_code: str = Field(min_length=1)
    drug_name: str = Field(min_length=1)
    quantity: int = Field(default=1, ge=1)


class ShortageOut(ShortageCreate):
    id: int
    status: str

    model_config = {"from_attributes": True}


@router.post(
    "/shortages",
    response_model=ShortageOut,
    status_code=201,
    dependencies=[Depends(require_roles("operator", "pharmacist"))],  # H2: 短缺登记
)
def register_shortage(body: ShortageCreate, db: Session = Depends(get_db)):
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="登记机构不存在")
    shortage = DrugShortage(**body.model_dump())
    db.add(shortage)
    db.commit()
    db.refresh(shortage)
    return shortage


@router.get("/shortages", response_model=list[ShortageOut])
def list_shortages(status: str | None = None, db: Session = Depends(get_db)):
    query = db.query(DrugShortage)
    if status:
        query = query.filter(DrugShortage.status == status)
    return query.order_by(DrugShortage.id.desc()).limit(200).all()


@router.post(
    "/shortages/{shortage_id}/advance",
    response_model=ShortageOut,
    dependencies=[Depends(require_roles("operator", "pharmacist"))],  # H2: 短缺流转
)
def advance_shortage(shortage_id: int, db: Session = Depends(get_db)):
    shortage = db.get(DrugShortage, shortage_id)
    if shortage is None:
        raise HTTPException(status_code=404, detail="缺药登记不存在")
    next_status = _SHORTAGE_FLOW.get(shortage.status)
    if next_status is None:
        raise HTTPException(status_code=409, detail=f"状态 {shortage.status} 已是终态")
    shortage.status = next_status
    db.commit()
    db.refresh(shortage)
    return shortage


@router.get("/profile/{patient_id}")
def medication_profile(patient_id: int, db: Session = Depends(get_db)):
    """居民用药画像：在用药品清单（通过审方的处方）+ 多重用药预警。"""
    if db.get(Patient, patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    rows = (
        db.query(PrescriptionItem, Prescription.status)
        .join(Prescription, PrescriptionItem.prescription_id == Prescription.id)
        .filter(
            Prescription.patient_id == patient_id,
            Prescription.status.in_(["auto_passed", "approved"]),
        )
        .all()
    )
    drugs: dict[str, dict] = {}
    for item, _status in rows:
        entry = drugs.setdefault(
            item.drug_code, {"drug_code": item.drug_code, "drug_name": item.drug_name, "times": 0, "max_daily_dose": 0.0}
        )
        entry["times"] += 1
        entry["max_daily_dose"] = max(entry["max_daily_dose"], item.daily_dose)
    drug_list = sorted(drugs.values(), key=lambda d: d["times"], reverse=True)
    return {
        "patient_id": patient_id,
        "distinct_drugs": len(drug_list),
        "polypharmacy_warning": len(drug_list) >= POLYPHARMACY_THRESHOLD,
        "drugs": drug_list,
    }


@router.get("/usage-stats")
def usage_stats(db: Session = Depends(get_db)):
    """全县用药地图：品种使用排名，支撑药品需求预测与供应保障。"""
    rows = (
        db.query(
            PrescriptionItem.drug_code,
            PrescriptionItem.drug_name,
            func.count(PrescriptionItem.id).label("rx_count"),
            func.count(func.distinct(Prescription.patient_id)).label("patient_count"),
        )
        .join(Prescription, PrescriptionItem.prescription_id == Prescription.id)
        .filter(Prescription.status.in_(["auto_passed", "approved"]))
        .group_by(PrescriptionItem.drug_code, PrescriptionItem.drug_name)
        .order_by(func.count(PrescriptionItem.id).desc())
        .limit(50)
        .all()
    )
    return [
        {"drug_code": r.drug_code, "drug_name": r.drug_name, "rx_count": r.rx_count, "patient_count": r.patient_count}
        for r in rows
    ]
