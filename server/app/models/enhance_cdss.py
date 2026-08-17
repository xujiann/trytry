"""E7 临床决策支持（CDSS）深化的数据模型。

在既有 `DrugRule`（合理用药规则库）之外补齐 CDSS 深化所需的两块结构化载体：

1. 结构化过敏史（`PatientAllergy`）——原先过敏史只散落在处方审方的诊断文本里，
   无法在开方前按结构化字段做过敏冲突核查。这里独立成表，按患者维度沉淀。
2. 临床路径（`ClinicalPathway` / `PathwayNode` / `PatientPathway` / `PathwayVariance`）——
   路径目录 + 节点医嘱套餐 + 患者入径 + 变异记录，支撑"入径—按日医嘱—变异—出径"闭环。

均带 `patient_id + org_id`（过敏史、患者入径）的表会被 `visibility._relation_tables`
自动纳入"本机构服务过该患者"的服务关系推导，无需手工登记。
"""
from datetime import datetime

from ._shared import (Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint,
                      utcnow, Mapped, mapped_column, relationship)
from ..database import Base


class PatientAllergy(Base):
    """结构化过敏史：按患者维度沉淀，供开方前过敏冲突核查使用。"""

    __tablename__ = "patient_allergies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # 过敏原：药品编码（drug）或自由文本（other）
    allergen: Mapped[str] = mapped_column(String(64))
    # drug=药品过敏（allergen 存药品编码，参与交互核查）, food/other=食物/其他
    allergen_type: Mapped[str] = mapped_column(String(16), default="drug")
    # 严重程度（自由分级：mild/moderate/severe 或空）
    severity: Mapped[str] = mapped_column(String(8), default="")
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ClinicalPathway(Base):
    """临床路径目录：一个病种一条路径，含若干按日排布的节点（医嘱套餐）。"""

    __tablename__ = "clinical_pathways"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    disease: Mapped[str] = mapped_column(String(64), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    nodes: Mapped[list["PathwayNode"]] = relationship(back_populates="pathway")


class PathwayNode(Base):
    """路径节点/医嘱套餐：某条路径第 day_no 天的一项标准医嘱。"""

    __tablename__ = "pathway_nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pathway_id: Mapped[int] = mapped_column(ForeignKey("clinical_pathways.id"), index=True)
    day_no: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(64))
    # exam=检查, lab=检验, drug=用药, nursing=护理, other=其他
    order_type: Mapped[str] = mapped_column(String(24))
    detail: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    pathway: Mapped[ClinicalPathway] = relationship(back_populates="nodes")


class PatientPathway(Base):
    """患者入径：某患者在某机构按某条路径管理的一次入径记录。"""

    __tablename__ = "patient_pathways"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    pathway_id: Mapped[int] = mapped_column(ForeignKey("clinical_pathways.id"), index=True)
    # in_path=在径, variant=已变异, completed=已完成（出径）
    status: Mapped[str] = mapped_column(String(16), default="in_path")
    # 入径日期（YYYY-MM-DD），据此 + 节点 day_no 推算当日医嘱
    enrolled_date: Mapped[str] = mapped_column(String(10), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PathwayVariance(Base):
    """变异记录：入径过程中偏离标准路径的一次记录。"""

    __tablename__ = "pathway_variances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_pathway_id: Mapped[int] = mapped_column(
        ForeignKey("patient_pathways.id"), index=True
    )
    # 关联节点（松引用，0 表示未指向具体节点）
    node_id: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
