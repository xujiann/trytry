"""中心药房：库存管理、批号效期台账、县乡村余缺调拨、缺药预警、采购建议。

**一条口径贯穿本模块**：`DrugStock.quantity` 是可用汇总，它的权威定义在批次侧——

    quantity == Σ(批次 quantity - used_quantity - blocked_quantity)

所以**任何改动汇总的端点都必须同事务改批次**。这不是洁癖：只改汇总那边会造出
"幽灵库存"——汇总说有 40 片、批次一片没有，一张方都发不出来，而缺药预警看的
恰恰是汇总，于是既发不出药、也不提示采购。实测四个存量端点都犯过这一条：
直接入库 50（汇总 150/批次 100）、采购验收 30（180/100）、调拨 40 到乡镇院
（乡镇院拿到 40 片发不出的幽灵库存）、盘点改 5（5/100）。

没有批号可报的入库（直接入库、采购验收、盘点盘盈）落到兜底批次
`未标批号`；有批号的一律走 `POST /batches`。
"""
from datetime import date, timedelta
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..clock import now_naive
from ..concurrency import (
    add_amount,
    ensure_present,
    insert_if_absent,
    insert_or_conflict,
    take_amount,
)
from ..visibility import assert_obj_org_writable, assert_org_writable, scope_org_list
from ..database import get_db
from ..datetypes import DateStr
from ..deps import (
    get_current_user,
    paginate,
    require_admin,
    require_roles,
    resolve_business_date,
)
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
from .dispense import _claim_batch, _fefo_batches, batch_available

router = APIRouter(prefix="/api/pharmacy", tags=["中心药房"])

#: 没有批号可报的入库落到的兜底批次批号。不是"允许不填批号"，
#: 而是"这笔入库没人报批号"这件事要在台账上看得见——真按效期管理的药
#: 必须走 POST /batches 报批号与效期。
UNSPECIFIED_BATCH_NO = "未标批号"
#: 兜底批次的效期哨兵：含义是"未登记效期"，不是"永不过期"。取远期是因为
#: 空串在 `expire_date >= today` 的字符串比较里永远为假，会让这批药一片发不出去。
UNSPECIFIED_EXPIRE_DATE = "9999-12-31"


def _receive_unspecified(db: Session, org_id: int, drug_code: str, quantity: int) -> None:
    """把没有批号可报的入库量落到兜底批次上（只动批次侧，汇总由调用方加）。

    直接入库、采购验收、盘点盘盈三条路径都没有批号字段。以前它们只加汇总，
    汇总就长出了没有实物对应的数——盘点对不上账，调拨还能把这个数搬到别的
    机构去，变成一片也发不出的幽灵库存。宁可批号是"没填"，也不能没有行。

    建行走 `insert_if_absent` + 原子累加：两笔入库同时落同一个兜底批次，
    先查再插会双双撞唯一约束，两笔药都没入成（§6 的老坑）。
    """
    if quantity <= 0:
        return
    insert_if_absent(
        db,
        DrugBatch(
            org_id=org_id,
            drug_code=drug_code,
            batch_no=UNSPECIFIED_BATCH_NO,
            expire_date=UNSPECIFIED_EXPIRE_DATE,
            quantity=0,
        ),
    )
    batch = ensure_present(
        _batch_of(db, org_id, drug_code, UNSPECIFIED_BATCH_NO), "药品批次"
    )
    add_amount(db, DrugBatch, batch.id, "quantity", quantity)


def _consume_batches(db: Session, org_id: int, drug_code: str, amount: int, today: str) -> int:
    """盘亏：按 FEFO 从批次扣掉 `amount`，返回实扣量。

    可发批次先扣，不够再动已过期/已召回的——盘亏说的是"实物少了"，
    与能不能发是两回事，只肯扣可发批次的话，一库过期药盘亏时两侧就再也对不上。
    每个批次最多扣它的可发余量（`quantity - used_quantity - blocked_quantity`）：
    已召回批次的余量早已记在 blocked 上、本就不在汇总里，不该被盘亏再扣一次。
    """
    rows = (
        db.query(DrugBatch)
        .filter(DrugBatch.org_id == org_id, DrugBatch.drug_code == drug_code)
        .order_by(DrugBatch.expire_date, DrugBatch.id)
        .all()
    )
    dispensable = [b for b in rows if b.status == "normal" and b.expire_date >= today]
    picked = {b.id for b in dispensable}
    rest = [b for b in rows if b.id not in picked]
    taken = 0
    for batch in [*dispensable, *rest]:
        if taken >= amount:
            break
        step = min(batch_available(batch), amount - taken)
        if step <= 0:
            continue
        if _claim_batch(db, batch.id, step, only_normal=False):
            taken += step
    return taken


@router.post("/stocks", response_model=StockOut, dependencies=[Depends(require_admin)])
def upsert_stock(body: StockUpsert, db: Session = Depends(get_db)):
    """入库/建档：已有库存记录则累加数量并更新阈值，同事务落兜底批次。

    这条路径没有批号字段，入库量落到 `未标批号` 批次上——只加汇总不落批次，
    加进去的 50 片就是发不出去的幽灵库存（实测汇总 150 / 批次和 100）。
    """
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
    _receive_unspecified(db, body.org_id, body.drug_code, body.quantity)
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
    """余缺调拨：从调出机构扣减、调入机构增加，**批次跟着一起搬**，全程留痕。

    H3 整改：调出扣减用条件 UPDATE（WHERE quantity >= 需求量）原子执行并校验
    影响行数，并发下不会把库存扣成负数。

    **只搬汇总不搬批次是本模块最贵的那个错**：实测调 40 片到乡镇院，乡镇院汇总
    多了 40、批次一行没有，一张方也发不出来（`_fefo_batches` 找不到批次），
    而缺药预警看汇总，于是那家院既发不出药、也不会被提示缺药。

    调出侧只从**可发批次**（未过期、未召回）按 FEFO 挑：过期与召回批次搬到哪
    都是发不出去的，搬过去只是把幽灵库存换个地方放。因此这里可能出现
    "汇总够、可发批次不够"而拒绝的情况——那说明调出方账上的量本就发不出。
    """
    if body.from_org_id == body.to_org_id:
        raise HTTPException(status_code=422, detail="调出与调入机构不能相同")
    # 只校验**调出方**（ADR-0020）：减少谁的库存就要能写谁。
    # 刻意不校验 `to_org_id`——收货不减少任何人的库存，而"甲把药调给乙"正是本接口的
    # 主用法，两端都要求可写等于只剩 admin/director 能调拨。
    # 这与 ADR-0019（目标池分发两端都校验）**故意不同**：那里的去向是改归属，这里是收货。
    assert_org_writable(db, user, body.from_org_id)
    source = (
        db.query(DrugStock)
        .filter(DrugStock.org_id == body.from_org_id, DrugStock.drug_code == body.drug_code)
        .first()
    )
    if source is None:
        raise HTTPException(status_code=409, detail="调出机构库存不足")
    if db.get(Organization, body.to_org_id) is None:
        raise HTTPException(status_code=404, detail="调入机构不存在")

    # 先按 FEFO 原子占用调出侧批次：判余量与占用同一条 SQL，抢不到就换下一批
    today = resolve_business_date(None).isoformat()
    moved: list[tuple[DrugBatch, int]] = []
    outstanding = body.quantity
    for batch in _fefo_batches(db, body.from_org_id, body.drug_code, today):
        take = min(batch_available(batch), outstanding)
        if take <= 0:
            continue
        if not _claim_batch(db, batch.id, take):
            continue
        moved.append((batch, take))
        outstanding -= take
        if outstanding <= 0:
            break
    if outstanding > 0:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="调出机构可发批次库存不足（过期与已召回批次不调拨，请先补入库）",
        )

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
    # 批次落到调入机构：同批号同效期累加，批号与效期跟着药走，
    # 否则调入方发出去的药回头查不到是哪一批（召回时唯一有用的那个查询）
    for batch, take in moved:
        insert_if_absent(
            db,
            DrugBatch(
                org_id=body.to_org_id,
                drug_code=body.drug_code,
                batch_no=batch.batch_no,
                expire_date=batch.expire_date,
                supplier=batch.supplier,
                quantity=0,
            ),
        )
        target = ensure_present(
            _batch_of(db, body.to_org_id, body.drug_code, batch.batch_no), "药品批次"
        )
        if target.expire_date != batch.expire_date:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"调入机构批号 {batch.batch_no} 已按效期 {target.expire_date} 登记，"
                f"与调出批次 {batch.expire_date} 不一致",
            )
        if target.status != "normal":
            db.rollback()
            raise HTTPException(
                status_code=409, detail=f"调入机构批号 {batch.batch_no} 已召回，不得调入"
            )
        add_amount(db, DrugBatch, target.id, "quantity", take)
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


class PurchaseSuggestionOut(BaseModel):
    """采购建议行。`usage_30d` 唯一产地是 `float(row.usage or 0)`——整数用量也以
    30.0 出参，声明 float 才是原样；库存与建议量是 Integer 列/int() 取整，恒 int
    （契约取证见 tests/test_pharmacy_contract.py）。"""

    drug_code: str
    drug_name: str
    usage_30d: float
    current_stock: int
    suggested_quantity: int


@router.get(
    "/purchase-suggestions",
    response_model=list[PurchaseSuggestionOut],
    dependencies=[Depends(get_current_user)],
)
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
    # 还躺在库房里的量（含退回来的不可发死货）
    remaining: int
    # 退回本批次但已不可发（退回时已召回/已过效期）的量
    blocked_quantity: int
    # 计入可用汇总的余量 = remaining - blocked_quantity。
    # 注意它不等于"今天发得出去"：批次过没过期按效期现算（见 ADR-0013 的口径边界）
    available: int
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
        "blocked_quantity": b.blocked_quantity,
        "available": batch_available(b),
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
    response: Response,
    org_id: int | None = None,
    drug_code: str | None = None,
    batch_no: str | None = None,
    offset: int = 0,
    limit: int = 500,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # 排序补了 `id` 尾键：`expire_date` 只有 index、没有 unique，表级唯一约束是
    # (org_id, drug_code, batch_no) 不含它——同一机构同一药品同日到期的多个批次
    # 是常态。不补尾键翻页会在并列行上重复+漏行。
    q = db.query(DrugBatch)
    q = scope_org_list(db, user, q, DrugBatch, org_id)
    if drug_code:
        q = q.filter(DrugBatch.drug_code == drug_code)
    if batch_no:
        q = q.filter(DrugBatch.batch_no == batch_no)
    rows = paginate(
        q.order_by(
            DrugBatch.org_id, DrugBatch.drug_code, DrugBatch.expire_date, DrugBatch.id
        ),
        response,
        offset,
        limit,
    )
    names = _stock_names(db, rows)
    return [_batch_out(b, names.get((b.org_id, b.drug_code), "")) for b in rows]


@router.get(
    "/batches/expiring",
    response_model=list[ExpiringBatchOut],
    dependencies=[Depends(get_current_user)],
)
def expiring_drug_batches(
    response: Response,
    days: int = Query(default=90, ge=1, le=3650),
    org_id: int | None = None,
    today: str | None = None,
    offset: int = 0,
    limit: int = 500,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """近效期预警：`days` 天内到期（含已过期）且仍有余量的批次。

    学医废滞留预警的口径：剩余天数服务端直接算好、按到期先后排序返回——
    预警页要按紧急程度排，别让前端自己减日期；已过期的负数天并标注，
    不是催人用掉，而是提示按报废流程处理。

    **「仍有余量」这个条件必须下推到 SQL，不能在取完行之后用 Python 过滤。**
    原实现是 `[b for b in q...limit(500).all() if b.quantity - b.used_quantity > 0]`
    ——先取 500 行、再筛掉发完的，于是这个预警在库存量上来之后就基本失效了：
    上面的过滤**只有上界没有下界**（含已过期），而排序是按到期日**升序**，
    所以被 `.limit(500)` 砍掉的恰好是「即将到期」那一端，留下的是
    「早就过期、也早就发完」那一端——**最该预警的批次一条都出不来**，
    页面上和「没有近效期批次」长得一模一样。发完的批次不会删行（只累加
    used_quantity），所以真实库里这 500 行大半是零余量。

    条件下推之后，`X-Total-Count` 与响应体数的才是同一批行。留在外面则两者
    对不上：头按「含已发完」的结果集计数，体只剩有余量的，按
    `len(page) < limit 即最后一页` 翻页的调用方会在第一页就早停——
    那是把静默截断换成了静默早停，比原缺陷更难发现。

    **谓词按原样下推，没有顺手"修正"**：这里判的是 `quantity - used_quantity > 0`，
    而 `DrugBatch` 的 docstring 说可发余量是 `quantity - used_quantity - blocked_quantity`
    （退回本批次但已不可发的量）。两者不一致——本端点可能把"库房里还有、但一片也发不出去"
    的批次也列进预警。**这是另一件事**：改谓词会改返回集合，属口径变更而非分页整改，
    已登记进 `docs/TECH_DEBT.md`，别在这里顺手动它。
    """
    today_d = resolve_business_date(today)
    limit_date = (today_d + timedelta(days=days)).isoformat()
    q = db.query(DrugBatch).filter(
        DrugBatch.expire_date <= limit_date,
        DrugBatch.quantity - DrugBatch.used_quantity > 0,
    )
    q = scope_org_list(db, user, q, DrugBatch, org_id)
    rows = paginate(
        q.order_by(DrugBatch.expire_date, DrugBatch.id), response, offset, limit
    )
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
    """召回批次：召回后不得再发药、不得再入库，**余量同事务退出可用汇总**。

    只翻 status 不动汇总，召回的药就一直被算成有货：实测召回一个余 80 片的批次
    后汇总仍是 160、实际可发只剩 50，采购建议因此不提示采购——缺药预警长期少报。
    余量记到批次的 `blocked_quantity` 上（药还在库房、只是发不出去），
    对账不变式 汇总 == Σ(批次量-已用-退回不可发) 因此仍然成立。

    状态翻转走条件 UPDATE：先判后改的话，两笔召回并发都判定"还没召回"，
    余量会被扣两次。
    """
    batch = db.get(DrugBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    assert_obj_org_writable(db, user, batch)
    recalled = cast(
        CursorResult,
        db.execute(
            update(DrugBatch)
            .where(DrugBatch.id == batch.id, DrugBatch.status == "normal")
            .values(status="recalled", recall_reason=body.reason)
        ),
    )
    if not recalled.rowcount:
        db.rollback()
        raise HTTPException(status_code=409, detail="该批次已召回")
    db.refresh(batch)  # 闸门已抢到，按行的最新值算余量（并发发药可能刚改过已用）
    available = batch_available(batch)
    if available > 0:
        stock = ensure_present(_stock_of(db, batch.org_id, batch.drug_code), "药品库存")
        if not take_amount(db, DrugStock, stock.id, "quantity", available):
            db.rollback()
            raise HTTPException(
                status_code=409, detail="可用汇总不足以扣减该批次余量，台账不符请先盘点"
            )
        add_amount(db, DrugBatch, batch.id, "blocked_quantity", available)
    db.commit()
    db.refresh(batch)
    named = _stock_of(db, batch.org_id, batch.drug_code)
    return _batch_out(batch, named.drug_name if named else "")


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


class SupplierCreatedOut(BaseModel):
    """建档回执：只回三键（与清单行不同形，不硬套一个模型）。"""

    id: int
    name: str
    active: bool


class SupplierOut(BaseModel):
    id: int
    name: str
    contact: str
    license_no: str
    active: bool


@router.post(
    "/suppliers",
    response_model=SupplierCreatedOut,
    status_code=201,
    dependencies=[Depends(require_roles("director", "operator"))],  # 供应商建档
)
def create_supplier(body: SupplierCreate, db: Session = Depends(get_db)):
    if db.query(Supplier).filter(Supplier.name == body.name).first():
        raise HTTPException(status_code=409, detail="供应商已存在")
    supplier = insert_or_conflict(db, Supplier(**body.model_dump()), "供应商已存在")
    return {"id": supplier.id, "name": supplier.name, "active": supplier.active}


@router.get(
    "/suppliers", response_model=list[SupplierOut], dependencies=[Depends(get_current_user)]
)
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


class PurchaseOrderActionOut(BaseModel):
    """申请/审批回执同形：只回单号与最新状态。"""

    id: int
    status: str


class PurchaseReceiveOut(BaseModel):
    """验收回执：药品自动入库后回最新汇总量；非药品（material）该键为 null。

    `stock_quantity` 是"键恒在值可空"，不是条件键——`DrugStock.quantity` 是
    Integer 列，声明成 float 会把 50 印成 50.0。
    """

    id: int
    status: str
    stock_quantity: int | None


class PurchaseOrderOut(BaseModel):
    id: int
    org_id: int
    supplier_id: int
    item_type: str
    item_code: str
    item_name: str
    quantity: int
    status: str


@router.post(
    "/purchase-orders",
    response_model=PurchaseOrderActionOut,
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
    response_model=PurchaseOrderActionOut,
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
    response_model=PurchaseReceiveOut,
    dependencies=[Depends(require_roles("operator", "pharmacist"))],  # 到货验收
)
def receive_purchase(order_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """到货验收：采购单置 received，药品同事务入汇总与兜底批次。

    状态闸门走条件 UPDATE 而不是先判后改：两笔验收同时判定"还没验收"，
    库存就按同一张单加两次——这条路径正是往库存里加数的路径。
    采购单没有批号字段，验收量与直接入库同样落 `未标批号` 批次
    （实测只加汇总时：汇总 180 / 批次和 100）。
    """
    order = db.get(PurchaseOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="采购单不存在")
    assert_obj_org_writable(db, user, order)
    received = cast(
        CursorResult,
        db.execute(
            update(PurchaseOrder)
            .where(PurchaseOrder.id == order.id, PurchaseOrder.status == "approved")
            .values(status="received")
        ),
    )
    if not received.rowcount:
        db.rollback()
        raise HTTPException(status_code=409, detail="仅已审批采购单可验收入库")
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
        _receive_unspecified(db, order.org_id, order.item_code, order.quantity)
        db.flush()
        db.refresh(stock)
        stock_qty = stock.quantity
    db.commit()
    db.refresh(order)
    return {"id": order.id, "status": order.status, "stock_quantity": stock_qty}


@router.get(
    "/purchase-orders",
    response_model=list[PurchaseOrderOut],
    dependencies=[Depends(get_current_user)],
)
def list_purchases(
    response: Response,
    status: str | None = None,
    org_id: int | None = None,
    offset: int = 0,
    limit: int = 200,
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
        for o in paginate(q.order_by(PurchaseOrder.id.desc()), response, offset, limit)
    ]


class StockTakeCreate(BaseModel):
    org_id: int
    drug_code: str = Field(min_length=1)
    actual_qty: int = Field(ge=0)
    note: str = ""


class StockTakeCreatedOut(BaseModel):
    """盘点回执：账面/实盘/差异（Integer 列，盘亏为负 int）。"""

    id: int
    book_qty: int
    actual_qty: int
    diff: int


class StockTakeOut(BaseModel):
    id: int
    org_id: int
    drug_code: str
    book_qty: int
    actual_qty: int
    diff: int
    note: str


@router.post(
    "/stock-takes",
    response_model=StockTakeCreatedOut,
    status_code=201,
    dependencies=[Depends(require_roles("operator", "pharmacist"))],  # 盘点
)
def create_stock_take(
    body: StockTakeCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """盘点：账面调成实盘数，批次侧同事务调平（盘亏 FEFO 核销、盘盈落兜底批次）。"""
    assert_org_writable(db, user, body.org_id)
    stock = (
        db.query(DrugStock)
        .filter(DrugStock.org_id == body.org_id, DrugStock.drug_code == body.drug_code)
        .first()
    )
    if stock is None:
        raise HTTPException(status_code=404, detail="该机构无此药品库存记录")
    book_qty = stock.quantity
    diff = body.actual_qty - book_qty
    # 账实相符要两边一起调：只改汇总的话，盘完账面 5 片、批次仍挂着 100 片，
    # 下一次发药按批次照发不误，这次盘点等于白盘（实测汇总 5 / 批次和 100）。
    # 汇总用条件 UPDATE 钉住盘点时读到的账面数：盘点期间有人发药或入库，
    # 按旧账面写回去会把那笔一起抹掉——宁可让人重盘一次。
    settled = cast(
        CursorResult,
        db.execute(
            update(DrugStock)
            .where(DrugStock.id == stock.id, DrugStock.quantity == book_qty)
            .values(quantity=body.actual_qty)
        ),
    )
    if not settled.rowcount:
        db.rollback()
        raise HTTPException(status_code=409, detail="盘点期间库存发生变动，请重新盘点")
    today = resolve_business_date(None).isoformat()
    if diff > 0:
        # 盘盈没有批号可归，落兜底批次
        _receive_unspecified(db, body.org_id, body.drug_code, diff)
    elif diff < 0:
        if _consume_batches(db, body.org_id, body.drug_code, -diff, today) != -diff:
            db.rollback()
            raise HTTPException(
                status_code=409, detail="批次余量不足以核销盘亏，台账不符请先核对批次"
            )
    take = StockTake(
        org_id=body.org_id,
        drug_code=body.drug_code,
        book_qty=book_qty,
        actual_qty=body.actual_qty,
        diff=diff,
        note=body.note,
        created_by=user.id,
    )
    db.add(take)
    db.commit()
    return {
        "id": take.id,
        "book_qty": take.book_qty,
        "actual_qty": take.actual_qty,
        "diff": take.diff,
    }


@router.get(
    "/stock-takes", response_model=list[StockTakeOut], dependencies=[Depends(get_current_user)]
)
def list_stock_takes(
    response: Response,
    org_id: int | None = None,
    offset: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
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
        for t in paginate(q.order_by(StockTake.id.desc()), response, offset, limit)
    ]
