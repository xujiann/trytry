"""ORM 模型 · 药事与中医制剂：审方、处方、库存、调剂。

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
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ._base import utcnow


class DrugRule(Base):
    """合理用药规则库：集中审方的"系统审"依据。"""

    __tablename__ = "drug_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    drug_code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    max_daily_dose: Mapped[float] = mapped_column(Float)
    dose_unit: Mapped[str] = mapped_column(String(16), default="mg")
    note: Mapped[str] = mapped_column(String(256), default="")
    # 相互作用：与该药冲突的药品编码（逗号分隔），同方出现冲突药对转药师审
    interactions: Mapped[str] = mapped_column(String(512), default="")
    # 禁忌诊断关键词（逗号分隔）：诊断名命中即转药师审
    contraindicated_diagnoses: Mapped[str] = mapped_column(String(512), default="")
    # 特殊人群（逗号分隔：pregnant,child,elderly）：患者命中即转药师审
    special_groups: Mapped[str] = mapped_column(String(64), default="")
    # 块2：肝肾功能提示（不拦截，开方与点评时随处方返回，供剂量调整参考）
    renal_hepatic_note: Mapped[str] = mapped_column(String(512), default="")
    # 块2：处方点评要点（事后点评规则化依据）
    review_points: Mapped[str] = mapped_column(String(512), default="")
    # 抗菌药物标记与 DDD（限定日剂量，单位同 dose_unit）。
    # 使用强度 = Σ(日剂量×天数 ÷ DDD) × 100 ÷ 同期收治人天，是国家监测指标。
    # ddd 为 0 表示未维护，统计时**跳过并计入未覆盖数**——按缺省值硬算会把强度
    # 算成无穷大或悄悄漏掉，两种都比"明说没维护"更糟。
    antibiotic: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    ddd: Mapped[float] = mapped_column(Float, default=0)
    # D-4 同类：录错一条规则原先只能靠 import 覆盖，删不掉也停不掉，
    # 而通用规则引擎 `/api/rules/{key}` 是有停用的。停用不删行——规则改过什么、
    # 什么时候不再生效，是处方点评复核时要回溯的。
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Prescription(Base):
    """处方：全量进入集中审方中心，系统审+药师审双重审核。"""

    __tablename__ = "prescriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    diagnosis_name: Mapped[str] = mapped_column(String(256), default="")
    # auto_passed=系统审通过, pending_review=待药师审, approved=药师审通过, rejected=退回
    status: Mapped[str] = mapped_column(String(16), default="auto_passed", index=True)
    review_comment: Mapped[str] = mapped_column(String(1024), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    items: Mapped[list["PrescriptionItem"]] = relationship(back_populates="prescription")


class PrescriptionItem(Base):
    __tablename__ = "prescription_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prescription_id: Mapped[int] = mapped_column(ForeignKey("prescriptions.id"), index=True)
    drug_code: Mapped[str] = mapped_column(String(64))
    drug_name: Mapped[str] = mapped_column(String(128))
    daily_dose: Mapped[float] = mapped_column(Float)
    days: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    prescription: Mapped[Prescription] = relationship(back_populates="items")


class DrugStock(Base):
    """中心药房库存：县乡村药品余缺调度的基础。"""

    __tablename__ = "drug_stocks"
    __table_args__ = (UniqueConstraint("org_id", "drug_code", name="uq_stock_org_drug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    drug_code: Mapped[str] = mapped_column(String(64), index=True)
    drug_name: Mapped[str] = mapped_column(String(128))
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    # 低于该阈值触发缺药预警
    threshold: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class StockTransfer(Base):
    __tablename__ = "stock_transfers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    drug_code: Mapped[str] = mapped_column(String(64))
    from_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    to_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TcmDispenseOrder(Base):
    """⑭中药智能药学（共享中药房）：调配→煎煮→配送全程追溯。"""

    __tablename__ = "tcm_dispense_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    from_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    herbs: Mapped[str] = mapped_column(String(1024))
    doses: Mapped[int] = mapped_column(Integer, default=1)
    decoct: Mapped[bool] = mapped_column(Boolean, default=True)
    # ordered=已下单, dispensed=已调配, decocted=已煎煮, delivering=配送中, delivered=已送达
    status: Mapped[str] = mapped_column(String(16), default="ordered", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class TcmTechnique(Base):
    """㉑中医药适宜技术库。"""

    __tablename__ = "tcm_techniques"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    category: Mapped[str] = mapped_column(String(64), default="")
    indication: Mapped[str] = mapped_column(String(512), default="")
    description: Mapped[str] = mapped_column(String(1024), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class DrugShortage(Base):
    """⑮基层缺药登记：登记→采购→配送→取药。

    `patient_id` 可空：既有按机构报缺（补库存），也有按患者登记（延伸处方、
    个性化治疗需求），两者共用一张表但含义不同——按患者登记的才谈得上
    "登记后不来取药"，也才进得了黑名单。

    末态分 collected 与 no_show 两个：都"结束了"，但一个是药送到人拿走了，
    一个是药白调了。混成一个 closed，缺药登记的履约率就永远算不出来。
    """

    __tablename__ = "drug_shortages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    patient_id: Mapped[int | None] = mapped_column(
        ForeignKey("patients.id"), nullable=True, index=True
    )
    drug_code: Mapped[str] = mapped_column(String(64))
    drug_name: Mapped[str] = mapped_column(String(128))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    # registered=已登记, purchasing=采购中, delivered=已配送,
    # collected=已取药, no_show=未取药, cancelled=已取消
    status: Mapped[str] = mapped_column(String(16), default="registered", index=True)
    close_reason: Mapped[str] = mapped_column(String(256), default="")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PrescriptionComment(Base):
    """⑱处方点评：药师对已审处方的事后点评（合理/不合理）与问题留痕。"""

    __tablename__ = "prescription_comments"
    __table_args__ = (UniqueConstraint("prescription_id", name="uq_rx_comment_prescription"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prescription_id: Mapped[int] = mapped_column(ForeignKey("prescriptions.id"), index=True)
    # reasonable=合理, unreasonable=不合理
    grade: Mapped[str] = mapped_column(String(16), index=True)
    # 问题类型：适应证不适宜/用法用量不适宜/重复用药/相互作用等（分号分隔）
    issues: Mapped[str] = mapped_column(String(256), default="")
    comment: Mapped[str] = mapped_column(String(1024), default="")
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TcmFormula(Base):
    """⑭中药制剂配方：院内制剂的处方组成、工艺与适应症（编码唯一）。"""

    __tablename__ = "tcm_formulas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    # 剂型：pill=丸剂, powder=散剂, paste=膏剂, granule=颗粒剂, decoction=合剂/汤剂
    dosage_form: Mapped[str] = mapped_column(String(16), default="decoction", index=True)
    composition: Mapped[str] = mapped_column(String(1024), default="")
    process: Mapped[str] = mapped_column(String(1024), default="")
    indication: Mapped[str] = mapped_column(String(512), default="")
    # 有效期（月），批次生产时据此推算效期
    shelf_life_months: Mapped[int] = mapped_column(Integer, default=12)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    batches: Mapped[list["TcmPreparationBatch"]] = relationship(back_populates="formula")


class TcmPreparationBatch(Base):
    """⑭中药制剂批次：批号、产量、生产与效期，过期批次禁止发放。"""

    __tablename__ = "tcm_preparation_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    formula_id: Mapped[int] = mapped_column(ForeignKey("tcm_formulas.id"), index=True)
    batch_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    unit: Mapped[str] = mapped_column(String(16), default="剂")
    produced_date: Mapped[str] = mapped_column(String(10), default="")
    expire_date: Mapped[str] = mapped_column(String(10), default="", index=True)
    # produced=已生产, released=已发放, recalled=已召回
    status: Mapped[str] = mapped_column(String(16), default="produced", index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    formula: Mapped[TcmFormula] = relationship(back_populates="batches")


class TcmMasterCase(Base):
    """⑬名老中医经验数字化传承：医案与按语。

    与知识库分表而不是塞进 `knowledge_entries` 的一个新 category：
    医案有它自己的结构（四诊、辨证、治法、处方、按语），塞进一个通用 body
    字段等于把结构化的东西压成一段文本，此后再也检索不出"某某老师治痹证
    常用哪几味药"。

    **不做疗效自动判定**：医案的价值在于传承思路，平台不替人下"这个方子
    有效"的结论。
    """

    __tablename__ = "tcm_master_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    master_name: Mapped[str] = mapped_column(String(64), index=True)
    successor_name: Mapped[str] = mapped_column(String(64), default="")
    title: Mapped[str] = mapped_column(String(256))
    disease: Mapped[str] = mapped_column(String(128), default="", index=True)
    syndrome: Mapped[str] = mapped_column(String(128), default="", index=True)
    # 四诊摘要 / 辨证 / 治法 / 处方 / 按语，分列存储便于按维度检索
    four_exams: Mapped[str] = mapped_column(String(2048), default="")
    treatment_method: Mapped[str] = mapped_column(String(512), default="")
    prescription: Mapped[str] = mapped_column(String(1024), default="")
    commentary: Mapped[str] = mapped_column(String(2048), default="")
    visit_date: Mapped[str] = mapped_column(String(10), default="")
    published: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
