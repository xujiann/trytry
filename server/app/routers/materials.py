"""物资采购与高值耗材追溯（T3.3 / T3.4）。

- **采购全流程**：申请→审批→签合同→到货验收。药品采购走 `/api/pharmacy`
  的 PurchaseOrder，此处是非药品物资，审批链与入库对象都不同，不合并。
  验收时自动生成入库流水（复用既有 Asset/AssetMovement 底座）。
- **高值耗材追溯**：一物一码，使用时绑定患者与手术，形成"这枚支架用在谁身上、
  哪台手术、哪个批次、哪家供应商"的完整链条。这是耗材召回时唯一有用的东西。
"""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..concurrency import add_amount, ensure_present, insert_if_absent
from ..visibility import assert_obj_org_writable, assert_org_writable, assert_patient_visible, scope_org_list, visible_org_ids
from ..database import get_db
from ..deps import get_current_user, paginate, require_roles
from ..models import (
    Asset,
    AssetMovement,
    Department,
    HighValueConsumable,
    MaterialPurchase,
    Organization,
    Patient,
    Supplier,
    SurgeryRequest,
    User,
    utcnow,
)

router = APIRouter(prefix="/api/materials", tags=["物资采购与耗材追溯"], dependencies=[Depends(get_current_user)])


# ---------------------------------------------------------------- 采购流程


class PurchaseIn(BaseModel):
    org_id: int
    dept_id: int | None = None
    item_name: str = Field(min_length=1, max_length=128)
    spec: str = ""
    unit: str = "件"
    quantity: int = Field(default=1, gt=0)
    estimated_price: float = Field(default=0, ge=0)
    reason: str = ""


def _purchase_out(p: MaterialPurchase) -> dict:
    return {
        "id": p.id,
        "org_id": p.org_id,
        "dept_id": p.dept_id,
        "item_name": p.item_name,
        "spec": p.spec,
        "unit": p.unit,
        "quantity": p.quantity,
        "estimated_price": p.estimated_price,
        "status": p.status,
        "supplier_id": p.supplier_id,
        "contract_no": p.contract_no,
        "contract_amount": p.contract_amount,
        "received_quantity": p.received_quantity,
    }


# ---------------------------------------------------------------- 响应契约
#
# 模型集中放在所有端点之前（`response_model=` 是装饰器参数，导入时求值）。


class MaterialPurchaseOut(BaseModel):
    """采购申请。`estimated_price` 与 `contract_amount` 都是 `Money`（Numeric）列
    ——整数金额读回来是 int，声明成 float 会把「50000 元」变成「50000.0 元」。"""

    id: int
    org_id: int
    # 未指定申领科室 / 未定供应商时为 null
    dept_id: int | None
    item_name: str
    spec: str
    unit: str
    quantity: int
    estimated_price: int | float
    status: str
    supplier_id: int | None
    contract_no: str
    contract_amount: int | float
    received_quantity: int


class PurchaseStatusOut(BaseModel):
    id: int
    status: str


class PurchaseContractOut(PurchaseStatusOut):
    contract_no: str


class PurchaseReceivedOut(PurchaseStatusOut):
    """到货验收会顺带建/加资产台账，故多回资产 id 与结存数量。"""

    asset_id: int
    asset_quantity: int


class ConsumableOut(BaseModel):
    """高值耗材。`used_at` 未使用时是空串（handler 已折），不是 null。"""

    id: int
    barcode: str
    name: str
    spec: str
    org_id: int
    supplier_id: int | None
    batch_no: str
    expire_date: str
    unit_price: int | float
    status: str
    used_patient_id: int | None
    used_patient_name: str
    used_surgery_id: int | None
    used_surgery_name: str
    used_at: str


class ConsumableTraceOut(ConsumableOut):
    """正向追溯 = 耗材本身 + 供应商名。是严格超集，故继承。"""

    supplier_name: str


@router.post("/purchases", response_model=MaterialPurchaseOut, status_code=201,
             dependencies=[Depends(require_roles("operator", "director"))])
def create_purchase(
    body: PurchaseIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    if body.dept_id is not None and db.get(Department, body.dept_id) is None:
        raise HTTPException(status_code=404, detail="科室不存在")
    purchase = MaterialPurchase(**body.model_dump(), requested_by=user.id)
    db.add(purchase)
    db.commit()
    db.refresh(purchase)
    return _purchase_out(purchase)


@router.get("/purchases", response_model=list[MaterialPurchaseOut])
def list_purchases(
    response: Response,
    org_id: int | None = None,
    status: str | None = None,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    query = db.query(MaterialPurchase)
    query = scope_org_list(db, user, query, MaterialPurchase, org_id)
    if status:
        query = query.filter(MaterialPurchase.status == status)
    return [
        _purchase_out(p)
        for p in paginate(query.order_by(MaterialPurchase.id.desc()), response, offset, limit)
    ]


class ApproveIn(BaseModel):
    approved: bool = True


@router.post("/purchases/{purchase_id}/approve", response_model=PurchaseStatusOut,
             dependencies=[Depends(require_roles("director"))])
def approve_purchase(
    purchase_id: int,
    body: ApproveIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """审批（限管理层）；申请人不得自批——与手术审批、双通道申报同一口径。"""
    purchase = db.get(MaterialPurchase, purchase_id)
    if purchase is None:
        raise HTTPException(status_code=404, detail="采购申请不存在")
    assert_obj_org_writable(db, user, purchase)
    if purchase.status != "requested":
        raise HTTPException(status_code=409, detail=f"当前状态 {purchase.status} 不可审批")
    if purchase.requested_by == user.id:
        raise HTTPException(status_code=403, detail="不得审批本人提出的采购申请")
    purchase.status = "approved" if body.approved else "cancelled"
    purchase.approved_by = user.id
    db.commit()
    return {"id": purchase.id, "status": purchase.status}


class ContractIn(BaseModel):
    supplier_id: int
    contract_no: str = Field(min_length=1, max_length=64)
    contract_amount: float = Field(ge=0)


@router.post(
    "/purchases/{purchase_id}/contract", response_model=PurchaseContractOut,
    dependencies=[Depends(require_roles("operator", "director"))]
)
def sign_contract(purchase_id: int, body: ContractIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    purchase = db.get(MaterialPurchase, purchase_id)
    if purchase is None:
        raise HTTPException(status_code=404, detail="采购申请不存在")
    assert_obj_org_writable(db, user, purchase)
    if purchase.status != "approved":
        raise HTTPException(status_code=409, detail=f"当前状态 {purchase.status} 不可签合同")
    supplier = db.get(Supplier, body.supplier_id)
    if supplier is None or not supplier.active:
        raise HTTPException(status_code=404, detail="供应商不存在或已停用")
    purchase.supplier_id = body.supplier_id
    purchase.contract_no = body.contract_no
    purchase.contract_amount = body.contract_amount
    purchase.status = "contracted"
    db.commit()
    return {"id": purchase.id, "status": purchase.status, "contract_no": purchase.contract_no}


class ReceiveIn(BaseModel):
    received_quantity: int = Field(gt=0)
    note: str = ""


@router.post(
    "/purchases/{purchase_id}/receive", response_model=PurchaseReceivedOut,
    dependencies=[Depends(require_roles("operator", "director"))]
)
def receive_purchase(
    purchase_id: int,
    body: ReceiveIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """到货验收：数量不得超过合同量；验收通过后自动生成物资台账与入库流水。"""
    purchase = db.get(MaterialPurchase, purchase_id)
    if purchase is None:
        raise HTTPException(status_code=404, detail="采购申请不存在")
    assert_obj_org_writable(db, user, purchase)
    if purchase.status != "contracted":
        raise HTTPException(status_code=409, detail=f"当前状态 {purchase.status} 不可验收")
    if body.received_quantity > purchase.quantity:
        raise HTTPException(status_code=422, detail="验收数量不得超过采购数量")

    code = f"MP{purchase.id:06d}"
    asset = db.query(Asset).filter(Asset.code == code).first()
    if asset is None:
        insert_if_absent(
            db,
            Asset(
                org_id=purchase.org_id,
                code=code,
                name=purchase.item_name,
                category="office",
                quantity=0,
            ),
        )
        asset = db.query(Asset).filter(Asset.code == code).first()
    asset = ensure_present(asset, "资产")
    add_amount(db, Asset, asset.id, "quantity", body.received_quantity)
    db.add(
        AssetMovement(
            asset_id=asset.id,
            movement_type="inbound",
            quantity=body.received_quantity,
            note=f"采购验收 {purchase.contract_no}",
            created_by=user.id,
        )
    )
    purchase.received_quantity = body.received_quantity
    purchase.received_note = body.note
    purchase.status = "received"
    db.commit()
    return {
        "id": purchase.id,
        "status": purchase.status,
        "asset_id": asset.id,
        "asset_quantity": asset.quantity,
    }


# ---------------------------------------------------------------- 高值耗材追溯


class ConsumableIn(BaseModel):
    barcode: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    spec: str = ""
    org_id: int
    supplier_id: int | None = None
    batch_no: str = ""
    expire_date: str = ""
    unit_price: float = Field(default=0, ge=0)


@router.post(
    "/consumables", response_model=ConsumableOut, status_code=201,
    dependencies=[Depends(require_roles("operator", "director"))]
)
def register_consumable(body: ConsumableIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """高值耗材入库登记（一物一码）。"""
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    if body.supplier_id is not None and db.get(Supplier, body.supplier_id) is None:
        raise HTTPException(status_code=404, detail="供应商不存在")
    item = HighValueConsumable(**body.model_dump())
    db.add(item)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该条码已登记") from None
    db.refresh(item)
    return _consumable_out(db, item)


def _consumable_out(db: Session, c: HighValueConsumable) -> dict:
    patient = db.get(Patient, c.used_patient_id) if c.used_patient_id else None
    surgery = db.get(SurgeryRequest, c.used_surgery_id) if c.used_surgery_id else None
    return {
        "id": c.id,
        "barcode": c.barcode,
        "name": c.name,
        "spec": c.spec,
        "org_id": c.org_id,
        "supplier_id": c.supplier_id,
        "batch_no": c.batch_no,
        "expire_date": c.expire_date,
        "unit_price": c.unit_price,
        "status": c.status,
        "used_patient_id": c.used_patient_id,
        "used_patient_name": patient.name if patient else "",
        "used_surgery_id": c.used_surgery_id,
        "used_surgery_name": surgery.surgery_name if surgery else "",
        "used_at": c.used_at.isoformat() if c.used_at else "",
    }


class UseIn(BaseModel):
    patient_id: int
    surgery_id: int | None = None


@router.post("/consumables/{barcode}/use", response_model=ConsumableOut,
             dependencies=[Depends(require_roles("doctor", "operator"))])
def use_consumable(
    barcode: str,
    body: UseIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """使用登记：绑定患者与手术，构成召回时可查的追溯链。"""
    item = db.query(HighValueConsumable).filter(HighValueConsumable.barcode == barcode).first()
    if item is None:
        raise HTTPException(status_code=404, detail="耗材条码不存在")
    assert_obj_org_writable(db, user, item)
    if item.status != "in_stock":
        raise HTTPException(status_code=409, detail=f"当前状态 {item.status} 不可使用")
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if body.surgery_id is not None:
        surgery = db.get(SurgeryRequest, body.surgery_id)
        if surgery is None:
            raise HTTPException(status_code=404, detail="手术申请不存在")
        if surgery.patient_id != body.patient_id:
            # 耗材记到别人的手术上，追溯链就断了，这里必须拦
            raise HTTPException(status_code=422, detail="该手术不属于此患者")
    item.status = "used"
    item.used_patient_id = body.patient_id
    item.used_surgery_id = body.surgery_id
    item.used_at = utcnow()
    db.commit()
    db.refresh(item)
    return _consumable_out(db, item)


@router.get("/consumables/trace/{barcode}", response_model=ConsumableTraceOut)
def trace_consumable(barcode: str, db: Session = Depends(get_db)):
    """按条码正向追溯：这枚耗材从哪来、用在谁身上、哪台手术。"""
    item = db.query(HighValueConsumable).filter(HighValueConsumable.barcode == barcode).first()
    if item is None:
        raise HTTPException(status_code=404, detail="耗材条码不存在")
    supplier = db.get(Supplier, item.supplier_id) if item.supplier_id else None
    out = _consumable_out(db, item)
    out["supplier_name"] = supplier.name if supplier else ""
    return out


@router.get("/consumables", response_model=list[ConsumableOut])
def list_consumables(
    response: Response,
    org_id: int | None = None,
    status: str | None = None,
    batch_no: str | None = None,
    patient_id: int | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """反向追溯：按批号查"这批货都用到了谁身上"（召回场景），或按患者查用了哪些耗材。"""
    query = db.query(HighValueConsumable)
    if org_id is not None:
        query = query.filter(HighValueConsumable.org_id == org_id)
    if status:
        query = query.filter(HighValueConsumable.status == status)
    if batch_no:
        query = query.filter(HighValueConsumable.batch_no == batch_no)
    if patient_id is not None:
        # 患者列叫 used_patient_id，不是 patient_id，套不了 scope_patient_list
        assert_patient_visible(db, user, patient_id, resource="consumable")
        query = query.filter(HighValueConsumable.used_patient_id == patient_id)
    else:
        # 召回场景按批号反查"这批货用到了谁身上"，同样只限本机构
        orgs = visible_org_ids(db, user)
        if orgs is not None:
            query = query.filter(HighValueConsumable.org_id.in_(orgs))
    rows = paginate(query.order_by(HighValueConsumable.id.desc()), response, offset, limit)
    return [_consumable_out(db, c) for c in rows]
