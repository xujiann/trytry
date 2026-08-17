"""S3 绩效工资二次分配：把一笔已到账的绩效资金按公式二次拆分到机构/科室/个人。

医共体层面常有多股资金最终要落到人头上——医保基金结余、DRG 结余、签约费、
公卫补助——它们各自的一次分配（到机构）由别的模块完成，这里补上的是"二次分配"：
拿到一笔总额后，按管理部门当年选定的公式（`formula.py` 白名单表达式）折算成
份额权重，再用 `allocation.hamilton_amounts` 精确到分地拆下去，合计分毫等于总额。

与 fund.py 结余分配同口径：
- 分配依据是**当次快照**——每条明细把得分与折算出的权重、占比、金额一起落库，
  此后改公式或改得分都不影响已分结果；重新分配则覆盖上一次。
- 权重全为 0 时**显式拒绝**，不静默把总额分成 0（那等于钱凭空蒸发）。
- 分配办法用公式表达，绝不写死进代码——各县办法不同且逐年调整。
"""
from datetime import datetime

from ._shared import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Money,
    String,
    UniqueConstraint,
    utcnow,
    Mapped,
    mapped_column,
    relationship,
)
from ..database import Base


class PerfDistributionPool(Base):
    """二次分配池：一笔待二次分配的绩效资金。

    `source` 标资金来源（医保结余/DRG 结余/签约费/公卫补助/混合），仅描述性，
    不影响分配逻辑。`org_group_id` 可空：按全域分或按片区分，与基金池同理。
    同年度、同范围、同来源只应有一个池，由唯一约束兜底并发重复建池。
    """

    __tablename__ = "perf_distribution_pools"
    __table_args__ = (
        UniqueConstraint(
            "year", "org_group_id", "source", name="uq_perf_pool_year_group_source"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    year: Mapped[str] = mapped_column(String(4), index=True)
    org_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("org_groups.id"), nullable=True, index=True
    )
    # fund_surplus/drg_surplus/sign_fee/public_health/mixed，仅描述性标签
    source: Mapped[str] = mapped_column(String(24))
    total_amount: Mapped[float] = mapped_column(Money, default=0)
    # draft=待分配, distributed=已分配
    status: Mapped[str] = mapped_column(String(16), default="draft")
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    distributions: Mapped[list["PerfDistribution"]] = relationship(
        back_populates="pool", cascade="all, delete-orphan"
    )


class PerfDistribution(Base):
    """二次分配明细：一笔分配对应一个目标（机构/科室/个人）。

    `score`/`weight`/`share_pct`/`amount` 是**冻结快照**——落库那一刻的公式求值
    结果，此后改公式或改得分都不回溯已分明细。重新分配走覆盖（先删后插）。
    `target_type` 决定 `target_id` 指向机构/科室/个人的哪张表，本表不做外键约束，
    因二次分配的目标粒度按各县办法变化（有的分到科室，有的直接到人）。
    """

    __tablename__ = "perf_distributions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pool_id: Mapped[int] = mapped_column(
        ForeignKey("perf_distribution_pools.id"), index=True
    )
    # org/dept/person
    target_type: Mapped[str] = mapped_column(String(12))
    target_id: Mapped[int] = mapped_column(Integer, default=0)
    target_name: Mapped[str] = mapped_column(String(64), default="")
    score: Mapped[float] = mapped_column(Float, default=0)
    weight: Mapped[float] = mapped_column(Float, default=0)      # 公式求出的份额权重
    share_pct: Mapped[float] = mapped_column(Float, default=0)   # 归一化后占比
    amount: Mapped[float] = mapped_column(Money, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    pool: Mapped["PerfDistributionPool"] = relationship(back_populates="distributions")
