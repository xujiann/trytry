"""S16 医共体治理架构：章程 / 理事会·管委会 / 编制周转池（县管乡用）。

治理是**医共体这一联合体自身**的组织事务，不属于任何单个成员机构——
章程一份、理事会一套、编制周转池全县统筹。因此这几张表刻意都不挂 org_id
作为"归属机构"，成员/调配里出现的 org_id 是被治理的对象，不是数据的主人。
可见性上它们是 consortium 级管理记录，只有全域角色（admin/director）看得到、
写得动，不做机构横向切分（详见 routers/enhance_governance.py）。
"""
from datetime import datetime

from ._shared import (
    Base,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Mapped,
    String,
    UniqueConstraint,
    mapped_column,
    relationship,
    utcnow,
)


class ConsortiumCharter(Base):
    """医共体章程：联合体的根本制度文本，逐版留存。

    同一标题可有多个版本（修订换版），故唯一约束落在 (title, version) 上而非
    单独 title——章程修订是常态，旧版仍要能查到。全局只应有一份"现行"章程，
    这由 active 单选保证（激活一份即熄灭其余，见路由），不放进唯一约束里，
    因为"当前生效"是可变状态，不是天然的身份键。
    """

    __tablename__ = "consortium_charters"
    __table_args__ = (
        UniqueConstraint("title", "version", name="uq_charter_title_version"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(128))
    version: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(String(4000), default="")
    effective_date: Mapped[str] = mapped_column(String(10), default="")
    # 现行标志：全局仅一份为真，激活时把其余全部置假
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class GovernanceBody(Base):
    """治理机构：理事会 / 管委会 / 监事会。

    联合体的决策与监督载体，一个医共体通常一套。body_type 区分三类，
    成员挂在下面（GovernanceMember）。
    """

    __tablename__ = "governance_bodies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    # council=理事会, committee=管委会, supervisory=监事会
    body_type: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    members: Mapped[list["GovernanceMember"]] = relationship(back_populates="body")


class GovernanceMember(Base):
    """治理机构成员：某人以某职务代表某机构进入某治理机构。

    org_id 记的是"这名成员代表哪家成员单位"，是被治理的对象而非这条记录的
    归属机构——治理机构本身属于联合体。
    """

    __tablename__ = "governance_members"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    body_id: Mapped[int] = mapped_column(ForeignKey("governance_bodies.id"), index=True)
    member_name: Mapped[str] = mapped_column(String(64))
    # 职务：理事长/副理事长/委员/监事…
    title: Mapped[str] = mapped_column(String(32), default="")
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    body: Mapped["GovernanceBody"] = relationship(back_populates="members")


class StaffPool(Base):
    """编制周转池·县管乡用 / 轮岗 / 支援：人员在成员机构间的统筹调配。

    紧密型医共体的核心机制之一——编制县级统一管理、人员下沉到乡镇使用。
    一条记录是一次调配：某人（可关联 employees，也可只留姓名）从 from_org 调到
    to_org，类型分县管乡用/轮岗/支援，起止日期与状态各存一列。employee_id 可空，
    容纳尚未在 employees 建档的外部支援人员。
    """

    __tablename__ = "staff_pools"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 可关联在编职工；外部支援等未建档人员留空，仅凭 member_name
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id"), nullable=True, index=True
    )
    member_name: Mapped[str] = mapped_column(String(64), default="")
    from_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    to_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # county_manage_town_use=县管乡用, rotation=轮岗, support=支援
    assign_type: Mapped[str] = mapped_column(String(24))
    start_date: Mapped[str] = mapped_column(String(10), default="")
    end_date: Mapped[str] = mapped_column(String(10), default="")
    # active=在调配中, ended=已结束
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
