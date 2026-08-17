"""E2 医保支付方式改革落地：DRG/DIP 实际结算 + 医保智能审核（新建，不改存量）。

- DrgRate：DRG 年度费率（元/权重点），病组支付 = drg_weight × rate_per_weight；
- DipCatalog / DipRate：DIP 病种分值目录与年度点值，病种支付 = score × point_value；
- PaymentCase：按病组/病种结算的病例（应支付 vs 实际成本 → 盈亏）；
- InsuranceAuditRule / InsuranceAuditFlag：医保智能审核规则与疑点。

与存量表保持**松耦合**：admission_id / settlement_id 用普通 Integer 存 id，
调用侧用 db.get(...) 校验存在，不建指向 admissions/insurance_settlements 的外键，
以免绑死表名、给中央装配添麻烦。org_id 指向 organizations（隔离要用）。
"""
from datetime import datetime

from ._shared import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, Money, String,
                      UniqueConstraint, utcnow, Mapped, mapped_column)
from ..database import Base


class DrgRate(Base):
    """DRG 年度费率：某年度某统筹区的"元/权重点"。病组支付 = drg_weight × rate_per_weight。"""

    __tablename__ = "drg_rates"
    __table_args__ = (UniqueConstraint("year", "region", name="uq_drg_rate_year_region"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    year: Mapped[str] = mapped_column(String(4))
    # 统筹区（市/县），空串表示全域默认费率
    region: Mapped[str] = mapped_column(String(16), default="")
    rate_per_weight: Mapped[float] = mapped_column(Money, default=0)
    note: Mapped[str] = mapped_column(String(128), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DipCatalog(Base):
    """DIP 病种分值目录：病种码 → 分值。病种支付 = score × DipRate.point_value。"""

    __tablename__ = "dip_catalog"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    disease_code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    disease_name: Mapped[str] = mapped_column(String(128))
    score: Mapped[float] = mapped_column(Float, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DipRate(Base):
    """DIP 年度点值：某年度的"元/分"。病种支付 = score × point_value。"""

    __tablename__ = "dip_rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    year: Mapped[str] = mapped_column(String(4), unique=True, index=True)
    point_value: Mapped[float] = mapped_column(Money, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PaymentCase(Base):
    """病组/病种结算病例：应支付 vs 实际成本 → 盈亏（profit = standard − actual）。"""

    __tablename__ = "payment_cases"
    __table_args__ = (UniqueConstraint("admission_id", "pay_mode", name="uq_payment_case_adm_mode"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 松耦合：住院登记 id（普通整数，不建外键）
    admission_id: Mapped[int] = mapped_column(Integer, index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # drg=按病组, dip=按病种
    pay_mode: Mapped[str] = mapped_column(String(8))
    # DRG 组码或 DIP 病种码
    group_code: Mapped[str] = mapped_column(String(64))
    weight_or_score: Mapped[float] = mapped_column(Float, default=0)
    standard_payment: Mapped[float] = mapped_column(Money, default=0)
    actual_cost: Mapped[float] = mapped_column(Money, default=0)
    profit: Mapped[float] = mapped_column(Money, default=0)
    year: Mapped[str] = mapped_column(String(4), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class InsuranceAuditRule(Base):
    """医保智能审核规则：按 rule_type 分派（超额/超频/性别禁忌/年龄禁忌/分解住院）。"""

    __tablename__ = "insurance_audit_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    # over_amount / over_frequency / gender_contra / age_contra / decompose_admission
    rule_type: Mapped[str] = mapped_column(String(24))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    # warn=提示, block=拦截
    severity: Mapped[str] = mapped_column(String(8), default="warn")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class InsuranceAuditFlag(Base):
    """审核疑点：命中某规则的结算记录。open→confirmed/dismissed。"""

    __tablename__ = "insurance_audit_flags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 松耦合：结算记录 id（普通整数，不建外键）
    settlement_id: Mapped[int] = mapped_column(Integer, index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    rule_code: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(String(512), default="")
    # open=待核, confirmed=确认疑点, dismissed=已排除
    status: Mapped[str] = mapped_column(String(16), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
