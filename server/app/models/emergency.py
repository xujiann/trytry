"""ORM 模型 · 急诊急救：接诊、时间节点、生命体征、急救资源。

由原 `models.py`（3989 行 / 187 类）分域拆出，见 ADR-0008。
**类的先后顺序保持原文件不变**——本仓库没开 `from __future__ import annotations`，
`Mapped[SomeClass]` 这类注解在建类时就要求被引用的类已经定义。
"""
from datetime import datetime

from sqlalchemy import (
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


class EmergencyCase(Base):
    """⑦智慧急救：呼救→出车→到达→入院，院前院内信息互通。"""

    __tablename__ = "emergency_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), nullable=True)
    caller_phone: Mapped[str] = mapped_column(String(20), default="")
    location: Mapped[str] = mapped_column(String(256))
    symptom: Mapped[str] = mapped_column(String(512), default="")
    ambulance_no: Mapped[str] = mapped_column(String(32), default="")
    dest_org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    # 急救绿色通道类型：""=普通, chest_pain=胸痛, stroke=卒中, trauma=创伤
    channel_type: Mapped[str] = mapped_column(String(16), default="", index=True)
    # dispatched=已调度, en_route=转运中, arrived=已到院, admitted=已收治
    status: Mapped[str] = mapped_column(String(16), default="dispatched", index=True)
    # 抢救转归：""=未判定（非抢救病例或尚未结论）, success=抢救成功, failed=抢救无效。
    # 空串与 failed 必须分开——把"没填"当成"没救过来"，抢救成功率会被算低。
    rescue_outcome: Mapped[str] = mapped_column(String(16), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    vitals: Mapped[list["EmergencyVital"]] = relationship(back_populates="case")
    milestones: Mapped[list["EmergencyMilestone"]] = relationship(back_populates="case")


class EmergencyMilestone(Base):
    """急救绿道时间节点：发病→呼救→出车→到达现场→到院→开始救治，绿道时效分析依据。"""

    __tablename__ = "emergency_milestones"
    __table_args__ = (
        UniqueConstraint("case_id", "milestone", name="uq_emergency_milestone_case"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("emergency_cases.id"), index=True)
    # onset=发病, call=呼救, depart=出车, arrive_scene=到达现场,
    # arrive_hospital=到达医院, treatment=开始救治
    milestone: Mapped[str] = mapped_column(String(16))
    # 发生时刻（字符串，如 "2026-08-10 14:32"）
    occurred_at: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    case: Mapped["EmergencyCase"] = relationship(back_populates="milestones")


class EmergencyVital(Base):
    __tablename__ = "emergency_vitals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("emergency_cases.id"), index=True)
    heart_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    sbp: Mapped[float | None] = mapped_column(Float, nullable=True)
    dbp: Mapped[float | None] = mapped_column(Float, nullable=True)
    spo2: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    case: Mapped[EmergencyCase] = relationship(back_populates="vitals")


class EmergencyResource(Base):
    """㉖卫生应急资源保障：物资与队伍台账。

    物资与队伍合一张表而不是两张：两者在应急调度时是同一件事——
    "手上有什么、在哪、够不够、谁负责"。分表只会让调度时要查两处。
    """

    __tablename__ = "emergency_resources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # material=应急物资, team=应急队伍, equipment=应急装备
    resource_type: Mapped[str] = mapped_column(String(16), default="material", index=True)
    name: Mapped[str] = mapped_column(String(128))
    # 数量：物资是件数，队伍是人数
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    unit: Mapped[str] = mapped_column(String(16), default="")
    # 储备下限：低于即在保障情况里标红。0 表示不设下限。
    min_quantity: Mapped[int] = mapped_column(Integer, default=0)
    # 物资效期（队伍留空）
    expire_date: Mapped[str] = mapped_column(String(10), default="", index=True)
    contact: Mapped[str] = mapped_column(String(64), default="")
    location: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
