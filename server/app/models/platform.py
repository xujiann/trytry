"""ORM 模型 · 平台底座：附件、通知、定时任务、集成平台、工作流、公文。

由原 `models.py`（3989 行 / 187 类）分域拆出，见 ADR-0008。
**类的先后顺序保持原文件不变**——本仓库没开 `from __future__ import annotations`，
`Mapped[SomeClass]` 这类注解在建类时就要求被引用的类已经定义。
"""
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ._base import Money, utcnow


class AdminProject(Base):
    """㉞行政协同：项目管理。

    进度用 0-100 的百分数而不是里程碑推算——里程碑权重各院口径不一，
    让项目负责人直接报数，比平台按里程碑数算一个没人认的进度诚实。
    """

    __tablename__ = "admin_projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(256))
    category: Mapped[str] = mapped_column(String(32), default="general", index=True)
    owner_name: Mapped[str] = mapped_column(String(64), default="")
    start_date: Mapped[str] = mapped_column(String(10), default="")
    due_date: Mapped[str] = mapped_column(String(10), default="", index=True)
    # planning=筹备, ongoing=进行中, done=已完成, suspended=已中止
    status: Mapped[str] = mapped_column(String(16), default="planning", index=True)
    progress_pct: Mapped[int] = mapped_column(Integer, default=0)
    budget_amount: Mapped[float] = mapped_column(Money, default=0)
    description: Mapped[str] = mapped_column(String(1024), default="")
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ProjectMilestone(Base):
    """㉞项目里程碑。逾期未完成按日期现算，不设定时任务改状态。"""

    __tablename__ = "project_milestones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("admin_projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(256))
    due_date: Mapped[str] = mapped_column(String(10), default="", index=True)
    done: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    done_date: Mapped[str] = mapped_column(String(10), default="")
    note: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OfficialDoc(Base):
    """㉞行政统一协同管理：公文/通知发布。"""

    __tablename__ = "official_docs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(256))
    # notice=通知, policy=政策文件, minutes=会议纪要
    doc_type: Mapped[str] = mapped_column(String(16), default="notice")
    body: Mapped[str] = mapped_column(String(4096), default="")
    issuer: Mapped[str] = mapped_column(String(64), default="")
    # draft=草稿, published=已发布
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ExchangeLog(Base):
    """交换日志：每次入站转换落一条日志（含失败详情），交换监控与失败率统计数据源。"""

    __tablename__ = "exchange_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 来源系统标识（调用方经 X-Source-System 头声明，缺省空串）
    source_system: Mapped[str] = mapped_column(String(64), default="", index=True)
    # hl7v2_patient / fhir_patient / fhir_observation ...
    message_type: Mapped[str] = mapped_column(String(32), index=True)
    # inbound=入站, outbound=出站
    direction: Mapped[str] = mapped_column(String(8), default="inbound")
    success: Mapped[bool] = mapped_column(Boolean, index=True)
    error_detail: Mapped[str] = mapped_column(String(1024), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Attachment(Base):
    """通用附件：检查报告影像截图/PDF、不良事件佐证材料等。

    文件内容按 sha256 去重落本地磁盘（MEDPLAT_UPLOAD_DIR，默认 server/uploads/），
    数据库仅存元数据；owner_type+owner_id 挂接业务对象。
    """

    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    filename: Mapped[str] = mapped_column(String(256))
    content_type: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    # 业务域：exam_report=检查报告附件, adverse_event=不良事件附件
    owner_type: Mapped[str] = mapped_column(String(32), index=True)
    owner_id: Mapped[int] = mapped_column(Integer, index=True)
    # 可空：居民端上传（慢专病任务佐证）没有工作人员账号，记 NULL 而不是伪造一个
    uploaded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # 病毒扫描旁路（P1-22，见 avscan.py）：pending=待扫, clean=已扫无毒,
    # infected=检出病毒（下载拦截 410）, unavailable=扫描器不可用未能扫,
    # skipped=未配置 clamd、明示未扫（不冒充已扫）
    scan_status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending", index=True
    )
    # 扫描详情：infected 记病毒签名名，unavailable 记原因；其余为空
    scan_detail: Mapped[str] = mapped_column(String(256), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PrintTemplate(Base):
    """打印模板（块1 报告打印）：按单据类型配置抬头、页脚与二维码开关。

    doc_type 取值：exam_report=检查报告, prescription=处方笺,
    exam_request=检验/检查申请单, cert=法定医学证明。
    未配置时打印页回落到业务机构名称与默认页脚。
    """

    __tablename__ = "print_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    # 抬头机构名（留空则用单据所属机构名）
    header_org_name: Mapped[str] = mapped_column(String(128), default="")
    footer_note: Mapped[str] = mapped_column(String(256), default="")
    show_qr: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class EsbEndpoint(Base):
    """ESB 接入方（端点）注册：外部系统凭 code + 令牌入队消息，按分钟限流。"""

    __tablename__ = "esb_endpoints"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    # his=医院信息系统, lis=检验, pacs=影像, insurance=医保, provincial=省级平台
    system_type: Mapped[str] = mapped_column(String(16), index=True)
    # inbound=入站（外部→平台）, outbound=出站（平台→外部）
    direction: Mapped[str] = mapped_column(String(8), default="inbound", index=True)
    # 接入令牌只存散列（与用户口令同一 PBKDF2 实现），明文仅注册时返回一次
    auth_token_hash: Mapped[str] = mapped_column(String(200), default="")
    # 出站投递地址：为空表示端点"仅登记"（消费时不真实投递，保持登记语义）
    endpoint_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 出站签名密钥（HMAC-SHA256 对投递报文体加签）；为空则投递时不带签名头
    secret: Mapped[str | None] = mapped_column(String(128), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    rate_limit_per_min: Mapped[int] = mapped_column(Integer, default=60)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EsbMessage(Base):
    """ESB 消息队列：入队 → 消费/编排 → 成功或重试，重试耗尽转死信。"""

    __tablename__ = "esb_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    endpoint_id: Mapped[int] = mapped_column(ForeignKey("esb_endpoints.id"), index=True)
    msg_type: Mapped[str] = mapped_column(String(32), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # queued=待处理, processing=处理中, succeeded=成功, failed=失败待重试, dead=死信
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=3)
    last_error: Mapped[str] = mapped_column(String(1024), default="")
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EsbFlow(Base):
    """ESB 编排流程：steps 为有序步骤数组 [{type, config}]，type ∈ transform|route|validate|persist。"""

    __tablename__ = "esb_flows"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    steps: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EsbFlowRun(Base):
    """ESB 编排执行记录：逐步结果落 step_results，便于回溯定位失败步骤。"""

    __tablename__ = "esb_flow_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    flow_id: Mapped[int] = mapped_column(ForeignKey("esb_flows.id"), index=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("esb_messages.id"), index=True)
    # succeeded=全部步骤成功, failed=某步骤失败（后续步骤不再执行）
    status: Mapped[str] = mapped_column(String(16), default="succeeded", index=True)
    step_results: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str] = mapped_column(String(1024), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ScheduledJob(Base):
    """定时任务注册表：代码里定义任务实现，库里存调度参数与运行状态。

    间隔而非 cron 表达式：平台的定时需求都是"每 N 分钟/小时扫一遍"，
    不需要"每月最后一个工作日"这种日历语义，多引入一个 cron 解析器不划算。
    """

    __tablename__ = "scheduled_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(128), default="")
    interval_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 下次到期时刻：调度器据此判断是否该跑，重启后不会因内存计时器丢失而漏跑
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    last_status: Mapped[str] = mapped_column(String(16), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class JobRun(Base):
    """任务执行留痕：每次执行一条，含结果摘要与耗时。"""

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_name: Mapped[str] = mapped_column(String(64), index=True)
    # scheduled=按计划触发, manual=人工触发
    trigger: Mapped[str] = mapped_column(String(16), default="scheduled")
    # succeeded=成功, failed=异常
    status: Mapped[str] = mapped_column(String(16), default="succeeded", index=True)
    # 结果摘要（"扫描到3条超期随访"）或异常信息
    message: Mapped[str] = mapped_column(String(1024), default="")
    # 本次处理的对象数，便于统计与画趋势
    affected: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class RuleDefinition(Base):
    """统一规则：条件表达式 + 命中后的处置（消息/严重度/扣分）。

    平台已有四套各自实现的规则（审方/数据质控/病历质控/绩效），它们的判定写死
    在各自路由里。这里不做大爆炸式重写——既有规则继续按原路运行，统一引擎
    负责**新增规则**与**统一目录视图**，旧规则以只读方式并入目录。
    """

    __tablename__ = "rule_definitions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    # 规则域：决定可用变量集合，见 routers/rules.py 的 DOMAIN_VARIABLES
    domain: Mapped[str] = mapped_column(String(24), index=True)
    condition: Mapped[str] = mapped_column(String(512))
    message: Mapped[str] = mapped_column(String(256), default="")
    # info=提示, warning=警告, error=拦截
    severity: Mapped[str] = mapped_column(String(16), default="warning", index=True)
    deduct_points: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


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
