"""费用结算（浙江省指南 M8）：收费项目目录、费用明细、门诊/住院结算。

- ChargeItem：收费项目目录（价格管理），编码关联四统一 charge 字典
  （字典已配置条目时强制目录内编码，兼容空字典）；
- BillDetail：费用明细——门诊按就诊（encounter_id）、住院按住院登记
  （admission_id）累计，计费取价格快照；
- Settlement：结算——汇总未结清明细 → 医保分担（insurance_pay>0 时联动
  生成 InsuranceSettlement 记录，纳入基金监测口径）→ 明细回填结算单号；
- 与 M7 联动：住院费用未结清不可出院（inpatient.discharge 调用
  unsettled_amount 校验）。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_admin, require_roles
from ..models import (
    Admission,
    BillDetail,
    ChargeItem,
    CodeEntry,
    CodeSystem,
    Encounter,
    InsuranceSettlement,
    Patient,
    Settlement,
    User,
)

router = APIRouter(prefix="/api/billing", tags=["费用结算"], dependencies=[Depends(get_current_user)])


def unsettled_amount(db: Session, admission_id: int) -> float:
    """住院未结清费用合计（M7 出院联动校验用）。"""
    total = (
        db.query(func.coalesce(func.sum(BillDetail.amount), 0.0))
        .filter(BillDetail.admission_id == admission_id, BillDetail.settlement_id.is_(None))
        .scalar()
    )
    return round(total or 0.0, 2)


# ---------- 收费项目目录 ----------


class ChargeItemCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    category: str = Field(default="other", pattern="^(drug|exam|treatment|bed|other)$")
    price: float = Field(gt=0)
    active: bool = True


class ChargeItemUpdate(BaseModel):
    name: str | None = None
    category: str | None = Field(default=None, pattern="^(drug|exam|treatment|bed|other)$")
    price: float | None = Field(default=None, gt=0)
    active: bool | None = None


def _charge_item_out(i: ChargeItem) -> dict:
    return {
        "id": i.id, "code": i.code, "name": i.name,
        "category": i.category, "price": i.price, "active": i.active,
    }


def _charge_dict_blocked(db: Session, code: str) -> bool:
    """收费字典管控：charge 字典已配置条目时，仅字典内编码可入目录。"""
    system = db.query(CodeSystem).filter(CodeSystem.code == "charge").first()
    if system is None:
        return False
    has_entries = (
        db.query(CodeEntry.id).filter(CodeEntry.system_id == system.id).first() is not None
    )
    if not has_entries:
        return False
    return (
        db.query(CodeEntry.id)
        .filter(CodeEntry.system_id == system.id, CodeEntry.code == code)
        .first()
        is None
    )


@router.post("/charge-items", status_code=201, dependencies=[Depends(require_admin)])
def create_charge_item(body: ChargeItemCreate, db: Session = Depends(get_db)):
    if db.query(ChargeItem).filter(ChargeItem.code == body.code).first():
        raise HTTPException(status_code=409, detail="该收费项目编码已存在")
    if _charge_dict_blocked(db, body.code):
        raise HTTPException(status_code=422, detail="编码不在四统一收费字典内")
    item = ChargeItem(**body.model_dump())
    db.add(item)
    db.commit()
    return _charge_item_out(item)


@router.get("/charge-items")
def list_charge_items(
    active: bool | None = None, category: str | None = None, db: Session = Depends(get_db)
):
    q = db.query(ChargeItem)
    if active is not None:
        q = q.filter(ChargeItem.active.is_(active))
    if category:
        q = q.filter(ChargeItem.category == category)
    return [_charge_item_out(i) for i in q.order_by(ChargeItem.code).limit(500).all()]


@router.patch("/charge-items/{item_id}", dependencies=[Depends(require_admin)])
def update_charge_item(item_id: int, body: ChargeItemUpdate, db: Session = Depends(get_db)):
    item = db.get(ChargeItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="收费项目不存在")
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(item, field, value)
    db.commit()
    return _charge_item_out(item)


# ---------- 费用明细 ----------


class BillDetailCreate(BaseModel):
    patient_id: int
    admission_id: int | None = None
    encounter_id: int | None = None
    item_code: str = Field(min_length=1)
    quantity: int = Field(default=1, ge=1)


def _bill_detail_out(d: BillDetail) -> dict:
    return {
        "id": d.id,
        "patient_id": d.patient_id,
        "admission_id": d.admission_id,
        "encounter_id": d.encounter_id,
        "item_code": d.item_code,
        "item_name": d.item_name,
        "unit_price": d.unit_price,
        "quantity": d.quantity,
        "amount": d.amount,
        "settled": d.settlement_id is not None,
        "settlement_id": d.settlement_id,
    }


@router.post(
    "/details",
    status_code=201,
    dependencies=[Depends(require_roles("operator", "doctor"))],  # 计费=经办/医师
)
def create_bill_detail(
    body: BillDetailCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if (body.admission_id is None) == (body.encounter_id is None):
        raise HTTPException(
            status_code=422, detail="须且仅须提供 admission_id（住院）或 encounter_id（门诊）之一"
        )
    if body.admission_id is not None:
        admission = db.get(Admission, body.admission_id)
        if admission is None:
            raise HTTPException(status_code=404, detail="住院记录不存在")
        if admission.patient_id != body.patient_id:
            raise HTTPException(status_code=422, detail="住院记录与患者不匹配")
        if admission.status != "admitted":
            raise HTTPException(status_code=409, detail="患者已出院，不可继续计费")
    else:
        encounter = db.get(Encounter, body.encounter_id)
        if encounter is None:
            raise HTTPException(status_code=404, detail="就诊记录不存在")
        if encounter.patient_id != body.patient_id:
            raise HTTPException(status_code=422, detail="就诊记录与患者不匹配")
    item = db.query(ChargeItem).filter(ChargeItem.code == body.item_code).first()
    if item is None:
        raise HTTPException(status_code=404, detail="收费项目不存在")
    if not item.active:
        raise HTTPException(status_code=422, detail="收费项目已停用")
    detail = BillDetail(
        patient_id=body.patient_id,
        admission_id=body.admission_id,
        encounter_id=body.encounter_id,
        item_code=item.code,
        item_name=item.name,
        unit_price=item.price,
        quantity=body.quantity,
        amount=round(item.price * body.quantity, 2),
        created_by=user.id,
    )
    db.add(detail)
    db.commit()
    return _bill_detail_out(detail)


@router.get("/details")
def list_bill_details(
    patient_id: int | None = None,
    admission_id: int | None = None,
    encounter_id: int | None = None,
    settled: bool | None = None,
    db: Session = Depends(get_db),
):
    q = db.query(BillDetail)
    if patient_id is not None:
        q = q.filter(BillDetail.patient_id == patient_id)
    if admission_id is not None:
        q = q.filter(BillDetail.admission_id == admission_id)
    if encounter_id is not None:
        q = q.filter(BillDetail.encounter_id == encounter_id)
    if settled is not None:
        q = q.filter(
            BillDetail.settlement_id.isnot(None) if settled else BillDetail.settlement_id.is_(None)
        )
    return [_bill_detail_out(d) for d in q.order_by(BillDetail.id.desc()).limit(500).all()]


# ---------- 结算 ----------


class SettlementCreate(BaseModel):
    bill_type: str = Field(pattern="^(outpatient|inpatient)$")
    admission_id: int | None = None
    encounter_id: int | None = None
    insurance_pay: float = Field(default=0, ge=0)


def _settlement_out(s: Settlement) -> dict:
    return {
        "id": s.id,
        "patient_id": s.patient_id,
        "org_id": s.org_id,
        "bill_type": s.bill_type,
        "admission_id": s.admission_id,
        "encounter_id": s.encounter_id,
        "total_amount": s.total_amount,
        "insurance_pay": s.insurance_pay,
        "self_pay": s.self_pay,
        "insurance_settlement_id": s.insurance_settlement_id,
        "created_at": s.created_at.isoformat(),
    }


@router.post(
    "/settlements",
    status_code=201,
    dependencies=[Depends(require_roles("operator"))],  # 结算=经办（对齐医保结算矩阵）
)
def create_settlement(
    body: SettlementCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    if body.bill_type == "inpatient":
        if body.admission_id is None:
            raise HTTPException(status_code=422, detail="住院结算须提供 admission_id")
        admission = db.get(Admission, body.admission_id)
        if admission is None:
            raise HTTPException(status_code=404, detail="住院记录不存在")
        patient_id, org_id = admission.patient_id, admission.org_id
        details = (
            db.query(BillDetail)
            .filter(
                BillDetail.admission_id == body.admission_id,
                BillDetail.settlement_id.is_(None),
            )
            .all()
        )
    else:
        if body.encounter_id is None:
            raise HTTPException(status_code=422, detail="门诊结算须提供 encounter_id")
        encounter = db.get(Encounter, body.encounter_id)
        if encounter is None:
            raise HTTPException(status_code=404, detail="就诊记录不存在")
        patient_id, org_id = encounter.patient_id, encounter.org_id
        details = (
            db.query(BillDetail)
            .filter(
                BillDetail.encounter_id == body.encounter_id,
                BillDetail.settlement_id.is_(None),
            )
            .all()
        )
    if not details:
        raise HTTPException(status_code=422, detail="无未结清费用明细，无需结算")
    total = round(sum(d.amount for d in details), 2)
    if round(body.insurance_pay, 2) > total:
        raise HTTPException(status_code=422, detail="医保支付不得超过费用总额")
    insurance_pay = round(body.insurance_pay, 2)
    self_pay = round(total - insurance_pay, 2)

    settlement = Settlement(
        patient_id=patient_id,
        org_id=org_id,
        bill_type=body.bill_type,
        admission_id=body.admission_id,
        encounter_id=body.encounter_id,
        total_amount=total,
        insurance_pay=insurance_pay,
        self_pay=self_pay,
        created_by=user.id,
    )
    db.add(settlement)
    db.flush()
    if insurance_pay > 0:
        # 复用医保结算记录（进入基金监测 fund-stats 口径）
        ins = InsuranceSettlement(
            patient_id=patient_id,
            org_id=org_id,
            settle_type="local",
            total_amount=total,
            insurance_pay=insurance_pay,
            self_pay=self_pay,
        )
        db.add(ins)
        db.flush()
        settlement.insurance_settlement_id = ins.id
    for d in details:
        d.settlement_id = settlement.id
    db.commit()
    db.refresh(settlement)
    return _settlement_out(settlement)


@router.get("/settlements")
def list_settlements(
    patient_id: int | None = None, bill_type: str | None = None, db: Session = Depends(get_db)
):
    q = db.query(Settlement)
    if patient_id is not None:
        q = q.filter(Settlement.patient_id == patient_id)
    if bill_type:
        q = q.filter(Settlement.bill_type == bill_type)
    return [_settlement_out(s) for s in q.order_by(Settlement.id.desc()).limit(200).all()]


@router.get("/stats")
def billing_stats(db: Session = Depends(get_db)):
    """费用分析基础口径：门诊/住院结算笔数与金额、均次费用、医保占比。"""
    rows = (
        db.query(
            Settlement.bill_type,
            func.count(Settlement.id).label("n"),
            func.coalesce(func.sum(Settlement.total_amount), 0.0).label("total"),
            func.coalesce(func.sum(Settlement.insurance_pay), 0.0).label("ins"),
        )
        .group_by(Settlement.bill_type)
        .all()
    )
    return [
        {
            "bill_type": r.bill_type,
            "count": r.n,
            "total_amount": round(r.total, 2),
            "insurance_pay": round(r.ins, 2),
            "avg_amount": round(r.total / r.n, 2) if r.n else 0.0,
            "insurance_ratio_pct": round(r.ins * 100.0 / r.total, 2) if r.total else 0.0,
        }
        for r in rows
    ]
