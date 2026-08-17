"""S3 绩效工资二次分配：把一笔已到账的绩效资金按公式二次拆分到机构/科室/个人。

一次分配（把钱分到机构）由医保结余、DRG 结余、签约费等各自的模块完成；这里做的是
**二次分配**：管理部门拿到某一范围的一笔总额，选一条当年的分配公式，按各目标的
绩效得分折算份额权重，再精确到分地拆下去。

四条口径与 `fund.py` 结余分配一致：
1. 分配依据是**当次快照**——得分/权重/占比/金额随明细一起落库，此后改公式或改得分
   都不回溯已分结果；重新分配走覆盖。
2. 分配办法用公式引擎（`formula.py` 白名单表达式）表达，不写死进代码。
3. 权重全为 0 时**显式拒绝**（409），绝不静默把总额分成 0 让钱凭空蒸发。
4. 按份额分钱用最大余数法（`allocation.hamilton_amounts`），合计分毫等于总额。

这是**医共体层面的管理动作 → 限管理层（director）**：与 fund.py 的 distribute 同档，
admin 自动通过；无逐机构写入口给非全域用户。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..allocation import hamilton_amounts
from ..concurrency import insert_or_conflict
from ..database import get_db
from ..deps import get_current_user, require_roles
from ..formula import FormulaError, evaluate, validate
from ..models.enhance_perf import PerfDistribution, PerfDistributionPool

router = APIRouter(
    prefix="/api/perf-distribution",
    tags=["绩效二次分配"],
    dependencies=[Depends(get_current_user)],
)

# 分配公式可用变量。刻意保持极少——变量越多，各县越容易写出没人能复核的表达式。
FORMULA_VARIABLES = {"score", "rank", "count"}

# 资金来源标签，仅描述性
SOURCES = {"fund_surplus", "drg_surplus", "sign_fee", "public_health", "mixed"}


# ---------------------------------------------------------------- schemas


class PoolIn(BaseModel):
    year: str = Field(pattern=r"^\d{4}$")
    org_group_id: int | None = None
    source: str = Field(pattern="^(fund_surplus|drg_surplus|sign_fee|public_health|mixed)$")
    total_amount: float = Field(default=0, ge=0)
    note: str = Field(default="", max_length=256)


class TargetIn(BaseModel):
    target_type: str = Field(pattern="^(org|dept|person)$")
    target_id: int = Field(default=0, ge=0)
    target_name: str = Field(default="", max_length=64)
    score: float = Field(default=0)


class DistributeIn(BaseModel):
    """分配参数。公式返回**份额权重**，平台按占比归一化后乘总额，精确到分。"""

    formula_expr: str = Field(default="score", max_length=256)
    targets: list[TargetIn] = Field(default_factory=list)


# ---------------------------------------------------------------- helpers


def _pool(db: Session, pool_id: int) -> PerfDistributionPool:
    pool = db.get(PerfDistributionPool, pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="二次分配池不存在")
    return pool


def _pool_out(db: Session, pool: PerfDistributionPool) -> dict:
    return {
        "id": pool.id,
        "year": pool.year,
        "org_group_id": pool.org_group_id,
        "source": pool.source,
        "total_amount": round(pool.total_amount, 2),
        "status": pool.status,
        "note": pool.note,
        "distributions": _dist_rows(db, pool.id),
    }


def _dist_rows(db: Session, pool_id: int) -> list[dict]:
    rows = (
        db.query(PerfDistribution)
        .filter(PerfDistribution.pool_id == pool_id)
        .order_by(PerfDistribution.amount.desc())
        .all()
    )
    return [
        {
            "id": r.id,
            "pool_id": r.pool_id,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "target_name": r.target_name,
            "score": r.score,
            "weight": r.weight,
            "share_pct": r.share_pct,
            "amount": r.amount,
        }
        for r in rows
    ]


# ---------------------------------------------------------------- endpoints


@router.post("/pools", status_code=201, dependencies=[Depends(require_roles("director"))])
def create_pool(body: PoolIn, db: Session = Depends(get_db)):
    """建二次分配池。同年度、同范围、同来源只能建一个——非空分组由唯一约束
    `uq_perf_pool_year_group_source` 兜底；**全域池（org_group_id 为空）** SQLite/PG
    的唯一约束把 NULL 视作互异不去重，故此处补一层代码级预检拦重复全域池。"""
    if body.org_group_id is None and db.query(PerfDistributionPool.id).filter(
        PerfDistributionPool.year == body.year,
        PerfDistributionPool.org_group_id.is_(None),
        PerfDistributionPool.source == body.source,
    ).first():
        raise HTTPException(status_code=409, detail="同年度同来源的全域二次分配池已存在")
    pool = PerfDistributionPool(**body.model_dump())
    return _pool_out(db, insert_or_conflict(db, pool, "同年度同范围同来源的二次分配池已存在"))


@router.get("/pools", dependencies=[Depends(require_roles("director"))])
def list_pools(year: str | None = None, db: Session = Depends(get_db)):
    query = db.query(PerfDistributionPool)
    if year is not None:
        query = query.filter(PerfDistributionPool.year == year)
    return [
        _pool_out(db, p)
        for p in query.order_by(PerfDistributionPool.id.desc()).limit(200).all()
    ]


@router.post(
    "/pools/{pool_id}/distribute", dependencies=[Depends(require_roles("director"))]
)
def distribute(pool_id: int, body: DistributeIn, db: Session = Depends(get_db)):
    """按公式把池内总额二次分配到各目标，并**冻结**当次快照。

    - 池不存在 → 404；总额 ≤ 0 → 409（无钱可分）；
    - 公式非法/引用未知变量 → 422；
    - 各目标按得分降序取名次，`weight = max(公式求值, 0)`；
    - 所有权重合计 ≤ 0 → 409（不静默把总额分成 0）；
    - 用最大余数法拆分，合计分毫等于总额；重新分配覆盖上一次。
    """
    pool = _pool(db, pool_id)
    if pool.total_amount <= 0:
        raise HTTPException(status_code=409, detail="池内总额为 0，无可分配金额")
    if not body.targets:
        raise HTTPException(status_code=409, detail="没有可参与分配的目标")

    try:
        validate(body.formula_expr, FORMULA_VARIABLES)
    except FormulaError as exc:
        raise HTTPException(status_code=422, detail=f"分配公式不合法：{exc}") from None

    # 按得分降序排名（1 为最高）；同分按原顺序稳定，可复现
    ranked = sorted(
        enumerate(body.targets), key=lambda it: it[1].score, reverse=True
    )
    n = len(body.targets)
    weighted = []  # (target, weight)
    for rank, (_, target) in enumerate(ranked, start=1):
        try:
            weight = evaluate(
                body.formula_expr,
                {"score": target.score, "rank": float(rank), "count": float(n)},
            )
        except FormulaError as exc:
            raise HTTPException(status_code=422, detail=f"分配公式求值失败：{exc}") from None
        # 负权重没有意义（分负数的钱），按 0 处理
        weighted.append((target, max(weight, 0.0)))

    wsum = sum(w for _, w in weighted)
    if wsum <= 0:
        # 与 fund.py 同口径：全零权重无法归一化，显式拒绝而非静默分 0。
        # 常见于本期得分普遍为 0，此时应改用与得分无关的公式（如均分写 1）。
        raise HTTPException(
            status_code=409,
            detail="所有目标的份额权重都为 0，无法归一化分配。"
                   "常见原因是本期得分普遍为 0，此时应改用与得分无关的公式（如均分写 1）",
        )

    amounts = hamilton_amounts(pool.total_amount, [w for _, w in weighted])

    # 覆盖式重算：分配方案改了要能重来，快照随之刷新
    db.query(PerfDistribution).filter(
        PerfDistribution.pool_id == pool.id
    ).delete(synchronize_session=False)

    for (target, weight), amount in zip(weighted, amounts):
        db.add(
            PerfDistribution(
                pool_id=pool.id,
                target_type=target.target_type,
                target_id=target.target_id,
                target_name=target.target_name,
                score=target.score,
                weight=round(weight, 6),
                share_pct=round(weight / wsum * 100, 4),
                amount=amount,
            )
        )
    pool.status = "distributed"
    db.commit()
    db.refresh(pool)
    return _pool_out(db, pool)


@router.get("/pools/{pool_id}/distributions", dependencies=[Depends(require_roles("director"))])
def list_distributions(pool_id: int, db: Session = Depends(get_db)):
    _pool(db, pool_id)
    return _dist_rows(db, pool_id)
