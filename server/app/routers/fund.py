"""医保基金总额付费：池子 — 预付 — 周期预结 — 年终清算 — 结余分配。

绩效考核已经算得很细，但**算完之后没有钱跟着走**——考核结果只是一张排名表。
这个模块补上的就是那条出口：把绩效得分变成分配依据。

四条口径写死在实现里，并随接口返回一起给出：

1. **分配依据是冻结的绩效得分快照**。指标权重随时可调，若分钱时"当下重算"，
   等于分完钱还能改分。
2. **超支不自动扣减**。算出来、标出来，处置方式（分摊/挂账/不处理）由人选，
   平台不替政策做决定。
3. **预结不产生资金流**。真正的钱只在预付与清算两处流动，故三者分表存放——
   年终对不上账时，必须能一眼分清"这笔是账面数还是真给了钱"。
4. **分配办法用公式引擎表达**，返回份额权重再归一化。各县办法不同且逐年调整，
   绝不写死进代码。

整个模块可不启用：不建池子的县完全感觉不到它存在，既有结算流程不受影响。
"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..datetypes import OptionalDateStr
from ..concurrency import insert_or_conflict, upsert_unique
from ..database import get_db
from ..deps import get_current_user, require_roles, resolve_org_scope
from ..formula import FormulaError, evaluate, validate
from ..models import (
    FundDistribution,
    FundPeriod,
    FundPool,
    FundPrepayment,
    FundSettlement,
    OrgGroup,
    Organization,
    Settlement,
    User,
    utcnow,
)
from .performance import org_scorecards

router = APIRouter(
    prefix="/api/fund",
    tags=["医保基金总额付费"],
    dependencies=[Depends(get_current_user)],
)

# 分配公式可用变量。刻意保持极少——变量越多，各县越容易写出没人能复核的表达式。
FORMULA_VARIABLES = {
    "score": "该机构绩效得分（0-100）",
    "rank": "绩效名次（1 为最高）",
    "org_count": "参与分配的机构数",
}

OVERRUN_ACTIONS = {"none": "不处理（仅记录）", "share": "按分配公式分摊", "carry": "挂账结转"}


# ============================================================ 基金池


class PoolIn(BaseModel):
    year: int = Field(ge=2000, le=2100)
    insurance_type: str = Field(default="resident", pattern="^(resident|employee)$")
    org_group_id: int | None = None
    total_amount: float = Field(ge=0)
    prepay_ratio_pct: float = Field(default=0, ge=0, le=100)
    note: str = Field(default="", max_length=256)


class PoolUpdate(BaseModel):
    total_amount: float | None = Field(default=None, ge=0)
    prepay_ratio_pct: float | None = Field(default=None, ge=0, le=100)
    note: str | None = None
    status: str | None = Field(default=None, pattern="^(active|closed)$")


def _pool_out(pool: FundPool, db: Session) -> dict:
    prepaid = (
        db.query(func.coalesce(func.sum(FundPrepayment.amount), 0.0))
        .filter(FundPrepayment.pool_id == pool.id)
        .scalar()
        or 0.0
    )
    accrued = (
        db.query(func.coalesce(func.sum(FundPeriod.actual_amount), 0.0))
        .filter(FundPeriod.pool_id == pool.id)
        .scalar()
        or 0.0
    )
    return {
        "id": pool.id,
        "year": pool.year,
        "insurance_type": pool.insurance_type,
        "org_group_id": pool.org_group_id,
        "total_amount": round(pool.total_amount, 2),
        "prepay_ratio_pct": pool.prepay_ratio_pct,
        "planned_prepay": round(pool.total_amount * pool.prepay_ratio_pct / 100, 2),
        "prepaid_amount": round(float(prepaid), 2),
        "accrued_expense": round(float(accrued), 2),
        # 账面结余：筹资 − 已归集发生额。这不是清算结果，清算另有单据。
        "book_balance": round(pool.total_amount - float(accrued), 2),
        "status": pool.status,
        "note": pool.note,
    }


@router.post("/pools", status_code=201, dependencies=[Depends(require_roles("director"))])
def create_pool(
    body: PoolIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    if body.org_group_id is not None and db.get(OrgGroup, body.org_group_id) is None:
        raise HTTPException(status_code=404, detail="机构分组不存在")
    # 应用层查重只为给出可读的提示；**真正兜底的是数据库**——全域池由部分唯一索引
    # `uq_fund_pool_global`（org_group_id IS NULL）拦住，分组池由 uq_fund_pool_scope
    # 拦住。check-then-act 中间有竞态窗口，6 线程并发实测建出过 2 个池。
    duplicate = (
        db.query(FundPool)
        .filter(
            FundPool.year == body.year,
            FundPool.insurance_type == body.insurance_type,
            FundPool.org_group_id.is_(None)
            if body.org_group_id is None
            else FundPool.org_group_id == body.org_group_id,
        )
        .first()
    )
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="同年度同险种同范围的基金池已存在")
    pool = FundPool(**body.model_dump(), created_by=user.id)
    db.add(pool)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="同年度同险种同范围的基金池已存在") from None
    return _pool_out(pool, db)


@router.get("/pools")
def list_pools(
    year: int | None = None, status: str | None = None, db: Session = Depends(get_db)
):
    query = db.query(FundPool)
    if year is not None:
        query = query.filter(FundPool.year == year)
    if status:
        query = query.filter(FundPool.status == status)
    return [_pool_out(p, db) for p in query.order_by(FundPool.id.desc()).limit(200).all()]


@router.patch("/pools/{pool_id}", dependencies=[Depends(require_roles("director"))])
def update_pool(pool_id: int, body: PoolUpdate, db: Session = Depends(get_db)):
    pool = _pool(db, pool_id)
    if pool.status == "settled":
        raise HTTPException(status_code=409, detail="已清算的基金池不可修改")
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(pool, field, value)
    db.commit()
    return _pool_out(pool, db)


# ============================================================ 预付（真实资金流）


class PrepaymentIn(BaseModel):
    batch_no: str = Field(default="", max_length=32)
    amount: float = Field(gt=0)
    paid_date: OptionalDateStr = ""
    note: str = Field(default="", max_length=256)


@router.post(
    "/pools/{pool_id}/prepayments",
    status_code=201,
    dependencies=[Depends(require_roles("director"))],
)
def add_prepayment(
    pool_id: int,
    body: PrepaymentIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """登记预付批次。**超出计划预付额只警告不拦截**——现实中确有追加预拨，
    平台不该因为一个比例参数就挡住真实发生的资金流。"""
    pool = _pool(db, pool_id)
    if pool.status != "active":
        raise HTTPException(status_code=409, detail=f"基金池状态为 {pool.status}，不可再预付")
    db.add(FundPrepayment(pool_id=pool_id, created_by=user.id, **body.model_dump()))
    db.commit()
    out = _pool_out(pool, db)
    if out["planned_prepay"] and out["prepaid_amount"] > out["planned_prepay"]:
        out["warning"] = (
            f"累计预付 {out['prepaid_amount']} 元已超过计划预付额 {out['planned_prepay']} 元，"
            "请确认是否为追加预拨"
        )
    return out


@router.get("/pools/{pool_id}/prepayments")
def list_prepayments(pool_id: int, db: Session = Depends(get_db)):
    _pool(db, pool_id)
    rows = (
        db.query(FundPrepayment)
        .filter(FundPrepayment.pool_id == pool_id)
        .order_by(FundPrepayment.id)
        .all()
    )
    return [
        {"id": r.id, "batch_no": r.batch_no, "amount": r.amount,
         "paid_date": r.paid_date, "note": r.note}
        for r in rows
    ]


# ============================================================ 周期预结（账面）


class PeriodIn(BaseModel):
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    # 留空即由系统按结算单归集；给值则视为人工核定，覆盖系统数
    actual_amount: float | None = Field(default=None, ge=0)
    note: str = Field(default="", max_length=256)


def _collect_expense(db: Session, pool: FundPool, period: str) -> float:
    """按结算单归集当期医保支付额。

    范围跟着池子走：绑了分组的池子只算该分组内机构，没绑的算全域。
    """
    start = datetime.strptime(period + "-01", "%Y-%m-%d")
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    query = db.query(func.coalesce(func.sum(Settlement.insurance_pay), 0.0)).filter(
        Settlement.created_at >= start, Settlement.created_at < end
    )
    scope = resolve_org_scope(db, pool.org_group_id, None)
    if scope is not None:
        query = query.filter(Settlement.org_id.in_(scope))
    return float(query.scalar() or 0.0)


@router.post(
    "/pools/{pool_id}/periods", status_code=201, dependencies=[Depends(require_roles("director"))]
)
def close_period(pool_id: int, body: PeriodIn, db: Session = Depends(get_db)):
    """月度预结：归集当期发生额，与预付对冲看进度。

    **不产生资金流**——这只是账面数。重复预结同一期会覆盖（月中核对反复跑是常态），
    但会把来源标为人工还是系统，便于事后分辨。
    """
    pool = _pool(db, pool_id)
    if pool.status != "active":
        raise HTTPException(status_code=409, detail=f"基金池状态为 {pool.status}，不可再预结")
    amount = (
        body.actual_amount
        if body.actual_amount is not None
        else _collect_expense(db, pool, body.period)
    )
    source = "manual" if body.actual_amount is not None else "auto"
    upsert_unique(
        db,
        FundPeriod,
        keys={"pool_id": pool_id, "period": body.period},
        values={"actual_amount": amount, "source": source, "note": body.note},
    )
    out = _pool_out(pool, db)
    return {
        "period": body.period,
        "actual_amount": round(amount, 2),
        "source": source,
        "cash_flow": False,
        "note": "预结仅为账面对冲，不产生资金流；真实资金在预付与年终清算两处",
        "pool": out,
    }


@router.get("/pools/{pool_id}/periods")
def list_periods(pool_id: int, db: Session = Depends(get_db)):
    _pool(db, pool_id)
    rows = (
        db.query(FundPeriod)
        .filter(FundPeriod.pool_id == pool_id)
        .order_by(FundPeriod.period)
        .all()
    )
    return [
        {"period": r.period, "actual_amount": round(r.actual_amount, 2),
         "source": r.source, "note": r.note}
        for r in rows
    ]


# ============================================================ 年终清算与分配


class SettleIn(BaseModel):
    # 留空即取各期预结之和；给值则以人工核定为准
    total_expense: float | None = Field(default=None, ge=0)
    overrun_action: str = Field(default="none", pattern="^(none|share|carry)$")
    note: str = Field(default="", max_length=256)


class DistributeIn(BaseModel):
    """分配参数。公式返回**份额权重**，平台归一化后乘结余额。"""

    formula_expr: str = Field(default="score", max_length=256)
    # 绩效计分口径参数，与 /api/performance/orgs 一致，记进快照便于复现
    volume_cap: int = Field(default=5, ge=1)
    include_auto_passed: bool = True


@router.get("/formula-variables")
def formula_variables():
    """分配公式可用变量。变量刻意保持极少——多了就没人能复核了。"""
    return {
        "variables": [{"name": k, "desc": v} for k, v in FORMULA_VARIABLES.items()],
        "note": "公式返回份额权重（非金额），平台按各机构权重占比归一化后乘结余额。"
                "例：均分写 1，按绩效写 score，拉开差距写 score ** 2",
        "examples": ["1", "score", "score ** 2", "0.5 + score / 100"],
    }


@router.post(
    "/pools/{pool_id}/settle", status_code=201, dependencies=[Depends(require_roles("director"))]
)
def settle(
    pool_id: int,
    body: SettleIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """年终清算：结出结余或超支，一个池子只能清算一次。

    超支（balance < 0）**不自动扣减任何机构**，只记录处置选择。
    """
    pool = _pool(db, pool_id)
    if pool.status != "active":
        raise HTTPException(status_code=409, detail=f"基金池状态为 {pool.status}，不可清算")
    expense = (
        body.total_expense
        if body.total_expense is not None
        else float(
            db.query(func.coalesce(func.sum(FundPeriod.actual_amount), 0.0))
            .filter(FundPeriod.pool_id == pool_id)
            .scalar()
            or 0.0
        )
    )
    settlement = FundSettlement(
        pool_id=pool_id,
        total_income=pool.total_amount,
        total_expense=expense,
        balance=round(pool.total_amount - expense, 2),
        overrun_action=body.overrun_action,
        created_by=user.id,
    )
    # 先改池状态再插清算单：insert_or_conflict 内部 commit，两者同一次提交，
    # 撞约束时一起回滚（D-1 的教训）。并发重复清算撞的正是 pool_id 唯一约束。
    pool.status = "settled"
    settlement = insert_or_conflict(db, settlement, "该基金池已清算")
    return _settlement_out(settlement, db)


@router.get("/pools/{pool_id}/settlement")
def get_settlement(pool_id: int, db: Session = Depends(get_db)):
    settlement = (
        db.query(FundSettlement).filter(FundSettlement.pool_id == pool_id).first()
    )
    if settlement is None:
        raise HTTPException(status_code=404, detail="该基金池尚未清算")
    return _settlement_out(settlement, db)


@router.post(
    "/pools/{pool_id}/distribute", dependencies=[Depends(require_roles("director"))]
)
def distribute(pool_id: int, body: DistributeIn, db: Session = Depends(get_db)):
    """按公式分配结余，并**冻结**当次绩效得分快照。

    超支时拒绝分配：没有结余可分，此时该做的是按 `overrun_action` 处置超支，
    而不是分一笔不存在的钱。
    """
    pool = _pool(db, pool_id)
    settlement = db.query(FundSettlement).filter(FundSettlement.pool_id == pool_id).first()
    if settlement is None:
        raise HTTPException(status_code=409, detail="请先完成年终清算")
    if settlement.balance <= 0:
        raise HTTPException(
            status_code=409,
            detail=f"本池超支 {abs(settlement.balance)} 元，无结余可分配；"
                   f"超支处置方式为「{OVERRUN_ACTIONS.get(settlement.overrun_action)}」",
        )
    try:
        validate(body.formula_expr, set(FORMULA_VARIABLES))
    except FormulaError as exc:
        raise HTTPException(status_code=422, detail=f"分配公式不合法：{exc}") from None

    # 取当次绩效得分。范围跟着池子走：绑了分组的池子只在该分组内分。
    #
    # `period` 必须显式传 `pool.year`：绩效评分自 2026-08 起是**周期口径**，
    # 缺省算当年。而基金池结算通常发生在次年年初——不传就会拿"次年至今"的
    # 近乎空白的分数去分上一年度的钱，轻则份额全错，重则所有权重为 0 直接 422。
    scorecards = org_scorecards(
        period=str(pool.year),
        volume_cap=body.volume_cap,
        include_auto_passed=body.include_auto_passed,
        group_id=pool.org_group_id,
        db=db,
    )["scorecards"]
    if not scorecards:
        raise HTTPException(status_code=409, detail="范围内没有可参与分配的机构")

    org_count = len(scorecards)
    weights = []
    for rank, card in enumerate(scorecards, start=1):
        try:
            weight = evaluate(
                body.formula_expr,
                {"score": card["score"], "rank": float(rank), "org_count": float(org_count)},
            )
        except FormulaError as exc:
            raise HTTPException(status_code=422, detail=f"分配公式求值失败：{exc}") from None
        # 负权重没有意义（分负数的钱），按 0 处理并在返回里说明
        weights.append((card, rank, max(weight, 0.0)))
    weight_sum = sum(w for _, _, w in weights)
    if weight_sum <= 0:
        raise HTTPException(
            status_code=422,
            detail="所有机构的份额权重都为 0，无法归一化分配。"
                   "常见原因是本期绩效得分普遍为 0（业务数据尚未产生），"
                   "此时应改用与得分无关的公式（如均分写 1）",
        )

    # 重新分配即覆盖上一次结果——分配方案改了要能重来，但快照随之刷新
    db.query(FundDistribution).filter(
        FundDistribution.settlement_id == settlement.id
    ).delete(synchronize_session=False)

    # 按份额分钱要保证**分出去的总额分毫等于结余**。逐户各自
    # `round(balance*share, 2)` 会各丢/各多几分钱，合计对不上结余——
    # 实测 100 元 3 户均分分出 99.99，凭空少一分。分配的是医共体真金白银的
    # 结余，账对不上就是事故。
    #
    # 用最大余数法（Hamilton）在**分**上分配：先给每户向下取整到分，
    # 剩下的零头一分一分地按小数余数从大到小补给各户，合计恰好等于结余。
    total_cents = round(settlement.balance * 100)
    raw = [(settlement.balance * (w / weight_sum)) * 100 for _, _, w in weights]
    floors = [int(x) for x in raw]  # 向下取整到分（金额非负，int() 即 floor）
    remainder = total_cents - sum(floors)  # 待补的零头分数，∈ [0, 户数)
    # 小数余数大的优先补一分；同余数按原顺序稳定，可复现
    order = sorted(range(len(weights)), key=lambda i: raw[i] - floors[i], reverse=True)
    cents = floors[:]
    for i in order[: max(remainder, 0)]:
        cents[i] += 1

    rows = []
    for (card, rank, weight), amount_cents in zip(weights, cents):
        share = weight / weight_sum
        row = FundDistribution(
            settlement_id=settlement.id,
            org_id=card["org_id"],
            score=card["score"],
            score_detail=card["detail"],
            weight=round(weight, 6),
            share_pct=round(share * 100, 4),
            amount=amount_cents / 100,
        )
        db.add(row)
        rows.append((card, rank, row))
    settlement.formula_expr = body.formula_expr
    settlement.score_basis = (
        f"volume_cap={body.volume_cap},include_auto_passed={body.include_auto_passed},"
        f"at={utcnow().strftime('%Y-%m-%d %H:%M')}"
    )
    db.commit()
    return _settlement_out(settlement, db)


@router.get("/pools/{pool_id}/distributions")
def list_distributions(pool_id: int, db: Session = Depends(get_db)):
    settlement = db.query(FundSettlement).filter(FundSettlement.pool_id == pool_id).first()
    if settlement is None:
        raise HTTPException(status_code=404, detail="该基金池尚未清算")
    return _distribution_rows(settlement, db)


def _distribution_rows(settlement: FundSettlement, db: Session) -> list[dict]:
    rows = (
        db.query(FundDistribution)
        .filter(FundDistribution.settlement_id == settlement.id)
        .order_by(FundDistribution.amount.desc())
        .all()
    )
    org_names = {o.id: o.name for o in db.query(Organization).all()}
    return [
        {
            "org_id": r.org_id,
            "org_name": org_names.get(r.org_id, ""),
            "score": r.score,
            "score_detail": r.score_detail,
            "weight": r.weight,
            "share_pct": r.share_pct,
            "amount": r.amount,
        }
        for r in rows
    ]


def _settlement_out(settlement: FundSettlement, db: Session) -> dict:
    distributions = _distribution_rows(settlement, db)
    return {
        "pool_id": settlement.pool_id,
        "total_income": round(settlement.total_income, 2),
        "total_expense": round(settlement.total_expense, 2),
        "balance": round(settlement.balance, 2),
        "is_overrun": settlement.balance < 0,
        "overrun_action": settlement.overrun_action,
        "overrun_action_name": OVERRUN_ACTIONS.get(
            settlement.overrun_action, settlement.overrun_action
        ),
        "formula_expr": settlement.formula_expr,
        "score_basis": settlement.score_basis,
        "settled_at": settlement.settled_at.isoformat(),
        "distributed_amount": round(sum(d["amount"] for d in distributions), 2),
        "distributions": distributions,
        "caliber": {
            "balance": "筹资总额 − 全年实际发生额（正=结余，负=超支）",
            "overrun": "超支不自动扣减任何机构，仅记录处置选择，由主管部门另行决定",
            "score": "分配依据为分配当时冻结的绩效得分快照，此后调整指标权重不影响已分结果",
        },
    }


def _pool(db: Session, pool_id: int) -> FundPool:
    pool = db.get(FundPool, pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="基金池不存在")
    return pool
