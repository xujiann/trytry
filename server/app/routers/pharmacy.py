"""中心药房：库存管理、批号效期台账、县乡村余缺调拨、缺药预警、采购建议。"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..clock import now_naive
from ..concurrency import add_amount, ensure_present, insert_if_absent, insert_or_conflict
from ..visibility import assert_obj_org_writable, assert_org_writable, scope_org_list
from ..database import get_db
from ..datetypes import DateStr
from ..deps import get_current_user, require_admin, require_roles, resolve_business_date
from pydantic import BaseModel, Field

from ..models import (
    DispenseItem,
    DispenseRecord,
    DrugBatch,
    DrugStock,
    Organization,
    Patient,
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
        # 累加语义用不了 upsert_unique（那是覆盖）。先试插一行零库存，
        # 谁插上都行，撞了说明别人刚建好，取回来照样走累加——
        # 直接 db.add 则两个入库请求都建行、撞唯一约束，两批药都没入成。
        insert_if_absent(db, DrugStock(**{**body.model_dump(), "quantity": 0}))
        stock = (
            db.query(DrugStock)
            .filter(DrugStock.org_id == body.org_id, DrugStock.drug_code == body.drug_code)
            .first()
        )
    stock = ensure_present(stock, "药品库存")
    add_amount(db, DrugStock, stock.id, "quantity", body.quantity)
    stock.threshold = body.threshold
    stock.drug_name = body.drug_name
    db.commit()
    db.refresh(stock)
    return stock


@router.get("/stocks", response_model=list[StockOut], dependencies=[Depends(get_current_user)])
def list_stocks(org_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user),):
    query = db.query(DrugStock)
    query = scope_org_list(db, user, query, DrugStock, org_id)
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
    since = now_naive() - timedelta(days=30)
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
def stock_alerts(
    org_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """缺药预警：库存低于阈值的品种清单（按可见机构过滤）。"""
    q = db.query(DrugStock).filter(DrugStock.quantity < DrugStock.threshold)
    q = scope_org_list(db, user, q, DrugStock, org_id)
    return q.order_by(DrugStock.org_id).all()


# ---------- 工程包 B1：药品批号效期台账（对齐疫苗 VaccineBatch 先例） ----------
#
# 批次是明细、DrugStock 是汇总：入库/发药/退药在同一事务里两边同改，
# 不变式 汇总量 == Σ(批次量 - 批次已用) 由 tests/test_pharmacy_batches.py 钉住。
# 效期照疫苗批次的口径按日期现算，不设定时任务改状态。


class BatchReceiveIn(BaseModel):
    org_id: int
    drug_code: str = Field(min_length=1, max_length=64)
    drug_name: str = Field(min_length=1, max_length=128)
    batch_no: str = Field(min_length=1, max_length=64)
    expire_date: DateStr
    supplier: str = Field(default="", max_length=128)
    quantity: int = Field(gt=0)


class BatchOut(BaseModel):
    id: int
    org_id: int
    drug_code: str
    drug_name: str
    batch_no: str
    expire_date: str
    supplier: str
    quantity: int
    used_quantity: int
    remaining: int
    status: str
    recall_reason: str


class ExpiringBatchOut(BatchOut):
    remaining_days: int
    expired: bool


class BatchRecallIn(BaseModel):
    reason: str = Field(min_length=1, max_length=256)


class BatchDispenseRow(BaseModel):
    dispense_id: int
    prescription_id: int
    patient_id: int
    patient_name: str
    quantity: int
    status: str
    dispensed_at: str


class BatchTraceOut(BaseModel):
    batch_id: int
    org_id: int
    drug_code: str
    drug_name: str
    batch_no: str
    expire_date: str
    status: str
    total_dispensed: int
    dispenses: list[BatchDispenseRow]


def _stock_of(db: Session, org_id: int, drug_code: str) -> DrugStock | None:
    return (
        db.query(DrugStock)
        .filter(DrugStock.org_id == org_id, DrugStock.drug_code == drug_code)
        .first()
    )


def _batch_of(db: Session, org_id: int, drug_code: str, batch_no: str) -> DrugBatch | None:
    return (
        db.query(DrugBatch)
        .filter(
            DrugBatch.org_id == org_id,
            DrugBatch.drug_code == drug_code,
            DrugBatch.batch_no == batch_no,
        )
        .first()
    )


def _batch_out(b: DrugBatch, drug_name: str) -> dict:
    return {
        "id": b.id,
        "org_id": b.org_id,
        "drug_code": b.drug_code,
        "drug_name": drug_name,
        "batch_no": b.batch_no,
        "expire_date": b.expire_date,
        "supplier": b.supplier,
        "quantity": b.quantity,
        "used_quantity": b.used_quantity,
        "remaining": b.quantity - b.used_quantity,
        "status": b.status,
        "recall_reason": b.recall_reason,
    }


def _stock_names(db: Session, batches: list[DrugBatch]) -> dict[tuple[int, str], str]:
    keys = {(b.org_id, b.drug_code) for b in batches}
    if not keys:
        return {}
    rows = (
        db.query(DrugStock.org_id, DrugStock.drug_code, DrugStock.drug_name)
        .filter(DrugStock.drug_code.in_({c for _, c in keys}))
        .all()
    )
    return {(r.org_id, r.drug_code): r.drug_name for r in rows}


@router.post(
    "/batches",
    response_model=BatchOut,
    status_code=201,
    dependencies=[Depends(require_roles("operator", "pharmacist"))],  # 入库=经办/药师
)
def receive_batch(
    body: BatchReceiveIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """按批次入库：批次落明细行，同事务累加 DrugStock 汇总（保留原汇总语义）。

    同批号再次到货按累加处理（与 /stocks 的入库累加语义一致），
    但效期必须与首登一致——同一批号两个效期说明录错了，宁可拦下。
    """
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    assert_org_writable(db, user, body.org_id)
    # 汇总行与批次行都用"先试插零行、再原子累加"，并发同批入库不会 500（§6）
    insert_if_absent(
        db,
        DrugStock(
            org_id=body.org_id,
            drug_code=body.drug_code,
            drug_name=body.drug_name,
            quantity=0,
            threshold=0,
        ),
    )
    stock = ensure_present(_stock_of(db, body.org_id, body.drug_code), "药品库存")
    insert_if_absent(
        db,
        DrugBatch(
            org_id=body.org_id,
            drug_code=body.drug_code,
            batch_no=body.batch_no,
            expire_date=body.expire_date,
            supplier=body.supplier,
            quantity=0,
        ),
    )
    batch = ensure_present(
        _batch_of(db, body.org_id, body.drug_code, body.batch_no), "药品批次"
    )
    if batch.expire_date != body.expire_date:
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail=f"该批号已按效期 {batch.expire_date} 登记，与本次 {body.expire_date} 不一致",
        )
    if batch.status != "normal":
        db.rollback()
        raise HTTPException(status_code=409, detail="该批次已召回，不得再入库")
    add_amount(db, DrugBatch, batch.id, "quantity", body.quantity)
    add_amount(db, DrugStock, stock.id, "quantity", body.quantity)
    stock.drug_name = body.drug_name
    db.commit()
    db.refresh(batch)
    return _batch_out(batch, body.drug_name)


@router.get(
    "/batches", response_model=list[BatchOut], dependencies=[Depends(get_current_user)]
)
def list_batches(
    org_id: int | None = None,
    drug_code: str | None = None,
    batch_no: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(DrugBatch)
    q = scope_org_list(db, user, q, DrugBatch, org_id)
    if drug_code:
        q = q.filter(DrugBatch.drug_code == drug_code)
    if batch_no:
        q = q.filter(DrugBatch.batch_no == batch_no)
    rows = q.order_by(DrugBatch.org_id, DrugBatch.drug_code, DrugBatch.expire_date).limit(500).all()
    names = _stock_names(db, rows)
    return [_batch_out(b, names.get((b.org_id, b.drug_code), "")) for b in rows]


@router.get(
    "/batches/expiring",
    response_model=list[ExpiringBatchOut],
    dependencies=[Depends(get_current_user)],
)
def expiring_drug_batches(
    days: int = Query(default=90, ge=1, le=3650),
    org_id: int | None = None,
    today: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """近效期预警：`days` 天内到期（含已过期）且仍有余量的批次。

    学医废滞留预警的口径：剩余天数服务端直接算好、按到期先后排序返回——
    预警页要按紧急程度排，别让前端自己减日期；已过期的负数天并标注，
    不是催人用掉，而是提示按报废流程处理。
    """
    today_d = resolve_business_date(today)
    limit_date = (today_d + timedelta(days=days)).isoformat()
    q = db.query(DrugBatch).filter(DrugBatch.expire_date <= limit_date)
    q = scope_org_list(db, user, q, DrugBatch, org_id)
    rows = [
        b
        for b in q.order_by(DrugBatch.expire_date, DrugBatch.id).limit(500).all()
        if b.quantity - b.used_quantity > 0
    ]
    names = _stock_names(db, rows)
    return [
        {
            **_batch_out(b, names.get((b.org_id, b.drug_code), "")),
            "remaining_days": (date.fromisoformat(b.expire_date) - today_d).days,
            "expired": b.expire_date < today_d.isoformat(),
        }
        for b in rows
    ]


@router.post(
    "/batches/{batch_id}/recall",
    response_model=BatchOut,
    dependencies=[Depends(require_roles("pharmacist", "director"))],  # 召回=药师/管理层
)
def recall_batch(
    batch_id: int,
    body: BatchRecallIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """召回批次：召回后不得再发药、不得再入库。不删行——发过的要查得到。"""
    batch = db.get(DrugBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    assert_obj_org_writable(db, user, batch)
    if batch.status == "recalled":
        raise HTTPException(status_code=409, detail="该批次已召回")
    batch.status = "recalled"
    batch.recall_reason = body.reason
    db.commit()
    db.refresh(batch)
    stock = _stock_of(db, batch.org_id, batch.drug_code)
    return _batch_out(batch, stock.drug_name if stock else "")


@router.get(
    "/batches/{batch_id}/dispenses",
    response_model=BatchTraceOut,
    dependencies=[Depends(get_current_user)],
)
def batch_dispense_trace(batch_id: int, db: Session = Depends(get_db)):
    """效期/召回按批号反查：这一批发给了谁——召回时唯一有用的那个查询。"""
    batch = db.get(DrugBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    rows = (
        db.query(DispenseItem, DispenseRecord, Prescription.patient_id, Patient.name)
        .join(DispenseRecord, DispenseItem.dispense_id == DispenseRecord.id)
        .join(Prescription, DispenseRecord.prescription_id == Prescription.id)
        .outerjoin(Patient, Patient.id == Prescription.patient_id)
        .filter(DispenseItem.batch_id == batch_id)
        .order_by(DispenseItem.id.desc())
        .limit(1000)
        .all()
    )
    stock = _stock_of(db, batch.org_id, batch.drug_code)
    return {
        "batch_id": batch.id,
        "org_id": batch.org_id,
        "drug_code": batch.drug_code,
        "drug_name": stock.drug_name if stock else "",
        "batch_no": batch.batch_no,
        "expire_date": batch.expire_date,
        "status": batch.status,
        # 冲销的不计入"仍在外面"的量——但行保留在下面清单里（status 标 reversed）
        "total_dispensed": sum(i.quantity for i, r, _, _ in rows if r.status == "dispensed"),
        "dispenses": [
            {
                "dispense_id": r.id,
                "prescription_id": r.prescription_id,
                "patient_id": patient_id,
                "patient_name": name or "",
                "quantity": i.quantity,
                "status": r.status,
                "dispensed_at": r.created_at.isoformat(),
            }
            for i, r, patient_id, name in rows
        ],
    }


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
    supplier = insert_or_conflict(db, Supplier(**body.model_dump()), "供应商已存在")
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
    assert_org_writable(db, user, body.org_id)
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
    assert_obj_org_writable(db, user, order)
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
def receive_purchase(order_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    order = db.get(PurchaseOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="采购单不存在")
    assert_obj_org_writable(db, user, order)
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
            # 两张采购单同时验收同一个药品，都查不到库存行就都去建，
            # 撞 (org_id, drug_code) 唯一约束 → 500，两张单都没入库。
            # 先试插一行零库存，谁插上都行，随后统一按增量累加。
            insert_if_absent(
                db,
                DrugStock(
                    org_id=order.org_id,
                    drug_code=order.item_code,
                    drug_name=order.item_name,
                    quantity=0,
                    threshold=0,
                ),
            )
            stock = (
                db.query(DrugStock)
                .filter(DrugStock.org_id == order.org_id, DrugStock.drug_code == order.item_code)
                .first()
            )
        stock = ensure_present(stock, "药品库存")
        add_amount(db, DrugStock, stock.id, "quantity", order.quantity)
        db.flush()
        db.refresh(stock)
        stock_qty = stock.quantity
    db.commit()
    return {"id": order.id, "status": order.status, "stock_quantity": stock_qty}


@router.get("/purchase-orders", dependencies=[Depends(get_current_user)])
def list_purchases(
    status: str | None = None,
    org_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(PurchaseOrder)
    q = scope_org_list(db, user, q, PurchaseOrder, org_id)
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
    assert_org_writable(db, user, body.org_id)
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
def list_stock_takes(org_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user),):
    q = db.query(StockTake)
    q = scope_org_list(db, user, q, StockTake, org_id)
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
