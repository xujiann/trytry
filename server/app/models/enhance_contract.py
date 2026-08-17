"""E1 家庭医生签约内容化：服务包目录、包条目、签约费池与按人头分配。

在既有 `FamilyDoctorContract`/`ContractService`（core.py）之上补齐"内容化"层：
- ServicePackage / ServicePackageItem：把不透明的 basic/standard/premium 枚举
  换成"有服务项、有年度应履约次数、有计价"的目录；签约挂 package_id。
- SignFeePool / SignFeeDistribution：签约费按有效签约人头计提，年终结合履约率与
  重点人群系数分配（复用 allocation.hamilton 精确到分）。
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


class ServicePackage(Base):
    """家医签约服务包目录（全域配置，管理员维护）。"""

    __tablename__ = "service_packages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    # 年签约费（元/人·年），按人头计提签约费池的单价来源之一
    annual_fee: Mapped[float] = mapped_column(Money, default=0)
    # 适用人群（general/chronic/elderly/maternal/disabled…），仅描述性
    target_population: Mapped[str] = mapped_column(String(32), default="")
    description: Mapped[str] = mapped_column(String(512), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    items: Mapped[list["ServicePackageItem"]] = relationship(
        back_populates="package", cascade="all, delete-orphan"
    )


class ServicePackageItem(Base):
    """服务包条目：一个服务项 + 年度应履约次数 + 是否计价。"""

    __tablename__ = "service_package_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("service_packages.id"), index=True)
    # 服务类型（与 ContractService.service_type 同域：visit/consult/followup/referral/exam…）
    service_type: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(64))
    # 年度应履约次数；0=不计入应履约（如"按需服务"）
    annual_times: Mapped[int] = mapped_column(Integer, default=0)
    billable: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    package: Mapped[ServicePackage] = relationship(back_populates="items")


class SignFeePool(Base):
    """签约费池：按年度、按（可选）分组计提，年终按人头+履约率分配。"""

    __tablename__ = "sign_fee_pools"
    __table_args__ = (
        UniqueConstraint("year", "org_group_id", name="uq_signfee_year_group"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    year: Mapped[str] = mapped_column(String(4), index=True)
    # 绑定片区（org_groups.id）则只在该片区内分配；空=全域
    org_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("org_groups.id"), nullable=True, index=True
    )
    # 人均签约费标准（元/有效签约·年）
    per_capita: Mapped[float] = mapped_column(Money, default=0)
    # 池子总额（元）；由 per_capita×总有效人头计提，也可管理层直接核定
    total_amount: Mapped[float] = mapped_column(Money, default=0)
    # draft=待分配, distributed=已分配
    status: Mapped[str] = mapped_column(String(16), default="draft")
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    distributions: Mapped[list["SignFeeDistribution"]] = relationship(
        back_populates="pool", cascade="all, delete-orphan"
    )


class SignFeeDistribution(Base):
    """签约费分配明细：某机构在某池子里分得的份额（人头×履约率×重点人群系数）。"""

    __tablename__ = "sign_fee_distributions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("sign_fee_pools.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    headcount: Mapped[int] = mapped_column(Integer, default=0)
    # 平均履约率（0~1）
    fulfillment_rate: Mapped[float] = mapped_column(Float, default=0)
    # 重点人群加权系数（有效签约里重点人群占比抬升的加权）
    key_pop_factor: Mapped[float] = mapped_column(Float, default=1.0)
    weight: Mapped[float] = mapped_column(Float, default=0)
    share_pct: Mapped[float] = mapped_column(Float, default=0)
    amount: Mapped[float] = mapped_column(Money, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    pool: Mapped[SignFeePool] = relationship(back_populates="distributions")
