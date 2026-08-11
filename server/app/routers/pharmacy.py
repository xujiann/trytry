"""中心药房：库存管理、县乡村余缺调拨、缺药预警、采购建议。"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_admin, require_roles
from pydantic import BaseModel, Field

from ..models import (
    DrugStock,
    Organization,
    Prescription,
    PrescriptionItem,
    PurchaseOrder,
    StockTake,
    StockTransfer,
    Supplier,
    User,
)
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
    try:
        db.commit()
    except IntegrityError:
        # L-7 整改：并发向同一新机构首次调拨触发 uq_stock_org_drug → 409（而非500）
        db.rollback()
        raise HTTPException(status_code=409, detail="并发调拨冲突，请重试")
    db.refresh(source)
    db.refresh(dest)
    if source.quantity < source.threshold:
        # M-2 整改：缺药预警定向广播——仅缺药机构在线用户与 admin/director 收到
        manager.broadcast(
            {
                "type": "stock_shortage",
                "org_id": source.org_id,
                "drug_code": source.drug_code,
                "drug_name": source.drug_name,
                "quantity": source.quantity,
                "threshold": source.threshold,
            },
            target_org_id=source.org_id,
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


# ---------- 终审轮：供应商管理 / 采购申请-审批-验收 / 存货盘点（㉜㉝） ----------


class SupplierCreate(BaseModel):
    name: str = Field(min_length=1)
    contact: str = ""
    license_no: str = ""


@router.post(
    "/suppliers",
    status_code=201,
    dependencies=[Depends(require_roles("director", "operator"))],  # 供应商建档
)
def create_supplier(body: SupplierCreate, db: Session = Depends(get_db)):
    if db.query(Supplier).filter(Supplier.name == body.name).first():
        raise HTTPException(status_code=409, detail="供应商已存在")
    supplier = Supplier(**body.model_dump())
    db.add(supplier)
    db.commit()
    return {"id": supplier.id, "name": supplier.name, "active": supplier.active}


@router.get("/suppliers", dependencies=[Depends(get_current_user)])
def list_suppliers(db: Session = Depends(get_db)):
    return [
        {
            "id": s.id,
            "name": s.name,
            "contact": s.contact,
            "license_no": s.license_no,
            "active": s.active,
        }
        for s in db.query(Supplier).order_by(Supplier.id).all()
    ]


class PurchaseCreate(BaseModel):
    org_id: int
    supplier_id: int
    item_type: str = Field(default="drug", pattern="^(drug|material)$")
    item_code: str = Field(min_length=1)
    item_name: str = Field(min_length=1)
    quantity: int = Field(gt=0)
    note: str = ""


@router.post(
    "/purchase-orders",
    status_code=201,
    dependencies=[Depends(require_roles("operator", "pharmacist"))],  # 采购申请
)
def create_purchase(
    body: PurchaseCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    supplier = db.get(Supplier, body.supplier_id)
    if supplier is None or not supplier.active:
        raise HTTPException(status_code=404, detail="供应商不存在或已停用")
    order = PurchaseOrder(requested_by=user.id, **body.model_dump())
    db.add(order)
    db.commit()
    return {"id": order.id, "status": order.status}


@router.post(
    "/purchase-orders/{order_id}/approve",
    dependencies=[Depends(require_roles("director"))],  # 采购审批=管理层
)
def approve_purchase(
    order_id: int,
    reject: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    order = db.get(PurchaseOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="采购单不存在")
    if order.status != "pending":
        raise HTTPException(status_code=409, detail="仅待审批采购单可审批")
    order.status = "rejected" if reject else "approved"
    order.approved_by = user.id
    db.commit()
    return {"id": order.id, "status": order.status}


@router.post(
    "/purchase-orders/{order_id}/receive",
    dependencies=[Depends(require_roles("operator", "pharmacist"))],  # 到货验收
)
def receive_purchase(order_id: int, db: Session = Depends(get_db)):
    order = db.get(PurchaseOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="采购单不存在")
    if order.status != "approved":
        raise HTTPException(status_code=409, detail="仅已审批采购单可验收入库")
    order.status = "received"
    stock_qty = None
    if order.item_type == "drug":
        # 药品验收自动入中心药房库存
        stock = (
            db.query(DrugStock)
            .filter(DrugStock.org_id == order.org_id, DrugStock.drug_code == order.item_code)
            .first()
        )
        if stock is None:
            stock = DrugStock(
                org_id=order.org_id,
                drug_code=order.item_code,
                drug_name=order.item_name,
                quantity=order.quantity,
                threshold=0,
            )
            db.add(stock)
        else:
            stock.quantity += order.quantity
        db.flush()
        stock_qty = stock.quantity
    db.commit()
    return {"id": order.id, "status": order.status, "stock_quantity": stock_qty}


@router.get("/purchase-orders", dependencies=[Depends(get_current_user)])
def list_purchases(status: str | None = None, db: Session = Depends(get_db)):
    q = db.query(PurchaseOrder)
    if status:
        q = q.filter(PurchaseOrder.status == status)
    return [
        {
            "id": o.id,
            "org_id": o.org_id,
            "supplier_id": o.supplier_id,
            "item_type": o.item_type,
            "item_code": o.item_code,
            "item_name": o.item_name,
            "quantity": o.quantity,
            "status": o.status,
        }
        for o in q.order_by(PurchaseOrder.id.desc()).limit(200).all()
    ]


class StockTakeCreate(BaseModel):
    org_id: int
    drug_code: str = Field(min_length=1)
    actual_qty: int = Field(ge=0)
    note: str = ""


@router.post(
    "/stock-takes",
    status_code=201,
    dependencies=[Depends(require_roles("operator", "pharmacist"))],  # 盘点
)
def create_stock_take(
    body: StockTakeCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    stock = (
        db.query(DrugStock)
        .filter(DrugStock.org_id == body.org_id, DrugStock.drug_code == body.drug_code)
        .first()
    )
    if stock is None:
        raise HTTPException(status_code=404, detail="该机构无此药品库存记录")
    take = StockTake(
        org_id=body.org_id,
        drug_code=body.drug_code,
        book_qty=stock.quantity,
        actual_qty=body.actual_qty,
        diff=body.actual_qty - stock.quantity,
        note=body.note,
        created_by=user.id,
    )
    stock.quantity = body.actual_qty  # 盘点后账实相符
    db.add(take)
    db.commit()
    return {
        "id": take.id,
        "book_qty": take.book_qty,
        "actual_qty": take.actual_qty,
        "diff": take.diff,
    }


@router.get("/stock-takes", dependencies=[Depends(get_current_user)])
def list_stock_takes(org_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(StockTake)
    if org_id is not None:
        q = q.filter(StockTake.org_id == org_id)
    return [
        {
            "id": t.id,
            "org_id": t.org_id,
            "drug_code": t.drug_code,
            "book_qty": t.book_qty,
            "actual_qty": t.actual_qty,
            "diff": t.diff,
            "note": t.note,
        }
        for t in q.order_by(StockTake.id.desc()).limit(200).all()
    ]
