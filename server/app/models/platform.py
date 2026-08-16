"""领域模型分块（见 app/models/__init__.py 说明）。
所有类共用 _shared 的 Base/Money/工具与 SQLAlchemy 名称；跨块引用一律走
字符串（ForeignKey("t.col") / relationship("Cls")），由 registry 惰性解析，
不依赖 import 顺序。"""
from ._shared import *  # noqa: F401,F403


class WorkflowDefinition(Base):
    """流程定义：节点串成有向链，每个节点声明谁能推进。

    nodes 形如 [{"key":"apply","name":"申请","role":"doctor","next":"review"}, ...]，
    next 为空表示终态。只支持线性流转——县域业务里会签、并行网关极少，
    为它们引入 BPMN 级复杂度不划算。
    """

    __tablename__ = "workflow_definitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    nodes: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WorkflowInstance(Base):
    """流程实例：一张单据在流程里的当前位置。"""

    __tablename__ = "workflow_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    definition_key: Mapped[str] = mapped_column(String(48), index=True)
    # 关联业务单据（类型 + id），不做外键：流程可挂到任何业务对象上
    business_type: Mapped[str] = mapped_column(String(32), index=True)
    business_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    title: Mapped[str] = mapped_column(String(256), default="")
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)
    current_node: Mapped[str] = mapped_column(String(48), index=True)
    # running=流转中, completed=已完成, cancelled=已终止
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class WorkflowTransition(Base):
    """流转留痕：谁在什么时候把单据从哪个节点推到哪个节点。"""

    __tablename__ = "workflow_transitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    instance_id: Mapped[int] = mapped_column(ForeignKey("workflow_instances.id"), index=True)
    from_node: Mapped[str] = mapped_column(String(48), default="")
    to_node: Mapped[str] = mapped_column(String(48), default="")
    # advance=推进, cancel=终止
    action: Mapped[str] = mapped_column(String(16), default="advance")
    comment: Mapped[str] = mapped_column(String(512), default="")
    actor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Notification(Base):
    """站内消息：与 WebSocket 广播互补的**可靠**触达。

    广播是纯内存、瞬时的——人不在线就丢了，多实例下还只送达同实例的连接。
    危急值、手术排期、报告出具这类必须让人看到的事件，需要一条能留存、
    能标记已读、能事后追查的记录。

    收件人二选一：`user_id` 是工作人员，`resident_account_id` 是居民账户。
    不合并成一个"主体表"——两类身份的鉴权路径本来就不同（业务令牌 vs
    scope=portal 令牌），分开存反而让越权更难写错。
    """

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    resident_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("resident_accounts.id"), nullable=True, index=True
    )
    # critical_value=危急值, exam_report=检查报告, surgery=手术安排,
    # followup=随访提醒, system=系统通知
    category: Mapped[str] = mapped_column(String(24), index=True)
    title: Mapped[str] = mapped_column(String(128))
    body: Mapped[str] = mapped_column(String(1024), default="")
    # 关联业务对象，供客户端跳转；不做外键（可指向任意业务表）
    link_type: Mapped[str] = mapped_column(String(32), default="")
    link_id: Mapped[int] = mapped_column(Integer, default=0)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ChargePriceChange(Base):
    """收费项目调价历史（浙江省指南 #55 价格管理与公示）。

    价格是要对外公示的，而公示的前提是**改过什么、什么时候改的、谁改的**
    能说清楚。直接 UPDATE 覆盖 `charge_items.price` 会把这些全抹掉，
    到时候患者质疑"上个月不是这个价"，机构拿不出任何东西。

    已计费的明细不受调价影响——`bill_details` 存的是计费时的价格快照，
    所以这张表纯粹是**对外解释用**的账，不参与任何金额计算。
    """

    __tablename__ = "charge_price_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("charge_items.id"), index=True)
    old_price: Mapped[float] = mapped_column(Money)
    new_price: Mapped[float] = mapped_column(Money)
    # 调价依据（如"省医保局 2026 年第 3 号文"）。留空也允许——真实场景里
    # 补录历史价格时常常找不到原始文号，强制填写只会逼人编一个。
    reason: Mapped[str] = mapped_column(String(256), default="")
    effective_date: Mapped[str] = mapped_column(String(10), default="")
    changed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


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


class ConsentTemplate(Base):
    """知情告知书模板（浙江省指南 #3）。

    模板与签署实例分开：模板会改（法规更新、律师意见），而**已签署的告知书
    必须冻结在签署时的措辞上**——患者签的是当时那份文本，不是今天这份。
    所以签署时把正文整段拷进 `InformedConsent.content`，不留外键引用。
    存储上确实重复，但这是知情同意这件事的本质要求。
    """

    __tablename__ = "consent_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # surgery=手术, anesthesia=麻醉, transfusion=输血, exam=特殊检查,
    # treatment=特殊治疗, other=其他
    consent_type: Mapped[str] = mapped_column(String(16), index=True)
    title: Mapped[str] = mapped_column(String(128))
    body: Mapped[str] = mapped_column(String(8192))
    version: Mapped[str] = mapped_column(String(16), default="v1")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class InformedConsent(Base):
    """知情告知书签署记录。

    三个状态而不是两个：`pending` 待签、`signed` 已签、`refused` **拒绝签署**。
    拒签是真实且必须留痕的临床事件——患者有权拒绝，机构需要证明"告知过、
    对方拒绝了"。把拒签当成"没签"处理，等于把最需要证据的那种情况抹掉。

    签署人可以不是患者本人（未成年、意识障碍、委托家属），所以记签署人姓名
    与关系，而不是简单地指向 patient。
    """

    __tablename__ = "informed_consents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    consent_type: Mapped[str] = mapped_column(String(16), index=True)
    title: Mapped[str] = mapped_column(String(128))
    # 签署时的正文快照（见 ConsentTemplate 的说明，刻意不做外键引用）
    content: Mapped[str] = mapped_column(String(8192), default="")
    template_version: Mapped[str] = mapped_column(String(16), default="")
    # 关联业务对象：surgery_request / transfusion_request / exam_request / encounter
    related_type: Mapped[str] = mapped_column(String(32), default="", index=True)
    related_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    doctor_name: Mapped[str] = mapped_column(String(64), default="")
    # pending=待签署, signed=已签署, refused=拒绝签署
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    signer_name: Mapped[str] = mapped_column(String(64), default="")
    # self=本人, spouse=配偶, parent=父母, child=子女, other=其他委托人
    signer_relation: Mapped[str] = mapped_column(String(16), default="")
    signed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    refuse_reason: Mapped[str] = mapped_column(String(256), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class TreatmentRecord(Base):
    """门（急）诊治疗处置记录（浙江省指南 #3）。

    挂在就诊上而不是患者上：处置是某一次就诊里发生的事，脱离就诊看"这个人
    做过雾化"没有临床意义，也无法核对当次诊断与处置是否匹配。

    `reaction` 记录处置中/后的反应（过敏、晕针、无不适）。留空表示**未记录**，
    不等于"无不适"——这两者在纠纷里的分量完全不同，所以不设默认值。
    """

    __tablename__ = "treatment_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    encounter_id: Mapped[int] = mapped_column(ForeignKey("encounters.id"), index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    treatment_name: Mapped[str] = mapped_column(String(128))
    treatment_code: Mapped[str] = mapped_column(String(64), default="")
    site: Mapped[str] = mapped_column(String(64), default="")          # 部位
    dose: Mapped[str] = mapped_column(String(64), default="")          # 剂量/参数
    executor_name: Mapped[str] = mapped_column(String(64), default="")
    performed_at: Mapped[str] = mapped_column(String(16), default="")
    reaction: Mapped[str] = mapped_column(String(256), default="")
    note: Mapped[str] = mapped_column(String(512), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


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


class FundPool(Base):
    """医保基金池：总额付费的账本主体（可不启用）。

    不建池子的县完全感觉不到这个模块存在——它不参与任何既有统计，
    也不改变结算流程。总额付费本身是地方选择，平台只提供账。

    `org_group_id` 可空：有的县按全域建一个池，有的按片区分池。绑到分组而不是
    机构，因为池子天然是"一群机构共用一笔钱"。
    """

    __tablename__ = "fund_pools"
    __table_args__ = (
        UniqueConstraint("year", "insurance_type", "org_group_id", name="uq_fund_pool_scope"),
        # D-2：上面那条唯一约束管不住全域池——SQL 里 NULL != NULL，`org_group_id`
        # 为空的行彼此不冲突，并发下实测建出过两个池，同一笔结余会被分两次。
        # 应用层查重是 check-then-act，中间有竞态窗口，兜底必须落在数据库上。
        # 部分唯一索引 SQLite 3.8+ 与 PostgreSQL 都支持；国产库（达梦/人大金仓）
        # 若不支持，需在阶段十二适配时改为"全域池写入哨兵值 0 而非 NULL"。
        Index(
            "uq_fund_pool_global",
            "year",
            "insurance_type",
            unique=True,
            sqlite_where=text("org_group_id IS NULL"),
            postgresql_where=text("org_group_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    year: Mapped[int] = mapped_column(Integer, index=True)
    # resident=城乡居民, employee=城镇职工
    insurance_type: Mapped[str] = mapped_column(String(16), default="resident", index=True)
    org_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("org_groups.id"), nullable=True, index=True
    )
    # 筹资总额（元）：年初核定的总盘子
    total_amount: Mapped[float] = mapped_column(Money, default=0)
    prepay_ratio_pct: Mapped[float] = mapped_column(Money, default=0)
    # active=执行中, settled=已清算, closed=已归档
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    note: Mapped[str] = mapped_column(String(256), default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FundPrepayment(Base):
    """预付批次：年初/分批预拨给医共体的资金，**产生真实资金流**。"""

    __tablename__ = "fund_prepayments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("fund_pools.id"), index=True)
    batch_no: Mapped[str] = mapped_column(String(32), default="")
    amount: Mapped[float] = mapped_column(Money)
    paid_date: Mapped[str] = mapped_column(String(10), default="")
    note: Mapped[str] = mapped_column(String(256), default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FundPeriod(Base):
    """周期预结：按月归集实际发生额，与预付对冲看账面进度。

    **预结不产生资金流**，只是账面对冲。真正的钱在预付与年终清算两处流动。
    与预付、清算分表而不是混在一张"资金流水"里——年终对不上账时，
    必须能一眼分清"这笔是账面数还是真给了钱"。
    """

    __tablename__ = "fund_periods"
    __table_args__ = (UniqueConstraint("pool_id", "period", name="uq_fund_period"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("fund_pools.id"), index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)  # YYYY-MM
    # 当期实际发生的医保支付额（由结算单归集，也允许人工调整后覆盖）
    actual_amount: Mapped[float] = mapped_column(Money, default=0)
    source: Mapped[str] = mapped_column(String(16), default="auto")  # auto=系统归集, manual=人工
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FundSettlement(Base):
    """年终清算单：一个池子一次，结出结余或超支。

    `balance` 为负即超支。**平台不自动扣减**——超支怎么办是政策决定
    （分摊/挂账/不处理），这里只记录选择，不代替任何人做决定。
    """

    __tablename__ = "fund_settlements"
    __table_args__ = (UniqueConstraint("pool_id", name="uq_fund_settlement_pool"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pool_id: Mapped[int] = mapped_column(ForeignKey("fund_pools.id"), unique=True, index=True)
    total_income: Mapped[float] = mapped_column(Money, default=0)   # 筹资总额快照
    total_expense: Mapped[float] = mapped_column(Money, default=0)  # 全年实际发生额
    balance: Mapped[float] = mapped_column(Money, default=0)        # 正=结余，负=超支
    # none=不处理（默认）, share=按分配公式分摊, carry=挂账结转
    overrun_action: Mapped[str] = mapped_column(String(16), default="none")
    # 分配公式（AST 白名单求值），返回**份额权重**，由平台归一化后乘结余额。
    # 各县办法不同且逐年调整，绝不写死进代码。
    formula_expr: Mapped[str] = mapped_column(String(256), default="score")
    # 绩效得分快照取自哪一次考核（记录参数，便于复现）
    score_basis: Mapped[str] = mapped_column(String(128), default="")
    settled_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class FundDistribution(Base):
    """结余分配明细：清算后按机构分钱。

    `score` 与 `score_detail` 是**冻结的快照**。绩效指标权重随时可调，
    若分钱时"当下重算"，等于分完钱还能改分——与知情告知书冻结正文同理。
    """

    __tablename__ = "fund_distributions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    settlement_id: Mapped[int] = mapped_column(ForeignKey("fund_settlements.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    score: Mapped[float] = mapped_column(Float, default=0)
    score_detail: Mapped[dict] = mapped_column(JSON, default=dict)
    weight: Mapped[float] = mapped_column(Float, default=0)      # 公式求出的份额权重
    share_pct: Mapped[float] = mapped_column(Float, default=0)   # 归一化后占比
    amount: Mapped[float] = mapped_column(Money, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DiseaseProgram(Base):
    """专病目录：单病种诊疗路径的定义（路径节点可配置）。

    与慢病管理（`ChronicPatient`）**不是一回事**，故另立一套表：慢病是长期
    随访分级（血压血糖录进来、系统定级、到期提醒），专病是一条有始有终的
    诊疗路径（入组—按节点推进—疗效评价—出组）。硬套慢病那套表，会得到一个
    既不像随访也不像路径的模型。

    `path_nodes` 是 JSON 数组，形如
    `[{"key": "assess", "name": "首次评估", "required": true}, ...]`。
    **不预置任何具体病种的路径**——各地专病中心管什么病、分几步，差异极大。
    """

    __tablename__ = "disease_programs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(512), default="")
    # 主办机构（专病中心所在），可空：有的县由医共体统一管而不落到具体机构
    org_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id"), nullable=True, index=True
    )
    path_nodes: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DiseaseEnrollment(Base):
    """专病入组：某患者进入某条专病路径。

    同一患者同一专病同时只允许一条在管记录（`enrolled`），但历史可以有多条——
    治好出组之后复发再入组是常态，不该被"已入组"挡住。
    """

    __tablename__ = "disease_enrollments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    program_id: Mapped[int] = mapped_column(ForeignKey("disease_programs.id"), index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # enrolled=在管, completed=完成路径出组, exited=中途退出
    status: Mapped[str] = mapped_column(String(16), default="enrolled", index=True)
    enrolled_at: Mapped[str] = mapped_column(String(10), default="")
    exited_at: Mapped[str] = mapped_column(String(10), default="")
    # 疗效评价：出组时填。留空表示未评价，不等于"无效"。
    outcome: Mapped[str] = mapped_column(String(16), default="")
    outcome_note: Mapped[str] = mapped_column(String(512), default="")
    exit_reason: Mapped[str] = mapped_column(String(256), default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DiseasePathRecord(Base):
    """路径节点执行记录：某次入组走到了哪一步、谁做的、结果如何。"""

    __tablename__ = "disease_path_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(
        ForeignKey("disease_enrollments.id"), index=True
    )
    node_key: Mapped[str] = mapped_column(String(32), index=True)
    performed_at: Mapped[str] = mapped_column(String(10), default="")
    operator_name: Mapped[str] = mapped_column(String(64), default="")
    result: Mapped[str] = mapped_column(String(256), default="")
    note: Mapped[str] = mapped_column(String(512), default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------- 阶段九·五 第一批：指引 ㉕㉖㊱ 子功能补齐 ----------


class VaccineBatch(Base):
    """㉕疫苗批次：批号、厂家、效期、在库数。

    第七轮子功能级重审发现，`vaccination_records` 只有 6 列，没有批号也没有
    厂家效期——指引第 25 条头一项"疫苗信息查询"根本无从查起。更要紧的是
    **疫苗出问题是按批号召回的**，没有批号就答不出"这批打给了谁"。

    效期按日期现算不设过期状态：与接种禁忌同理，靠定时任务改状态会让
    "何时过期"取决于任务跑没跑，而这条直接决定能不能给人打针。
    """

    __tablename__ = "vaccine_batches"
    __table_args__ = (
        UniqueConstraint("vaccine_code", "batch_no", name="uq_vaccine_batch"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vaccine_code: Mapped[str] = mapped_column(String(64), index=True)
    vaccine_name: Mapped[str] = mapped_column(String(128))
    batch_no: Mapped[str] = mapped_column(String(64), index=True)
    manufacturer: Mapped[str] = mapped_column(String(128), default="")
    expire_date: Mapped[str] = mapped_column(String(10), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    used_quantity: Mapped[int] = mapped_column(Integer, default=0)
    # normal=正常, frozen=封存（超温/召回），封存后不得再用于接种
    status: Mapped[str] = mapped_column(String(16), default="normal", index=True)
    frozen_reason: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ColdChainRecord(Base):
    """㉕冷链监测记录：设备温度与超温处置。

    超温不自动封存批次——封存是有成本的决定（整批报废），
    平台的职责是把超温标出来、把该批次点出来，由人决定封不封。
    与"超支不自动扣减"同一条原则。
    """

    __tablename__ = "cold_chain_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    device_name: Mapped[str] = mapped_column(String(128))
    temperature: Mapped[float] = mapped_column(Float)
    # 该设备的允许区间（不同疫苗要求不同，故记在记录上而不是写死常量）
    min_allowed: Mapped[float] = mapped_column(Float, default=2.0)
    max_allowed: Mapped[float] = mapped_column(Float, default=8.0)
    exceeded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    recorded_at: Mapped[str] = mapped_column(String(19), index=True)
    handled: Mapped[bool] = mapped_column(Boolean, default=False)
    handle_note: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AefiReport(Base):
    """㉕疑似预防接种异常反应（AEFI）报告。

    指引明文列出的子功能，此前平台一处都没有。AEFI 是免疫规划的刚性上报项：
    一般反应（发热、局部红肿）与严重反应（过敏性休克、卡介苗淋巴结炎）
    处置路径完全不同，故分级必填。

    **关联到具体接种记录**而不只是患者：同一人可能打过多种疫苗，
    不落到剂次上就查不出是哪一针引起的。
    """

    __tablename__ = "aefi_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    record_id: Mapped[int | None] = mapped_column(
        ForeignKey("vaccination_records.id"), nullable=True, index=True
    )
    vaccine_code: Mapped[str] = mapped_column(String(64), index=True)
    batch_no: Mapped[str] = mapped_column(String(64), default="", index=True)
    # general=一般反应, severe=严重反应, psychogenic=心因性, coincidental=偶合症
    reaction_type: Mapped[str] = mapped_column(String(16), default="general", index=True)
    symptom: Mapped[str] = mapped_column(String(512))
    onset_date: Mapped[str] = mapped_column(String(10), index=True)
    # 转归：recovered=痊愈, improving=好转中, sequelae=留有后遗症, death=死亡, unknown=未知
    outcome: Mapped[str] = mapped_column(String(16), default="unknown", index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    reported_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WasteLocation(Base):
    """㊱医废点位：产生点与暂存间。

    此前 `medical_wastes` 只有 `org_id`，答得出"哪家机构"，答不出"哪个科室
    产生的、存在哪间暂存间"——而医废管理条例要求的正是点位级的可追溯。
    """

    __tablename__ = "waste_locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    # source=产生点（科室/病区）, storage=暂存间
    location_type: Mapped[str] = mapped_column(String(16), default="source", index=True)
    manager_name: Mapped[str] = mapped_column(String(64), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SyndromeMonitor(Base):
    """㉖症候群监测：按机构按日的症候群就诊计数与阈值预警。

    "智慧化多点触发传染病监测预警体系"的两条腿之一（另一条是病原监测）。
    此前平台只有法定传染病病例上报——那是**确诊之后**的事，
    而症候群监测的意义正是在确诊之前就看出异常聚集。

    阈值随机构规模不同，故存在记录上而不是全局常量：
    县医院发热门诊日均 50 人不算异常，村卫生室 5 人就该看一眼。
    """

    __tablename__ = "syndrome_monitors"
    __table_args__ = (
        UniqueConstraint("org_id", "syndrome", "record_date", name="uq_syndrome_daily"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # fever=发热, respiratory=呼吸道, diarrhea=腹泻, rash=皮疹, jaundice=黄疸, neuro=脑炎脑膜炎
    syndrome: Mapped[str] = mapped_column(String(16), index=True)
    case_count: Mapped[int] = mapped_column(Integer, default=0)
    threshold: Mapped[int] = mapped_column(Integer, default=0)
    record_date: Mapped[str] = mapped_column(String(10), index=True)
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PathogenMonitor(Base):
    """㉖病原监测：标本检出情况。"""

    __tablename__ = "pathogen_monitors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    pathogen_name: Mapped[str] = mapped_column(String(128), index=True)
    # 标本类型：咽拭子/粪便/血液/脑脊液等，自由文本（各院送检口径不一）
    specimen_type: Mapped[str] = mapped_column(String(64), default="")
    tested_count: Mapped[int] = mapped_column(Integer, default=0)
    positive_count: Mapped[int] = mapped_column(Integer, default=0)
    record_date: Mapped[str] = mapped_column(String(10), index=True)
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EmergencyResource(Base):
    """㉖卫生应急资源保障：物资与队伍台账。

    物资与队伍合一张表而不是两张：两者在应急调度时是同一件事——
    "手上有什么、在哪、够不够、谁负责"。分表只会让调度时要查两处。
    """

    __tablename__ = "emergency_resources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # material=应急物资, team=应急队伍, equipment=应急装备
    resource_type: Mapped[str] = mapped_column(String(16), default="material", index=True)
    name: Mapped[str] = mapped_column(String(128))
    # 数量：物资是件数，队伍是人数
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    unit: Mapped[str] = mapped_column(String(16), default="")
    # 储备下限：低于即在保障情况里标红。0 表示不设下限。
    min_quantity: Mapped[int] = mapped_column(Integer, default=0)
    # 物资效期（队伍留空）
    expire_date: Mapped[str] = mapped_column(String(10), default="", index=True)
    contact: Mapped[str] = mapped_column(String(64), default="")
    location: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)



# ---------- 阶段九·五 第三批：⑧一码通、⑨便捷寻医、⑬名老中医传承、㉑模拟诊疗 ----------


class TcmMasterCase(Base):
    """⑬名老中医经验数字化传承：医案与按语。

    与知识库分表而不是塞进 `knowledge_entries` 的一个新 category：
    医案有它自己的结构（四诊、辨证、治法、处方、按语），塞进一个通用 body
    字段等于把结构化的东西压成一段文本，此后再也检索不出"某某老师治痹证
    常用哪几味药"。

    **不做疗效自动判定**：医案的价值在于传承思路，平台不替人下"这个方子
    有效"的结论。
    """

    __tablename__ = "tcm_master_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    master_name: Mapped[str] = mapped_column(String(64), index=True)
    successor_name: Mapped[str] = mapped_column(String(64), default="")
    title: Mapped[str] = mapped_column(String(256))
    disease: Mapped[str] = mapped_column(String(128), default="", index=True)
    syndrome: Mapped[str] = mapped_column(String(128), default="", index=True)
    # 四诊摘要 / 辨证 / 治法 / 处方 / 按语，分列存储便于按维度检索
    four_exams: Mapped[str] = mapped_column(String(2048), default="")
    treatment_method: Mapped[str] = mapped_column(String(512), default="")
    prescription: Mapped[str] = mapped_column(String(1024), default="")
    commentary: Mapped[str] = mapped_column(String(2048), default="")
    visit_date: Mapped[str] = mapped_column(String(10), default="")
    published: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SimulationCase(Base):
    """㉑模拟诊疗：情境化病例与关键决策点。

    与培训考核（`training_assessments`）的区别：考核是问答题对错，
    模拟诊疗是**在情境里做一串决策**，每一步的选择会决定下一步看到什么。
    平台这一版只做"决策点 + 参考答案 + 得分"，不做分支剧情——
    分支剧情要配编辑器，属阶段十的流程编排范畴。
    """

    __tablename__ = "simulation_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(256))
    # tcm=中医药适宜技术, clinical=临床, emergency=急救
    category: Mapped[str] = mapped_column(String(16), default="clinical", index=True)
    scenario: Mapped[str] = mapped_column(String(4096), default="")
    # 决策点：[{"key","question","options":[...],"answer","score","explain"}]
    decision_points: Mapped[list] = mapped_column(JSON, default=list)
    pass_score: Mapped[int] = mapped_column(Integer, default=60)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SimulationAttempt(Base):
    """㉑模拟诊疗作答记录。

    允许重复作答并全部留痕，取最高分参与考核——与培训考核"重考取高分"
    一致。留痕是因为"第几次才做对"本身就是教学反馈。
    """

    __tablename__ = "simulation_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("simulation_cases.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    score: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)



# ---------- 阶段十 统一资源与排程撮合 ----------


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



# ---------- 阶段十一 权限模型：角色 / 权限点 / 关联 ----------


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


# ============================================================================
# 全域慢专病全流程管理系统（`spd_*` 共 50 张表）
#
# 独立成 `app/spd/` 子系统包，但在这里 import 进来：`Base.metadata` 必须在
# `create_all` 之前认识这些表，而各处（测试 conftest、alembic env、应用启动）
# 导入的都是 `app.models`。放在文件末尾而不是开头——`app/spd/models.py` 要用本模块的
# `Money` 与 `utcnow`，此刻它们已经定义完毕。
# ============================================================================
from ..spd.models import *  # noqa: E402,F401,F403

