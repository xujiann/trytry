"""西药发药：审方通过的处方按批次 FEFO 发药、退药冲销、发药记录查询。

三条口径，都写进代码而不是留在文档里：

1. **只发审过的方**。处方状态机（routers/prescriptions.py）里 auto_passed
   （系统审通过）与 approved（药师审通过）可发；pending_review / rejected 拒发。
2. **FEFO（先到效期先出）**：同药多批次按 expire_date 升序扣，过期与已召回
   批次一律不发。发药量按处方明细 日剂量×天数 向上取整——与采购建议
   （pharmacy.purchase_suggestions）同一口径，两处对不上账就没法盘。
3. **同一事务**写发药记录、扣批次（`_claim_batch` 原子占用）、扣 DrugStock 汇总
   （take_amount 原子扣减），保住"汇总 == Σ(批次量-已用-退回不可发)"的对账不变式；
   退药走冲销：记录置 reversed 并回补两侧台账，不删行。
4. **冲销的状态闸门必须原子**。回补动作本身是原子的，闸门却曾是先判后改：
   8 路并发冲销同一张发药单，实测 3 笔同时通过，把别的处方发出的药也一起
   退了回来（可用汇总 440→470，凭空多出 20 片）。闸门改条件 UPDATE 并**放在
   回补之前**——判定与翻转同一条 SQL，抢不到的拿 409。
"""
import math
from typing import cast, overload

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import ColumnElement, Subquery, and_, exists, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Query, Session

from ..concurrency import add_amount, ensure_present, take_amount
from ..database import get_db
from ..deps import get_current_user, paginate, require_roles, resolve_business_date
from ..models import (
    DispenseItem,
    DispenseRecord,
    DrugBatch,
    DrugStock,
    Organization,
    Prescription,
    User,
    utcnow,
)
from ..visibility import assert_org_writable, scope_org_list
from ..ws import manager
from ..texttypes import NON_BLANK
from .prescriptions import PRESCRIPTION_STATUS_NAMES

router = APIRouter(prefix="/api/dispense", tags=["西药发药"], dependencies=[Depends(get_current_user)])

#: 可发药的处方状态：系统审通过 / 药师审通过（见 prescriptions.py 状态机）
DISPENSABLE_STATUSES = ("auto_passed", "approved")


def prescription_not_reversed() -> ColumnElement[bool]:
    """处方没有被退药冲销——统计「用了多少药」的口径（P2-624）。

    退药冲销只把发药记录置 reversed、药回库房，处方表不动（它的状态是审方结论）；冲销后的处方不能再发，确需再发的
    开新处方（见 `reverse_dispense`）。按处方状态筛的用量统计因此把退掉的那张照算、重开的那张再算一遍——抗菌药物使用
    强度、采购建议、用药画像、用药地图都接这一句，与批号追溯排除冲销（`pharmacy.batch_dispense_trace`）同一个口径。
    没发过药的处方照算（开了还没取，与药师待审的照算同理：统计现算，之后退掉自然就掉出去）。
    """
    return ~exists().where(DispenseRecord.prescription_id == Prescription.id, DispenseRecord.status == "reversed")


class DispenseCreate(BaseModel):
    prescription_id: int
    # 缺省按开方机构发药；中心药房代发成员机构时显式给出
    org_id: int | None = None


class ReverseIn(BaseModel):
    reason: str = Field(min_length=1, max_length=256, pattern=NON_BLANK)


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


def _dispense_items(db: Session, records: list[DispenseRecord]) -> dict[int, list]:
    """一页发药记录的明细（连同批号），按页一次 IN 取齐、按记录分好（P2-1157）：清单原先逐条记录再查一次明细。
    先按明细编号排好再分，每条记录内的先后与逐条查时一样。"""
    grouped: dict[int, list] = {}
    if records:
        for row in (
            db.query(DispenseItem, DrugBatch)
            .join(DrugBatch, DispenseItem.batch_id == DrugBatch.id)
            .filter(DispenseItem.dispense_id.in_([r.id for r in records]))
            .order_by(DispenseItem.id)
        ):
            grouped.setdefault(row[0].dispense_id, []).append(row)
    return grouped


def _dispense_out(db: Session, record: DispenseRecord, items: list | None = None) -> dict:
    """`items` 是清单按页取齐的这条记录的明细（`_dispense_items`）；单条出参不给，照旧现查。"""
    if items is None:
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


def _claim_batch(db: Session, batch_id: int, step: int, *, only_normal: bool = True) -> bool:
    """原子占用批次余量：判"够不够"与占用压进同一条 SQL，返回是否占到。

    没有直接用 `concurrency.claim_quota`，因为可发余量是三列算出来的
    （累计入库 - 已出库 - 退回后不可发），而 claim_quota 只认两列；
    `only_normal` 顺带把召回也压进同一条 SQL——批次在"被选中"与"被占用"
    之间被召回，照两列的写法照样能占到，占完这一批的可发余量就成了负数。

    盘亏要能扣到已召回/已过期批次（实物少了与能不能发是两回事），
    那条路径传 `only_normal=False`；余量口径仍是三列。
    """
    conditions = [
        DrugBatch.id == batch_id,
        DrugBatch.used_quantity + DrugBatch.blocked_quantity + step <= DrugBatch.quantity,
    ]
    if only_normal:
        conditions.append(DrugBatch.status == "normal")
    result = cast(
        CursorResult,
        db.execute(
            update(DrugBatch)
            .where(*conditions)
            .values(used_quantity=DrugBatch.used_quantity + step)
        ),
    )
    return bool(result.rowcount)


@overload
def batch_available(batch: DrugBatch) -> int: ...
@overload
def batch_available(batch: type[DrugBatch]) -> ColumnElement[int]: ...
def batch_available(batch: DrugBatch | type[DrugBatch]) -> int | ColumnElement[int]:
    """批次可发余量：累计入库 - 已出库 - 退回后不可发。

    不是 `quantity - used_quantity`——那是"还躺在库房里的量"，含退回来的死货。

    传批次行得到这一批的数；传 `DrugBatch` 类本身得到同一个算式的 SQL 表达式（P2-1250）：读侧现算可发量
    （`dispensable_by_drug`）要在库里按 (机构, 药品) 求和，算式仍只写在这一处（单一判定源守卫第 6 条）。
    """
    return batch.quantity - batch.used_quantity - batch.blocked_quantity


def dispensable_clause(today: str) -> ColumnElement[bool]:
    """批次此刻可发：未召回、未过效期（P2-1250 从 `_fefo_batches` 原样抽出）。

    发药与调拨挑批次（`_fefo_batches`）、读侧现算可发量（`dispensable_by_drug`）共用这一句。缺药预警、待办、驾驶舱、
    供应风险、采购建议原先读汇总 `DrugStock.quantity`，汇总里留着静置过期的量（ADR-0013「已知边界」：过期没有事件
    可挂），于是发药这边 409「可发批次库存不足」、预警那边照旧当有货——两边的谓词各写一份，改了一边另一边不会跟。
    逐行的版本是 `batch_dispensable`，两处改一处要同改另一处。
    """
    return and_(DrugBatch.status == "normal", DrugBatch.expire_date >= today)


def batch_dispensable(batch: DrugBatch, today: str) -> int:
    """这一批此刻能发多少（`dispensable_clause` 的逐行版，P2-1250）：已召回、已过效期、余量不为正的一律 0。

    批次台账原先只给 `available`（计入可用汇总的余量），过了效期的批次照印「状态 正常 / 可用 100」，发药却一片不取它。
    """
    available = batch_available(batch)
    if batch.status != "normal" or batch.expire_date < today or available <= 0:
        return 0
    return available


def dispensable_by_drug() -> Subquery:
    """各 (机构, 药品) 此刻的可发量：可发批次余量之和，一条 SQL 按 (org_id, drug_code) 聚合（P2-1250）。

    批次的筛法与 `_fefo_batches` 同一份（`dispensable_clause` + 余量为正）：发药按 FEFO 取得到的，就是这里数到的。
    照 ADR-0013 写好的下一步在读侧现算——不动汇总、不动对账不变式、不加定时任务：汇总回答「账上还有多少可用」，
    这里回答「此刻能发多少」。「此刻」取业务日，与发药同源。没有可发批次的 (机构, 药品) 不在结果里，左连后按 0 算。
    """
    available = batch_available(DrugBatch)
    return (
        select(DrugBatch.org_id, DrugBatch.drug_code, func.sum(available).label("dispensable"))
        .where(dispensable_clause(resolve_business_date(None).isoformat()), available > 0)
        .group_by(DrugBatch.org_id, DrugBatch.drug_code)
        .subquery("dispensable_by_drug")
    )


def q_dispensable_shortage(db: Session) -> Query:
    """可发量低于阈值的库存行（缺药口径的唯一来源，P2-1250），每行 `(DrugStock, 可发量)`。

    `DrugStock` 左连 `dispensable_by_drug`、没有可发批次的按 0 算，比的是 `可发量 < 阈值`；阈值 0（没配预警）照旧永不报。
    缺药预警（`pharmacy.stock_alerts`）、待办（`todos._stock_alerts`）、驾驶舱（`metrics.q_stock_alerts` 委托这里）、
    供应风险（`medication.supply_risk`）都走它。原先四处各比 `DrugStock.quantity < threshold`：批次过了效期，汇总一片
    不少，四处都当有货，发药同时 409。没有过期量时可发量就等于汇总（对账不变式），结果与原先一致。
    """
    sub = dispensable_by_drug()
    dispensable = func.coalesce(sub.c.dispensable, 0)
    return (
        db.query(DrugStock, dispensable.label("dispensable"))
        .outerjoin(sub, and_(sub.c.org_id == DrugStock.org_id, sub.c.drug_code == DrugStock.drug_code))
        .filter(dispensable < DrugStock.threshold)
    )


def broadcast_shortage(stock: DrugStock) -> None:
    """缺药预警定向广播：只推给缺药机构的在线用户与 admin / director（M-2）。调拨、发药、召回三处共用（P2-504）。"""
    manager.broadcast(
        {
            "type": "stock_shortage",
            "org_id": stock.org_id,
            "drug_code": stock.drug_code,
            "drug_name": stock.drug_name,
            "quantity": stock.quantity,
            "threshold": stock.threshold,
        },
        target_org_id=stock.org_id,
    )


def broadcast_if_crossed(stock: DrugStock, taken: int) -> None:
    """这一笔扣减把库存从阈值上扣到阈值下，才广播缺药预警（P2-504）。调用前先 `db.refresh(stock)` 拿提交后的量。

    只认「跨过阈值」这一下：发药一天几十张方，库存已经低于阈值之后每发一张都推一遍，等于把预警刷成噪音。
    阈值 0（没配预警）永不触发。
    """
    if stock.quantity < stock.threshold <= stock.quantity + taken:
        broadcast_shortage(stock)


def _fefo_batches(db: Session, org_id: int, drug_code: str, today: str) -> list[DrugBatch]:
    """可发批次，按 FEFO 排序：未过期、未召回、仍有余量，先到效期先出。

    调拨（pharmacy.transfer_stock）复用这一条口径挑批次：调出的必须是**能发的**
    批次，否则调入方拿到的是一片也发不出的幽灵库存。
    「可发」的谓词与读侧现算可发量共用 `dispensable_clause`（P2-1250）。
    """
    rows = (
        db.query(DrugBatch)
        .filter(
            DrugBatch.org_id == org_id,
            DrugBatch.drug_code == drug_code,
            dispensable_clause(today),
        )
        .order_by(DrugBatch.expire_date, DrugBatch.id)
        .all()
    )
    return [b for b in rows if batch_available(b) > 0]


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
    批次占用走 `_claim_batch`（判够与扣减同一条 SQL），两张方同时抢同一批
    不会超扣——抢不到的换下一批，全都抢不到才整体回滚报库存不足。
    """
    prescription = db.get(Prescription, body.prescription_id)
    if prescription is None:
        raise HTTPException(status_code=404, detail="处方不存在")
    if prescription.status not in DISPENSABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"处方当前状态 {PRESCRIPTION_STATUS_NAMES.get(prescription.status, prescription.status)} 不可发药（须审方通过）",
        )
    org_id = body.org_id if body.org_id is not None else prescription.org_id
    assert_org_writable(db, user, org_id)
    # 全域角色过守卫不查机构在不在（P2-411，同 P2-169）：填错一位撞外键，原先被下面为唯一约束写的 except 翻成
    # 「该处方已发药」
    if db.get(Organization, org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
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
    taken_stocks: list[tuple[DrugStock, int]] = []   # 每个品种扣了汇总多少：提交后判要不要发缺药预警
    for rx_item in rx_items:
        need = _required_quantity(rx_item.daily_dose, rx_item.days)
        taken_total = 0
        # 并发下批次可能被别的发药请求抢走：重扫最多 3 轮，仍不够即整体回滚
        for _ in range(3):
            for batch in _fefo_batches(db, org_id, rx_item.drug_code, today):
                take = min(batch_available(batch), need)
                if take <= 0:
                    continue
                # 原子占用：判余量、判未召回与占用同一条 SQL，抢不到就换下一批
                if not _claim_batch(db, batch.id, take):
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
        # 同事务扣汇总，保住 汇总==Σ(批次量-已用-退回不可发) 的对账不变式
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
        taken_stocks.append((stock, taken_total))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该处方已发药，不可重复发药") from None
    # 发药把库存扣到阈值以下，同样秒级推缺药预警（P2-504）：原先只有调拨推，库存最常见的下降途径——发药——从不推，
    # 「WebSocket 缺药预警秒级广播」要等有人打开缺药清单才看得到
    for stock, taken in taken_stocks:
        db.refresh(stock)
        broadcast_if_crossed(stock, taken)
    db.refresh(record)
    return _dispense_out(db, record)


@router.get("", response_model=list[DispenseOut])
def list_dispenses(
    response: Response,
    prescription_id: int | None = None,
    org_id: int | None = None,
    status: str | None = None,
    offset: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(DispenseRecord)
    q = scope_org_list(db, user, q, DispenseRecord, org_id)
    if prescription_id is not None:
        q = q.filter(DispenseRecord.prescription_id == prescription_id)
    if status:
        q = q.filter(DispenseRecord.status == status)
    rows = paginate(q.order_by(DispenseRecord.id.desc()), response, offset, limit)
    items = _dispense_items(db, rows)
    return [_dispense_out(db, r, items.get(r.id, [])) for r in rows]


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

    两条口径写在代码里而不是留在文档里：

    1. **闸门先于回补，且必须原子**。`if record.status != "dispensed"` 是先判后改：
       8 路并发冲销同一张单，实测 3 笔同时判定"还没冲销"，各回补一次，
       可用汇总 440→470、批次已用 60→30——把别的处方发出的药也退了回来。
       条件 UPDATE 把判定与翻转压进一条 SQL，抢不到的拿 409。
    2. **退回不可发批次的量不回可用汇总**。批次已召回或已过效期时，药确实回到了
       库房，却一片也发不出去；回补进汇总就等于凭空多出可发库存，缺药预警与
       采购建议看的正是汇总，于是既发不出药也不提示采购。这笔量记到批次的
       `blocked_quantity` 上——只有显式记下来，对账不变式才仍然算得出来。
    """
    record = db.get(DispenseRecord, dispense_id)
    if record is None:
        raise HTTPException(status_code=404, detail="发药记录不存在")
    assert_org_writable(db, user, record.org_id)
    # 状态闸门：判定与翻转同一条 SQL，且放在任何回补动作之前
    flipped = cast(
        CursorResult,
        db.execute(
            update(DispenseRecord)
            .where(DispenseRecord.id == record.id, DispenseRecord.status == "dispensed")
            .values(
                status="reversed",
                reverse_reason=body.reason,
                reversed_by=user.id,
                reversed_at=utcnow(),
            )
        ),
    )
    if not flipped.rowcount:
        db.rollback()
        raise HTTPException(status_code=409, detail="该发药记录已冲销，不可重复退药")
    today = resolve_business_date(None).isoformat()
    items = db.query(DispenseItem).filter(DispenseItem.dispense_id == record.id).all()
    for item in items:
        batch = ensure_present(db.get(DrugBatch, item.batch_id), "药品批次")
        # 回补批次：used_quantity 原子减（不够减说明台账已不符，拦下别越改越乱）
        if not take_amount(db, DrugBatch, item.batch_id, "used_quantity", item.quantity):
            db.rollback()
            raise HTTPException(
                status_code=409, detail=f"批次 {item.batch_id} 台账不符，无法冲销，请先盘点"
            )
        # 回补去向按行锁到手之后的批次状态判（P2-402）：上面取批次是锁外读的，读到「正常」之后别人刚召回并提交——召回已把
        # 余量整笔转进不可发——这里再按旧状态把这笔加回可用汇总，召回的批次上就又长出可发余量：一片也发不出，汇总却当有货，
        # 正是上面第 2 条要防的。take_amount 已拿到这一行的锁，重读一次就是召回之后的状态（同 recall_batch 抢到闸门后重读）
        db.refresh(batch)
        if batch.status == "normal" and batch.expire_date >= today:
            stock = ensure_present(
                db.query(DrugStock)
                .filter(
                    DrugStock.org_id == record.org_id,
                    DrugStock.drug_code == item.drug_code,
                )
                .first(),
                "药品库存",
            )
            add_amount(db, DrugStock, stock.id, "quantity", item.quantity)
        else:
            # 已召回/已过期批次：药在库房、发不出去——只回批次不回可用汇总
            add_amount(db, DrugBatch, item.batch_id, "blocked_quantity", item.quantity)
    db.commit()
    db.refresh(record)
    return _dispense_out(db, record)
