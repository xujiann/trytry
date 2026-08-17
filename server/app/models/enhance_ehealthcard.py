"""S15 电子健康卡一卡通：一人一卡，跨机构就诊核身、绑定医保电子凭证。

`Patient` 表已有 `ehc_no`（电子健康卡号，主索引对外标识），但那只是**号**，
不是一张可展码核身、可绑医保凭证、可冻结挂失的**卡**。本模块把"卡"作为
独立实体落库：

- `card_no` / `qr_token` 由患者稳定标识**确定性派生**（SM3 杂凑），不引入随机源——
  平台在部分路径禁用不确定性（见规划的可复现性约束），且患者 id 唯一，
  派生值天然不撞。
- 一患者至多一张卡（`patient_id` 唯一），发卡走 `insert_or_conflict` 防并发重复。
- `qr_token` 是展码核身的凭证：核身入口按 token 反查患者，等同刷卡的读卡动作。
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


class EHealthCard(Base):
    """电子健康卡（一卡通）。一患者一卡，承载展码核身与医保凭证绑定。"""

    __tablename__ = "ehealth_cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 一患者一卡：patient_id 唯一索引
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id"), unique=True, index=True
    )
    # 卡面号：确定性派生（"EHC" + SM3 前 12 位十六进制）
    card_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    # 展码核身凭证：确定性派生（SM3 十六进制，64 位）
    qr_token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # 绑定的医保电子凭证号（医保移动支付/一码通用）
    medical_voucher: Mapped[str] = mapped_column(String(64), default="")
    # active=正常, frozen=冻结（挂失/停用）
    status: Mapped[str] = mapped_column(String(16), default="active")
    issued_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
