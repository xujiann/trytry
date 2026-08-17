"""S12 临床路径自动开立医嘱的数据模型。

E7 的 `enroll_pathway` 只写一条 `PatientPathway`（入径登记），并不把路径节点
（`PathwayNode` 医嘱套餐）落成该患者名下**实际的、可执行/可作废的医嘱**——
于是"入径了却没有医嘱"，护士台上看不到当日该做什么。

`PatientPathwayOrder` 补上这一层：入径时按路径节点逐条物化成患者医嘱，
拷贝节点的 day_no/name/order_type/detail，初始 status="ordered"，
之后可 execute（已执行）/cancel（作废）。它挂在 `patient_pathways` 下，
机构归属随其父入径记录（父带 org_id），不再单独冗余 org_id。
"""
from datetime import datetime

from ._shared import (DateTime, ForeignKey, Integer, String, utcnow, Mapped,
                      mapped_column)
from ..database import Base


class PatientPathwayOrder(Base):
    """患者路径医嘱：由入径时的路径节点物化而来的一条实际医嘱。"""

    __tablename__ = "patient_pathway_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_pathway_id: Mapped[int] = mapped_column(
        ForeignKey("patient_pathways.id"), index=True
    )
    node_id: Mapped[int] = mapped_column(ForeignKey("pathway_nodes.id"), index=True)
    day_no: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(64))
    order_type: Mapped[str] = mapped_column(String(24))
    detail: Mapped[str] = mapped_column(String(256), default="")
    # ordered=已开立, executed=已执行, cancelled=已作废
    status: Mapped[str] = mapped_column(String(16), default="ordered")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
