"""中心药房：库存管理、县乡村余缺调拨、缺药预警、采购建议。"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_admin, require_roles
from ..models import DrugStock, Organization, Prescription, PrescriptionItem, StockTransfer, User
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


@router.post(
    "/transfers",
    response_model=StockOut,
    status_code=201,
    dependencies=[Depends(require_roles("operator", "pharmacist"))],  # H2: 调拨经办
)
def transfer_stock(
    body: TransferCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """余缺调拨：从调出机构扣减、调入机构增加，全程留痕。

    H3 整改：调出扣减用条件 UPDATE（WHERE quantity >= 需求量）原子执行并校验
    影响行数，并发下不会把库存扣成负数。
    """
    if body.from_org_id == body.to_org_id:
        raise HTTPException(status_code=422, detail="调出与调入机构不能相同")
    source = (
        db.query(DrugStock)
        .filter(DrugStock.org_id == body.from_org_id, DrugStock.drug_code == body.drug_code)
        .first()
    )
    if source is None:
        raise HTTPException(status_code=409, detail="调出机构库存不足")
    if db.get(Organization, body.to_org_id) is None:
        raise HTTPException(status_code=404, detail="调入机构不存在")

    # 原子扣减：条件不满足（库存不足）时影响行数为 0
    deducted = (
        db.query(DrugStock)
        .filter(
            DrugStock.org_id == body.from_org_id,
            DrugStock.drug_code == body.drug_code,
            DrugStock.quantity >= body.quantity,
        )
        .update({DrugStock.quantity: DrugStock.quantity - body.quantity}, synchronize_session=False)
    )
    if not deducted:
        db.rollback()
        raise HTTPException(status_code=409, detail="调出机构库存不足")

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
            quantity=body.quantity,
            threshold=0,
        )
        db.add(dest)
    else:
        # 调入侧同样用原子自增，避免读改写竞态
        db.query(DrugStock).filter(DrugStock.id == dest.id).update(
            {DrugStock.quantity: DrugStock.quantity + body.quantity}, synchronize_session=False
        )
    db.add(StockTransfer(**body.model_dump(), created_by=user.id))
    db.commit()
    db.refresh(source)
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


@router.get("/purchase-suggestions", dependencies=[Depends(get_current_user)])
def purchase_suggestions(db: Session = Depends(get_db)):
    """采购建议：近30天处方用药量与全网当前库存差值为正的品种清单。

    用药量按处方明细 日剂量×天数 汇总（退回处方不计入）。
    """
    since = datetime.now(timezone.utc) - timedelta(days=30)
    usage_rows = (
        db.query(
            PrescriptionItem.drug_code,
            func.max(PrescriptionItem.drug_name).label("drug_name"),
            func.sum(PrescriptionItem.daily_dose * PrescriptionItem.days).label("usage"),
        )
        .join(Prescription, PrescriptionItem.prescription_id == Prescription.id)
        .filter(
            Prescription.created_at >= since.replace(tzinfo=None),
            Prescription.status != "rejected",
        )
        .group_by(PrescriptionItem.drug_code)
        .all()
    )
    stock_rows = (
        db.query(DrugStock.drug_code, func.sum(DrugStock.quantity).label("quantity"))
        .group_by(DrugStock.drug_code)
        .all()
    )
    stock_by_code = {r.drug_code: int(r.quantity or 0) for r in stock_rows}

    suggestions = []
    for row in usage_rows:
        usage = float(row.usage or 0)
        current = stock_by_code.get(row.drug_code, 0)
        gap = usage - current
        if gap > 0:
            suggestions.append(
                {
                    "drug_code": row.drug_code,
                    "drug_name": row.drug_name,
                    "usage_30d": usage,
                    "current_stock": current,
                    "suggested_quantity": int(gap + 0.999),  # 缺口向上取整
                }
            )
    suggestions.sort(key=lambda s: s["suggested_quantity"], reverse=True)
    return suggestions


@router.get("/alerts", response_model=list[StockOut], dependencies=[Depends(get_current_user)])
def stock_alerts(db: Session = Depends(get_db)):
    """缺药预警：库存低于阈值的品种清单。"""
    return (
        db.query(DrugStock)
        .filter(DrugStock.quantity < DrugStock.threshold)
        .order_by(DrugStock.org_id)
        .all()
    )
