"""E8 居民端从"可查"到"可办"：居家监测自报、续方申请。

在线复诊/图文咨询复用既有 `OnlineConsult`；账单在线支付复用 `PaymentOrder` 网关；
检查检验报告查看复用 `ExamReport`——故此处只需两张新表承载"居民主动产生的数据"。
"""
from datetime import datetime

from ._shared import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    utcnow,
    Mapped,
    mapped_column,
)
from ..database import Base


class HomeMonitorReading(Base):
    """居家监测自报：居民端上传血压/血糖/体重，异常自动标记，供慢病随访消费。"""

    __tablename__ = "home_monitor_readings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    # bp=血压, glucose=血糖, weight=体重
    reading_type: Mapped[str] = mapped_column(String(16), index=True)
    # bp: value1=收缩压 value2=舒张压；glucose: value1=血糖；weight: value1=体重
    value1: Mapped[float] = mapped_column(Float, default=0)
    value2: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str] = mapped_column(String(16), default="")
    measured_at: Mapped[str] = mapped_column(String(16), default="")
    abnormal: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RefillRequest(Base):
    """慢病长处方续方申请：居民发起→医师审核→药房配送（接 E5 配送）。"""

    __tablename__ = "refill_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # 关联原处方（可空，居民也可自由描述）
    prescription_id: Mapped[int | None] = mapped_column(
        ForeignKey("prescriptions.id"), nullable=True
    )
    drug_summary: Mapped[str] = mapped_column(String(256), default="")
    # S2：结构化药品与数量（可选），配送/发药时据此扣减机构库存
    drug_code: Mapped[str] = mapped_column(String(64), default="")
    qty: Mapped[int] = mapped_column(Integer, default=0)
    # pending=待审, approved=已通过, rejected=已驳回, dispensed=已配送
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    reviewed_by: Mapped[str] = mapped_column(String(64), default="")
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
