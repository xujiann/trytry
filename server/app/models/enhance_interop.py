"""E9 互操作标准化：省平台上报连接器骨架的落库载体。

FHIR 资源扩展与 CDA 导出是无状态序列化（读既有数据即可），无需新表；
唯一需要落库的是"上报单"——把 esb 的 route 抽象成外部适配器后，
每次上报留一条可追踪的记录（排队→提交→回执/失败）。
"""
from datetime import datetime

from ._shared import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    String,
    utcnow,
    Mapped,
    mapped_column,
)
from ..database import Base


class ProvincialReport(Base):
    """省市监管平台上报单（连接器骨架；实盘投递属外部依赖，此处走 mock 适配器）。"""

    __tablename__ = "provincial_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 上报类型：infectious/chronic/vaccination/settlement…（描述性）
    report_type: Mapped[str] = mapped_column(String(32), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # 目标平台标识（provincial/municipal/national…）
    target: Mapped[str] = mapped_column(String(32), default="provincial")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # queued=待报, submitted=已提交, acked=已回执, failed=失败
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    ack: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
