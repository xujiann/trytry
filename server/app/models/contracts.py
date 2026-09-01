"""ORM 模型 · 家医签约与服务：签约、预约、转诊、上门、满意度。

由原 `models.py`（3989 行 / 187 类）分域拆出，见 ADR-0008。
**类的先后顺序保持原文件不变**——本仓库没开 `from __future__ import annotations`，
`Mapped[SomeClass]` 这类注解在建类时就要求被引用的类已经定义。
"""
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ._base import utcnow


class FamilyDoctorContract(Base):
    """家庭医生签约：协议、服务包、履约记录。"""

    __tablename__ = "fd_contracts"
    __table_args__ = (UniqueConstraint("patient_id", "org_id", name="uq_contract_patient_org"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    doctor_name: Mapped[str] = mapped_column(String(64))
    # basic=基础包, standard=标准包, premium=个性包
    package: Mapped[str] = mapped_column(String(16), default="basic")
    signed_date: Mapped[str] = mapped_column(String(10), default="")
    # active=履约中, terminated=已解约
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    services: Mapped[list["ContractService"]] = relationship(back_populates="contract")


class ContractService(Base):
    __tablename__ = "fd_contract_services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("fd_contracts.id"), index=True)
    # visit=上门服务, consult=健康咨询, followup=随访, referral=转诊协助
    service_type: Mapped[str] = mapped_column(String(16))
    note: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    contract: Mapped[FamilyDoctorContract] = relationship(back_populates="services")


class AppointmentSlot(Base):
    """预约资源：机构发布分时段号源（门诊/检查/检验）。"""

    __tablename__ = "appointment_slots"
    __table_args__ = (
        # 同机构+同医师+同资源+同日期+同时段只应有一条号源：重复建出来的两条各带
        # capacity，放号量凭空翻倍，超出的号最终无人可看。批量生成接口本就把这五元组
        # 当幂等键用（且已在防御性接 IntegrityError，注释写着"若后续加约束"），
        # 缺的正是库里这道兜底。
        # **拆成两条部分索引**是因为 SQL 里 NULL != NULL：检查/检验号源不挂医师
        # （employee_id 为 NULL），单一复合唯一索引对这类号源等于不设防。
        Index(
            "uq_slot_with_employee",
            "org_id", "employee_id", "resource_type", "resource_name", "slot_date", "slot_time",
            unique=True,
            sqlite_where=text("employee_id IS NOT NULL"),
            postgresql_where=text("employee_id IS NOT NULL"),
        ),
        Index(
            "uq_slot_without_employee",
            "org_id", "resource_type", "resource_name", "slot_date", "slot_time",
            unique=True,
            sqlite_where=text("employee_id IS NULL"),
            postgresql_where=text("employee_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # outpatient=门诊, exam=检查, lab=检验
    resource_type: Mapped[str] = mapped_column(String(16))
    resource_name: Mapped[str] = mapped_column(String(128))
    # ⑨便捷寻医：号源挂到医师档案上。原先只有 resource_name 自由文本，
    # "找王主任的号"只能靠字符串匹配——同名、写法不一都会漏。可空是因为
    # 检查与检验号源本就不对应某一位医师。
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id"), nullable=True, index=True
    )
    slot_date: Mapped[str] = mapped_column(String(10), index=True)
    slot_time: Mapped[str] = mapped_column(String(16), default="")
    capacity: Mapped[int] = mapped_column(Integer, default=1)
    booked: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (
        UniqueConstraint("slot_id", "patient_id", name="uq_appointment_slot_patient"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey("appointment_slots.id"), index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    # booked=已预约, cancelled=已取消, fulfilled=已就诊
    status: Mapped[str] = mapped_column(String(16), default="booked")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReferralCert(Base):
    """⑲转诊证明：基于已接诊/结案的转诊记录签发。"""

    __tablename__ = "referral_certs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    referral_id: Mapped[int] = mapped_column(ForeignKey("referrals.id"), unique=True)
    cert_no: Mapped[str] = mapped_column(String(32), unique=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ServiceBlacklist(Base):
    """通用服务黑名单（⑫预约、⑮缺药登记）。

    指引第 12 与第 15 条各要一个黑名单。原先只有预约那一个，补第二个时
    有两条路：再建一张几乎一样的表，或者给现有的加一个业务域。选后者——
    第三个域（比如上门护理反复放空）迟早会来，届时又要第三张表。

    唯一约束是 (domain, patient_id)：同一人可以同时在预约黑名单和缺药
    黑名单里，两者互不影响。
    """

    __tablename__ = "service_blacklists"
    __table_args__ = (
        UniqueConstraint("domain", "patient_id", name="uq_service_blacklist"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # appointment=预约爽约, shortage=缺药登记后不取药
    domain: Mapped[str] = mapped_column(String(16), default="appointment", index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    reason: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class HealthArticle(Base):
    """⑨⑩健康宣教内容管理。"""

    __tablename__ = "health_articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(256))
    category: Mapped[str] = mapped_column(String(32), default="general")
    content: Mapped[str] = mapped_column(String(4096), default="")
    # draft=草稿, published=已发布（居民端可见）
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SatisfactionSurvey(Base):
    """⑪㉟满意度调查（签约服务/就诊等通用评价）。"""

    __tablename__ = "satisfaction_surveys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # contract=家医签约, encounter=就诊, consultation=会诊
    target_type: Mapped[str] = mapped_column(String(16), index=True)
    target_id: Mapped[int] = mapped_column(Integer)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    score: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Referral(Base):
    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    from_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    to_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    # up=上转, down=下转
    direction: Mapped[str] = mapped_column(String(8))
    reason: Mapped[str] = mapped_column(String(512), default="")
    # pending=待接诊, accepted=已接诊, completed=已结案, rejected=已退回
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class HomeVisitOrder(Base):
    """⑨上门服务调度（送医送护上门）：申请 → 派单 → 完成，关联家医签约。"""

    __tablename__ = "home_visit_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    contract_id: Mapped[int | None] = mapped_column(ForeignKey("fd_contracts.id"), nullable=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # nursing=上门护理, doctor=上门诊疗, rehab=康复指导, sampling=上门采样
    service_type: Mapped[str] = mapped_column(String(16), index=True)
    demand: Mapped[str] = mapped_column(String(512), default="")
    address: Mapped[str] = mapped_column(String(256), default="")
    expect_date: Mapped[str] = mapped_column(String(10), default="")
    # applied=待派单, dispatched=已派单, completed=已完成, cancelled=已取消
    status: Mapped[str] = mapped_column(String(16), default="applied", index=True)
    assignee_name: Mapped[str] = mapped_column(String(64), default="")
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    service_note: Mapped[str] = mapped_column(String(512), default="")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
