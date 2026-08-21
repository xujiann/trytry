"""ORM 模型 · 主数据与账号权限：机构/人员/患者/字典/角色权限/审计留痕。

由原 `models.py`（3989 行 / 187 类）分域拆出，见 ADR-0008。
**类的先后顺序保持原文件不变**——本仓库没开 `from __future__ import annotations`，
`Mapped[SomeClass]` 这类注解在建类时就要求被引用的类已经定义。
"""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ._base import utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    full_name: Mapped[str] = mapped_column(String(64), default="")
    # admin=平台管理员, director=院长/管理层, doctor=医师, pharmacist=药师,
    # public_health=公卫人员, operator=经办人员
    role: Mapped[str] = mapped_column(String(32), default="operator")
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    # M-4 整改：改密时刻基线——签发时刻(iat)早于该时刻的令牌一律拒绝（改密吊销既有令牌）
    token_valid_from: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    """审计日志：记录全部写操作（等保三级安全审计要求）。"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    username: Mapped[str] = mapped_column(String(64), default="", index=True)
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(String(256), index=True)
    status_code: Mapped[int] = mapped_column(Integer)
    # 防篡改哈希链（阶段十一）：hash = MAC(平台密钥, 本条内容 + 上一条 hash)。
    # 改任何一条历史记录，其后全部记录的链都对不上——**发现得了篡改，
    # 但拦不住有库权限的人重算整条链**。真正的不可抵赖要靠外部存证或
    # 只追加存储，平台侧能做到的是"改过就看得出来"，这一点要说清楚。
    prev_hash: Mapped[str] = mapped_column(String(64), default="")
    entry_hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class AccessLog(Base):
    """敏感读留痕：谁、什么时候、凭什么依据、看了谁的档案。

    与 `AuditLog` 分表而不是并进去，两条理由：

    1. `AuditLog` 只记写操作，且带防篡改哈希链——链要串行取上一条，
       为低频写做的设计。诊疗数据的读远比写频繁，塞进同一条链会把它拖垮。
    2. 两者回答的问题不同。写审计回答"谁改了什么"，读留痕回答
       "谁看了谁的档案、凭什么"——《个人信息保护法》与《医疗卫生机构网络安全
       管理办法》要的是后面这一条，而平台此前一条都没记。

    `basis` 是这张表的核心：跨机构调阅在医共体里是正常业务（转诊、签约、
    结果互认），所以不能只记"访问过"，要记**凭什么访问**。没有这一列，
    事后审计只能看到一片"某医生查了某患者"，分不出哪次是正当的。
    """

    __tablename__ = "access_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    username: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 调阅人所属机构（可空：居民端账号不挂机构）
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    # 调阅的是哪一类数据：archive=健康档案, encounter=就诊记录, exam=检查检验…
    resource: Mapped[str] = mapped_column(String(32), default="", index=True)
    # 依据：global｜encounter｜contract｜referral｜authorization｜self
    basis: Mapped[str] = mapped_column(String(16), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    # lead_hospital=牵头医院, township=乡镇卫生院/社区卫生服务中心, village=村卫生室, public_health=公卫机构
    org_type: Mapped[str] = mapped_column(String(32))
    # 层级：city=市级（协作医院）, county=县级, township=乡级, village=村级
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    entries: Mapped[list["CodeEntry"]] = relationship(back_populates="system")


class CodeEntry(Base):
    __tablename__ = "code_entries"
    __table_args__ = (UniqueConstraint("system_id", "code", name="uq_entry_system_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    system_id: Mapped[int] = mapped_column(ForeignKey("code_systems.id"), index=True)
    code: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(256), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    system: Mapped[CodeSystem] = relationship(back_populates="entries")


class SystemParam(Base):
    """浙#45 系统参数配置：平台运行参数集中管理（管理员维护）。"""

    __tablename__ = "system_params"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    value: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(String(256), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class RoleChangeLog(Base):
    """浙#43 用户角色变更记录：变更前后角色与操作人留痕。"""

    __tablename__ = "role_change_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    old_role: Mapped[str] = mapped_column(String(16))
    new_role: Mapped[str] = mapped_column(String(16))
    changed_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ArchiveAuthorization(Base):
    """浙#59 患者档案调阅授权：授权机构/范围/有效期，可撤销（跨域调阅对接的数据准备）。"""

    __tablename__ = "archive_authorizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    grantee_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # all=全部档案, encounter=就诊记录, exam=检查检验报告
    scope: Mapped[str] = mapped_column(String(16), default="all")
    expire_date: Mapped[str] = mapped_column(String(10), default="")
    # active=有效, revoked=已撤销
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OrgGroup(Base):
    """机构协作分组：机构之间的**横向**分组。

    各地叫法不一——片区、健共体、医共体分片、网格、专科联盟——但结构是同一个，
    所以这里做通用分组而不是叫"片区"。`group_type` 只是标签，不影响任何逻辑，
    平台不为某一种类型写特殊分支。

    与 `Organization.parent_id` 正交：`parent_id` 表达纵向隶属（谁是谁的上级），
    分组表达横向协作（谁和谁归一拨管）。**刻意不做成第二棵树**——做成树，
    跨组调拨、跨组转诊立刻会遇到"到底归谁"的归属冲突，而现实里这两种关系本来
    就不重合：一家卫生院的行政上级与它所属的专科联盟牵头单位常常不是同一家。

    一家机构可以同时属于多个分组（既在某片区，又在某专科联盟），故用关联表。
    """

    __tablename__ = "org_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # zone=片区/分片, alliance=专科联盟, grid=网格, other=其他。仅作标签用途。
    group_type: Mapped[str] = mapped_column(String(16), default="zone", index=True)
    # 牵头机构可空：网格化管理常常没有"牵头单位"这一说
    lead_org_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id"), nullable=True
    )
    note: Mapped[str] = mapped_column(String(256), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OrgGroupMember(Base):
    """分组成员：分组与机构的多对多关联。"""

    __tablename__ = "org_group_members"
    __table_args__ = (UniqueConstraint("group_id", "org_id", name="uq_org_group_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("org_groups.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Resource(Base):
    """通用资源登记（阶段十）。

    **只收没有领域表的资源**——工勤、后勤设施、通用设备、会议室。
    号源、检查资源、手术间、血制品各有自己的表与状态机，进这张表只会造成
    两处维护、两处不一致。统一资源视图靠聚合实现，不靠这张表兜住全部。

    发布/撤回状态也只对通用资源生效：号源有余量、手术间有 active、
    血制品有库存量，各自已经表达了"能不能用"，再压一层发布状态只会打架。
    """

    __tablename__ = "resources"
    __table_args__ = (
        UniqueConstraint("org_id", "code", name="uq_resource_org_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # logistics=工勤服务, facility=后勤设施, equipment=通用设备, meeting_room=会议室
    resource_type: Mapped[str] = mapped_column(String(16), index=True)
    code: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128))
    capacity: Mapped[int] = mapped_column(Integer, default=1)
    unit: Mapped[str] = mapped_column(String(16), default="")
    location: Mapped[str] = mapped_column(String(256), default="")
    contact: Mapped[str] = mapped_column(String(64), default="")
    # draft=草稿, published=已发布, withdrawn=已撤回。新登记一律草稿——
    # 直接发布意味着还没核对完就能被申请到，而填错的代价是有人白跑一趟。
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    withdraw_reason: Mapped[str] = mapped_column(String(256), default="")
    note: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Role(Base):
    """角色。六个内置角色作为预置数据保留，保证现有部署零感知升级。

    **内置角色不可删不可改 key**：删掉 doctor 这一行，全平台 200 多处
    `require_roles("doctor")` 会同时失效——那不是"配置"，那是拆平台。
    自定义角色则可增可删，这正是本阶段要给的能力。
    """

    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(256), default="")
    builtin: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Permission(Base):
    """权限点：一个写接口就是一个权限点，`METHOD:path` 为唯一键。

    **由平台启动时从路由表自动登记**，不手工维护——手工维护的权限点清单
    与真实接口的偏差，是这类系统最常见也最难查的问题：清单里有的接口不存在，
    存在的接口清单里没有，而两种偏差都表现为"权限配了不生效"。
    """

    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    method: Mapped[str] = mapped_column(String(8))
    path: Mapped[str] = mapped_column(String(300), index=True)
    # 模块（按路径第二段），用于按模块批量授权
    module: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 内置角色的默认授权（逗号分隔），来自代码里的 require_roles 声明。
    # 只作展示与"复制内置角色"的起点，运行期判定不看它。
    builtin_roles: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RolePermission(Base):
    """角色—权限点关联。只对自定义角色有意义：内置角色的判定仍走代码里的
    `require_roles`，不查库——每个请求多查一次库换不来任何东西。"""

    __tablename__ = "role_permissions"
    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uq_role_permission"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"), index=True)
    permission_id: Mapped[int] = mapped_column(ForeignKey("permissions.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
