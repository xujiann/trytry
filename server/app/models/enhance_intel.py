"""E6 数据智能层 · 模型（新增，集中挂载）。

只落一张配置表：慢病风险分层的因子权重。风险评分本身是**现算**的
（见 app/routers/enhance_intel.py），不落库——落库的分数会随随访数据变化
而与档案脱节，正如 chronic.risk_score 也从不存分数。这里存的是"怎么算"
（各因子的权重），不是"算出来多少"。
"""
from datetime import datetime

from ._shared import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    UniqueConstraint,
    Mapped,
    mapped_column,
    utcnow,
)
from ..database import Base


class RiskModelWeight(Base):
    """慢病风险分层权重配置。

    factor 是可评估的风险因子编码，权重由 admin 配置，可停用。评分接口
    只取 active 行，score = Σ(被触发因子的权重)。约定的 factor 取值：
    bp_uncontrolled / glucose_uncontrolled / age_65 / complication / poor_adherence。
    """

    __tablename__ = "risk_model_weights"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    factor: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    weight: Mapped[float] = mapped_column(Float, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
