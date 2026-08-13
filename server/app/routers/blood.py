"""血液管理服务（浙#71）：血液库存台账、临床用血申请→审批→发血（血站系统为对接项）。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..concurrency import add_amount, insert_if_absent, take_amount
from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import BloodStock, Organization, Patient, TransfusionRequest, User

router = APIRouter(prefix="/api/blood", tags=["血液管理"], dependencies=[Depends(get_current_user)])

_BLOOD_TYPE = "^(A|B|AB|O)$"
_COMPONENT = "^(rbc|plasma|platelet)$"


class BloodStockUpsert(BaseModel):
    blood_type: str = Field(pattern=_BLOOD_TYPE)
    component: str = Field(pattern=_COMPONENT)
    quantity_ml: int = Field(gt=0)


@router.post(
    "/stocks",
    dependencies=[Depends(require_roles("operator"))],  # 血库登记=经办
)
def upsert_blood_stock(body: BloodStockUpsert, db: Session = Depends(get_db)):
    stock = (
        db.query(BloodStock)
        .filter(BloodStock.blood_type == body.blood_type, BloodStock.component == body.component)
        .first()
    )
    if stock is None:
        # 累加语义：先保证行存在（谁插上都行），再统一累加。
        # 直接 db.add 则两次入库并发时都建行、撞 (blood_type, component) 唯一约束。
        insert_if_absent(db, BloodStock(**{**body.model_dump(), "quantity_ml": 0}))
        stock = (
            db.query(BloodStock)
            .filter(BloodStock.blood_type == body.blood_type, BloodStock.component == body.component)
            .first()
        )
    add_amount(db, BloodStock, stock.id, "quantity_ml", body.quantity_ml)
    db.commit()
    db.refresh(stock)
    return {"blood_type": stock.blood_type, "component": stock.component, "quantity_ml": stock.quantity_ml}


@router.get("/stocks")
def list_blood_stocks(db: Session = Depends(get_db)):
    return [
        {"blood_type": s.blood_type, "component": s.component, "quantity_ml": s.quantity_ml}
        for s in db.query(BloodStock).order_by(BloodStock.blood_type, BloodStock.component).all()
    ]


class TransfusionCreate(BaseModel):
    patient_id: int
    org_id: int
    blood_type: str = Field(pattern=_BLOOD_TYPE)
    component: str = Field(pattern=_COMPONENT)
    quantity_ml: int = Field(gt=0)
    reason: str = ""


@router.post(
    "/requests",
    status_code=201,
    dependencies=[Depends(require_roles("doctor"))],  # 用血申请=医师
)
def create_transfusion_request(
    body: TransfusionCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    request = TransfusionRequest(requested_by=user.id, **body.model_dump())
    db.add(request)
    db.commit()
    return {"id": request.id, "status": request.status}


@router.post(
    "/requests/{request_id}/review",
    dependencies=[Depends(require_roles("director"))],  # 用血审批=管理层（申请/审批分离）
)
def review_transfusion(
    request_id: int,
    approve: bool,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    request = db.get(TransfusionRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="用血申请不存在")
    if request.status != "pending":
        raise HTTPException(status_code=409, detail="该申请已处理")
    request.status = "approved" if approve else "rejected"
    request.approved_by = user.id
    db.commit()
    return {"id": request.id, "status": request.status}


@router.post(
    "/requests/{request_id}/issue",
    dependencies=[Depends(require_roles("operator"))],  # 发血=血库经办
)
def issue_blood(request_id: int, db: Session = Depends(get_db)):
    request = db.get(TransfusionRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="用血申请不存在")
    if request.status != "approved":
        raise HTTPException(status_code=409, detail="仅已审批申请可发血")
    stock = (
        db.query(BloodStock)
        .filter(
            BloodStock.blood_type == request.blood_type,
            BloodStock.component == request.component,
        )
        .first()
    )
    # 判定与扣减同一条 SQL。原先并发两笔发血都判定够，实际库存只扣一笔——
    # 血是按毫升对账的东西，发出去的比账上扣的多，血库盘点必然对不上。
    if stock is None or not take_amount(db, BloodStock, stock.id, "quantity_ml", request.quantity_ml):
        db.rollback()
        raise HTTPException(status_code=409, detail="血液库存不足，请向血站调剂（对接项）")
    request.status = "issued"
    db.commit()
    db.refresh(stock)
    return {"id": request.id, "status": "issued", "stock_remaining_ml": stock.quantity_ml}


@router.get("/requests")
def list_transfusion_requests(status: str | None = None, db: Session = Depends(get_db)):
    q = db.query(TransfusionRequest)
    if status:
        q = q.filter(TransfusionRequest.status == status)
    return [
        {
            "id": r.id,
            "patient_id": r.patient_id,
            "org_id": r.org_id,
            "blood_type": r.blood_type,
            "component": r.component,
            "quantity_ml": r.quantity_ml,
            "status": r.status,
        }
        for r in q.order_by(TransfusionRequest.id.desc()).limit(200).all()
    ]
