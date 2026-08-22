"""全域慢专病全流程管理系统（SPD, Specialty & chronic Disease Management）数据模型。

对应招标文件《服务内容及要求》十一个端的领域实体。与既有 `chronic`（慢病随访分级）
和 `disease_programs`（专病入组—路径—出组）**不是替换关系**：

- `chronic_*`：单病种随访分级台账，公卫口径，已在用，保留；
- `disease_programs`：院内专病入组路径，单机构口径，保留；
- `spd_*`：**全域**慢专病，跨县乡村三级、跨机构、跨端，把"筛查—评估—纳管—路径—
  随访—干预—转诊—考核"串成一条链。招标文件平台管理端第 21 条明确要求"慢病管理与
  专病管理并行运行，共用患者、组织机构、任务、随访、消息和考核底座，同时分别配置
  病种规则、工作台、路径和统计口径，避免不同业务数据相互覆盖"——所以这里**共用**
  `patients` / `organizations` / `users`（外键直接指过去），而**自建**病种规则、路径、
  任务与考核，两套业务各写各的表，互不覆盖。

命名一律 `spd_` 前缀，便于运维按前缀做备份、迁移与权限切分。

**本文件是唯一直接 import 平台 `models` 的子系统模块**，且只取 `Money` / `utcnow`
两个列类型——业务依赖一律走 `platform.py`。这条例外是为了断开一个真实的导入环：
`app/models.py` 末尾要 import 本文件（让 `Base.metadata` 认识这批表），若本文件
再经 `platform.py` 去 import `app.routers.portal`，就会在"平台还没初始化完"时
反向拉起平台路由，触发 `partially initialized module` 报错。
`tests/test_spd_boundary.py` 把这条例外的**范围**钉死在这两个名字上。

## 一处贯穿全表的取舍：状态用字符串而不是枚举

SQLAlchemy 的 `Enum` 在 SQLite 上是 CHECK 约束、在国产库上行为各异，
而本域的状态集合在实施期会随各县业务口径调整（比如转诊链路是否设"服务站复核"）。
落库存字符串、取值范围写在列注释与路由的 `pattern` 里，改口径不需要改 DDL。
"""
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ..models import Money, utcnow  # 仅列类型；业务依赖一律走 platform.py

# ============================================================================
# 一、配置域：病种规则 / 管理目标 / 标准路径 / 量表 / 宣教 / 服务包
# ============================================================================


class SpdProgram(Base):
    """专病档案（病种）定义：纳入规则、排除规则、管理阶段与关键节点。

    平台管理端 #8 / 全程管理中心端 #4 / 专家端 #3 要求"依据诊断、检查和治疗条件
    自动识别患者，设置阶段预警和节点触发规则"。规则用 JSON 表达而不是建规则表：

        include_rules = [{"field": "diagnosis", "op": "in", "value": ["I10", "I11"]},
                         {"field": "bp_sys",    "op": ">=", "value": 140}]

    字段名取自 `rules.FIELD_SOURCES`，求值在 `app/spd/rules.py`，**不用 eval**。
    分开存 include/exclude 是因为两者语义不同：命中任一 include 且不命中任何
    exclude 才算目标人群——合并成一张表就要再加一列区分，读起来反而绕。
    """

    __tablename__ = "spd_programs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    # chronic=慢病口径（长期随访）, specialty=专病口径（有始有终的诊疗路径）
    category: Mapped[str] = mapped_column(String(16), default="chronic", index=True)
    lead_org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    lead_dept: Mapped[str] = mapped_column(String(64), default="")
    description: Mapped[str] = mapped_column(String(512), default="")
    include_rules: Mapped[list] = mapped_column(JSON, default=list)
    exclude_rules: Mapped[list] = mapped_column(JSON, default=list)
    # 管理阶段：[{"key":"screen","name":"筛查","alert_days":30}]，阶段预警按 alert_days 计
    stages: Mapped[list] = mapped_column(JSON, default=list)
    # 关键时间节点：[{"key":"surgery","name":"手术","trigger":"encounter.surgery"}]
    milestones: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[str] = mapped_column(String(16), default="v1")
    effective_from: Mapped[str] = mapped_column(String(10), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SpdProgramVersion(Base):
    """病种规则版本留痕：改一次规则存一份快照，供历史追溯与"按哪版规则纳的管"对账。

    考核端 #17 要求指标"规则版本、生效范围、历史追溯"，纳管口径同理——
    半年后有人问"这批病人当初为什么被纳进来"，没有快照就只能靠记忆。
    """

    __tablename__ = "spd_program_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    program_id: Mapped[int] = mapped_column(ForeignKey("spd_programs.id"), index=True)
    version: Mapped[str] = mapped_column(String(16))
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    changed_by: Mapped[str] = mapped_column(String(64), default="")
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SpdTarget(Base):
    """管理目标：按病种 + 管理阶段定义量化目标（血压/血糖/尿酸/血氧）与定性目标。

    平台管理端 #9：量化目标存 `low/high`，定性目标（"病情稳定"）存 `qualitative`。
    同一条记录还挂随访周期、随访表单与宣教内容——因为这三样在业务上就是
    "这个阶段该怎么管"的同一句话，拆表只会让配置页要来回跳。
    """

    __tablename__ = "spd_targets"
    __table_args__ = (
        UniqueConstraint("program_id", "stage", "metric", name="uq_spd_target_stage_metric"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    program_id: Mapped[int] = mapped_column(ForeignKey("spd_programs.id"), index=True)
    stage: Mapped[str] = mapped_column(String(32), default="", index=True)
    # 量化指标编码（bp_sys/bp_dia/glucose_fasting/ua/spo2/...）或定性目标编码
    metric: Mapped[str] = mapped_column(String(32))
    metric_name: Mapped[str] = mapped_column(String(64), default="")
    kind: Mapped[str] = mapped_column(String(16), default="quantitative")
    target_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str] = mapped_column(String(16), default="")
    qualitative: Mapped[str] = mapped_column(String(128), default="")
    risk_level: Mapped[str] = mapped_column(String(16), default="")
    followup_interval_days: Mapped[int] = mapped_column(Integer, default=90)
    form_code: Mapped[str] = mapped_column(String(32), default="")
    edu_code: Mapped[str] = mapped_column(String(32), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdPathTemplate(Base):
    """标准化指导路径模板：新建 / 复制 / 发布 / 启停 / 删除全生命周期。

    `status` 三态而不是一个 `active` 布尔：草稿期改节点不该影响在跑的患者，
    发布后才允许被路径实例引用，停用只挡新建、不打断已启动的实例。
    """

    __tablename__ = "spd_path_templates"
    __table_args__ = (UniqueConstraint("code", "version", name="uq_spd_path_code_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    program_id: Mapped[int] = mapped_column(ForeignKey("spd_programs.id"), index=True)
    code: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(64))
    # outpatient=门诊路径, inpatient=住院路径, home=居家管理, followup=随访路径
    scene: Mapped[str] = mapped_column(String(16), default="outpatient", index=True)
    risk_level: Mapped[str] = mapped_column(String(16), default="")
    version: Mapped[str] = mapped_column(String(16), default="v1")
    # draft=草稿, published=已发布, disabled=已停用
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    copied_from_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scope: Mapped[str] = mapped_column(String(16), default="region")
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    team_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str] = mapped_column(String(512), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SpdPathNode(Base):
    """路径节点：阶段、责任科室、执行角色、进入/完成条件、下一节点、超时处理。

    专家端 #7 要求"根据患者进入节点自动生成有时限、有表单和证据要求的任务"——
    `due_days` / `require_form` / `require_evidence` 三列就是任务生成的依据，
    落在节点上而不是任务上：任务是节点的实例，规则应当只有一份。
    """

    __tablename__ = "spd_path_nodes"
    __table_args__ = (UniqueConstraint("template_id", "key", name="uq_spd_node_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("spd_path_templates.id"), index=True)
    key: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(32), default="")
    seq: Mapped[int] = mapped_column(Integer, default=0)
    dept: Mapped[str] = mapped_column(String(64), default="")
    exec_role: Mapped[str] = mapped_column(String(32), default="doctor")
    # followup=随访, revisit=复诊复查, edu=宣教, scale=量表, exam=检查检验,
    # intervention=干预, referral=转诊, monitor=监测
    service_type: Mapped[str] = mapped_column(String(16), default="followup")
    enter_condition: Mapped[list] = mapped_column(JSON, default=list)
    complete_condition: Mapped[list] = mapped_column(JSON, default=list)
    next_key: Mapped[str] = mapped_column(String(32), default="")
    due_days: Mapped[int] = mapped_column(Integer, default=7)
    # remind=提醒, escalate=升级上报, auto_complete=自动跳过
    timeout_action: Mapped[str] = mapped_column(String(16), default="remind")
    require_form: Mapped[bool] = mapped_column(Boolean, default=False)
    require_evidence: Mapped[bool] = mapped_column(Boolean, default=False)
    form_code: Mapped[str] = mapped_column(String(32), default="")
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdScale(Base):
    """评估量表模板：风险评估 / 阶段评估 / 康复评估，含版本与二维码令牌。

    量表要"经审核发布"后才能被评估引用（专家端 #6），所以同样是三态 `status`。
    `qr_token` 用于成员端 #8 的"生成评估二维码"——令牌而非自增 id，
    二维码印出去就收不回，用 id 等于把内部序号贴到墙上。
    """

    __tablename__ = "spd_scales"
    __table_args__ = (UniqueConstraint("code", "version", name="uq_spd_scale_code_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(64))
    # risk=风险评估, stage=阶段评估, rehab=康复评估, screen=筛查问卷
    category: Mapped[str] = mapped_column(String(16), default="risk", index=True)
    program_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    version: Mapped[str] = mapped_column(String(16), default="v1")
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    # [{"key":"q1","title":"...","type":"single","options":[{"label":"是","score":2}]}]
    items: Mapped[list] = mapped_column(JSON, default=list)
    # {"ranges":[{"min":0,"max":3,"risk":"low","advice":"..."}]}
    scoring: Mapped[dict] = mapped_column(JSON, default=dict)
    qr_token: Mapped[str] = mapped_column(String(32), default="", index=True)
    owner_team_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SpdEduMaterial(Base):
    """健康宣教素材：图文 / 音频 / 视频，可按病种与科室归类。"""

    __tablename__ = "spd_edu_materials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(128))
    program_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    # text=图文, audio=音频, video=视频
    media_type: Mapped[str] = mapped_column(String(16), default="text")
    content: Mapped[str] = mapped_column(String(8192), default="")
    media_url: Mapped[str] = mapped_column(String(256), default="")
    dept: Mapped[str] = mapped_column(String(64), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SpdServicePackage(Base):
    """服务包：项目、周期、价格。项目明细存 JSON，含每项次数上限，用于扣减登记。"""

    __tablename__ = "spd_service_packages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    program_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    price: Mapped[float] = mapped_column(Money, default=0)
    period_days: Mapped[int] = mapped_column(Integer, default=365)
    # [{"code":"bp_check","name":"血压测量","times":12,"price":5}]
    items: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdTag(Base):
    """标签：患者/团队通用标签，供分组与筛选（平台管理端 #3）。"""

    __tablename__ = "spd_tags"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(32), default="patient")
    color: Mapped[str] = mapped_column(String(16), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdCenter(Base):
    """重点慢专病中心：牵头科室、负责人、适用机构与团队、分中心运行状态（专家端 #3）。"""

    __tablename__ = "spd_centers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    program_code: Mapped[str] = mapped_column(String(32), index=True)
    lead_org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    lead_dept: Mapped[str] = mapped_column(String(64), default="")
    leader_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    org_ids: Mapped[list] = mapped_column(JSON, default=list)
    team_ids: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[str] = mapped_column(String(16), default="v1")
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ============================================================================
# 二、组织域：服务团队 / 村医 / 设备 / 数据源
# ============================================================================


class SpdTeam(Base):
    """慢专病服务团队：县乡村三级、跨科室，绑定病种与服务范围。"""

    __tablename__ = "spd_teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # county=县级团队, township=乡镇团队, village=村级团队, center=专病中心团队
    level: Mapped[str] = mapped_column(String(16), default="township", index=True)
    program_codes: Mapped[list] = mapped_column(JSON, default=list)
    leader_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    dept: Mapped[str] = mapped_column(String(64), default="")
    service_area: Mapped[str] = mapped_column(String(256), default="")
    # 数据权限范围：org=本机构, group=医共体分组, region=全域
    data_scope: Mapped[str] = mapped_column(String(16), default="org")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SpdTeamMember(Base):
    """团队成员与其权限位。

    权限用五个布尔列而不是一个 JSON：这五项（查看/随访/转诊/审核/考核）在
    招标文件全程管理中心端 #12 里是**逐项点名**的，会被逐个查询与统计；
    塞进 JSON 就查不动，也无法在库侧做"谁有审核权"的清单。
    """

    __tablename__ = "spd_team_members"
    __table_args__ = (UniqueConstraint("team_id", "user_id", name="uq_spd_team_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("spd_teams.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # doctor=医生, nurse=护士, rehab=康复治疗师, case_manager=个案管理师,
    # village_doctor=村医, expert=专家
    member_role: Mapped[str] = mapped_column(String(16), default="doctor", index=True)
    program_codes: Mapped[list] = mapped_column(JSON, default=list)
    stage_scope: Mapped[str] = mapped_column(String(64), default="")
    patient_scope: Mapped[str] = mapped_column(String(16), default="team")
    can_view: Mapped[bool] = mapped_column(Boolean, default=True)
    can_followup: Mapped[bool] = mapped_column(Boolean, default=True)
    can_referral: Mapped[bool] = mapped_column(Boolean, default=False)
    can_audit: Mapped[bool] = mapped_column(Boolean, default=False)
    can_assess: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdVillageDoctor(Base):
    """村医档案：乡镇/行政村归属、资质、扫码绑定令牌（平台管理端 #7）。

    与 `users` 一对一而不是把这些列加到 `users` 上：村医特有的行政村归属、
    执业资质、绑定二维码，对其余五类角色全是空列。
    """

    __tablename__ = "spd_village_doctors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    township: Mapped[str] = mapped_column(String(64), default="", index=True)
    village: Mapped[str] = mapped_column(String(64), default="", index=True)
    license_no: Mapped[str] = mapped_column(String(64), default="")
    license_valid_to: Mapped[str] = mapped_column(String(10), default="")
    phone: Mapped[str] = mapped_column(String(20), default="")
    bind_token: Mapped[str] = mapped_column(String(32), default="", index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SpdDevice(Base):
    """健康监测设备台账：血压计 / 血糖仪 / 智能手环 / 体脂秤 / POCT（平台管理端 #16）。"""

    __tablename__ = "spd_devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sn: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    device_type: Mapped[str] = mapped_column(String(16), index=True)
    model: Mapped[str] = mapped_column(String(64), default="")
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    bound_patient_id: Mapped[int | None] = mapped_column(
        ForeignKey("patients.id"), nullable=True, index=True
    )
    # idle=闲置, bound=已绑定, offline=离线, scrapped=报废
    status: Mapped[str] = mapped_column(String(16), default="idle", index=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SpdDataSource(Base):
    """数据源接入配置与运行监控（平台管理端 #14/#15）。

    `success_rate` 是**冗余列**：由同步日志算得，但监控页要按成功率排序、
    按阈值告警，每次现算要扫全表日志。写入时同步更新，日志仍是唯一事实来源。
    """

    __tablename__ = "spd_data_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    # HIS/EMR/LIS/PACS/checkup=体检/publichealth=公卫/device=物联网
    source_type: Mapped[str] = mapped_column(String(16), index=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    endpoint: Mapped[str] = mapped_column(String(256), default="")
    freq_minutes: Mapped[int] = mapped_column(Integer, default=60)
    scope: Mapped[str] = mapped_column(String(256), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_rows: Mapped[int] = mapped_column(Integer, default=0)
    last_latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    success_rate: Mapped[float] = mapped_column(Float, default=0.0)
    # running=正常, delayed=延迟, failed=异常, stopped=停用
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdSyncLog(Base):
    """数据源同步日志：每次同步的行数、耗时、成败与说明。"""

    __tablename__ = "spd_sync_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("spd_data_sources.id"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    rows: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    message: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ============================================================================
# 三、人群域：筛查 → 目标池 → 纳管 → 生命周期
# ============================================================================


class SpdScreening(Base):
    """筛查记录：机会性筛查（就诊顺带）/ 主动筛查（组织开展）/ 居民自查。

    结论三分：suspect（疑似，进目标池）、excluded（排除）、normal（正常）。
    高风险复核（`reviewed`/`review_result`）单独留列而不是再建一张复核表——
    复核是对同一条筛查的第二次判定，同表两列比跨表 join 更容易看懂。
    """

    __tablename__ = "spd_screenings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    program_code: Mapped[str] = mapped_column(String(32), index=True)
    # opportunistic=机会性, active=主动筛查, self=居民自查, import=数据比对
    source: Mapped[str] = mapped_column(String(16), default="opportunistic", index=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    operator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    scale_code: Mapped[str] = mapped_column(String(32), default="")
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    score: Mapped[float] = mapped_column(Float, default=0)
    # low=低危, mid=中危, high=高危
    risk_level: Mapped[str] = mapped_column(String(16), default="low", index=True)
    # suspect=疑似, excluded=排除, normal=未见异常
    result: Mapped[str] = mapped_column(String(16), default="normal", index=True)
    advice: Mapped[str] = mapped_column(String(512), default="")
    reviewed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    review_result: Mapped[str] = mapped_column(String(16), default="")
    review_note: Mapped[str] = mapped_column(String(256), default="")
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdCandidate(Base):
    """目标池：疑似 / 目标 / 排除人群，是"筛出来"到"管起来"之间的中转站。

    一个人同一病种只该有一条在池记录，靠唯一约束挡住重复筛查造成的重复分发。
    """

    __tablename__ = "spd_candidates"
    __table_args__ = (
        UniqueConstraint("patient_id", "program_code", name="uq_spd_candidate_patient_program"),
        Index("ix_spd_candidate_status_org", "status", "org_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    program_code: Mapped[str] = mapped_column(String(32), index=True)
    # suspect=疑似, target=目标人群（可签约）, excluded=排除, enrolled=已纳管
    status: Mapped[str] = mapped_column(String(16), default="suspect", index=True)
    source: Mapped[str] = mapped_column(String(16), default="screening")
    screening_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("spd_teams.id"), nullable=True)
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    risk_level: Mapped[str] = mapped_column(String(16), default="")
    reason: Mapped[str] = mapped_column(String(256), default="")
    matched_rules: Mapped[list] = mapped_column(JSON, default=list)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdEnrollment(Base):
    """慢专病纳管档案：签约 + 建档 + 责任关系 + 风险分级 + 生命周期状态。

    平台的**中心实体**：任务、路径、随访、服务包、考核全部挂在它下面。
    同一患者可同时纳管多个病种（多病共存是慢病常态），所以唯一键是
    (patient_id, program_code) 而不是 patient_id。
    """

    __tablename__ = "spd_enrollments"
    __table_args__ = (
        # 部分唯一索引："同病种只有一条**在管**档案"。全量唯一是错的语义——
        # 迁出/排除后的患者要能重新纳管（P1-2 跨机构迁入、召回后再签约都撞它）。
        Index(
            "uq_spd_enroll_active_patient_program",
            "patient_id", "program_code",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_spd_enroll_org_status", "org_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    program_code: Mapped[str] = mapped_column(String(32), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("spd_teams.id"), nullable=True, index=True)
    doctor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    manager_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    village_doctor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    stage: Mapped[str] = mapped_column(String(32), default="", index=True)
    # low=低危, mid=中危, high=高危, very_high=极高危（成员端 #4 四级颜色分级）
    risk_level: Mapped[str] = mapped_column(String(16), default="low", index=True)
    # active=在管, excluded=已排除, migrated=已迁出, dead=已死亡,
    # lost=脱管, recalled=召回中, completed=结案
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    source: Mapped[str] = mapped_column(String(16), default="screening")
    # 跨机构迁入时指向原机构的档案（P1-2）：迁移只建关系不搬历史，
    # 原机构的任务/随访留痕与考核数字保持原样
    migrated_from_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sign_date: Mapped[str] = mapped_column(String(10), default="")
    consent_signed: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_no: Mapped[str] = mapped_column(String(64), default="")
    service_start: Mapped[str] = mapped_column(String(10), default="")
    service_end: Mapped[str] = mapped_column(String(10), default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    habits: Mapped[dict] = mapped_column(JSON, default=dict)
    risk_factors: Mapped[list] = mapped_column(JSON, default=list)
    complications: Mapped[list] = mapped_column(JSON, default=list)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    last_followup_at: Mapped[str] = mapped_column(String(10), default="")
    next_followup_at: Mapped[str] = mapped_column(String(10), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdLifecycleEvent(Base):
    """生命周期事件：排除 / 迁出 / 死亡 / 召回 / 恢复，逐条留痕。

    个案管理师端 #10~#13 的四个动作共用一张表：它们在业务上是同一件事的四种
    理由——"这个人的管理关系变了"，且都要求"同步终止后续任务"。
    跨机构迁出需目标机构确认（`confirmed`），本地迁出直接生效。
    """

    __tablename__ = "spd_lifecycle_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("spd_enrollments.id"), index=True)
    # exclude=移除病种标签, migrate=迁出, death=死亡, recall=召回, resume=恢复管理
    event: Mapped[str] = mapped_column(String(16), index=True)
    reason: Mapped[str] = mapped_column(String(256), default="")
    detail: Mapped[str] = mapped_column(String(512), default="")
    target_org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=True)
    confirmed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    occurred_at: Mapped[str] = mapped_column(String(10), default="")
    operator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SpdRecall(Base):
    """召回记录：对长期脱管或中断服务的患者发起召回，记录联系过程与结果。"""

    __tablename__ = "spd_recalls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("spd_enrollments.id"), index=True)
    reason: Mapped[str] = mapped_column(String(256), default="")
    contacts: Mapped[list] = mapped_column(JSON, default=list)
    # pending=待联系, contacted=已联系, returned=已重新纳管, failed=召回失败
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    result: Mapped[str] = mapped_column(String(256), default="")
    operator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SpdGroup(Base):
    """患者分组：个人分组 / 科室分组，可配自动分组规则并批量干预。"""

    __tablename__ = "spd_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    owner_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    dept: Mapped[str] = mapped_column(String(64), default="")
    # personal=本人分组, dept=科室分组, team=团队分组
    scope: Mapped[str] = mapped_column(String(16), default="personal", index=True)
    auto_rule: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SpdGroupMember(Base):
    __tablename__ = "spd_group_members"
    __table_args__ = (UniqueConstraint("group_id", "patient_id", name="uq_spd_group_member"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("spd_groups.id"), index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    added_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdPackageBinding(Base):
    """服务包绑定：剩余次数随扣减实时更新，解绑保留台账不删记录。"""

    __tablename__ = "spd_package_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("spd_enrollments.id"), index=True)
    package_id: Mapped[int] = mapped_column(ForeignKey("spd_service_packages.id"), index=True)
    # [{"code":"bp_check","name":"血压测量","total":12,"used":3}]
    items: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="bound", index=True)
    bound_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    unbound_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    period_end: Mapped[str] = mapped_column(String(10), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdPackageUsage(Base):
    """服务包项目扣减台账（个案管理师端 #5 的消费台账）。"""

    __tablename__ = "spd_package_usages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    binding_id: Mapped[int] = mapped_column(ForeignKey("spd_package_bindings.id"), index=True)
    item_code: Mapped[str] = mapped_column(String(32))
    item_name: Mapped[str] = mapped_column(String(64), default="")
    qty: Mapped[int] = mapped_column(Integer, default=1)
    price: Mapped[float] = mapped_column(Money, default=0)
    operator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    note: Mapped[str] = mapped_column(String(256), default="")
    used_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ============================================================================
# 四、服务域：路径实例 / 统一任务 / 随访 / 干预 / 评估 / 监测 / 复诊 / 上报
# ============================================================================


class SpdPathInstance(Base):
    """患者路径实例：模板发布后按患者启动，可个性化调整而不回改模板。"""

    __tablename__ = "spd_path_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enrollment_id: Mapped[int] = mapped_column(ForeignKey("spd_enrollments.id"), index=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("spd_path_templates.id"), index=True)
    template_code: Mapped[str] = mapped_column(String(32), default="")
    current_node_key: Mapped[str] = mapped_column(String(32), default="", index=True)
    current_stage: Mapped[str] = mapped_column(String(32), default="")
    # running=执行中, completed=已完成, cancelled=已取消, paused=暂停
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    # 个性化覆盖：{"node_key": {"due_days": 3}}，为空表示完全按模板
    overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdTask(Base):
    """统一任务模型：路径节点、随访、干预、评估、复诊、转诊、上报共用一张表。

    全程管理中心端 #13 要求任务能"按机构、类型、状态、优先级、时限和责任人查询，
    并提供接收、分配、转派、催办、超时升级、批量处理和导出"。**只有共用一张表，
    这句话才可能被一次实现**；每类业务各建一张任务表，就要写七遍催办和七遍导出，
    而且"我的待办"永远要 union 七次。

    代价是 `form` / `result` 两个 JSON 列承载各业务差异——这是刻意的：
    表单结构由 `form_code` 指向的模板定义，任务表不该知道血压表单长什么样。
    """

    __tablename__ = "spd_tasks"
    __table_args__ = (
        Index("ix_spd_task_assignee_status", "assignee_id", "status"),
        Index("ix_spd_task_org_due", "org_id", "due_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    program_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    enrollment_id: Mapped[int | None] = mapped_column(
        ForeignKey("spd_enrollments.id"), nullable=True, index=True
    )
    instance_id: Mapped[int | None] = mapped_column(
        ForeignKey("spd_path_instances.id"), nullable=True, index=True
    )
    node_key: Mapped[str] = mapped_column(String(32), default="")
    # path=路径节点, followup=随访, intervention=干预, assess=评估, revisit=复诊,
    # referral=转诊, report=上报, recall=召回, edu=宣教, screen=筛查复核
    task_type: Mapped[str] = mapped_column(String(16), default="followup", index=True)
    title: Mapped[str] = mapped_column(String(128))
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("spd_teams.id"), nullable=True)
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    exec_role: Mapped[str] = mapped_column(String(32), default="")
    # pending=待接收, claimed=已接收, doing=办理中, submitted=待审核,
    # done=已完成, rejected=已退回, overdue=已超期, cancelled=已取消
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # 1=普通, 2=紧急, 3=特急
    priority: Mapped[int] = mapped_column(Integer, default=1, index=True)
    due_date: Mapped[str] = mapped_column(String(10), default="", index=True)
    form_code: Mapped[str] = mapped_column(String(32), default="")
    require_evidence: Mapped[bool] = mapped_column(Boolean, default=False)
    form: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    urged_count: Mapped[int] = mapped_column(Integer, default=0)
    escalated: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    transferred_from: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    review_note: Mapped[str] = mapped_column(String(256), default="")
    source: Mapped[str] = mapped_column(String(16), default="auto")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SpdMeasurement(Base):
    """体征与检测数据：手工录入 / 设备采集 / 院内系统同步，统一存这里。

    `level` 落库而不是每次现算：判定依据是**采集当时**的管理目标，
    目标改了不该让历史数据的"偏高/偏低"跟着变。
    """

    __tablename__ = "spd_measurements"
    __table_args__ = (Index("ix_spd_meas_patient_metric", "patient_id", "metric", "measured_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    program_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    metric: Mapped[str] = mapped_column(String(32), index=True)
    value: Mapped[float] = mapped_column(Float, default=0)
    unit: Mapped[str] = mapped_column(String(16), default="")
    # normal=正常, high=偏高, low=偏低
    level: Mapped[str] = mapped_column(String(16), default="normal", index=True)
    # manual=手工, device=设备, his=院内系统, poct=POCT, publichealth=公卫随访同步
    source: Mapped[str] = mapped_column(String(16), default="manual")
    # 采集器同步的溯源键（如 chronic_fu:123），兼作幂等判重——重复同步不产生重复行
    source_ref: Mapped[str] = mapped_column(String(64), default="", index=True)
    device_sn: Mapped[str] = mapped_column(String(64), default="")
    measured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    operator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdAssessment(Base):
    """评估记录：量表版本随记录固化，量表改版不回改历史评估结论。"""

    __tablename__ = "spd_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    scale_id: Mapped[int] = mapped_column(ForeignKey("spd_scales.id"), index=True)
    scale_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    scale_version: Mapped[str] = mapped_column(String(16), default="")
    program_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    score: Mapped[float] = mapped_column(Float, default=0)
    risk_level: Mapped[str] = mapped_column(String(16), default="", index=True)
    advice: Mapped[str] = mapped_column(String(512), default="")
    # doctor=医护录入, self=居民自评（扫码）
    channel: Mapped[str] = mapped_column(String(16), default="doctor")
    operator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdInterventionTemplate(Base):
    """干预模板：饮食 / 运动 / 用药 / 心理，可按病种与高危标记自动触发。"""

    __tablename__ = "spd_intervention_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    program_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    category: Mapped[str] = mapped_column(String(16), default="diet")
    content: Mapped[str] = mapped_column(String(2048), default="")
    measures: Mapped[str] = mapped_column(String(1024), default="")
    frequency: Mapped[str] = mapped_column(String(32), default="")
    cycle_days: Mapped[int] = mapped_column(Integer, default=30)
    # 命中该风险等级即自动触发（成员端 #9 "按病种高危标记自动触发任务"）
    auto_risk_level: Mapped[str] = mapped_column(String(16), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdIntervention(Base):
    """干预计划与执行：患者端可标记已读并反馈执行情况（患者端 #10）。"""

    __tablename__ = "spd_interventions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    enrollment_id: Mapped[int | None] = mapped_column(
        ForeignKey("spd_enrollments.id"), nullable=True, index=True
    )
    program_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("spd_intervention_templates.id"), nullable=True
    )
    goal: Mapped[str] = mapped_column(String(256), default="")
    content: Mapped[str] = mapped_column(String(2048), default="")
    measures: Mapped[str] = mapped_column(String(1024), default="")
    frequency: Mapped[str] = mapped_column(String(32), default="")
    next_at: Mapped[str] = mapped_column(String(10), default="", index=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # planned=待执行, doing=执行中, done=已完成, removed=已移除
    status: Mapped[str] = mapped_column(String(16), default="planned", index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    feedback: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdEduPush(Base):
    """宣教推送记录：短信 / 公众号 / App，含发送时间、频率与阅读回执。"""

    __tablename__ = "spd_edu_pushes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("spd_edu_materials.id"), index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    channel: Mapped[str] = mapped_column(String(16), default="sms")
    send_at: Mapped[str] = mapped_column(String(19), default="")
    frequency: Mapped[str] = mapped_column(String(32), default="once")
    # pending=待发送, sent=已发送, read=已阅读, failed=发送失败
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    #: 失败原因。"没配微信模板""患者没绑微信""通道未受理"是三件不同的事，
    #: 处置也不同——只记一个 failed 等于让实施期去猜到底该找谁。
    fail_reason: Mapped[str] = mapped_column(String(200), default="")
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    operator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdRevisit(Base):
    """复诊计划：路径节点 / 出院计划 / 高危首次标记 / 手工，四种来源同一张表。"""

    __tablename__ = "spd_revisits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    program_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    plan_date: Mapped[str] = mapped_column(String(10), default="", index=True)
    dept: Mapped[str] = mapped_column(String(64), default="")
    doctor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    items: Mapped[str] = mapped_column(String(512), default="")
    # path=路径节点派生, discharge=出院计划, high_risk=高危自动, manual=手工
    source: Mapped[str] = mapped_column(String(16), default="manual", index=True)
    # planned=已计划, done=已复诊, overdue=逾期未复诊, removed=已移除
    status: Mapped[str] = mapped_column(String(16), default="planned", index=True)
    remind_status: Mapped[str] = mapped_column(String(16), default="none")
    actual_date: Mapped[str] = mapped_column(String(10), default="")
    log: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SpdCaseReportTask(Base):
    """上报任务定义：配置病种、管理科室与医生，任务自动分配给相关人员。"""

    __tablename__ = "spd_case_report_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    program_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    dept: Mapped[str] = mapped_column(String(64), default="")
    manager_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    assignee_ids: Mapped[list] = mapped_column(JSON, default=list)
    org_ids: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SpdCaseReport(Base):
    """上报明细：规则触发的异常患者上报为复核 / 转诊 / 随访 / 处置任务。"""

    __tablename__ = "spd_case_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("spd_case_report_tasks.id"), nullable=True, index=True
    )
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    program_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    # review=复核, referral=转诊, followup=随访, dispose=处置
    report_type: Mapped[str] = mapped_column(String(16), default="review", index=True)
    content: Mapped[str] = mapped_column(String(1024), default="")
    trigger_rule: Mapped[str] = mapped_column(String(128), default="")
    reporter_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    # pending=待处理, handling=处理中, done=已处置, closed=已关闭
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    handled_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    handle_note: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SpdHealthPrescription(Base):
    """健康处方：用药指导、康复训练、日常健康管理建议（个案管理师端 #14）。"""

    __tablename__ = "spd_health_prescriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    program_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    drug_advice: Mapped[str] = mapped_column(String(1024), default="")
    rehab_advice: Mapped[str] = mapped_column(String(1024), default="")
    life_advice: Mapped[str] = mapped_column(String(1024), default="")
    target_note: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdConsult(Base):
    """专病在线咨询会话（患者端 #18 / 个案管理师端 #6）。"""

    __tablename__ = "spd_consults"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    program_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    doctor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SpdConsultMessage(Base):
    __tablename__ = "spd_consult_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consult_id: Mapped[int] = mapped_column(ForeignKey("spd_consults.id"), index=True)
    # patient=居民, doctor=医生
    sender: Mapped[str] = mapped_column(String(16), default="patient")
    sender_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content: Mapped[str] = mapped_column(String(2048), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdServiceApply(Base):
    """居民专病服务申请：未纳管居民自查后提交，由团队受理（患者端 #2/#13）。"""

    __tablename__ = "spd_service_applies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    program_code: Mapped[str] = mapped_column(String(32), index=True)
    screening_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(String(512), default="")
    # pending=待受理, accepted=已受理, rejected=已拒绝
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    handled_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    handle_note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# ============================================================================
# 五、转诊域：村医 → 乡镇卫生院 → 区市县医院 三级链路（ADR-0005 收敛）
# ============================================================================


class SpdReferralRule(Base):
    """转诊规则：组合诊断、检查检验、体征、风险、随访和路径异常等触发条件。"""

    __tablename__ = "spd_referral_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    program_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    scene: Mapped[str] = mapped_column(String(16), default="followup")
    conditions: Mapped[list] = mapped_column(JSON, default=list)
    notify_role: Mapped[str] = mapped_column(String(32), default="doctor")
    # village=村医, station=服务站, township=卫生院, county=县级医院
    handle_level: Mapped[str] = mapped_column(String(16), default="township")
    target_org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    auto_task: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdReferralCase(Base):
    """逐级转诊单：一单贯穿"发起—分级审核—接诊—到院—下转—随访接收"。

    不复用既有 `referrals`：那张表是两点之间的一次转诊（A 院 → B 院），
    而这里要求的是**链路**——村医发起、卫生院审核、县医院接收（ADR-0005 起
    三级；原"服务站复核"档已收敛，存量在途单兼容），各环节有责任人与时限，
    且闭环率要按"下转后随访是否接收"统计。
    强行复用会把 `referrals.status` 撑成十几个状态，影响既有转诊业务。
    """

    __tablename__ = "spd_referral_cases"
    __table_args__ = (Index("ix_spd_ref_status_org", "status", "current_org_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    program_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    enrollment_id: Mapped[int | None] = mapped_column(
        ForeignKey("spd_enrollments.id"), nullable=True, index=True
    )
    # up=上转, down=下转
    direction: Mapped[str] = mapped_column(String(8), default="up", index=True)
    initiator_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    initiator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    current_org_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id"), nullable=True, index=True
    )
    current_level: Mapped[str] = mapped_column(String(16), default="village", index=True)
    target_org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    # submitted=待审核, station_reviewed=服务站已复核(存量兼容,新单不产生), township_reviewed=卫生院已审核,
    # accepted=县级已接收, arrived=已到院, down_referred=已下转,
    # followup_received=下转随访已接收, closed=已闭环, rejected=已退回, withdrawn=已撤回
    status: Mapped[str] = mapped_column(String(24), default="submitted", index=True)
    reason: Mapped[str] = mapped_column(String(512), default="")
    trigger_rule_code: Mapped[str] = mapped_column(String(32), default="")
    trigger_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    materials: Mapped[list] = mapped_column(JSON, default=list)
    # 有效就诊判定（全程管理中心端 #14）：到院并产生就诊记录才计入绩效
    effective_visit: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    stable_for_down: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SpdReferralStep(Base):
    """转诊环节轨迹：每一次审核 / 接收 / 退回都留一行，供全轨迹追溯。"""

    __tablename__ = "spd_referral_steps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("spd_referral_cases.id"), index=True)
    step: Mapped[str] = mapped_column(String(24))
    action: Mapped[str] = mapped_column(String(16), default="")
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    opinion: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ============================================================================
# 六、考核域：指标库 / 考核方案 / 计分 / 村医积分与兑换
# ============================================================================


class SpdIndicator(Base):
    """专病考核指标库：数据来源、取数范围、计算公式、评分规则、权重、目标值。

    `formula` 走既有 `app/formula.py` 的 AST 白名单求值器——平台已经有一个
    受控求值器，再引一个就是再引一处注入面。
    """

    __tablename__ = "spd_indicators"
    __table_args__ = (UniqueConstraint("code", "version", name="uq_spd_indicator_code_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(64))
    program_codes: Mapped[list] = mapped_column(JSON, default=list)
    # org=机构, doctor=医生, village_doctor=村医, team=团队
    object_type: Mapped[str] = mapped_column(String(16), default="org", index=True)
    data_source: Mapped[str] = mapped_column(String(32), default="task")
    scope_expr: Mapped[str] = mapped_column(String(256), default="")
    formula: Mapped[str] = mapped_column(String(256), default="")
    # {"type":"ratio","full":100,"target":0.9} 或 {"type":"step","steps":[...]}
    score_rule: Mapped[dict] = mapped_column(JSON, default=dict)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    target_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    abnormal_rule: Mapped[str] = mapped_column(String(256), default="")
    version: Mapped[str] = mapped_column(String(16), default="v1")
    effective_from: Mapped[str] = mapped_column(String(10), default="")
    effective_scope: Mapped[str] = mapped_column(String(64), default="region")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SpdAssessPlan(Base):
    """分级考核方案：医院 / 卫生院 / 服务站 / 村医四级，按病种与对象组合指标。"""

    __tablename__ = "spd_assess_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    # hospital=县级医院, township=卫生院, station=服务站, village=村医, team=团队
    level: Mapped[str] = mapped_column(String(16), default="township", index=True)
    program_codes: Mapped[list] = mapped_column(JSON, default=list)
    object_type: Mapped[str] = mapped_column(String(16), default="org")
    # month=月度, quarter=季度, year=年度
    period_type: Mapped[str] = mapped_column(String(16), default="month")
    # [{"indicator_code":"followup_rate","weight":30}]
    items: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SpdScore(Base):
    """考核结果：综合得分、分项明细、排名，支持下钻到指标与原始数据。"""

    __tablename__ = "spd_scores"
    __table_args__ = (
        UniqueConstraint("plan_id", "period", "object_type", "object_id", name="uq_spd_score"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("spd_assess_plans.id"), index=True)
    period: Mapped[str] = mapped_column(String(16), index=True)
    object_type: Mapped[str] = mapped_column(String(16), default="org")
    object_id: Mapped[int] = mapped_column(Integer, index=True)
    object_name: Mapped[str] = mapped_column(String(64), default="")
    program_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    total_score: Mapped[float] = mapped_column(Float, default=0)
    rank: Mapped[int] = mapped_column(Integer, default=0)
    # [{"indicator_code":..,"value":..,"score":..,"deduction":..,"reason":..,"samples":[..]}]
    detail: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdPointRule(Base):
    """村医积分规则：业务事件 → 分值，含每日上限。"""

    __tablename__ = "spd_point_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    # sign=签约, referral_up=有效上转, referral_down=下转承接,
    # followup=随访完成, abnormal_report=异常上报
    event: Mapped[str] = mapped_column(String(24), index=True)
    points: Mapped[int] = mapped_column(Integer, default=1)
    daily_limit: Mapped[int] = mapped_column(Integer, default=0)
    condition: Mapped[str] = mapped_column(String(256), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdPointAccount(Base):
    """积分账户：余额 = 累计获得 - 累计兑换，三列同时维护便于对账。"""

    __tablename__ = "spd_point_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    balance: Mapped[int] = mapped_column(Integer, default=0)
    earned: Mapped[int] = mapped_column(Integer, default=0)
    used: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdPointRecord(Base):
    """积分收支明细：来源、扣减、余额快照与历史结算台账。"""

    __tablename__ = "spd_point_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("spd_point_accounts.id"), index=True)
    rule_code: Mapped[str] = mapped_column(String(32), default="")
    # in=获得, out=兑换扣减
    direction: Mapped[str] = mapped_column(String(8), default="in", index=True)
    points: Mapped[int] = mapped_column(Integer, default=0)
    balance_after: Mapped[int] = mapped_column(Integer, default=0)
    ref_type: Mapped[str] = mapped_column(String(24), default="")
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdGoods(Base):
    """积分商品：库存与所需积分。"""

    __tablename__ = "spd_goods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    points: Mapped[int] = mapped_column(Integer, default=100)
    stock: Mapped[int] = mapped_column(Integer, default=0)
    image_url: Mapped[str] = mapped_column(String(256), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdRedeem(Base):
    """兑换与核销：兑换即冻结库存与积分，核销码线下核销后置为 verified。"""

    __tablename__ = "spd_redeems"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("spd_point_accounts.id"), index=True)
    goods_id: Mapped[int] = mapped_column(ForeignKey("spd_goods.id"), index=True)
    points: Mapped[int] = mapped_column(Integer, default=0)
    verify_code: Mapped[str] = mapped_column(String(16), index=True)
    # pending=待核销, verified=已核销, cancelled=已取消
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    verified_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SpdSignin(Base):
    """积分签到：每日一次，唯一约束挡住重复签到。"""

    __tablename__ = "spd_signins"
    __table_args__ = (UniqueConstraint("account_id", "day", name="uq_spd_signin_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("spd_point_accounts.id"), index=True)
    day: Mapped[str] = mapped_column(String(10))
    points: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ============================================================================
# 七、智能随访域（通用随访能力应用端）
# ============================================================================


class SpdFollowupRule(Base):
    """随访方案规则：基于诊断 / 手术 / 医嘱关键词命中，配多时间点随访。"""

    __tablename__ = "spd_followup_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    # inpatient=出院随访, outpatient=门诊随访, surgery=术后随访, checkup=体检随访
    scene: Mapped[str] = mapped_column(String(16), default="inpatient", index=True)
    dept: Mapped[str] = mapped_column(String(64), default="")
    program_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    diagnosis_keywords: Mapped[list] = mapped_column(JSON, default=list)
    surgery_keywords: Mapped[list] = mapped_column(JSON, default=list)
    order_keywords: Mapped[list] = mapped_column(JSON, default=list)
    # 随访时间点（天）：[1, 7, 30, 90]
    points: Mapped[list] = mapped_column(JSON, default=list)
    questionnaire_code: Mapped[str] = mapped_column(String(32), default="")
    executor_role: Mapped[str] = mapped_column(String(32), default="nurse")
    # 可使用/可制定本方案的科室与角色（"控制不同科室、角色及人员的制定和使用权限"）
    allow_depts: Mapped[list] = mapped_column(JSON, default=list)
    allow_roles: Mapped[list] = mapped_column(JSON, default=list)
    preset: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdQuestionnaire(Base):
    """结构化随访问卷：题目 + 异常分级规则 + 跟踪科室与处置措施。"""

    __tablename__ = "spd_questionnaires"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    scene: Mapped[str] = mapped_column(String(16), default="inpatient")
    items: Mapped[list] = mapped_column(JSON, default=list)
    # [{"when":{"key":"pain","op":">=","value":7},"level":"high","action":"通知主管医师"}]
    abnormal_rules: Mapped[list] = mapped_column(JSON, default=list)
    track_dept: Mapped[str] = mapped_column(String(64), default="")
    handle_role: Mapped[str] = mapped_column(String(32), default="doctor")
    preset: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdFollowupRecord(Base):
    """随访任务与执行结果：自助 / 电话 / 微信 / 短信多渠道，异常分级留列。"""

    __tablename__ = "spd_followup_records"
    __table_args__ = (Index("ix_spd_fu_status_planned", "status", "planned_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    program_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    rule_id: Mapped[int | None] = mapped_column(ForeignKey("spd_followup_rules.id"), nullable=True)
    questionnaire_code: Mapped[str] = mapped_column(String(32), default="")
    scene: Mapped[str] = mapped_column(String(16), default="inpatient", index=True)
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    dept: Mapped[str] = mapped_column(String(64), default="")
    planned_at: Mapped[str] = mapped_column(String(10), default="", index=True)
    executed_at: Mapped[str] = mapped_column(String(10), default="")
    # phone=电话, wechat=微信, sms=短信, self=患者自助, visit=上门
    channel: Mapped[str] = mapped_column(String(16), default="phone")
    executor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    # none=无异常, low=轻度, mid=中度, high=重度
    abnormal_level: Mapped[str] = mapped_column(String(16), default="none", index=True)
    result: Mapped[str] = mapped_column(String(512), default="")
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    # planned=待随访, done=已完成, overdue=已超期, removed=已移除, unreachable=失访
    status: Mapped[str] = mapped_column(String(16), default="planned", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdCallTask(Base):
    """呼叫任务与通话记录：随访 / 复诊 / 宣教 / 异常处置统一转呼叫。"""

    __tablename__ = "spd_call_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    phone: Mapped[str] = mapped_column(String(20), default="", index=True)
    ref_type: Mapped[str] = mapped_column(String(24), default="followup")
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # pending=待呼叫, connected=已接通, failed=未接通, cancelled=已取消
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    operator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_s: Mapped[int] = mapped_column(Integer, default=0)
    record_url: Mapped[str] = mapped_column(String(256), default="")
    result: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdQcSample(Base):
    """随访质量抽查：按比例或数量抽，回听录音并记录抽查结果。"""

    __tablename__ = "spd_qc_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("spd_followup_records.id"), index=True)
    batch: Mapped[str] = mapped_column(String(32), default="", index=True)
    dept: Mapped[str] = mapped_column(String(64), default="")
    sampler_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    method: Mapped[str] = mapped_column(String(16), default="record")
    # pass=合格, warn=基本合格, fail=不合格
    result: Mapped[str] = mapped_column(String(16), default="", index=True)
    note: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ============================================================================
# 八、智能辅助域：报告模板与推送
# ============================================================================


class SpdReportTemplate(Base):
    """报告模板：日报 / 周报 / 月报，段落与变量可配，按层级形成分层报告。"""

    __tablename__ = "spd_report_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    # daily=日报, weekly=周报, monthly=月报, custom=自定义
    period: Mapped[str] = mapped_column(String(16), default="daily", index=True)
    # center=专病中心, dept=科室团队, grassroots=基层机构, personal=个人
    scope_level: Mapped[str] = mapped_column(String(16), default="center", index=True)
    # [{"key":"summary","title":"总体概览","type":"text","expr":"..."}]
    sections: Mapped[list] = mapped_column(JSON, default=list)
    variables: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SpdReportTask(Base):
    """报告推送任务：频率、目标用户、推送时间、有效期与优先级调度。"""

    __tablename__ = "spd_report_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("spd_report_templates.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    frequency: Mapped[str] = mapped_column(String(16), default="daily")
    push_time: Mapped[str] = mapped_column(String(5), default="08:00")
    subscriber_ids: Mapped[list] = mapped_column(JSON, default=list)
    org_ids: Mapped[list] = mapped_column(JSON, default=list)
    valid_from: Mapped[str] = mapped_column(String(10), default="")
    valid_to: Mapped[str] = mapped_column(String(10), default="")
    priority: Mapped[int] = mapped_column(Integer, default=1)
    # active=启用, paused=暂停
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SpdReportInstance(Base):
    """已生成的报告实例：内容按模板编排为文本 / 表格 / 图表数据。"""

    __tablename__ = "spd_report_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int | None] = mapped_column(ForeignKey("spd_report_tasks.id"), nullable=True)
    template_code: Mapped[str] = mapped_column(String(32), default="", index=True)
    title: Mapped[str] = mapped_column(String(128), default="")
    period_label: Mapped[str] = mapped_column(String(32), default="", index=True)
    scope_level: Mapped[str] = mapped_column(String(16), default="center")
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    content: Mapped[dict] = mapped_column(JSON, default=dict)
    subscriber_ids: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SpdDedupReport(Base):
    """唯一键冲突台账：迁移探到的重复行，整行 JSON 存档，处置进度记在这里。

    存量库里 18 张 spd 表的唯一索引被迁移建成了普通索引（模型上一直写着
    `unique=True`），于是"同一编码两条配置""同一村医两个积分账户"这类数据
    真的能落库。补唯一索引前必须先去重，而**迁移不替人删数据**（CLAUDE.md §4）：
    `e1a2b3c4d5e9` 探到重复只做三件事——跳过这张表的唯一索引、把冲突逐行记进
    这张表、打一条点名到表/键值/行 id 的 ERROR 日志。

    真正的归并由人执行 `python scripts/spd_dedup.py --apply`，它把处置结果写回
    `strategy`。三个取值：

    - `pending`：迁移刚记下的冲突，**还没处置**，业务表里两行都还在
    - `merge`：已自动归并（当前只有积分账户：余额/累计相加后并成一条）
    - `keep_earliest`：已保留最早一条、其余存档移除，
      **差异是否补回保留的那条由实施期人工裁定**

    `removed_row` 存被并掉那一行的完整原始字段——事后核对"两份配置差在哪"全靠它。
    除处置脚本回填 `strategy`/`note` 外不改这张表：它是审计性质的账。
    """

    __tablename__ = "spd_dedup_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    table_name: Mapped[str] = mapped_column(String(64), default="", index=True)
    key_column: Mapped[str] = mapped_column(String(64), default="")
    key_value: Mapped[str] = mapped_column(String(128), default="", index=True)
    kept_id: Mapped[int] = mapped_column(Integer, default=0)
    removed_id: Mapped[int] = mapped_column(Integer, default=0)
    # pending=迁移记下的冲突待处置；merge=已自动归并；keep_earliest=保留最早一条其余已存档移除
    strategy: Mapped[str] = mapped_column(String(16), default="pending")
    removed_row: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
