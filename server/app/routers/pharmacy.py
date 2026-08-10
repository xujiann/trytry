"""中心药房：库存管理、县乡村余缺调拨、缺药预警。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_admin
from ..models import DrugStock, Organization, StockTransfer, User
from ..schemas import StockOut, StockUpsert, TransferCreate
from ..ws import manager

router = APIRouter(prefix="/api/pharmacy", tags=["中心药房"])


@router.post("/stocks", response_model=StockOut, dependencies=[Depends(require_admin)])
def upsert_stock(body: StockUpsert, db: Session = Depends(get_db)):
    """入库/建档：已有库存记录则累加数量并更新阈值。"""
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    stock = (
        db.query(DrugStock)
        .filter(DrugStock.org_id == body.org_id, DrugStock.drug_code == body.drug_code)
        .first()
    )
    if stock is None:
        stock = DrugStock(**body.model_dump())
        db.add(stock)
    else:
        stock.quantity += body.quantity
        stock.threshold = body.threshold
        stock.drug_name = body.drug_name
    db.commit()
    db.refresh(stock)
    return stock


@router.get("/stocks", response_model=list[StockOut], dependencies=[Depends(get_current_user)])
def list_stocks(org_id: int | None = None, db: Session = Depends(get_db)):
    query = db.query(DrugStock)
    if org_id is not None:
        query = query.filter(DrugStock.org_id == org_id)
    return query.order_by(DrugStock.org_id, DrugStock.drug_code).all()


@router.post("/transfers", response_model=StockOut, status_code=201)
def transfer_stock(
    body: TransferCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """余缺调拨：从调出机构扣减、调入机构增加，全程留痕。"""
    if body.from_org_id == body.to_org_id:
        raise HTTPException(status_code=422, detail="调出与调入机构不能相同")
    source = (
        db.query(DrugStock)
        .filter(DrugStock.org_id == body.from_org_id, DrugStock.drug_code == body.drug_code)
        .first()
    )
    if source is None or source.quantity < body.quantity:
        raise HTTPException(status_code=409, detail="调出机构库存不足")
    if db.get(Organization, body.to_org_id) is None:
        raise HTTPException(status_code=404, detail="调入机构不存在")

    dest = (
        db.query(DrugStock)
        .filter(DrugStock.org_id == body.to_org_id, DrugStock.drug_code == body.drug_code)
        .first()
    )
    if dest is None:
        dest = DrugStock(
            org_id=body.to_org_id,
            drug_code=body.drug_code,
            drug_name=source.drug_name,
            quantity=0,
            threshold=0,
        )
        db.add(dest)
    source.quantity -= body.quantity
    dest.quantity += body.quantity
    db.add(StockTransfer(**body.model_dump(), created_by=user.id))
    db.commit()
    db.refresh(dest)
    if source.quantity < source.threshold:
        # 调拨后调出机构跌破阈值：实时广播缺药预警
        manager.broadcast(
            {
                "type": "stock_shortage",
                "org_id": source.org_id,
                "drug_code": source.drug_code,
                "drug_name": source.drug_name,
                "quantity": source.quantity,
                "threshold": source.threshold,
            }
        )
    return dest


@router.get("/alerts", response_model=list[StockOut], dependencies=[Depends(get_current_user)])
def stock_alerts(db: Session = Depends(get_db)):
    """缺药预警：库存低于阈值的品种清单。"""
    return (
        db.query(DrugStock)
        .filter(DrugStock.quantity < DrugStock.threshold)
        .order_by(DrugStock.org_id)
        .all()
    )
