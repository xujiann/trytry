"""核心数据模型：对应规划第一期"平台支撑层 + 数据中心层"基础实体。

- User            统一认证用户（医共体工作人员账号）
- Organization    医共体成员单位（牵头医院/乡镇卫生院/村卫生室等）
- Patient         患者主索引（EMPI，电子健康卡号为对外统一标识）
- CodeSystem/CodeEntry  统一编码字典（诊断、药品、耗材、收费"四统一"）
- Referral        双向转诊记录（上转/下转，状态流转）
"""
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    full_name: Mapped[str] = mapped_column(String(64), default="")
    # admin=平台管理员, doctor=医师, operator=经办人员
    role: Mapped[str] = mapped_column(String(32), default="operator")
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    # lead_hospital=牵头医院, township=乡镇卫生院/社区卫生服务中心, village=村卫生室, public_health=公卫机构
    org_type: Mapped[str] = mapped_column(String(32))
    # 层级：county=县级, township=乡级, village=村级
    level: Mapped[str] = mapped_column(String(16))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    address: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    children: Mapped[list["Organization"]] = relationship()


class Patient(Base):
    __tablename__ = "patients"
    __table_args__ = (UniqueConstraint("id_card", name="uq_patient_id_card"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 电子健康卡号（主索引对外标识），由平台生成
    ehc_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    id_card: Mapped[str] = mapped_column(String(18), index=True)
    gender: Mapped[str] = mapped_column(String(8), default="未知")
    birth_date: Mapped[str] = mapped_column(String(10), default="")
    phone: Mapped[str] = mapped_column(String(20), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CodeSystem(Base):
    __tablename__ = "code_systems"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # diagnosis=诊断(ICD-10), drug=药品, consumable=耗材, charge=收费
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))

    entries: Mapped[list["CodeEntry"]] = relationship(back_populates="system")


class CodeEntry(Base):
    __tablename__ = "code_entries"
    __table_args__ = (UniqueConstraint("system_id", "code", name="uq_entry_system_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    system_id: Mapped[int] = mapped_column(ForeignKey("code_systems.id"), index=True)
    code: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(256), index=True)

    system: Mapped[CodeSystem] = relationship(back_populates="entries")


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
