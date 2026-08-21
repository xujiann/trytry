"""ORM 模型 · 物资资产与院感物流：资产、采购、盘点、医废、消毒供应。

由原 `models.py`（3989 行 / 187 类）分域拆出，见 ADR-0008。
**类的先后顺序保持原文件不变**——本仓库没开 `from __future__ import annotations`，
`Mapped[SomeClass]` 这类注解在建类时就要求被引用的类已经定义。
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ._base import Money, utcnow


class SterilizationBatch(Base):
    """消毒供应中心：复用器械批次的清洗消毒灭菌、发放、回收全流程追溯。"""

    __tablename__ = "sterilization_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    center_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    item_name: Mapped[str] = mapped_column(String(128))
    quantity: Mapped[int] = mapped_column(Integer)
    # sterilizing=灭菌中, sterile=已灭菌, dispatched=已发放, recycled=已回收
    status: Mapped[str] = mapped_column(String(16), default="sterilizing", index=True)
    dispatched_to_org_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class MedicalWaste(Base):
    """医疗废弃物：收集、暂存、交接全过程实时监管与追溯。"""

    __tablename__ = "medical_wastes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # infectious=感染性, sharp=损伤性, pathological=病理性, pharmaceutical=药物性, chemical=化学性
    waste_type: Mapped[str] = mapped_column(String(16))
    weight_kg: Mapped[float] = mapped_column(Float)
    # collected=已收集, stored=已暂存, handed_over=已交接
    status: Mapped[str] = mapped_column(String(16), default="collected", index=True)
    handler_name: Mapped[str] = mapped_column(String(64), default="")
    collected_date: Mapped[str] = mapped_column(String(10), index=True)
    handed_over_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 追溯码：指引明文点名的技术手段。一包医废一码，扫码即知从哪来、
    # 现在在哪、谁交接的。全局唯一，由平台生成而非录入——让人填码，
    # 迟早会有两包医废共用一个码。
    trace_code: Mapped[str] = mapped_column(String(32), default="", unique=True, index=True)
    # 产生点位与暂存点位。产生点必填，暂存点在入暂存间时补
    source_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("waste_locations.id"), nullable=True, index=True
    )
    storage_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("waste_locations.id"), nullable=True, index=True
    )
    # 转运人员挂员工档案而不是自由文本：转运人员管理是指引的子功能，
    # 靠 prompt 让人手打名字，既统计不出人次也追不了责。
    handler_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id"), nullable=True, index=True
    )
    stored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Asset(Base):
    """㉜物资统一协同管理（非医疗设备、办公用品）。"""

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    # equipment=非医疗设备, office=办公用品
    category: Mapped[str] = mapped_column(String(16), default="office")
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    # in_use=在用, idle=闲置, scrapped=报废
    status: Mapped[str] = mapped_column(String(16), default="in_use")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CssdRequest(Base):
    """⑥消毒供应物品申领：基层申领→中心关联批次发放。"""

    __tablename__ = "cssd_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    item_name: Mapped[str] = mapped_column(String(128))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    # requested=已申领, fulfilled=已发放
    status: Mapped[str] = mapped_column(String(16), default="requested", index=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("sterilization_batches.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Supplier(Base):
    """㉜㉝供应商管理（药品耗材/非医疗物资共用）。"""

    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    contact: Mapped[str] = mapped_column(String(64), default="")
    license_no: Mapped[str] = mapped_column(String(64), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PurchaseOrder(Base):
    """㉝采购管理：采购申请→审批→到货验收（药品验收自动入库）。"""

    __tablename__ = "purchase_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"))
    # drug=药品, material=耗材/物资
    item_type: Mapped[str] = mapped_column(String(16), default="drug")
    item_code: Mapped[str] = mapped_column(String(64))
    item_name: Mapped[str] = mapped_column(String(128))
    quantity: Mapped[int] = mapped_column(Integer)
    # pending=待审批, approved=已审批, received=已验收入库, rejected=已驳回
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    note: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class StockTake(Base):
    """㉝存货盘点：账面数/实盘数差异留痕并调整库存。"""

    __tablename__ = "stock_takes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    drug_code: Mapped[str] = mapped_column(String(64))
    book_qty: Mapped[int] = mapped_column(Integer)
    actual_qty: Mapped[int] = mapped_column(Integer)
    diff: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(256), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AssetMovement(Base):
    """㉜物资出入库管理：入库/领用/归还/报废联动数量与状态。"""

    __tablename__ = "asset_movements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), index=True)
    # inbound=入库, issue=领用出库, return=归还, scrap=报废出库
    movement_type: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(256), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CssdCostItem(Base):
    """⑥消毒供应成本核算：灭菌批次的成本项，单件成本 = 批次成本合计 / 件数。"""

    __tablename__ = "cssd_cost_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("sterilization_batches.id"), index=True)
    # labor=人工, material=耗材, energy=能耗, equipment=设备折旧, other=其他
    cost_type: Mapped[str] = mapped_column(String(16), index=True)
    amount: Mapped[float] = mapped_column(Money, default=0)
    note: Mapped[str] = mapped_column(String(256), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MaterialPurchase(Base):
    """物资采购：申请→审批→合同→到货验收。

    药品采购走 PurchaseOrder（/api/pharmacy），此处是**非药品物资**，
    两者审批链与入库对象不同，不合并。
    """

    __tablename__ = "material_purchases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    dept_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"), nullable=True)
    item_name: Mapped[str] = mapped_column(String(128))
    spec: Mapped[str] = mapped_column(String(64), default="")
    unit: Mapped[str] = mapped_column(String(16), default="件")
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    estimated_price: Mapped[float] = mapped_column(Money, default=0)
    reason: Mapped[str] = mapped_column(String(512), default="")
    # requested=待审批, approved=已审批, contracted=已签合同, received=已验收, cancelled=已取消
    status: Mapped[str] = mapped_column(String(16), default="requested", index=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"), nullable=True)
    contract_no: Mapped[str] = mapped_column(String(64), default="")
    contract_amount: Mapped[float] = mapped_column(Money, default=0)
    received_quantity: Mapped[int] = mapped_column(Integer, default=0)
    received_note: Mapped[str] = mapped_column(String(512), default="")
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class HighValueConsumable(Base):
    """高值耗材：一物一码，使用时绑定患者与手术，构成可追溯链。"""

    __tablename__ = "high_value_consumables"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    barcode: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    spec: Mapped[str] = mapped_column(String(64), default="")
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"), nullable=True)
    batch_no: Mapped[str] = mapped_column(String(64), default="")
    expire_date: Mapped[str] = mapped_column(String(10), default="", index=True)
    unit_price: Mapped[float] = mapped_column(Money, default=0)
    # in_stock=在库, used=已使用, returned=已退回, scrapped=已报废
    status: Mapped[str] = mapped_column(String(16), default="in_stock", index=True)
    used_patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), nullable=True, index=True)
    used_surgery_id: Mapped[int | None] = mapped_column(
        ForeignKey("surgery_requests.id"), nullable=True, index=True
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WasteLocation(Base):
    """㊱医废点位：产生点与暂存间。

    此前 `medical_wastes` 只有 `org_id`，答得出"哪家机构"，答不出"哪个科室
    产生的、存在哪间暂存间"——而医废管理条例要求的正是点位级的可追溯。
    """

    __tablename__ = "waste_locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    # source=产生点（科室/病区）, storage=暂存间
    location_type: Mapped[str] = mapped_column(String(16), default="source", index=True)
    manager_name: Mapped[str] = mapped_column(String(64), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
