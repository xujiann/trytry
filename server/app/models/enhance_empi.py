"""S14 EMPI 跨机构主索引合并（merge/link）。

`Patient` 是全县主索引（core.py），建档期用身份证号去重（patients.py）。但真实县域
里同一个人仍会落成多条主档：身份证录错、无证儿童先建临时档、多头建档没并起来。
这类"运行期发现的重复"要能由管理员把重复档**link 到主档**，并可在误并时**revert**。

本表只记录"谁是谁的重复"这条关系，不搬动、不改写任何一条 `Patient` 行——
合并是一层可解释、可回退的指向，而不是把重复档物理删掉（删了就回不来）。

## 唯一约束与 revert 设计（design (b)：revert = 物理删除该 link 行）

`uq_merge_duplicate` 钉在 `duplicate_patient_id` 上，与 status 无关：一条重复档在
任一时刻最多被合并进一个主档。这条 DB 唯一约束是并发安全的兜底（配合
`concurrency.insert_or_conflict`），两个管理员同时把同一重复档并入不同主档时，
只有一个能成功，另一个拿到可读的 409，而不是并发建出两条相互矛盾的合并关系。

由此**revert 只能是删除该行**：若保留行并把 status 改成 "reverted"，那条
`duplicate_patient_id` 仍占着唯一约束，后续再也无法对同一重复档重新合并——
而"误并→撤销→重新并对"是主索引运维的正常动作，必须走得通。删除行则把
`duplicate_patient_id` 让出来，重新合并即可。

`status` 列仍按 S14 建表口径保留（默认 "merged"，取值 merged/reverted），用于
schema 兼容与未来软状态扩展；在本实现下**存活的行恒为 "merged"**（reverted 的行
已被删除），审计上"存在一行"即等价于"当前是一条生效合并"。
"""
from datetime import datetime

from ._shared import (
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


class PatientMergeLink(Base):
    """主索引合并链：把 duplicate_patient_id 指向 primary_patient_id。

    一条重复档最多被合并进一个主档（`uq_merge_duplicate`）。
    """

    __tablename__ = "patient_merge_links"
    __table_args__ = (
        UniqueConstraint("duplicate_patient_id", name="uq_merge_duplicate"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 主档（保留、作为规范记录）
    primary_patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id"), index=True
    )
    # 重复档（被合并方，一条最多被合并一次）
    duplicate_patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id"), index=True
    )
    reason: Mapped[str] = mapped_column(String(256), default="")
    # merged / reverted；本实现下存活行恒为 merged（revert 即删除该行）
    status: Mapped[str] = mapped_column(String(16), default="merged")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
