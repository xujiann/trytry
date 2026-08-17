"""E5 药品统一采购、统一配送与追溯。

医共体的"三统一"落在四段业务上，各自一张表，刻意不并成一张"药事流水"：

- **统一药品目录**（`ConsortiumDrugCatalog`）：集采中选、约定量、统一价——
  这是全域一份的平台级字典，不挂机构，与 `code_entries`（编码字典）分开，
  因为它带集采属性（中选/约定量/统一价），塞进通用编码表等于把集采语义抹平。
- **带量采购**（`VolumePurchase` + `VolumePurchaseAllocation`）：一个批次的
  总量按机构分配，机构侧记履约量。分配与批次分表，是因为"分给谁多少"与
  "这个批次采什么"是两个维度，合表会让同一批次的机构行彼此挤在一起。
- **中心统一配送**（`DistributionOrder`）：中心药房 → 成员机构，天然跨机构，
  故带 `from_org_id` + `to_org_id`，与双向转诊、共享中药房同一形状。
- **药品追溯**（`DrugTraceCode`）：一物一码的出入库/发药流转，与医废追溯码
  同理——扫码即知这盒药从哪来、现在在谁手上、发给谁了。
"""
from datetime import datetime

from ._shared import (Boolean, DateTime, Float, ForeignKey, Integer, Money, String,
                      UniqueConstraint, Mapped, mapped_column, relationship, utcnow)
from ..database import Base


class ConsortiumDrugCatalog(Base):
    """统一药品目录（集采中选目录）：全域一份，平台级字典，不挂机构。

    `selected` 标集采中选，`volume_agreed` 记约定量，`unit_price` 为统一采购价。
    停用不删行（`active`）：目录改过什么、什么时候不再执行，是采购复核要回溯的。
    """

    __tablename__ = "consortium_drug_catalog"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    drug_code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    drug_name: Mapped[str] = mapped_column(String(128))
    spec: Mapped[str] = mapped_column(String(64), default="")
    unit: Mapped[str] = mapped_column(String(16), default="")
    # 集采中选
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    # 约定量（带量采购的年度约定采购量）
    volume_agreed: Mapped[int] = mapped_column(Integer, default=0)
    unit_price: Mapped[float] = mapped_column(Money, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class VolumePurchase(Base):
    """带量采购批次：某目录药品在某年度的总采购量与开闭状态。"""

    __tablename__ = "volume_purchases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    catalog_id: Mapped[int] = mapped_column(
        ForeignKey("consortium_drug_catalog.id"), index=True
    )
    year: Mapped[str] = mapped_column(String(4))
    total_volume: Mapped[int] = mapped_column(Integer, default=0)
    # open=可分配, closed=已封批（封批后不得再分配）
    status: Mapped[str] = mapped_column(String(16), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class VolumePurchaseAllocation(Base):
    """带量采购量分配到机构：约定量按机构分派，机构侧记履约量。

    同一批次同一机构只有一行（`UniqueConstraint`），重复分配走 upsert 覆盖，
    避免同一机构分出两条互相打架的约定量。
    """

    __tablename__ = "volume_purchase_allocations"
    __table_args__ = (
        UniqueConstraint("purchase_id", "org_id", name="uq_volume_alloc_purchase_org"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    purchase_id: Mapped[int] = mapped_column(ForeignKey("volume_purchases.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    allocated_volume: Mapped[int] = mapped_column(Integer, default=0)
    fulfilled_volume: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DistributionOrder(Base):
    """中心统一配送单：中心药房（from_org）→ 成员机构（to_org），天然跨机构。

    状态机 created→picked→shipped→received：拣货、发货由发货方（from_org）推进，
    签收由收货方（to_org）确认。签收时自动落一条入库追溯码。
    """

    __tablename__ = "distribution_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    to_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    drug_code: Mapped[str] = mapped_column(String(64))
    qty: Mapped[int] = mapped_column(Integer, default=0)
    # created=已创建, picked=已拣货, shipped=已发货, received=已签收
    status: Mapped[str] = mapped_column(String(16), default="created")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DrugTraceCode(Base):
    """药品追溯码流转：一物一码的出入库/发药留痕。

    `direction`：in=入库, out=出库, dispense=发药。`ref_type`/`ref_id` 记来源单据
    （如 distribution + 配送单 id），扫码即可倒查整条流转链。
    """

    __tablename__ = "drug_trace_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trace_code: Mapped[str] = mapped_column(String(64), index=True)
    drug_code: Mapped[str] = mapped_column(String(64))
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # in=入库, out=出库, dispense=发药
    direction: Mapped[str] = mapped_column(String(8))
    ref_type: Mapped[str] = mapped_column(String(24), default="")
    ref_id: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
