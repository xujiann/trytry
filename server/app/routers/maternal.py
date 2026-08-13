"""㉔妇幼保健业务协同：孕产妇建册/高危管理/产检/产后访视/分娩记录，
儿童保健档案与访视、新生儿疾病筛查、高危儿管理，婚前/孕前/妇女保健与避孕节育记录。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..datetypes import DateStr
from ..concurrency import insert_if_absent
from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import (
    ChildRecord,
    ChildVisit,
    DeliveryRecord,
    MaternalRecord,
    MaternalVisit,
    NewbornScreening,
    Organization,
    Patient,
    WomenHealthRecord,
)

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
    # 建册幂等：并发下两个请求都查不到就都去插，撞 patient_id 唯一约束时
    # 返回既有那本，不是 500——一个孕产妇两本册子，产检记录会分叉。
    record = MaternalRecord(**body.model_dump())
    if not insert_if_absent(db, record):
        # 必须先结束事务再返回：SAVEPOINT 回滚只退掉这一行，外层写事务还开着，
        # 一路握着 SQLite 的写锁走完请求，审计中间件那一笔就写不进去
        # （实测 database is locked）。
        db.rollback()
        return (
            db.query(MaternalRecord)
            .filter(MaternalRecord.patient_id == body.patient_id)
            .first()
        )
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
    birth_date: DateStr
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


# ---------- 终审轮：分娩服务记录（㉔分娩服务） ----------


class DeliveryCreate(BaseModel):
    org_id: int
    delivery_date: DateStr
    delivery_mode: str = Field(default="natural", pattern="^(natural|cesarean)$")
    newborn_count: int = Field(default=1, ge=1, le=5)
    outcome: str = ""


@router.post(
    "/records/{record_id}/delivery",
    status_code=201,
    dependencies=[Depends(require_roles("doctor"))],  # 分娩记录=医师
)
def add_delivery(record_id: int, body: DeliveryCreate, db: Session = Depends(get_db)):
    record = db.get(MaternalRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="孕产妇档案不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    if record.status == "closed":
        raise HTTPException(status_code=409, detail="档案已结案，不可登记分娩")
    if db.query(DeliveryRecord).filter(DeliveryRecord.record_id == record_id).first():
        raise HTTPException(status_code=409, detail="该档案已有分娩记录")
    delivery = DeliveryRecord(record_id=record_id, **body.model_dump())
    record.status = "delivered"
    db.add(delivery)
    db.commit()
    return {
        "id": delivery.id,
        "record_id": record_id,
        "delivery_mode": delivery.delivery_mode,
        "status": record.status,
    }


@router.get("/records/{record_id}/delivery")
def get_delivery(record_id: int, db: Session = Depends(get_db)):
    delivery = db.query(DeliveryRecord).filter(DeliveryRecord.record_id == record_id).first()
    if delivery is None:
        raise HTTPException(status_code=404, detail="暂无分娩记录")
    return {
        "id": delivery.id,
        "record_id": delivery.record_id,
        "org_id": delivery.org_id,
        "delivery_date": delivery.delivery_date,
        "delivery_mode": delivery.delivery_mode,
        "newborn_count": delivery.newborn_count,
        "outcome": delivery.outcome,
    }


# ---------- 终审轮：新生儿疾病筛查（异常自动纳入高危儿） ----------


class ScreeningCreate(BaseModel):
    item: str = Field(pattern="^(metabolic|hearing|chd)$")
    result: str = Field(default="normal", pattern="^(normal|abnormal)$")
    screen_date: str = ""
    note: str = ""


_SCREEN_ITEM_NAMES = {"metabolic": "遗传代谢病筛查", "hearing": "听力筛查", "chd": "先心病筛查"}


@router.post(
    "/children/{child_id}/screenings",
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # 新筛
)
def add_screening(child_id: int, body: ScreeningCreate, db: Session = Depends(get_db)):
    child = db.get(ChildRecord, child_id)
    if child is None:
        raise HTTPException(status_code=404, detail="儿童档案不存在")
    screening = NewbornScreening(child_id=child_id, **body.model_dump())
    if body.result == "abnormal" and not child.high_risk:
        child.high_risk = True
        note = f"{_SCREEN_ITEM_NAMES[body.item]}阳性/可疑"
        child.risk_note = (child.risk_note + "；" if child.risk_note else "") + note
    db.add(screening)
    db.commit()
    return {
        "id": screening.id,
        "child_id": child_id,
        "item": screening.item,
        "result": screening.result,
        "child_high_risk": child.high_risk,
    }


@router.get("/children/{child_id}/screenings")
def list_screenings(child_id: int, db: Session = Depends(get_db)):
    if db.get(ChildRecord, child_id) is None:
        raise HTTPException(status_code=404, detail="儿童档案不存在")
    return [
        {
            "id": s.id,
            "item": s.item,
            "result": s.result,
            "screen_date": s.screen_date,
            "note": s.note,
        }
        for s in db.query(NewbornScreening)
        .filter(NewbornScreening.child_id == child_id)
        .order_by(NewbornScreening.id)
        .all()
    ]


# ---------- 终审轮：高危儿管理 ----------


class HighRiskUpdate(BaseModel):
    high_risk: bool
    risk_note: str = ""


@router.post(
    "/children/{child_id}/high-risk",
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # 高危儿标记/解除
)
def set_high_risk(child_id: int, body: HighRiskUpdate, db: Session = Depends(get_db)):
    child = db.get(ChildRecord, child_id)
    if child is None:
        raise HTTPException(status_code=404, detail="儿童档案不存在")
    child.high_risk = body.high_risk
    child.risk_note = body.risk_note if body.high_risk else ""
    db.commit()
    return {"id": child_id, "high_risk": child.high_risk, "risk_note": child.risk_note}


@router.get("/children/high-risk")
def list_high_risk_children(db: Session = Depends(get_db)):
    """高危儿清单：新筛异常自动纳入 + 人工标记，供专案随访。"""
    return [
        {
            "id": c.id,
            "name": c.name,
            "birth_date": c.birth_date,
            "risk_note": c.risk_note,
        }
        for c in db.query(ChildRecord)
        .filter(ChildRecord.high_risk.is_(True))
        .order_by(ChildRecord.id.desc())
        .limit(200)
        .all()
    ]


# ---------- 终审轮：婚前/孕前/妇女保健、避孕节育 ----------


class WomenHealthCreate(BaseModel):
    patient_id: int
    record_type: str = Field(pattern="^(premarital|preconception|gynecology|contraception)$")
    exam_date: str = ""
    result: str = ""
    advice: str = ""


class WomenHealthOut(WomenHealthCreate):
    id: int

    model_config = {"from_attributes": True}


@router.post(
    "/women-health",
    response_model=WomenHealthOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # 妇女保健服务
)
def add_women_health(body: WomenHealthCreate, db: Session = Depends(get_db)):
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    record = WomenHealthRecord(**body.model_dump())
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.get("/women-health", response_model=list[WomenHealthOut])
def list_women_health(
    patient_id: int | None = None, record_type: str | None = None, db: Session = Depends(get_db)
):
    query = db.query(WomenHealthRecord)
    if patient_id is not None:
        query = query.filter(WomenHealthRecord.patient_id == patient_id)
    if record_type:
        query = query.filter(WomenHealthRecord.record_type == record_type)
    return query.order_by(WomenHealthRecord.id.desc()).limit(200).all()
