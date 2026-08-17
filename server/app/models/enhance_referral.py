"""E3 双向转诊闭环深化：病历随转、下转康复计划/回复单、转诊号源预留。

在既有 `Referral`（core.py，纯状态机）之上补三处闭环：
- ReferralClinicalRef：转诊单挂接就诊摘要/检查结论/用药（引用既有对象，不复制病历）。
- ReferralPlan：上级出"下转康复计划"，基层回填"下转回复单"，形成上下闭环。
- ReferralSlotReservation：接诊时为患者从上级号源池预留一个号（复用 appointments 原子占位）。
"""
from datetime import datetime

from ._shared import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    utcnow,
    Mapped,
    mapped_column,
)
from ..database import Base


class ReferralClinicalRef(Base):
    """病历随转：转诊单引用一条既有临床对象（不搬运原文，只挂引用+摘要）。"""

    __tablename__ = "referral_clinical_refs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    referral_id: Mapped[int] = mapped_column(ForeignKey("referrals.id"), index=True)
    # encounter=就诊, exam_report=检查报告, prescription=处方, summary=病情摘要
    ref_type: Mapped[str] = mapped_column(String(24))
    ref_id: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(String(1024), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReferralPlan(Base):
    """下转闭环：rehab_plan=上级下转康复计划；reply=基层接诊回复单。"""

    __tablename__ = "referral_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    referral_id: Mapped[int] = mapped_column(ForeignKey("referrals.id"), index=True)
    plan_type: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(String(2048), default="")
    # 出具机构（康复计划=上转接收上级；回复单=下转接收基层）
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReferralSlotReservation(Base):
    """转诊号源预留：一条转诊最多预留一个号（唯一），复用 appointments 原子占位。"""

    __tablename__ = "referral_slot_reservations"
    __table_args__ = (
        UniqueConstraint("referral_id", name="uq_referral_slot_reservation"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    referral_id: Mapped[int] = mapped_column(ForeignKey("referrals.id"), index=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey("appointment_slots.id"), index=True)
    appointment_id: Mapped[int] = mapped_column(ForeignKey("appointments.id"))
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    reserved_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(16), default="reserved")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
