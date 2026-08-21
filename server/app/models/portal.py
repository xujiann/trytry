"""ORM 模型 · 居民端账号：账号、验证码、家庭成员、就诊凭证。

由原 `models.py`（3989 行 / 187 类）分域拆出，见 ADR-0008。
**类的先后顺序保持原文件不变**——本仓库没开 `from __future__ import annotations`，
`Mapped[SomeClass]` 这类注解在建类时就要求被引用的类已经定义。
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ._base import utcnow


class ResidentAccount(Base):
    """居民账户：登录凭据（手机号/微信 openid）与患者主索引的绑定关系。

    账户与档案是两件事——先登录拿到账户身份，再实名绑定到 Patient 才看得到档案。
    未绑定的账户只能看健康宣教，这样验证码泄露也不会直接泄露他人档案。

    phone / wechat_openid 用可空唯一列而非空串：SQLite 与 PostgreSQL 的唯一索引
    都允许多个 NULL，既能让"只有微信没有手机号"的账户共存，又能靠数据库约束
    挡住并发首登产生的重复账户（撞约束后回查既有账户即可）。
    """

    __tablename__ = "resident_accounts"
    __table_args__ = (
        # "一份档案只绑一个账户"下沉到数据库：应用层查重是 check-then-act，
        # 并发下两个账户可同时绑上同一份档案（与基金池 D-2 同形）。部分唯一
        # 索引放行多个 NULL（未绑定账户共存），绑定列上唯一。
        Index(
            "uq_resident_account_patient",
            "patient_id",
            unique=True,
            sqlite_where=text("patient_id IS NOT NULL"),
            postgresql_where=text("patient_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    wechat_openid: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    wechat_unionid: Mapped[str] = mapped_column(String(64), default="")
    nickname: Mapped[str] = mapped_column(String(64), default="")
    # 实名绑定后的患者主索引；未绑定为 None
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), nullable=True, index=True)
    # active=正常, disabled=停用（停用后令牌校验即失败）
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SmsCode(Base):
    """短信验证码：只落散列不落明文，与口令同等对待（6位码空间小，用 PBKDF2）。"""

    __tablename__ = "sms_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(String(20), index=True)
    # login=登录, bind=已登录账户绑定手机号
    purpose: Mapped[str] = mapped_column(String(16), default="login")
    code_hash: Mapped[str] = mapped_column(String(160))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    # 单条验证码的试错次数，超限即作废，防止对同一条码穷举
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ResidentFamilyMember(Base):
    """家庭成员代管：一个账户代管家人的档案（老人、儿童往往不自己用手机）。

    与 ResidentAccount.patient_id（本人档案）分开存：本人档案唯一且由实名绑定
    产生，代管关系可以有多条，且同一份档案可被多个子女同时代管。
    """

    __tablename__ = "resident_family_members"
    __table_args__ = (
        UniqueConstraint("account_id", "patient_id", name="uq_family_account_patient"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("resident_accounts.id"), index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    # spouse=配偶, child=子女, parent=父母, other=其他
    relation: Mapped[str] = mapped_column(String(16), default="other")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class VisitCredential(Base):
    """就诊凭据（浙江省指南 #27）：发放、回收、作废。

    与电子健康卡号（`patients.ehc_no`）并存而不是替代它：健康卡号是**身份**，
    终身唯一、不回收；就诊凭据是**介质**，卡片会丢、临时条码会过期，
    丢了要挂失重发，重发后旧的必须立刻失效。把两者混成一个字段，
    就等于患者一丢卡就得换身份。

    同一患者可以有多张历史凭据，但**同时只允许一张 active**——发新的自动
    作废旧的，避免挂失后旧卡还能用。
    """

    __tablename__ = "visit_credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    credential_no: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # 发放机构。第九轮补：原先这张表只有 patient_id，答不出"这张卡是哪家发的"。
    # 后果不止是统计缺一维——横向隔离按"本机构服务过的患者"判可见性，
    # 而发卡这个动作因为没记机构，**发卡机构反而看不到自己发的卡**。
    org_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id"), nullable=True, index=True
    )
    # card=实体就诊卡, qrcode=电子二维码, temp=临时凭据（无证件急诊等）
    credential_type: Mapped[str] = mapped_column(String(16), default="card", index=True)
    # active=有效, recycled=已回收（患者主动交回）, void=已作废（挂失/换发）
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    issued_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    close_reason: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
