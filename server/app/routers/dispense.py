"""西药发药：审方通过的处方按批次 FEFO 发药、退药冲销、发药记录查询。

三条口径，都写进代码而不是留在文档里：

1. **只发审过的方**。处方状态机（routers/prescriptions.py）里 auto_passed
   （系统审通过）与 approved（药师审通过）可发；pending_review / rejected 拒发。
2. **FEFO（先到效期先出）**：同药多批次按 expire_date 升序扣，过期与已召回
   批次一律不发。发药量按处方明细 日剂量×天数 向上取整——与采购建议
   （pharmacy.purchase_suggestions）同一口径，两处对不上账就没法盘。
3. **同一事务**写发药记录、扣批次（claim_quota 原子占用）、扣 DrugStock 汇总
   （take_amount 原子扣减），保住"汇总 == Σ(批次量-已用)"的对账不变式；
   退药走冲销：记录置 reversed 并回补两侧台账，不删行。
"""
import math

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..concurrency import add_amount, claim_quota, ensure_present, take_amount
from ..database import get_db
from ..deps import get_current_user, require_roles, resolve_business_date
from ..models import (
    DispenseItem,
    DispenseRecord,
    DrugBatch,
    DrugStock,
    Prescription,
    User,
    utcnow,
)
from ..visibility import assert_org_writable, scope_org_list

router = APIRouter(prefix="/api/dispense", tags=["西药发药"], dependencies=[Depends(get_current_user)])

#: 可发药的处方状态：系统审通过 / 药师审通过（见 prescriptions.py 状态机）
DISPENSABLE_STATUSES = ("auto_passed", "approved")


class DispenseCreate(BaseModel):
    prescription_id: int
    # 缺省按开方机构发药；中心药房代发成员机构时显式给出
    org_id: int | None = None


class ReverseIn(BaseModel):
    reason: str = Field(min_length=1, max_length=256)


class DispenseItemOut(BaseModel):
    id: int
    batch_id: int
    batch_no: str
    expire_date: str
    drug_code: str
    drug_name: str
    quantity: int


class DispenseOut(BaseModel):
    id: int
    prescription_id: int
    org_id: int
    status: str
    dispensed_by: int
    reverse_reason: str
    reversed_at: str | None
    created_at: str
    items: list[DispenseItemOut]


def _required_quantity(daily_dose: float, days: int) -> int:
    """处方明细的应发量：日剂量×天数向上取整（与采购建议同口径），至少 1。"""
    return max(1, math.ceil(round(daily_dose * days, 6)))


def _dispense_out(db: Session, record: DispenseRecord) -> dict:
    items = (
        db.query(DispenseItem, DrugBatch)
        .join(DrugBatch, DispenseItem.batch_id == DrugBatch.id)
        .filter(DispenseItem.dispense_id == record.id)
        .order_by(DispenseItem.id)
        .all()
    )
    return {
        "id": record.id,
        "prescription_id": record.prescription_id,
        "org_id": record.org_id,
        "status": record.status,
        "dispensed_by": record.dispensed_by,
        "reverse_reason": record.reverse_reason,
        "reversed_at": record.reversed_at.isoformat() if record.reversed_at else None,
        "created_at": record.created_at.isoformat(),
        "items": [
            {
                "id": i.id,
                "batch_id": i.batch_id,
                "batch_no": b.batch_no,
                "expire_date": b.expire_date,
                "drug_code": i.drug_code,
                "drug_name": i.drug_name,
                "quantity": i.quantity,
            }
            for i, b in items
        ],
    }


def _fefo_batches(db: Session, org_id: int, drug_code: str, today: str) -> list[DrugBatch]:
    """可发批次，按 FEFO 排序：未过期、未召回、仍有余量，先到效期先出。"""
    rows = (
        db.query(DrugBatch)
        .filter(
            DrugBatch.org_id == org_id,
            DrugBatch.drug_code == drug_code,
            DrugBatch.status == "normal",
            DrugBatch.expire_date >= today,
        )
        .order_by(DrugBatch.expire_date, DrugBatch.id)
        .all()
    )
    return [b for b in rows if b.quantity - b.used_quantity > 0]


@router.post(
    "",
    response_model=DispenseOut,
    status_code=201,
    dependencies=[Depends(require_roles("pharmacist", "operator"))],  # 发药=药师/经办
)
def dispense_prescription(
    body: DispenseCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """发药：校验审方状态 → FEFO 逐批原子占用 → 同事务落记录+明细+扣汇总。

    并发安全：`prescription_id` 唯一约束防重复发药（撞了给 409 而不是 500）；
    批次占用走 claim_quota（判够与扣减同一条 SQL），两张方同时抢同一批
    不会超扣——抢不到的换下一批，全都抢不到才整体回滚报库存不足。
    """
    prescription = db.get(Prescription, body.prescription_id)
    if prescription is None:
        raise HTTPException(status_code=404, detail="处方不存在")
    if prescription.status not in DISPENSABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"处方当前状态 {prescription.status} 不可发药（须审方通过）",
        )
    org_id = body.org_id if body.org_id is not None else prescription.org_id
    assert_org_writable(db, user, org_id)
    rx_items = list(prescription.items)
    if not rx_items:
        raise HTTPException(status_code=422, detail="处方无用药明细，无法发药")

    record = DispenseRecord(
        prescription_id=prescription.id, org_id=org_id, dispensed_by=user.id
    )
    db.add(record)
    try:
        db.flush()  # 先占住 prescription_id 唯一约束，重复发药在扣库存前就拦下
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该处方已发药，不可重复发药") from None

    today = resolve_business_date(None).isoformat()
    for rx_item in rx_items:
        need = _required_quantity(rx_item.daily_dose, rx_item.days)
        taken_total = 0
        # 并发下批次可能被别的发药请求抢走：重扫最多 3 轮，仍不够即整体回滚
        for _ in range(3):
            for batch in _fefo_batches(db, org_id, rx_item.drug_code, today):
                take = min(batch.quantity - batch.used_quantity, need)
                if take <= 0:
                    continue
                # 原子占用：判余量与占用同一条 SQL，抢不到就换下一批
                if not claim_quota(
                    db, DrugBatch, batch.id, "used_quantity", "quantity", step=take
                ):
                    continue
                db.add(
                    DispenseItem(
                        dispense_id=record.id,
                        batch_id=batch.id,
                        drug_code=rx_item.drug_code,
                        drug_name=rx_item.drug_name,
                        quantity=take,
                    )
                )
                taken_total += take
                need -= take
                if need <= 0:
                    break
            if need <= 0:
                break
            db.expire_all()  # 重扫前丢弃会话缓存，拿到别的事务提交后的余量
        if need > 0:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"药品 {rx_item.drug_code} 可发批次库存不足"
                "（过期与已召回批次不发，请先补入库或换药）",
            )
        # 同事务扣汇总，保住 汇总==Σ(批次量-已用) 的对账不变式
        stock = ensure_present(
            db.query(DrugStock)
            .filter(DrugStock.org_id == org_id, DrugStock.drug_code == rx_item.drug_code)
            .first(),
            "药品库存",
        )
        if not take_amount(db, DrugStock, stock.id, "quantity", taken_total):
            db.rollback()
            raise HTTPException(
                status_code=409, detail=f"药品 {rx_item.drug_code} 汇总库存不足，台账不符请先盘点"
            )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该处方已发药，不可重复发药") from None
    db.refresh(record)
    return _dispense_out(db, record)


@router.get("", response_model=list[DispenseOut])
def list_dispenses(
    prescription_id: int | None = None,
    org_id: int | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(DispenseRecord)
    q = scope_org_list(db, user, q, DispenseRecord, org_id)
    if prescription_id is not None:
        q = q.filter(DispenseRecord.prescription_id == prescription_id)
    if status:
        q = q.filter(DispenseRecord.status == status)
    rows = q.order_by(DispenseRecord.id.desc()).limit(200).all()
    return [_dispense_out(db, r) for r in rows]


@router.post(
    "/{dispense_id}/reverse",
    response_model=DispenseOut,
    dependencies=[Depends(require_roles("pharmacist", "operator"))],  # 退药=药师/经办
)
def reverse_dispense(
    dispense_id: int,
    body: ReverseIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """退药冲销：记录置 reversed、明细原样保留，同事务回补批次已用与汇总。

    不删行——批号追溯（这批发给了谁、后来退没退）在退药之后仍要答得出来。
    冲销后的处方不可再发（唯一约束仍占着）：确需再发的走新处方。
    """
    record = db.get(DispenseRecord, dispense_id)
    if record is None:
        raise HTTPException(status_code=404, detail="发药记录不存在")
    assert_org_writable(db, user, record.org_id)
    if record.status != "dispensed":
        raise HTTPException(status_code=409, detail="该发药记录已冲销，不可重复退药")
    items = db.query(DispenseItem).filter(DispenseItem.dispense_id == record.id).all()
    for item in items:
        # 回补批次：used_quantity 原子减（不够减说明台账已不符，拦下别越改越乱）
        if not take_amount(db, DrugBatch, item.batch_id, "used_quantity", item.quantity):
            db.rollback()
            raise HTTPException(
                status_code=409, detail=f"批次 {item.batch_id} 台账不符，无法冲销，请先盘点"
            )
        stock = (
            db.query(DrugStock)
            .filter(DrugStock.org_id == record.org_id, DrugStock.drug_code == item.drug_code)
            .first()
        )
        stock = ensure_present(stock, "药品库存")
        add_amount(db, DrugStock, stock.id, "quantity", item.quantity)
    record.status = "reversed"
    record.reverse_reason = body.reason
    record.reversed_by = user.id
    record.reversed_at = utcnow()
    db.commit()
    db.refresh(record)
    return _dispense_out(db, record)
