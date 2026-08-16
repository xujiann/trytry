"""费用结算（浙江省指南 M8）：收费项目目录、费用明细、门诊/住院结算。

- ChargeItem：收费项目目录（价格管理），编码关联四统一 charge 字典
  （字典已配置条目时强制目录内编码，兼容空字典）；
- BillDetail：费用明细——门诊按就诊（encounter_id）、住院按住院登记
  （admission_id）累计，计费取价格快照；
- Settlement：结算——汇总未结清明细 → 医保分担（insurance_pay>0 时联动
  生成 InsuranceSettlement 记录，纳入基金监测口径）→ 明细回填结算单号；
- 与 M7 联动：住院费用未结清不可出院（inpatient.discharge 调用
  unsettled_amount 校验）。

块3 深化：统一支付（PaymentOrder + PaymentGateway 协议，内置 MockGateway）
与日终对账（ReconciliationBatch/ReconciliationDiff，三类差异检出）。
"""
import secrets
from datetime import datetime
from typing import Protocol

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..datetypes import OptionalDateStr
from ..concurrency import insert_or_conflict
from ..visibility import assert_org_writable, scope_patient_list
from ..database import get_db
from ..deps import get_current_user, require_admin, require_roles
from ..models import (
    Admission,
    BillDetail,
    ChargeItem,
    ChargePriceChange,
    CodeEntry,
    CodeSystem,
    Encounter,
    InsuranceSettlement,
    Patient,
    PaymentOrder,
    ReconciliationBatch,
    ReconciliationDiff,
    Settlement,
    User,
    utcnow,
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
    item = insert_or_conflict(db, ChargeItem(**body.model_dump()), "该收费项目编码已存在")
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


class RepriceIn(BaseModel):
    new_price: float = Field(gt=0)
    reason: str = Field(default="", max_length=256)
    effective_date: OptionalDateStr = ""


@router.patch("/charge-items/{item_id}", dependencies=[Depends(require_admin)])
def update_charge_item(
    item_id: int,
    body: ChargeItemUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """维护收费项目。改价走这里也会留调价历史——不能靠调用方自觉走 reprice。"""
    item = db.get(ChargeItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="收费项目不存在")
    changes = body.model_dump(exclude_unset=True)
    new_price = changes.get("price")
    if new_price is not None and new_price != item.price:
        db.add(ChargePriceChange(item_id=item.id, old_price=item.price,
                                 new_price=new_price, changed_by=user.id))
    for field, value in changes.items():
        if value is not None:
            setattr(item, field, value)
    db.commit()
    return _charge_item_out(item)


@router.post("/charge-items/{item_id}/reprice", dependencies=[Depends(require_admin)])
def reprice_charge_item(
    item_id: int,
    body: RepriceIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """调价（浙#55）：留依据与生效日期，供对外公示与事后解释。

    已计费的明细不受影响——`bill_details` 存的是计费时的价格快照。
    """
    item = db.get(ChargeItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="收费项目不存在")
    if body.new_price == item.price:
        raise HTTPException(status_code=409, detail="新价格与现价相同，无需调价")
    db.add(
        ChargePriceChange(
            item_id=item.id,
            old_price=item.price,
            new_price=body.new_price,
            reason=body.reason,
            effective_date=body.effective_date,
            changed_by=user.id,
        )
    )
    item.price = body.new_price
    db.commit()
    return _charge_item_out(item)


@router.get("/charge-items/{item_id}/price-history", dependencies=[Depends(require_admin)])
def charge_price_history(item_id: int, db: Session = Depends(get_db)):
    if db.get(ChargeItem, item_id) is None:
        raise HTTPException(status_code=404, detail="收费项目不存在")
    rows = (
        db.query(ChargePriceChange)
        .filter(ChargePriceChange.item_id == item_id)
        .order_by(ChargePriceChange.id.desc())
        .limit(200)
        .all()
    )
    return [
        {
            "id": r.id,
            "old_price": r.old_price,
            "new_price": r.new_price,
            "reason": r.reason,
            "effective_date": r.effective_date,
            "changed_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


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
        # 归属校验：不校验即可给他院在院患者计费（跨机构做账）
        assert_org_writable(db, user, admission.org_id)
    else:
        encounter = db.get(Encounter, body.encounter_id)
        if encounter is None:
            raise HTTPException(status_code=404, detail="就诊记录不存在")
        if encounter.patient_id != body.patient_id:
            raise HTTPException(status_code=422, detail="就诊记录与患者不匹配")
        assert_org_writable(db, user, encounter.org_id)
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
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    q = db.query(BillDetail)
    q = scope_patient_list(db, user, q, BillDetail, patient_id, "billing")
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
    # 归属校验：结算/派生医保记录进入基金监测口径，须以本机构名义发起
    assert_org_writable(db, user, org_id)
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
    patient_id: int | None = None, bill_type: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    q = db.query(Settlement)
    q = scope_patient_list(db, user, q, Settlement, patient_id, "billing")
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


# ---------------------------------------------------------------------------
# 块3：统一支付与日终对账
#
# 支付通道抽象为 PaymentGateway 协议：平台侧只依赖协议方法，
# **真实通道（微信/支付宝/银联/医保电子凭证）对接属外部依赖**，
# 需按各通道 SDK 实现同一协议后在 _gateway() 中按渠道注册；
# 仓库内置 MockGateway 仅供测试与演示（流水号本地生成、通道流水由本地单据派生）。
# ---------------------------------------------------------------------------

PAYMENT_CHANNELS = {"cash": "现金", "card": "银行卡", "insurance": "医保基金", "online": "线上支付"}
PAYMENT_STATUS = {"pending": "待支付", "paid": "已支付", "refunded": "已退款", "failed": "支付失败"}
DIFF_TYPES = {
    "missing_local": "通道有本地无",
    "missing_remote": "本地有通道无",
    "amount_mismatch": "金额不一致",
}


class PaymentGateway(Protocol):
    """支付通道协议：各真实通道实现同一接口后可平替（依赖倒置）。"""

    name: str

    def pay(self, order_id: int, amount: float, channel: str) -> dict:
        """发起支付，返回 {"success": bool, "trade_no": str, "message": str}。"""
        ...

    def refund(self, trade_no: str, amount: float) -> dict:
        """发起退款，返回 {"success": bool, "refund_no": str, "message": str}。"""
        ...

    def query_transactions(self, db: Session, date: str) -> list[dict]:
        """拉取某日通道流水，返回 [{"trade_no": str, "amount": float}]。"""
        ...


class MockGateway:
    """演示/测试用支付通道：流水号本地生成，日终流水默认与本地支付单一致。

    可控开关（仅用于演示与自动化测试，生产实现不含此类字段）：
    - fail_next / fail_channels：模拟支付或退款失败；
    - drop_trade_nos：通道侧缺失该流水（→ missing_remote 差异）；
    - amount_overrides：通道侧金额与本地不同（→ amount_mismatch 差异）；
    - extra_transactions：通道侧多出的单边流水（→ missing_local 差异）。
    """

    name = "mock"

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.fail_next = False
        self.fail_channels: set[str] = set()
        self.drop_trade_nos: set[str] = set()
        self.amount_overrides: dict[str, float] = {}
        self.extra_transactions: list[dict] = []

    def pay(self, order_id: int, amount: float, channel: str) -> dict:
        if self.fail_next or channel in self.fail_channels:
            self.fail_next = False
            return {"success": False, "trade_no": "", "message": "通道返回支付失败（演示）"}
        return {
            "success": True,
            "trade_no": f"MOCK{order_id:08d}{secrets.token_hex(3).upper()}",
            "message": "",
        }

    def refund(self, trade_no: str, amount: float) -> dict:
        if self.fail_next:
            self.fail_next = False
            return {"success": False, "refund_no": "", "message": "通道返回退款失败（演示）"}
        return {"success": True, "refund_no": f"RF{trade_no[-10:]}", "message": ""}

    def query_transactions(self, db: Session, date: str) -> list[dict]:
        """通道日流水：默认镜像本地当日已支付单（净额=支付额-退款额），叠加可控差异。"""
        rows = []
        for order in _orders_of_day(db, date):
            if order.trade_no in self.drop_trade_nos:
                continue
            amount = self.amount_overrides.get(
                order.trade_no, round(order.amount - order.refunded_amount, 2)
            )
            rows.append({"trade_no": order.trade_no, "amount": round(amount, 2)})
        return rows + [dict(t) for t in self.extra_transactions]


# 通道注册表：真实部署时按渠道替换为对应实现（如 {"online": WechatPayGateway()}）
MOCK_GATEWAY = MockGateway()
_GATEWAYS: dict[str, PaymentGateway] = {}


def _gateway(channel: str) -> PaymentGateway:
    return _GATEWAYS.get(channel, MOCK_GATEWAY)


def _orders_of_day(db: Session, date: str) -> list[PaymentOrder]:
    """当日已支付/已退款且有流水号的支付单（对账本地侧口径）。"""
    return [
        o
        for o in db.query(PaymentOrder)
        .filter(PaymentOrder.status.in_(["paid", "refunded"]), PaymentOrder.trade_no != "")
        .order_by(PaymentOrder.id)
        .all()
        if o.paid_at and o.paid_at.strftime("%Y-%m-%d") == date
    ]


def _payment_out(o: PaymentOrder) -> dict:
    return {
        "id": o.id,
        "settlement_id": o.settlement_id,
        "channel": o.channel,
        "channel_name": PAYMENT_CHANNELS.get(o.channel, o.channel),
        "amount": o.amount,
        "refunded_amount": o.refunded_amount,
        "status": o.status,
        "status_name": PAYMENT_STATUS.get(o.status, o.status),
        "trade_no": o.trade_no,
        "fail_reason": o.fail_reason,
        "paid_at": o.paid_at.isoformat() if o.paid_at else None,
        "refunded_at": o.refunded_at.isoformat() if o.refunded_at else None,
        "created_at": o.created_at.isoformat(),
    }


class PaymentCreate(BaseModel):
    settlement_id: int
    channel: str = Field(pattern="^(cash|card|insurance|online)$")
    # 缺省按结算单个人自付金额（医保渠道按医保支付金额）
    amount: float | None = Field(default=None, gt=0)


@router.post(
    "/payments",
    status_code=201,
    dependencies=[Depends(require_roles("operator"))],  # 收费=经办（与结算同岗）
)
def create_payment(
    body: PaymentCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """创建支付单并调用通道支付，回写状态与外部流水号。"""
    settlement = db.get(Settlement, body.settlement_id)
    if settlement is None:
        raise HTTPException(status_code=404, detail="结算单不存在")
    assert_org_writable(db, user, settlement.org_id)
    default_amount = (
        settlement.insurance_pay if body.channel == "insurance" else settlement.self_pay
    )
    amount = round(body.amount if body.amount is not None else default_amount, 2)
    if amount <= 0:
        raise HTTPException(status_code=422, detail="支付金额须大于 0")
    # 已付额只计 paid：refunded 表示款项已退回，仍占额度会导致退款后无法重新收款（少收）
    paid_already = round(
        sum(
            o.amount
            for o in db.query(PaymentOrder)
            .filter(
                PaymentOrder.settlement_id == settlement.id,
                PaymentOrder.status == "paid",
            )
            .all()
        ),
        2,
    )
    if paid_already + amount > round(settlement.total_amount, 2) + 1e-6:
        raise HTTPException(
            status_code=422,
            detail=f"支付金额超出结算单未付余额（总额 {settlement.total_amount}，已付 {paid_already}）",
        )
    order = PaymentOrder(
        settlement_id=settlement.id, channel=body.channel, amount=amount, created_by=user.id
    )
    db.add(order)
    db.flush()
    result = _gateway(body.channel).pay(order.id, amount, body.channel)
    if result.get("success"):
        order.status = "paid"
        order.trade_no = result.get("trade_no", "")
        order.paid_at = utcnow()
    else:
        order.status = "failed"
        order.fail_reason = result.get("message", "通道未返回原因")[:256]
    db.commit()
    db.refresh(order)
    return _payment_out(order)


@router.get("/payments")
def list_payments(
    settlement_id: int | None = None,
    status: str | None = None,
    channel: str | None = None,
    db: Session = Depends(get_db),
):
    q = db.query(PaymentOrder)
    if settlement_id is not None:
        q = q.filter(PaymentOrder.settlement_id == settlement_id)
    if status:
        q = q.filter(PaymentOrder.status == status)
    if channel:
        q = q.filter(PaymentOrder.channel == channel)
    return [_payment_out(o) for o in q.order_by(PaymentOrder.id.desc()).limit(300).all()]


class RefundIn(BaseModel):
    # 缺省全额退款；部分退款须小于等于剩余可退金额
    amount: float | None = Field(default=None, gt=0)
    reason: str = Field(default="", max_length=256)


@router.post("/payments/{order_id}/refund", dependencies=[Depends(require_roles("operator"))])
def refund_payment(
    order_id: int, body: RefundIn, db: Session = Depends(get_db)
):
    """退款：仅已支付单可退，退款金额不得超过剩余可退金额。"""
    order = db.get(PaymentOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="支付单不存在")
    if order.status != "paid":
        raise HTTPException(
            status_code=409, detail=f"当前状态 {PAYMENT_STATUS.get(order.status, order.status)} 不可退款"
        )
    refundable = round(order.amount - order.refunded_amount, 2)
    amount = round(body.amount if body.amount is not None else refundable, 2)
    if amount > refundable + 1e-6:
        raise HTTPException(status_code=422, detail=f"退款金额超出可退余额 {refundable}")
    result = _gateway(order.channel).refund(order.trade_no, amount)
    if not result.get("success"):
        raise HTTPException(status_code=502, detail=result.get("message", "通道退款失败"))
    order.refunded_amount = round(order.refunded_amount + amount, 2)
    order.refunded_at = utcnow()
    if order.refunded_amount >= round(order.amount, 2) - 1e-6:
        order.status = "refunded"  # 全额退回
    db.commit()
    db.refresh(order)
    return {**_payment_out(order), "refund_no": result.get("refund_no", ""), "refund_amount": amount}


# ---------- 日终对账 ----------


def _batch_out(b: ReconciliationBatch, diffs: list[ReconciliationDiff]) -> dict:
    return {
        "id": b.id,
        "date": b.date,
        "total_orders": b.total_orders,
        "total_amount": b.total_amount,
        "matched": b.matched,
        "unmatched": b.unmatched,
        "diff_amount": b.diff_amount,
        "created_at": b.created_at.isoformat(),
        "diffs": [
            {
                "id": d.id,
                "order_id": d.order_id,
                "trade_no": d.trade_no,
                "diff_type": d.diff_type,
                "diff_type_name": DIFF_TYPES.get(d.diff_type, d.diff_type),
                "local_amount": d.local_amount,
                "remote_amount": d.remote_amount,
                "detail": d.detail,
            }
            for d in diffs
        ],
    }


@router.post(
    "/reconciliation/run",
    status_code=201,
    dependencies=[Depends(require_roles("operator", "director"))],  # 日终对账=财务/经办
)
def run_reconciliation(
    date: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """日终对账：比对当日本地支付单与通道流水，三类差异落明细。

    同一日期重跑覆盖上一批次（对账单以最后一次结果为准）。
    """
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=422, detail="date 参数须为 YYYY-MM-DD 格式") from None

    local_orders = _orders_of_day(db, date)
    # 通道流水按渠道拉取；Mock 实现下各渠道共用同一份日流水
    remote = {t["trade_no"]: round(float(t["amount"]), 2) for t in _gateway("").query_transactions(db, date)}

    old = db.query(ReconciliationBatch).filter(ReconciliationBatch.date == date).all()
    for batch in old:
        db.query(ReconciliationDiff).filter(ReconciliationDiff.batch_id == batch.id).delete()
        db.delete(batch)
    db.flush()

    batch = ReconciliationBatch(date=date, created_by=user.id)
    db.add(batch)
    db.flush()

    diffs: list[ReconciliationDiff] = []
    matched = 0
    total_amount = 0.0
    for order in local_orders:
        local_amount = round(order.amount - order.refunded_amount, 2)
        total_amount += local_amount
        if order.trade_no not in remote:
            diffs.append(
                ReconciliationDiff(
                    batch_id=batch.id,
                    order_id=order.id,
                    trade_no=order.trade_no,
                    diff_type="missing_remote",
                    local_amount=local_amount,
                    remote_amount=0.0,
                    detail=f"本地支付单 {order.id} 金额 {local_amount} 在通道流水中不存在",
                )
            )
            continue
        remote_amount = remote.pop(order.trade_no)
        if abs(remote_amount - local_amount) > 0.005:
            diffs.append(
                ReconciliationDiff(
                    batch_id=batch.id,
                    order_id=order.id,
                    trade_no=order.trade_no,
                    diff_type="amount_mismatch",
                    local_amount=local_amount,
                    remote_amount=remote_amount,
                    detail=f"本地 {local_amount} 与通道 {remote_amount} 金额不一致",
                )
            )
        else:
            matched += 1
    for trade_no, amount in remote.items():  # 通道剩余流水即本地无单
        diffs.append(
            ReconciliationDiff(
                batch_id=batch.id,
                order_id=None,
                trade_no=trade_no,
                diff_type="missing_local",
                local_amount=0.0,
                remote_amount=amount,
                detail=f"通道流水 {trade_no} 金额 {amount} 无对应本地支付单",
            )
        )

    batch.total_orders = len(local_orders)
    batch.total_amount = round(total_amount, 2)
    batch.matched = matched
    batch.unmatched = len(diffs)
    batch.diff_amount = round(sum(abs(d.remote_amount - d.local_amount) for d in diffs), 2)
    for d in diffs:
        db.add(d)
    db.commit()
    db.refresh(batch)
    return _batch_out(batch, diffs)


@router.get("/reconciliation")
def list_reconciliation(date: str | None = None, db: Session = Depends(get_db)):
    """对账单列表与差异明细（date 缺省返回最近 30 个批次）。"""
    q = db.query(ReconciliationBatch)
    if date:
        q = q.filter(ReconciliationBatch.date == date)
    batches = q.order_by(ReconciliationBatch.date.desc(), ReconciliationBatch.id.desc()).limit(30).all()
    result = []
    for batch in batches:
        diffs = (
            db.query(ReconciliationDiff)
            .filter(ReconciliationDiff.batch_id == batch.id)
            .order_by(ReconciliationDiff.id)
            .all()
        )
        result.append(_batch_out(batch, diffs))
    return result
