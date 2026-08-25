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

工程包 I2：真通道接入——`MEDPLAT_PAYMENT_GATEWAY_URL` 非空且过出网校验时，
HTTP 网关（app/payments.py）注册为 channel="gateway"（异步语义：下单 pending，
回调 `POST /api/billing/payments/callback` 验签确认后转 paid）；缺省渠道仍走
Mock 同步语义，既有测试与演示不受影响。
"""
import contextlib
import json
import logging
import secrets
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Protocol, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import case, func, insert, literal, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..datetypes import OptionalDateStr
from ..concurrency import insert_or_conflict
from ..egress import egress_url_allowed, verify_signature
from ..payments import HttpGatewayPaymentGateway, to_fen
from ..visibility import assert_patient_visible, scope_patient_list
from ..database import get_db
from ..deps import get_current_user, require_admin, require_roles
from ..models import (
    Admission,
    BillDetail,
    ChargeItem,
    ChargePriceChange,
    CodeEntry,
    CodeSystem,
    Deposit,
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

logger = logging.getLogger("medplat.billing")


def unsettled_amount(db: Session, admission_id: int) -> float:
    """住院未结清费用合计（M7 出院联动校验用）。"""
    total = (
        db.query(func.coalesce(func.sum(BillDetail.amount), 0.0))
        .filter(BillDetail.admission_id == admission_id, BillDetail.settlement_id.is_(None))
        .scalar()
    )
    return round(total or 0.0, 2)


# ---------- 金额并发闸门 ----------

#: SQLite 侧的进程内互斥（见 `_serialized_on` 文档）。RLock 而非 Lock：
#: 结算的临界区里还会再进押金冲抵这一段，同线程重入不该把自己锁死。
_MONEY_SQLITE_LOCK = threading.RLock()


@contextlib.contextmanager
def _serialized_on(db: Session, model, row_id: int) -> Iterator[None]:
    """把"读金额 → 判定 → 写金额"整段圈成临界区，两种方言各用各的办法。

    **为什么"一条 SQL 里判定 + 写入"在这里不够用。** `concurrency.take_amount`
    的 `UPDATE ... WHERE col >= amount` 之所以对，是因为 UPDATE 会对**既有行**
    取行锁，等锁到手后再重新求值 WHERE（PG 的 EvalPlanQual）。而押金余额不是
    一列而是流水现算，扣减写的是 `INSERT ... FROM SELECT ... WHERE 余额 >= 扣减额`
    ——INSERT **不给任何既有行加锁**，聚合子查询读的是语句开始时的快照，
    READ COMMITTED 下并发事务彼此不可见。实测（PG）：预交 1000、八路并发各退 200，
    八笔全过，refunded=1600、balance=-600。SQLite 的库级写锁把这条掩盖了，
    所以它只在生产库上现形。

    - PostgreSQL：对父行（住院登记 / 就诊 / 支付单所属结算单）`SELECT ... FOR UPDATE`。
      锁到手之后的每条语句都取新快照（READ COMMITTED 逐语句取快照），
      判定读到的就是上一个赢家提交后的值。锁随事务提交/回滚释放，
      因此 **commit 必须写在 with 块内**。
    - SQLite：没有 FOR UPDATE，且库级写锁只在第一条**写**语句才生效，
      判定阶段的读根本不排队。沿用 main.py 审计链的分流写法——单进程内用一把
      进程内锁串行化（SQLite 本就只用于开发/单实例）。

    临界区按"父行"划分而不是全局一把锁：不同住院登记、不同结算单的收退费互不阻塞。
    """
    if db.get_bind().dialect.name == "postgresql":
        db.execute(select(model.id).where(model.id == row_id).with_for_update())
        yield
        return
    with _MONEY_SQLITE_LOCK:
        yield


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


# ============================================================ 响应契约
#
# **本模块是 Money 陷阱最密集的一处**：12 个金额字段全部来自 `Money`
# （`Numeric(14,2, asdecimal=False)`）列，整数金额读回来是 **int** 而不是 float。
# 一律 `int | float` 原样透传——声明 float 会把「200 元」变成「200.0 元」，
# 而这是收费/结算/支付/对账页面，直接给患者看。
#
# 已经出过账的同类 bug：`portal/me/deposits` 的 1000 元押金以 `1000.0` 返回。


class ChargeItemOut(BaseModel):
    id: int
    code: str
    name: str
    category: str
    price: int | float
    active: bool


class PriceHistoryOut(BaseModel):
    id: int
    old_price: int | float
    new_price: int | float
    reason: str
    effective_date: str
    changed_at: str


class BillDetailOut(BaseModel):
    id: int
    patient_id: int
    admission_id: int | None
    encounter_id: int | None
    item_code: str
    item_name: str
    unit_price: int | float
    quantity: int | float
    amount: int | float
    #: 由 `settlement_id is not None` 派生，不是列
    settled: bool
    settlement_id: int | None


class SettlementOut(BaseModel):
    id: int
    patient_id: int
    org_id: int | None
    bill_type: str
    admission_id: int | None
    encounter_id: int | None
    total_amount: int | float
    insurance_pay: int | float
    self_pay: int | float
    #: **INTEGER 外键，可空**——名字读着像医保系统的外部单号，其实是本地
    #: `insurance_settlements.id`。按名字猜成 str 会让整个结算端点 500。
    insurance_settlement_id: int | None
    created_at: str


class SettlementCreatedOut(SettlementOut):
    """结算响应。三个押金键**只在住院冲抵时**追加，且都在末尾——继承 + 
    `exclude_unset` 即可。门诊结算没有押金概念，注入 `null` 会让前端以为
    "有押金但金额为空"。"""

    deposit_offset: int | float | None = None
    payable_after_offset: int | float | None = None
    deposit_balance: int | float | None = None


class BillingStatOut(BaseModel):
    bill_type: str
    count: int
    total_amount: int | float
    insurance_pay: int | float
    #: 均价与占比是 `round(a / b, 2)`——除法恒为 float，兜底字面量也写成 0.0
    avg_amount: float
    insurance_ratio_pct: float


class PaymentOut(BaseModel):
    """支付单。三个时间戳可为 null——未支付/未退款/未回调时就是没有这个时刻，
    折成空串会让"没付过"和"付款时间不详"分不开。"""

    id: int
    settlement_id: int
    channel: str
    channel_name: str
    amount: int | float
    refunded_amount: int | float
    status: str
    status_name: str
    trade_no: str
    fail_reason: str
    paid_at: str | None
    refunded_at: str | None
    callback_at: str | None
    created_at: str


class PaymentCreatedOut(PaymentOut):
    """下单响应。`pay_url`/`qr_code` 是**异步渠道**才有的两个键，追加在末尾，
    故继承是对的（与 `spd/followup` 的报表详情不同，那里新字段插在中间）。
    同步渠道（Mock/现金类）走另一条 return，不带这两个键——端点声明
    `response_model_exclude_unset=True`。"""

    pay_url: str | None = None
    qr_code: str | None = None


class RefundOut(PaymentOut):
    """退款响应：在支付单之后追加两个键。`refund_amount` 同样是 Money 派生。"""

    refund_no: str
    refund_amount: int | float


class ReconciliationDiffOut(BaseModel):
    id: int
    order_id: int | None
    trade_no: str
    diff_type: str
    diff_type_name: str
    local_amount: int | float
    remote_amount: int | float
    detail: str


class ReconciliationBatchOut(BaseModel):
    id: int
    date: str
    total_orders: int
    total_amount: int | float
    matched: int
    unmatched: int
    diff_amount: int | float
    created_at: str
    diffs: list[ReconciliationDiffOut]


@router.post("/charge-items", response_model=ChargeItemOut, status_code=201,
             dependencies=[Depends(require_admin)])
def create_charge_item(body: ChargeItemCreate, db: Session = Depends(get_db)):
    if db.query(ChargeItem).filter(ChargeItem.code == body.code).first():
        raise HTTPException(status_code=409, detail="该收费项目编码已存在")
    if _charge_dict_blocked(db, body.code):
        raise HTTPException(status_code=422, detail="编码不在四统一收费字典内")
    item = insert_or_conflict(db, ChargeItem(**body.model_dump()), "该收费项目编码已存在")
    return _charge_item_out(item)


@router.get("/charge-items", response_model=list[ChargeItemOut])
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


@router.patch("/charge-items/{item_id}", response_model=ChargeItemOut,
              dependencies=[Depends(require_admin)])
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


@router.post("/charge-items/{item_id}/reprice", response_model=ChargeItemOut,
             dependencies=[Depends(require_admin)])
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


@router.get("/charge-items/{item_id}/price-history", response_model=list[PriceHistoryOut],
            dependencies=[Depends(require_admin)])
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
    response_model=BillDetailOut,
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


@router.get("/details", response_model=list[BillDetailOut])
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


# ---------- 住院押金（工程包 B1） ----------
#
# 押金是**只增不改的流水**（预交/退费/结算冲抵），余额按流水现算：
# admissions 是冻结核心表，不能加余额列。退费与冲抵的"不得超余额"用
# INSERT..SELECT WHERE 余额充足 的单条 SQL 原子判定（与 take_amount 同一
# 原则：判定与扣减在同一条语句里），并发退费不会把余额退成负数。

DEPOSIT_TYPES = {"prepay": "预交", "refund": "退费", "offset": "结算冲抵"}


class DepositCreate(BaseModel):
    admission_id: int
    amount: float = Field(gt=0)
    method: str = Field(default="cash", pattern="^(cash|card|online)$")


class DepositRefundIn(BaseModel):
    admission_id: int
    amount: float = Field(gt=0)
    method: str = Field(default="cash", pattern="^(cash|card|online)$")


class DepositOut(BaseModel):
    id: int
    admission_id: int
    amount: float
    deposit_type: str
    deposit_type_name: str
    method: str
    operator: str
    balance: float
    created_at: str


class DepositBalanceOut(BaseModel):
    admission_id: int
    prepaid: float
    refunded: float
    offset: float
    balance: float


class DepositAlertOut(BaseModel):
    admission_id: int
    patient_id: int
    patient_name: str
    org_id: int
    balance: float
    unsettled: float
    gap: float


def deposit_balance(db: Session, admission_id: int) -> float:
    """押金余额 = 预交 - 退费 - 结算冲抵（流水现算）。"""
    total = (
        db.query(
            func.coalesce(
                func.sum(
                    case((Deposit.deposit_type == "prepay", Deposit.amount), else_=-Deposit.amount)
                ),
                0.0,
            )
        )
        .filter(Deposit.admission_id == admission_id)
        .scalar()
    )
    return round(total or 0.0, 2)


def _atomic_deposit_deduct(
    db: Session, admission_id: int, amount: float, deposit_type: str, method: str, operator: str
) -> bool:
    """扣押金：INSERT..SELECT，仅当当前余额 ≥ 扣减额时才落行。

    **调用方必须先进 `_serialized_on(db, Admission, admission_id)` 临界区**，
    并在临界区内提交。这一条 SQL 本身**不是**原子判定：INSERT 不给既有流水行
    加锁，聚合子查询读的是语句开始时的快照，PG READ COMMITTED 下八路并发退费
    实测全部通过（余额 1000 退出 1600）。判定的原子性由外层的行锁承担，
    这条 SQL 只负责"锁内再算一次，不够就一行都不落"这层兜底。

    返回是否扣到；调用方随后自行 commit / rollback。
    """
    balance_expr = (
        select(
            func.coalesce(
                func.sum(
                    case((Deposit.deposit_type == "prepay", Deposit.amount), else_=-Deposit.amount)
                ),
                0.0,
            )
        )
        .where(Deposit.admission_id == admission_id)
        .scalar_subquery()
    )
    stmt = insert(Deposit).from_select(
        ["admission_id", "amount", "deposit_type", "method", "operator", "created_at"],
        select(
            literal(admission_id),
            literal(round(amount, 2)),
            literal(deposit_type),
            literal(method),
            literal(operator),
            literal(utcnow()),
        ).where(balance_expr >= round(amount, 2)),
    )
    return bool(cast(CursorResult, db.execute(stmt)).rowcount)


def _deposit_out(d: Deposit, balance: float) -> dict:
    return {
        "id": d.id,
        "admission_id": d.admission_id,
        "amount": d.amount,
        "deposit_type": d.deposit_type,
        "deposit_type_name": DEPOSIT_TYPES.get(d.deposit_type, d.deposit_type),
        "method": d.method,
        "operator": d.operator,
        "balance": balance,
        "created_at": d.created_at.isoformat(),
    }


@router.post(
    "/deposits",
    response_model=DepositOut,
    status_code=201,
    dependencies=[Depends(require_roles("operator"))],  # 押金收退=经办（与结算同岗）
)
def create_deposit(
    body: DepositCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """押金预交：仅在院患者可预交。"""
    admission = db.get(Admission, body.admission_id)
    if admission is None:
        raise HTTPException(status_code=404, detail="住院记录不存在")
    if admission.status != "admitted":
        raise HTTPException(status_code=409, detail="患者已出院，不可预交押金")
    deposit = Deposit(
        admission_id=body.admission_id,
        amount=round(body.amount, 2),
        deposit_type="prepay",
        method=body.method,
        operator=user.full_name or user.username,
    )
    db.add(deposit)
    db.commit()
    db.refresh(deposit)
    return _deposit_out(deposit, deposit_balance(db, body.admission_id))


@router.post(
    "/deposits/refund",
    response_model=DepositOut,
    status_code=201,
    dependencies=[Depends(require_roles("operator"))],
)
def refund_deposit(
    body: DepositRefundIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """押金退费：不得超余额（原子判定），出院后退余额也走这里。"""
    if db.get(Admission, body.admission_id) is None:
        raise HTTPException(status_code=404, detail="住院记录不存在")
    operator = user.full_name or user.username
    # 判定余额与落退费行必须在同一段临界区里，且提交也要在里头——
    # 锁一放，下一个退费请求读到的就必须是本笔已提交后的余额。
    with _serialized_on(db, Admission, body.admission_id):
        if not _atomic_deposit_deduct(
            db, body.admission_id, body.amount, "refund", body.method, operator
        ):
            db.rollback()
            raise HTTPException(
                status_code=422,
                detail=f"退费金额超出押金余额（当前余额 {deposit_balance(db, body.admission_id)}）",
            )
        deposit_id = (
            db.query(Deposit.id)
            .filter(
                Deposit.admission_id == body.admission_id, Deposit.deposit_type == "refund"
            )
            .order_by(Deposit.id.desc())
            .limit(1)
            .scalar()
        )
        db.commit()
    deposit = db.get(Deposit, deposit_id)
    assert deposit is not None  # 刚插入成功，必有一行
    return _deposit_out(deposit, deposit_balance(db, body.admission_id))


@router.get("/deposits", response_model=list[DepositOut])
def list_deposits(
    admission_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    admission = db.get(Admission, admission_id)
    if admission is None:
        raise HTTPException(status_code=404, detail="住院记录不存在")
    # 押金流水挂在住院记录上，等同患者维度数据：按可见性判定并留痕
    assert_patient_visible(db, user, admission.patient_id, resource="deposit")
    balance = deposit_balance(db, admission_id)
    rows = (
        db.query(Deposit)
        .filter(Deposit.admission_id == admission_id)
        .order_by(Deposit.id.desc())
        .limit(200)
        .all()
    )
    return [_deposit_out(d, balance) for d in rows]


@router.get("/deposits/balance", response_model=DepositBalanceOut)
def get_deposit_balance(
    admission_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    admission = db.get(Admission, admission_id)
    if admission is None:
        raise HTTPException(status_code=404, detail="住院记录不存在")
    assert_patient_visible(db, user, admission.patient_id, resource="deposit")
    sums: dict[str, float] = {
        row[0]: float(row[1] or 0.0)
        for row in db.query(Deposit.deposit_type, func.coalesce(func.sum(Deposit.amount), 0.0))
        .filter(Deposit.admission_id == admission_id)
        .group_by(Deposit.deposit_type)
        .all()
    }
    return {
        "admission_id": admission_id,
        "prepaid": round(sums.get("prepay", 0.0), 2),
        "refunded": round(sums.get("refund", 0.0), 2),
        "offset": round(sums.get("offset", 0.0), 2),
        "balance": deposit_balance(db, admission_id),
    }


@router.get("/deposits/alerts", response_model=list[DepositAlertOut])
def deposit_alerts(
    threshold: float = 0,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """押金余额不足预警：在院患者中 押金余额 - 未结费用 < 阈值 的清单。

    口径：gap = 余额 - 未结费用。阈值缺省 0，即"押金已不够抵未结费用"；
    调大阈值可提前预警（如 gap < 500 就该催缴）。按 gap 从小到大排序——
    预警页要按紧要程度排，最缺钱的排最前。
    """
    q = db.query(Admission).filter(Admission.status == "admitted")
    q = scope_patient_list(db, user, q, Admission, None, "billing")
    alerts = []
    for admission in q.order_by(Admission.id).limit(500).all():
        balance = deposit_balance(db, admission.id)
        unsettled = unsettled_amount(db, admission.id)
        gap = round(balance - unsettled, 2)
        if gap < threshold:
            patient = db.get(Patient, admission.patient_id)
            alerts.append(
                {
                    "admission_id": admission.id,
                    "patient_id": admission.patient_id,
                    "patient_name": patient.name if patient else "",
                    "org_id": admission.org_id,
                    "balance": balance,
                    "unsettled": unsettled,
                    "gap": gap,
                }
            )
    alerts.sort(key=lambda a: a["gap"])
    return alerts


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
    response_model=SettlementCreatedOut,
    response_model_exclude_unset=True,
    status_code=201,
    dependencies=[Depends(require_roles("operator"))],  # 结算=经办（对齐医保结算矩阵）
)
def create_settlement(
    body: SettlementCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """建结算单：认领未结明细 → 医保分担 → 押金冲抵，整段落在一个临界区里。

    并发下的老写法是 check-then-act：先查未结明细、再建单、再回填 settlement_id，
    三步之间没有任何闸门。实测（PG）四路并发出院结算 → **四张结算单、四条医保
    结算记录（基金支出重复计入）、押金被多冲 1500**，且前三张单挂着金额却一条
    明细都没有（明细最终只归属最后一张）。

    改成两道闸门：
    1. 明细认领改为 `UPDATE ... WHERE settlement_id IS NULL` 按 rowcount 判定
       ——UPDATE 对既有行取行锁、锁后重新求值，抢不到的那几路 rowcount=0，
       整个事务回滚，结算单与医保结算记录一并不落库；
    2. 住院结算再加一条部分唯一索引（bill_type='inpatient' 的 admission_id 唯一），
       把"一次住院一张结算单"这条语义下沉到数据库——应用层的判定再怎么写，
       兜底也该在库里（同全域基金池 D-2、居民账户绑定的先例）。
    """
    if body.bill_type == "inpatient":
        if body.admission_id is None:
            raise HTTPException(status_code=422, detail="住院结算须提供 admission_id")
        admission = db.get(Admission, body.admission_id)
        if admission is None:
            raise HTTPException(status_code=404, detail="住院记录不存在")
        patient_id, org_id = admission.patient_id, admission.org_id
        gate_model: type = Admission
        gate_id = body.admission_id
        detail_filter = BillDetail.admission_id == body.admission_id
    else:
        if body.encounter_id is None:
            raise HTTPException(status_code=422, detail="门诊结算须提供 encounter_id")
        encounter = db.get(Encounter, body.encounter_id)
        if encounter is None:
            raise HTTPException(status_code=404, detail="就诊记录不存在")
        patient_id, org_id = encounter.patient_id, encounter.org_id
        gate_model = Encounter
        gate_id = body.encounter_id
        detail_filter = BillDetail.encounter_id == body.encounter_id

    # 临界区按"这次住院/这次就诊"划分：同一笔的并发结算排队，不同笔互不阻塞。
    # 押金冲抵也在里头——它与 /deposits/refund 抢的是同一把住院登记行锁。
    with _serialized_on(db, gate_model, gate_id):
        # 前置轻读只为保住既有口径：没有任何未结明细时按 422 回，
        # 与"重复结算"的既有响应一字不差（真正的并发判定在下面的 UPDATE）。
        if (
            db.query(BillDetail.id)
            .filter(detail_filter, BillDetail.settlement_id.is_(None))
            .first()
            is None
        ):
            raise HTTPException(status_code=422, detail="无未结清费用明细，无需结算")

        settlement = Settlement(
            patient_id=patient_id,
            org_id=org_id,
            bill_type=body.bill_type,
            admission_id=body.admission_id,
            encounter_id=body.encounter_id,
            total_amount=0,
            insurance_pay=0,
            self_pay=0,
            created_by=user.id,
        )
        db.add(settlement)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise HTTPException(
                status_code=409, detail="该住院记录已有结算单，不可重复结算"
            ) from None

        # 认领明细：判定（settlement_id IS NULL）与写入在同一条 UPDATE 里，
        # rowcount 就是"我认领到几条"。0 条 = 有人抢先结完了。
        claimed = cast(
            CursorResult,
            db.execute(
                update(BillDetail)
                .where(detail_filter, BillDetail.settlement_id.is_(None))
                .values(settlement_id=settlement.id)
            ),
        ).rowcount
        if not claimed:
            db.rollback()
            raise HTTPException(status_code=422, detail="无未结清费用明细，无需结算")

        # 总额按**实际认领到的**明细算，而不是认领前那次查询的结果
        total = round(
            db.query(func.coalesce(func.sum(BillDetail.amount), 0.0))
            .filter(BillDetail.settlement_id == settlement.id)
            .scalar()
            or 0.0,
            2,
        )
        if round(body.insurance_pay, 2) > total:
            db.rollback()  # 回滚把明细认领一并退回，明细不会被这次失败的结算占住
            raise HTTPException(status_code=422, detail="医保支付不得超过费用总额")
        insurance_pay = round(body.insurance_pay, 2)
        self_pay = round(total - insurance_pay, 2)
        settlement.total_amount = total
        settlement.insurance_pay = insurance_pay
        settlement.self_pay = self_pay

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

        # 工程包 B1：住院结算自动用押金冲抵个人自付。
        # 差额口径——冲抵额 = min(押金余额, 个人自付)：
        #   余额 ≥ 自付：自付全额冲抵，剩余押金留待退费（/deposits/refund）；
        #   余额 < 自付：全部押金冲抵，患者补缴 payable_after_offset。
        # 余额在临界区内是稳定的（退费与冲抵抢同一把行锁），所以一次就够，
        # 不必再像旧写法那样"抢不到就按当下余额重算再试"。
        deposit_offset = 0.0
        if body.bill_type == "inpatient" and body.admission_id is not None:
            operator = user.full_name or user.username
            balance = deposit_balance(db, body.admission_id)
            offset = round(min(balance, self_pay), 2)
            if offset > 0 and _atomic_deposit_deduct(
                db, body.admission_id, offset, "offset", "settle", operator
            ):
                deposit_offset = offset
        db.commit()
    db.refresh(settlement)
    out = _settlement_out(settlement)
    if body.bill_type == "inpatient" and body.admission_id is not None:
        out["deposit_offset"] = deposit_offset
        out["payable_after_offset"] = round(self_pay - deposit_offset, 2)
        out["deposit_balance"] = deposit_balance(db, body.admission_id)
    return out


@router.get("/settlements", response_model=list[SettlementOut])
def list_settlements(
    patient_id: int | None = None, bill_type: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    q = db.query(Settlement)
    q = scope_patient_list(db, user, q, Settlement, patient_id, "billing")
    if bill_type:
        q = q.filter(Settlement.bill_type == bill_type)
    return [_settlement_out(s) for s in q.order_by(Settlement.id.desc()).limit(200).all()]


@router.get("/stats", response_model=list[BillingStatOut])
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

PAYMENT_CHANNELS = {
    "cash": "现金", "card": "银行卡", "insurance": "医保基金", "online": "线上支付",
    "gateway": "网关支付",  # I2：HTTP 支付网关（异步回调确认），未注册时该渠道不可用
}
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
        """拉取某日通道流水，返回 [{"trade_no": str, "amount": float}]。

        拉取失败应抛 RuntimeError 中止对账（空列表会把当日全部本地单
        误判成"通道缺失"）；Mock 实现本地镜像永不失败。
        """
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


def register_http_gateway() -> bool:
    """I2：`MEDPLAT_PAYMENT_GATEWAY_URL` 非空时注册 HTTP 网关为 channel="gateway"。

    URL 未过出网校验（仅 http(s)、禁内网/环回，见 app/egress.py）时拒绝注册并
    log——带病注册比该渠道不可用更糟。下单入参 channel 缺省仍走 Mock，向后兼容；
    测试可 monkeypatch settings 后重呼本函数（幂等，先摘再挂）。
    """
    _GATEWAYS.pop("gateway", None)
    if not settings.payment_gateway_url:
        return False
    if not egress_url_allowed(settings.payment_gateway_url, "MEDPLAT_PAYMENT_GATEWAY_URL"):
        return False
    _GATEWAYS["gateway"] = HttpGatewayPaymentGateway(
        settings.payment_gateway_url, settings.payment_gateway_key
    )
    return True


register_http_gateway()


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
        "callback_at": o.callback_at.isoformat() if o.callback_at else None,
        "created_at": o.created_at.isoformat(),
    }


#: pending 支付单的占额时效（分钟）。异步渠道下单只是"受理"，钱还没到；
#: 不占额就能同一张账单扫三次码收三次钱（实测 100 元账单收进 300），
#: 占额不设时效又会被"只下单不付款"永久占死。取 30 分钟：比常见扫码码有效期
#: （微信/支付宝 2 小时）短，比收银台一次交互长得多。
_PENDING_ORDER_TTL_MINUTES = 30


def _expire_stale_pending(db: Session, settlement_id: int) -> None:
    """把超时未回调的 pending 单作废，释放它占住的额度。

    取舍写明白：作废之后网关若再送来 paid 回调，`payment_callback` 会按
    "当前状态不接受支付回调" 409 拒绝入账，这笔钱靠日终对账（missing_local）
    捞出来人工处理。反过来不作废的代价更大——任何人都能用永不付款的扫码单
    把一张账单占死，收银台再也收不了钱。
    """
    cutoff = utcnow() - timedelta(minutes=_PENDING_ORDER_TTL_MINUTES)
    db.execute(
        update(PaymentOrder)
        .where(
            PaymentOrder.settlement_id == settlement_id,
            PaymentOrder.status == "pending",
            PaymentOrder.created_at < cutoff,
        )
        .values(
            status="failed",
            fail_reason=f"下单后 {_PENDING_ORDER_TTL_MINUTES} 分钟未收到支付回调，自动作废",
        )
    )


def _collected_amount(db: Session, settlement_id: int, *, include_pending: bool) -> float:
    """结算单已占用的收款额。

    两处口径差别都是缺陷修出来的：

    - **按净额 `amount - refunded_amount` 而不是 amount**：全额退款的单原先
      仍按全额计入已付，患者换个渠道重付就被 422 挡住——一张 100 元的账单
      退过一次就再也收不了钱了；
    - **pending 计入（include_pending）**：异步渠道下单后钱没到但额度必须先占住，
      否则同一张账单能被扫码收多次。回调入账时的复核则**只看已到账的**
      （include_pending=False）：那时要回答的是"这笔钱收下会不会收超"，
      其它还没到账的 pending 不该抢这个额度。
    """
    statuses = ["paid", "refunded"] + (["pending"] if include_pending else [])
    total = (
        db.query(
            func.coalesce(
                func.sum(PaymentOrder.amount - PaymentOrder.refunded_amount), 0.0
            )
        )
        .filter(
            PaymentOrder.settlement_id == settlement_id,
            PaymentOrder.status.in_(statuses),
        )
        .scalar()
    )
    return round(total or 0.0, 2)


class PaymentCreate(BaseModel):
    settlement_id: int
    channel: str = Field(pattern="^(cash|card|insurance|online|gateway)$")
    # 缺省按结算单个人自付金额（医保渠道按医保支付金额）
    amount: float | None = Field(default=None, gt=0)


@router.post(
    "/payments",
    response_model=PaymentCreatedOut,
    response_model_exclude_unset=True,
    status_code=201,
    dependencies=[Depends(require_roles("operator"))],  # 收费=经办（与结算同岗）
)
def create_payment(
    body: PaymentCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """创建支付单并调用通道支付，回写状态与外部流水号。

    同步渠道（Mock/现金类）通道应答即终态；gateway 渠道为异步语义——
    下单只受理，订单停在 pending，由网关回调确认后转 paid（见 payment_callback）。
    """
    settlement = db.get(Settlement, body.settlement_id)
    if settlement is None:
        raise HTTPException(status_code=404, detail="结算单不存在")
    if body.channel == "gateway" and "gateway" not in _GATEWAYS:
        # 未配置/未过出网校验时绝不能悄悄落回 Mock：Mock 会把单标成已支付，
        # 而现实中一分钱都没收到。
        raise HTTPException(status_code=503, detail="支付网关未配置或未通过出网校验，gateway 渠道不可用")
    default_amount = (
        settlement.insurance_pay if body.channel == "insurance" else settlement.self_pay
    )
    amount = round(body.amount if body.amount is not None else default_amount, 2)
    if amount <= 0:
        raise HTTPException(status_code=422, detail="支付金额须大于 0")
    # "算已付额 → 判超额 → 落单"三步之间原先没有闸门，通道 RTT 就是竞态窗口：
    # 实测 1000 元结算单五路并发各缴 1000，五张单全部 paid，收进 5000。
    # 判定与落单一起圈进结算单这一行的临界区，并在里头提交。
    with _serialized_on(db, Settlement, settlement.id):
        _expire_stale_pending(db, settlement.id)
        paid_already = _collected_amount(db, settlement.id, include_pending=True)
        if paid_already + amount > round(settlement.total_amount, 2) + 1e-6:
            # 先收事务再抛：作废写入还挂在未提交的事务里，SQLite 的库级写锁
            # 要等依赖清理才放，下一个请求会撞 "database is locked"
            db.rollback()
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
        pending = bool(result.get("success") and result.get("pending"))
        if result.get("success"):
            order.trade_no = result.get("trade_no", "")
            # 异步通道（pending）受理≠到账，单子留在 pending 等回调；
            # 同步通道通道应答即终态，当场转 paid。
            if not pending:
                order.status = "paid"
                order.paid_at = utcnow()
        else:
            order.status = "failed"
            order.fail_reason = result.get("message", "通道未返回原因")[:256]
        db.commit()
    db.refresh(order)
    if pending:
        # 把跳转/二维码参数带回给收银端
        return {
            **_payment_out(order),
            "pay_url": result.get("pay_url", ""),
            "qr_code": result.get("qr_code", ""),
        }
    return _payment_out(order)


@router.get("/payments", response_model=list[PaymentOut])
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


class PaymentCallbackOut(BaseModel):
    ok: bool
    order_id: int
    status: str
    idempotent: bool = False


# 回调免登录：支付网关没有平台账号，身份由 HMAC-SHA256 验签 + 时间窗承担
# （口径见 app/egress.py）。billing 路由器带路由器级登录依赖，注册这一条时
# 临时摘除、注册完立即恢复——只有回调走这条路，其余端点的鉴权一个字节不变。
_authed_dependencies = router.dependencies
router.dependencies = []


@router.post("/payments/callback", response_model=PaymentCallbackOut)
async def payment_callback(request: Request, db: Session = Depends(get_db)):
    """支付网关回调：验签后把 pending 单置为终态（paid/failed）。

    防重放两道：时间戳窗口（窗外 401）＋订单状态幂等（已 paid 的同单
    重放不再产生任何写入，返回 idempotent=True）。金额与本地单核对，
    不一致按篡改拒绝。既有"支付成功后续逻辑"（status/trade_no/paid_at
    回写）原子迁移到这里，与 Mock 同步路径口径一致。
    """
    key = settings.payment_gateway_key
    if not key:
        raise HTTPException(status_code=503, detail="未配置支付网关密钥，回调不可用")
    raw = await request.body()
    problem = verify_signature(
        key, request.headers.get("X-Timestamp", ""), raw, request.headers.get("X-Signature", "")
    )
    if problem:
        logger.warning("[PAY-CALLBACK] 验签失败：%s", problem)
        raise HTTPException(status_code=401, detail=f"回调验签失败：{problem}")
    try:
        payload = json.loads(raw)
        order_id = int(payload["order_id"])
        trade_no = str(payload.get("trade_no", ""))
        result_status = str(payload["status"])
        amount_fen = int(payload["amount_fen"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=422, detail="回调报文缺字段或类型不合法") from None
    if result_status not in ("paid", "failed"):
        raise HTTPException(status_code=422, detail="status 仅接受 paid/failed")
    order = db.get(PaymentOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="支付单不存在")
    if amount_fen != to_fen(order.amount):
        raise HTTPException(status_code=422, detail="回调金额与支付单不一致，拒绝入账")
    if order.status == "paid":
        if result_status == "paid" and (not order.trade_no or order.trade_no == trade_no):
            # 幂等：同一笔的重复回调不再产生任何写入
            return {"ok": True, "order_id": order.id, "status": order.status, "idempotent": True}
        raise HTTPException(status_code=409, detail="支付单已入账，回调与已有流水不符")
    if order.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"当前状态 {PAYMENT_STATUS.get(order.status, order.status)} 不接受支付回调",
        )
    if result_status != "paid":
        order.status = "failed"
        order.fail_reason = str(payload.get("message", "通道回调支付失败"))[:256]
        order.callback_at = utcnow()
        db.commit()
        return {"ok": True, "order_id": order.id, "status": order.status, "idempotent": False}
    # 入账前复核**结算单**层面的未付余额，而不只是"回调金额==本单金额"。
    # 只核对本单金额挡不住"同一张账单开了多张单"这类超收：三张 100 元的
    # gateway 单各自金额都对，回调三次就收进 300（实测）。这里按已到账口径
    # （不含其它 pending）复核一次，收足了就拒绝入账。
    with _serialized_on(db, Settlement, order.settlement_id):
        settlement = db.get(Settlement, order.settlement_id)
        settled = _collected_amount(db, order.settlement_id, include_pending=False)
        if settlement is not None and settled + round(order.amount, 2) > round(
            settlement.total_amount, 2
        ) + 1e-6:
            logger.error(
                "[PAY-CALLBACK] 结算单 %s 已收 %s / 总额 %s，支付单 %s 的 %s 元回调超出未付余额，拒绝入账",
                settlement.id, settled, settlement.total_amount, order.id, order.amount,
            )
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"该结算单已收足（总额 {settlement.total_amount}，已收 {settled}），回调超出未付余额，拒绝入账",
            )
        order.status = "paid"
        order.trade_no = trade_no or order.trade_no
        order.paid_at = utcnow()
        order.callback_at = utcnow()
        db.commit()
    return {"ok": True, "order_id": order.id, "status": order.status, "idempotent": False}


router.dependencies = _authed_dependencies


class RefundIn(BaseModel):
    # 缺省全额退款；部分退款须小于等于剩余可退金额
    amount: float | None = Field(default=None, gt=0)
    reason: str = Field(default="", max_length=256)


@router.post("/payments/{order_id}/refund", response_model=RefundOut,
             dependencies=[Depends(require_roles("operator"))])
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
    # 先占额、再调通道。老写法是"读 refunded_amount → 判可退 → 回写"的读-改-写，
    # 且通道调用在提交之前：八路并发各退 50，八笔全部 200、通道真退了 400，
    # 台账只记到 150——退出去的钱比账上多。
    # 占额这条 UPDATE 同 concurrency.take_amount：判定与累加在一条 SQL 里，
    # 对既有行取行锁、锁后重新求值，抢不到的那几路 rowcount=0 → 422。
    claimed = cast(
        CursorResult,
        db.execute(
            update(PaymentOrder)
            .where(
                PaymentOrder.id == order.id,
                PaymentOrder.status == "paid",
                PaymentOrder.amount - PaymentOrder.refunded_amount >= amount - 1e-6,
            )
            .values(refunded_amount=PaymentOrder.refunded_amount + amount)
        ),
    ).rowcount
    if not claimed:
        db.rollback()
        db.refresh(order)  # 抢输了就把真实可退额报出来，别回一个读旧值算出的数
        raise HTTPException(
            status_code=422,
            detail=f"退款金额超出可退余额 {round(order.amount - order.refunded_amount, 2)}",
        )
    result = _gateway(order.channel).refund(order.trade_no, amount)
    if not result.get("success"):
        # 通道明确回失败＝钱没出去，回滚把占额退回。
        # 取舍：不落"待冲正"记录——通道已给出确定答复的失败，落一条待冲正
        # 只会制造需要人工消化的假差错；真正的不确定（超时/无应答）由通道侧
        # 返回失败结果 + 日终对账兜底，那才是对账要解决的问题。
        db.rollback()
        raise HTTPException(status_code=502, detail=result.get("message", "通道退款失败"))
    db.refresh(order)  # 占额走的是 Core UPDATE，会话里的对象还是旧值
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
    response_model=ReconciliationBatchOut,
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
    # 通道流水：注册了 HTTP 网关时拉真通道流水（GET /transactions?date=），
    # 否则仍为 Mock 本地镜像；Mock 实现下各渠道共用同一份日流水。
    try:
        remote_rows = _gateway("gateway").query_transactions(db, date)
    except RuntimeError as exc:
        # 拉不到流水必须中止：空流水会把当日全部本地单误判成"通道缺失"
        raise HTTPException(status_code=502, detail=str(exc)) from None
    remote = {t["trade_no"]: round(float(t["amount"]), 2) for t in remote_rows}

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


@router.get("/reconciliation", response_model=list[ReconciliationBatchOut])
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
