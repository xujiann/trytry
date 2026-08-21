"""消毒供应中心：复用器械批次灭菌→发放→回收全流程追溯。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..concurrency import insert_or_conflict
from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import Organization, SterilizationBatch
from ..schemas import BatchCreate, BatchOut

router = APIRouter(prefix="/api/cssd", tags=["消毒供应"], dependencies=[Depends(get_current_user)])

_FLOW = {"sterilizing": "sterile", "sterile": "dispatched", "dispatched": "recycled"}


@router.post(
    "/batches",
    response_model=BatchOut,
    status_code=201,
    dependencies=[Depends(require_roles("operator"))],  # H2: 消毒批次=经办
)
def create_batch(body: BatchCreate, db: Session = Depends(get_db)):
    if db.get(Organization, body.center_org_id) is None:
        raise HTTPException(status_code=404, detail="消毒供应中心机构不存在")
    if db.query(SterilizationBatch).filter(SterilizationBatch.batch_no == body.batch_no).first():
        raise HTTPException(status_code=409, detail="批次号已存在")
    batch = insert_or_conflict(db, SterilizationBatch(**body.model_dump()), "批次号已存在")
    return batch


@router.get("/batches", response_model=list[BatchOut])
def list_batches(status: str | None = None, batch_no: str | None = None, db: Session = Depends(get_db)):
    query = db.query(SterilizationBatch)
    if status:
        query = query.filter(SterilizationBatch.status == status)
    if batch_no:
        query = query.filter(SterilizationBatch.batch_no == batch_no)
    return query.order_by(SterilizationBatch.id.desc()).limit(200).all()


@router.post(
    "/batches/{batch_id}/advance",
    response_model=BatchOut,
    dependencies=[Depends(require_roles("operator"))],  # H2: 批次流转=经办
)
def advance(batch_id: int, dispatched_to_org_id: int | None = None, db: Session = Depends(get_db)):
    """流转到下一状态：灭菌中→已灭菌→已发放（需指定接收机构）→已回收。"""
    batch = db.get(SterilizationBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    next_status = _FLOW.get(batch.status)
    if next_status is None:
        raise HTTPException(status_code=409, detail=f"状态 {batch.status} 已是终态")
    if next_status == "dispatched":
        if dispatched_to_org_id is None:
            raise HTTPException(status_code=422, detail="发放需指定接收机构")
        if db.get(Organization, dispatched_to_org_id) is None:
            raise HTTPException(status_code=404, detail="接收机构不存在")
        batch.dispatched_to_org_id = dispatched_to_org_id
    batch.status = next_status
    db.commit()
    db.refresh(batch)
    return batch


from typing import Any


from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import (
    CssdCostItem,
    User,
)

# ===========================================================================
# ⑥ 消毒供应成本核算
# ===========================================================================


COST_TYPES = {
    "labor": "人工",
    "material": "耗材",
    "energy": "能耗",
    "equipment": "设备折旧",
    "other": "其他",
}


class CostItemCreate(BaseModel):
    batch_id: int
    cost_type: str = Field(pattern="^(labor|material|energy|equipment|other)$")
    amount: float = Field(gt=0)
    note: str = ""




class CostItemOut(BaseModel):
    id: int
    batch_id: int
    cost_type: str
    cost_type_name: str
    #: `CssdCostItem.amount` 是 `Money`（`Numeric(14,2, asdecimal=False)`），出参恒为 float
    amount: float
    note: str


class BatchCost(BaseModel):
    batch_id: int
    batch_no: str
    item_name: str
    quantity: int
    #: `round(totals.get(b.id, 0), 2)`——**没有成本项时 `round(0, 2)` 返回 int `0`**，
    #: 有成本项时才是 float。声明成 float 会把 `0` 变成 `0.0`，那是改响应字节
    #: （CLAUDE.md §11）。故写成联合类型，让 Pydantic 原样透传。
    total_cost: int | float
    #: 除法结果，两条分支都是 float（`round(a/b, 2)` 或字面量 `0.0`）
    unit_cost: float


class CostTypeAmount(BaseModel):
    amount: float
    name: str


class CostStatsOut(BaseModel):
    batches: list[BatchCost]
    total_cost: int | float          # 同 BatchCost.total_cost，空集时是 int 0
    total_quantity: int
    overall_unit_cost: float
    #: 键是成本类型码（labor/material/energy/depreciation），随字典增删而变，故用 dict
    by_cost_type: dict[str, CostTypeAmount]


@router.post(
    "/cost-items", status_code=201, response_model=CostItemOut,
    dependencies=[Depends(require_roles("operator", "director"))],
)
def create_cost_item(
    body: CostItemCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """登记灭菌批次成本项（人工/耗材/能耗/设备折旧）。"""
    if db.get(SterilizationBatch, body.batch_id) is None:
        raise HTTPException(status_code=404, detail="灭菌批次不存在")
    item = CssdCostItem(**body.model_dump(), created_by=user.id)
    db.add(item)
    db.commit()
    db.refresh(item)
    return {
        "id": item.id,
        "batch_id": item.batch_id,
        "cost_type": item.cost_type,
        "cost_type_name": COST_TYPES[item.cost_type],
        "amount": item.amount,
        "note": item.note,
    }


@router.get("/cost-items", response_model=list[CostItemOut])
def list_cost_items(batch_id: int | None = None, db: Session = Depends(get_db)):
    query = db.query(CssdCostItem)
    if batch_id is not None:
        query = query.filter(CssdCostItem.batch_id == batch_id)
    return [
        {
            "id": i.id,
            "batch_id": i.batch_id,
            "cost_type": i.cost_type,
            "cost_type_name": COST_TYPES.get(i.cost_type, i.cost_type),
            "amount": i.amount,
            "note": i.note,
        }
        for i in query.order_by(CssdCostItem.id.desc()).limit(500).all()
    ]


@router.get("/cost-stats", response_model=CostStatsOut)
def cost_stats(batch_id: int | None = None, db: Session = Depends(get_db)):
    """成本统计：按批次汇总总成本与单件成本，并给出成本构成与整体单件成本。"""
    batch_query = db.query(SterilizationBatch)
    if batch_id is not None:
        batch_query = batch_query.filter(SterilizationBatch.id == batch_id)
    batches = batch_query.order_by(SterilizationBatch.id.desc()).limit(200).all()
    ids = [b.id for b in batches]
    totals: dict[int, float] = {}
    by_type: dict[str, float] = {}
    if ids:
        for bid, ctype, amount in (
            db.query(CssdCostItem.batch_id, CssdCostItem.cost_type, func.sum(CssdCostItem.amount))
            .filter(CssdCostItem.batch_id.in_(ids))
            .group_by(CssdCostItem.batch_id, CssdCostItem.cost_type)
            .all()
        ):
            totals[bid] = round(totals.get(bid, 0) + float(amount), 2)
            by_type[ctype] = round(by_type.get(ctype, 0) + float(amount), 2)
    rows: list[dict[str, Any]] = [
        {
            "batch_id": b.id,
            "batch_no": b.batch_no,
            "item_name": b.item_name,
            "quantity": b.quantity,
            "total_cost": round(totals.get(b.id, 0), 2),
            "unit_cost": round(totals.get(b.id, 0) / b.quantity, 2) if b.quantity else 0.0,
        }
        for b in batches
    ]
    total_cost = round(sum(r["total_cost"] for r in rows), 2)
    total_quantity = sum(r["quantity"] for r in rows)
    return {
        "batches": rows,
        "total_cost": total_cost,
        "total_quantity": total_quantity,
        "overall_unit_cost": round(total_cost / total_quantity, 2) if total_quantity else 0.0,
        "by_cost_type": {
            k: {"amount": v, "name": COST_TYPES.get(k, k)} for k, v in sorted(by_type.items())
        },
    }
