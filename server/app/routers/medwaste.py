"""医疗废弃物：收集、暂存、交接全过程监管，滞留预警。"""
from datetime import date, timedelta, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import MedicalWaste, Organization
from ..schemas import WasteCreate, WasteHandover, WasteOut

router = APIRouter(prefix="/api/medwaste", tags=["医废追溯"], dependencies=[Depends(get_current_user)])

# 收集后超过该天数未交接即预警（《医疗废物管理条例》暂存不得超过2天）
STORAGE_LIMIT_DAYS = 2


@router.post("", response_model=WasteOut, status_code=201)
def collect(body: WasteCreate, db: Session = Depends(get_db)):
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    waste = MedicalWaste(**body.model_dump())
    db.add(waste)
    db.commit()
    db.refresh(waste)
    return waste


@router.get("", response_model=list[WasteOut])
def list_wastes(org_id: int | None = None, status: str | None = None, db: Session = Depends(get_db)):
    query = db.query(MedicalWaste)
    if org_id is not None:
        query = query.filter(MedicalWaste.org_id == org_id)
    if status:
        query = query.filter(MedicalWaste.status == status)
    return query.order_by(MedicalWaste.id.desc()).limit(500).all()


@router.post("/{waste_id}/handover", response_model=WasteOut)
def handover(waste_id: int, body: WasteHandover, db: Session = Depends(get_db)):
    waste = db.get(MedicalWaste, waste_id)
    if waste is None:
        raise HTTPException(status_code=404, detail="医废记录不存在")
    if waste.status == "handed_over":
        raise HTTPException(status_code=409, detail="该批医废已交接")
    waste.status = "handed_over"
    waste.handler_name = body.handler_name
    waste.handed_over_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    db.refresh(waste)
    return waste


@router.get("/alerts", response_model=list[WasteOut])
def overdue_alerts(today: str | None = None, db: Session = Depends(get_db)):
    """滞留预警：收集超过 2 天仍未交接的医废。"""
    end = date.fromisoformat(today) if today else date.today()
    cutoff = (end - timedelta(days=STORAGE_LIMIT_DAYS)).isoformat()
    return (
        db.query(MedicalWaste)
        .filter(MedicalWaste.status != "handed_over", MedicalWaste.collected_date <= cutoff)
        .order_by(MedicalWaste.collected_date)
        .all()
    )
