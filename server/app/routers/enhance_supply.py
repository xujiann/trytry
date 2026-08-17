"""E5 药品统一采购、统一配送与追溯。

四段业务四组接口，隔离口径按 A 案：

- 统一药品目录：全域字典，admin 独占，不做机构 scope；
- 带量采购批次/分配：批次全域可见，分配落到机构（`scope_org_list`），
  履约按已取出对象的机构归属校验（`assert_obj_org_writable`）；
- 中心配送单：跨机构（from_org/to_org），可见性按"任一端在可见机构内"，
  与双向转诊同一形状——拣货/发货锁发货方可写，签收锁收货方可写；
- 追溯码：按机构可写、按可见机构可读。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from ..database import get_db
from ..deps import get_current_user, require_roles, paginate
from ..models import (
    User,
    Organization,
    ConsortiumDrugCatalog,
    VolumePurchase,
    VolumePurchaseAllocation,
    DistributionOrder,
    DrugTraceCode,
)
from ..visibility import (assert_org_writable, assert_org_visible, scope_org_list,
                          assert_obj_org_writable, assert_obj_org_visible, visible_org_ids)

router = APIRouter(prefix="/api", tags=["统一采购配送"], dependencies=[Depends(get_current_user)])


# ============================================================================
# 统一药品目录（集采中选目录）：全域字典，admin 独占
# ============================================================================


class CatalogCreate(BaseModel):
    drug_code: str = Field(min_length=1, max_length=64)
    drug_name: str = Field(min_length=1, max_length=128)
    spec: str = ""
    unit: str = ""
    selected: bool = False
    volume_agreed: int = Field(default=0, ge=0)
    unit_price: float = Field(default=0, ge=0)


class CatalogUpdate(BaseModel):
    active: bool | None = None
    unit_price: float | None = Field(default=None, ge=0)
    volume_agreed: int | None = Field(default=None, ge=0)


class CatalogOut(BaseModel):
    id: int
    drug_code: str
    drug_name: str
    spec: str
    unit: str
    selected: bool
    volume_agreed: int
    unit_price: float
    active: bool

    model_config = {"from_attributes": True}


@router.post(
    "/consortium-drugs",
    response_model=CatalogOut,
    status_code=201,
    dependencies=[Depends(require_roles("admin"))],
)
def create_consortium_drug(body: CatalogCreate, db: Session = Depends(get_db)):
    """统一目录建档：drug_code 全域唯一，重复 409。"""
    from ..concurrency import insert_or_conflict

    return insert_or_conflict(
        db, ConsortiumDrugCatalog(**body.model_dump()), "药品编码已在统一目录中"
    )


@router.get(
    "/consortium-drugs",
    response_model=list[CatalogOut],
    dependencies=[Depends(get_current_user)],
)
def list_consortium_drugs(active: bool | None = None, db: Session = Depends(get_db)):
    """统一目录清单：全域字典，任何登录账号可查（不做机构 scope）。"""
    q = db.query(ConsortiumDrugCatalog)
    if active is not None:
        q = q.filter(ConsortiumDrugCatalog.active == active)
    return q.order_by(ConsortiumDrugCatalog.id).all()


@router.patch(
    "/consortium-drugs/{catalog_id}",
    response_model=CatalogOut,
    dependencies=[Depends(require_roles("admin"))],
)
def update_consortium_drug(
    catalog_id: int, body: CatalogUpdate, db: Session = Depends(get_db)
):
    """统一目录维护：改停用状态 / 统一价 / 约定量。"""
    row = db.get(ConsortiumDrugCatalog, catalog_id)
    if row is None:
        raise HTTPException(status_code=404, detail="目录药品不存在")
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(row, k, v)
    db.commit()
    db.refresh(row)
    return row


# ============================================================================
# 带量采购批次 + 量分配 + 履约
# ============================================================================


class VolumePurchaseCreate(BaseModel):
    catalog_id: int
    year: str = Field(min_length=1, max_length=4)
    total_volume: int = Field(default=0, ge=0)


class VolumePurchaseOut(BaseModel):
    id: int
    catalog_id: int
    year: str
    total_volume: int
    status: str

    model_config = {"from_attributes": True}


class AllocationCreate(BaseModel):
    org_id: int
    allocated_volume: int = Field(default=0, ge=0)


class AllocationOut(BaseModel):
    id: int
    purchase_id: int
    org_id: int
    allocated_volume: int
    fulfilled_volume: int

    model_config = {"from_attributes": True}


class FulfillUpdate(BaseModel):
    # 履约增量（本次入库/到货新增的履约量），累加到 fulfilled_volume
    delta: int = Field(gt=0)


@router.post(
    "/volume-purchases",
    response_model=VolumePurchaseOut,
    status_code=201,
    dependencies=[Depends(require_roles("director"))],
)
def create_volume_purchase(body: VolumePurchaseCreate, db: Session = Depends(get_db)):
    """带量采购建批：校验目录药品存在。"""
    if db.get(ConsortiumDrugCatalog, body.catalog_id) is None:
        raise HTTPException(status_code=404, detail="目录药品不存在")
    row = VolumePurchase(**body.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get(
    "/volume-purchases",
    response_model=list[VolumePurchaseOut],
    dependencies=[Depends(get_current_user)],
)
def list_volume_purchases(db: Session = Depends(get_db)):
    """带量采购批次清单：批次是全域采购计划，不做机构 scope。"""
    return db.query(VolumePurchase).order_by(VolumePurchase.id.desc()).limit(200).all()


@router.post(
    "/volume-purchases/{purchase_id}/allocations",
    response_model=AllocationOut,
    status_code=201,
    dependencies=[Depends(require_roles("director"))],
)
def allocate_volume(
    purchase_id: int, body: AllocationCreate, db: Session = Depends(get_db)
):
    """把约定量分配给某机构：按 (purchase_id, org_id) upsert；批次已封批 409。"""
    purchase = db.get(VolumePurchase, purchase_id)
    if purchase is None:
        raise HTTPException(status_code=404, detail="采购批次不存在")
    if purchase.status == "closed":
        raise HTTPException(status_code=409, detail="采购批次已封批，不可再分配")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    from ..concurrency import upsert_unique

    # 并发下"先查再插"会双插撞 (purchase_id, org_id) 唯一键→500；改用 upsert_unique
    row, _overwrote = upsert_unique(
        db, VolumePurchaseAllocation,
        keys={"purchase_id": purchase_id, "org_id": body.org_id},
        values={"allocated_volume": body.allocated_volume},
    )
    return row


@router.get(
    "/volume-purchases/{purchase_id}/allocations",
    response_model=list[AllocationOut],
    dependencies=[Depends(get_current_user)],
)
def list_allocations(
    purchase_id: int,
    org_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """某批次的机构分配清单：按可见机构过滤（scope_org_list）。"""
    q = db.query(VolumePurchaseAllocation).filter(
        VolumePurchaseAllocation.purchase_id == purchase_id
    )
    q = scope_org_list(db, user, q, VolumePurchaseAllocation, org_id)
    return q.order_by(VolumePurchaseAllocation.id).all()


@router.patch(
    "/volume-allocations/{allocation_id}/fulfill",
    response_model=AllocationOut,
    dependencies=[Depends(require_roles("operator", "pharmacist"))],
)
def fulfill_allocation(
    allocation_id: int,
    body: FulfillUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """记录履约增量：只能录本机构的分配（按对象机构归属校验）。"""
    row = db.get(VolumePurchaseAllocation, allocation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="分配记录不存在")
    assert_obj_org_writable(db, user, row)
    row.fulfilled_volume = row.fulfilled_volume + body.delta
    db.commit()
    db.refresh(row)
    return row


# ============================================================================
# 中心统一配送（跨机构，形同双向转诊）
# ============================================================================


class DistributionCreate(BaseModel):
    from_org_id: int
    to_org_id: int
    drug_code: str = Field(min_length=1, max_length=64)
    qty: int = Field(gt=0)


class DistributionAdvance(BaseModel):
    action: str = Field(pattern="^(pick|ship|receive)$")


class DistributionOut(BaseModel):
    id: int
    from_org_id: int
    to_org_id: int
    drug_code: str
    qty: int
    status: str

    model_config = {"from_attributes": True}


# 动作 → (前置状态, 后置状态)
_ADVANCE = {
    "pick": ("created", "picked"),
    "ship": ("picked", "shipped"),
    "receive": ("shipped", "received"),
}


@router.post(
    "/distribution-orders",
    response_model=DistributionOut,
    status_code=201,
    dependencies=[Depends(require_roles("operator", "pharmacist"))],
)
def create_distribution(
    body: DistributionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """建配送单：以发货方（from_org）名义写入，校验双方机构存在。"""
    assert_org_writable(db, user, body.from_org_id)
    if body.from_org_id == body.to_org_id:
        raise HTTPException(status_code=422, detail="发货与收货机构不能相同")
    for org_id, label in ((body.from_org_id, "发货"), (body.to_org_id, "收货")):
        if db.get(Organization, org_id) is None:
            raise HTTPException(status_code=404, detail=f"{label}机构不存在")
    row = DistributionOrder(**body.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get(
    "/distribution-orders",
    response_model=list[DistributionOut],
    dependencies=[Depends(get_current_user)],
)
def list_distributions(
    status: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """配送单清单：跨机构可见——发货方或收货方任一在可见机构内即可见。"""
    q = db.query(DistributionOrder)
    if status:
        q = q.filter(DistributionOrder.status == status)
    orgs = visible_org_ids(db, user)
    if orgs is not None:
        q = q.filter(
            DistributionOrder.from_org_id.in_(orgs)
            | DistributionOrder.to_org_id.in_(orgs)
        )
    return q.order_by(DistributionOrder.id.desc()).limit(200).all()


@router.patch(
    "/distribution-orders/{order_id}/advance",
    response_model=DistributionOut,
    dependencies=[Depends(require_roles("operator", "pharmacist"))],
)
def advance_distribution(
    order_id: int,
    body: DistributionAdvance,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """推进配送单状态机。

    - pick/ship：发货方（from_org）操作，锁 from_org 可写；
    - receive：收货方（to_org）操作，锁 to_org 可写，并自动落一条入库追溯码。
    """
    order = db.get(DistributionOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="配送单不存在")
    expected, nxt = _ADVANCE[body.action]
    if order.status != expected:
        raise HTTPException(
            status_code=409,
            detail=f"状态不可从 {order.status} 执行 {body.action}",
        )
    if body.action == "receive":
        assert_obj_org_writable(db, user, order, org_attr="to_org_id")
    else:
        assert_obj_org_writable(db, user, order, org_attr="from_org_id")
    order.status = nxt
    if body.action == "receive":
        db.add(
            DrugTraceCode(
                trace_code=f"DIST-{order.id}",
                drug_code=order.drug_code,
                org_id=order.to_org_id,
                direction="in",
                ref_type="distribution",
                ref_id=order.id,
            )
        )
    db.commit()
    db.refresh(order)
    return order


# ============================================================================
# 药品追溯码流转
# ============================================================================


class TraceCreate(BaseModel):
    trace_code: str = Field(min_length=1, max_length=64)
    drug_code: str = Field(min_length=1, max_length=64)
    org_id: int
    direction: str = Field(pattern="^(in|out|dispense)$")
    ref_type: str = ""
    ref_id: int = 0


class TraceOut(BaseModel):
    id: int
    trace_code: str
    drug_code: str
    org_id: int
    direction: str
    ref_type: str
    ref_id: int

    model_config = {"from_attributes": True}


@router.post(
    "/drug-traces",
    response_model=TraceOut,
    status_code=201,
    dependencies=[Depends(require_roles("operator", "pharmacist"))],
)
def create_trace(
    body: TraceCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """登记一条追溯码流转：以本机构名义写入。"""
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    row = DrugTraceCode(**body.model_dump())
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get(
    "/drug-traces",
    response_model=list[TraceOut],
    dependencies=[Depends(get_current_user)],
)
def list_traces(
    trace_code: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """某追溯码的全部流转行：仅返回机构对调用者可见的行。"""
    q = db.query(DrugTraceCode).filter(DrugTraceCode.trace_code == trace_code)
    orgs = visible_org_ids(db, user)
    if orgs is not None:
        q = q.filter(DrugTraceCode.org_id.in_(orgs))
    return q.order_by(DrugTraceCode.id).all()
