"""领域模型分块（见 app/models/__init__.py 说明）。
所有类共用 _shared 的 Base/Money/工具与 SQLAlchemy 名称；跨块引用一律走
字符串（ForeignKey("t.col") / relationship("Cls")），由 registry 惰性解析，
不依赖 import 顺序。"""
from ._shared import *  # noqa: F401,F403


class ResidentAccount(Base):
    """居民账户：登录凭据（手机号/微信 openid）与患者主索引的绑定关系。

    账户与档案是两件事——先登录拿到账户身份，再实名绑定到 Patient 才看得到档案。
    未绑定的账户只能看健康宣教，这样验证码泄露也不会直接泄露他人档案。

    phone / wechat_openid 用可空唯一列而非空串：SQLite 与 PostgreSQL 的唯一索引
    都允许多个 NULL，既能让"只有微信没有手机号"的账户共存，又能靠数据库约束
    挡住并发首登产生的重复账户（撞约束后回查既有账户即可）。
    """

    __tablename__ = "resident_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    wechat_openid: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    wechat_unionid: Mapped[str] = mapped_column(String(64), default="")
    nickname: Mapped[str] = mapped_column(String(64), default="")
    # 实名绑定后的患者主索引；未绑定为 None
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), nullable=True, index=True)
    # active=正常, disabled=停用（停用后令牌校验即失败）
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SmsCode(Base):
    """短信验证码：只落散列不落明文，与口令同等对待（6位码空间小，用 PBKDF2）。"""

    __tablename__ = "sms_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(String(20), index=True)
    # login=登录, bind=已登录账户绑定手机号
    purpose: Mapped[str] = mapped_column(String(16), default="login")
    code_hash: Mapped[str] = mapped_column(String(160))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    # 单条验证码的试错次数，超限即作废，防止对同一条码穷举
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ResidentFamilyMember(Base):
    """家庭成员代管：一个账户代管家人的档案（老人、儿童往往不自己用手机）。

    与 ResidentAccount.patient_id（本人档案）分开存：本人档案唯一且由实名绑定
    产生，代管关系可以有多条，且同一份档案可被多个子女同时代管。
    """

    __tablename__ = "resident_family_members"
    __table_args__ = (
        UniqueConstraint("account_id", "patient_id", name="uq_family_account_patient"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("resident_accounts.id"), index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    # spouse=配偶, child=子女, parent=父母, other=其他
    relation: Mapped[str] = mapped_column(String(16), default="other")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ============================================================================
# T1.1 定时任务基座：任务注册与执行留痕
# ============================================================================


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


# ============================================================================
# 阶段二 T2.1/T2.2：住院临床文书（病程记录、护理记录、体温单、交接班）
# ============================================================================


class ProgressNote(Base):
    """住院病程记录。

    与门诊 MedicalRecord 的区别：门诊病历是"一次就诊一份"，住院病程是同一次
    住院内的连续文书流，因此挂在 admission 上而不是 encounter 上，且可有多条。
    """

    __tablename__ = "progress_notes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admission_id: Mapped[int] = mapped_column(ForeignKey("admissions.id"), index=True)
    # first=首次病程, daily=日常病程, ward_round=上级医师查房,
    # rescue=抢救记录, consultation=会诊记录, discharge=出院记录
    note_type: Mapped[str] = mapped_column(String(16), index=True)
    content: Mapped[str] = mapped_column(String(4096))
    doctor_name: Mapped[str] = mapped_column(String(64), default="")
    # 记录时刻（YYYY-MM-DD HH:MM），与创建时刻分开：补记时二者不同
    recorded_at: Mapped[str] = mapped_column(String(16), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class NursingRecord(Base):
    """护理记录：护理级别执行与病情观察。

    挂载点二选一：`admission_id` 是住院护理，`encounter_id` 是门急诊护理
    （输液观察、留观、清创换药）。原先只支持住院，导致门急诊护理无处可落，
    指南 #3 要求的门急诊护理记录一直是空的。

    不合并成一个"就诊上下文 id"——两者的查询路径、权限归属和保留期都不同，
    合并后每次用都要先判断这个 id 到底指向哪张表，反而更容易写错。
    """

    __tablename__ = "nursing_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admission_id: Mapped[int | None] = mapped_column(
        ForeignKey("admissions.id"), nullable=True, index=True
    )
    encounter_id: Mapped[int | None] = mapped_column(
        ForeignKey("encounters.id"), nullable=True, index=True
    )
    # special=特级护理, level1=一级, level2=二级, level3=三级
    nursing_level: Mapped[str] = mapped_column(String(16), default="level2", index=True)
    content: Mapped[str] = mapped_column(String(2048), default="")
    nurse_name: Mapped[str] = mapped_column(String(64), default="")
    recorded_at: Mapped[str] = mapped_column(String(16), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class VitalSignRecord(Base):
    """体温单：住院期间体征时序（体温/脉搏/呼吸/血压/出入量/体重）。

    各项均可空——一次测量未必测全，用 0 冒充"未测"会污染趋势曲线。
    """

    __tablename__ = "vital_sign_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admission_id: Mapped[int] = mapped_column(ForeignKey("admissions.id"), index=True)
    measured_at: Mapped[str] = mapped_column(String(16), index=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    pulse: Mapped[int | None] = mapped_column(Integer, nullable=True)
    respiration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sbp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dbp: Mapped[int | None] = mapped_column(Integer, nullable=True)
    intake_ml: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_ml: Mapped[int | None] = mapped_column(Integer, nullable=True)
    weight_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    recorder: Mapped[str] = mapped_column(String(64), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ShiftHandover(Base):
    """病区交接班记录。"""

    __tablename__ = "shift_handovers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ward_id: Mapped[int] = mapped_column(ForeignKey("wards.id"), index=True)
    # day=白班, evening=小夜, night=大夜
    shift: Mapped[str] = mapped_column(String(16), index=True)
    handover_date: Mapped[str] = mapped_column(String(10), index=True)
    from_staff: Mapped[str] = mapped_column(String(64), default="")
    to_staff: Mapped[str] = mapped_column(String(64), default="")
    # 交班时在院人数与危重人数：交接班的核心数字，从住院数据快照落库
    patient_count: Mapped[int] = mapped_column(Integer, default=0)
    critical_count: Mapped[int] = mapped_column(Integer, default=0)
    content: Mapped[str] = mapped_column(String(2048), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ============================================================================
# 阶段二 T2.3：手术麻醉管理
# ============================================================================


class OperatingRoom(Base):
    """手术间资源。"""

    __tablename__ = "operating_rooms"
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_or_org_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SurgeryRequest(Base):
    """手术申请：申请→审批→排班→完成。"""

    __tablename__ = "surgery_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admission_id: Mapped[int] = mapped_column(ForeignKey("admissions.id"), index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    surgery_name: Mapped[str] = mapped_column(String(256))
    surgery_code: Mapped[str] = mapped_column(String(32), default="")
    # I=I类切口 … IV；与院感监测的手术部位感染统计对齐
    incision_level: Mapped[str] = mapped_column(String(4), default="II")
    # general=全麻, spinal=椎管内, local=局麻, nerve_block=神经阻滞
    anesthesia_type: Mapped[str] = mapped_column(String(16), default="general")
    surgeon_name: Mapped[str] = mapped_column(String(64), default="")
    # elective=择期, urgent=限期, emergency=急诊
    urgency: Mapped[str] = mapped_column(String(16), default="elective", index=True)
    # 非计划重返手术室：同一次住院内因并发症/失误等原因再次手术。
    # 必须由医师显式标记，不能靠"同一住院有第二台手术"推断——分期手术、
    # 计划内二次探查都是正常的，推断出来的指标只会冤枉人。
    unplanned_return: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    planned_date: Mapped[str] = mapped_column(String(10), default="")
    # requested=待审批, approved=已审批, scheduled=已排班, completed=已完成, cancelled=已取消
    status: Mapped[str] = mapped_column(String(16), default="requested", index=True)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SurgerySchedule(Base):
    """手术排班：手术间 + 日期 + 时段。

    (room_id, scheduled_date, start_time) 唯一只挡得住起点完全相同的重排，
    真正的区间重叠由应用层比较 start/end 判定（见 routers/surgery.py）。
    """

    __tablename__ = "surgery_schedules"
    __table_args__ = (
        UniqueConstraint("room_id", "scheduled_date", "start_time", name="uq_schedule_room_slot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("surgery_requests.id"), unique=True, index=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("operating_rooms.id"), index=True)
    scheduled_date: Mapped[str] = mapped_column(String(10), index=True)
    start_time: Mapped[str] = mapped_column(String(5))
    end_time: Mapped[str] = mapped_column(String(5))
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SurgeryRecord(Base):
    """术中记录：一台手术一份，完成时填写。"""

    __tablename__ = "surgery_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("surgery_requests.id"), unique=True, index=True)
    actual_surgery_name: Mapped[str] = mapped_column(String(256))
    surgeon_name: Mapped[str] = mapped_column(String(64), default="")
    assistants: Mapped[str] = mapped_column(String(256), default="")
    anesthetist_name: Mapped[str] = mapped_column(String(64), default="")
    anesthesia_type: Mapped[str] = mapped_column(String(16), default="general")
    incision_level: Mapped[str] = mapped_column(String(4), default="II")
    start_at: Mapped[str] = mapped_column(String(16), default="")
    end_at: Mapped[str] = mapped_column(String(16), default="")
    blood_loss_ml: Mapped[int] = mapped_column(Integer, default=0)
    findings: Mapped[str] = mapped_column(String(2048), default="")
    procedure: Mapped[str] = mapped_column(String(4096), default="")
    complications: Mapped[str] = mapped_column(String(1024), default="")
    # 治愈/好转/未愈/死亡
    outcome: Mapped[str] = mapped_column(String(16), default="好转")
    # 术前/术后诊断：术前术后诊断符合率的唯一数据来源。
    # 不复用 surgery_requests.surgery_name——术式名不是诊断，拿术式比对算出来的
    # "符合率"只是在比两段一模一样的文本，好看但没有意义。
    preop_diagnosis: Mapped[str] = mapped_column(String(256), default="")
    postop_diagnosis: Mapped[str] = mapped_column(String(256), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ============================================================================
# 阶段二 T2.4：统一随访中心
# ============================================================================


class FollowupTask(Base):
    """统一随访任务：慢病、出院、术后、妇幼四类随访收敛到同一任务模型。

    此前只有慢病随访有载体，出院随访与术后随访无处安放。source_id 指向来源
    业务单据（慢病档案/住院记录/手术申请），不做外键——四类来源表不同，
    用外键就得开四个可空列，反而更难查。
    """

    __tablename__ = "followup_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # chronic=慢病随访, discharge=出院随访, surgery=术后随访, maternal=妇幼访视
    category: Mapped[str] = mapped_column(String(16), index=True)
    source_id: Mapped[int] = mapped_column(Integer, default=0, index=True)
    title: Mapped[str] = mapped_column(String(128), default="")
    due_date: Mapped[str] = mapped_column(String(10), index=True)
    assigned_to: Mapped[str] = mapped_column(String(64), default="")
    # pending=待随访, done=已完成, cancelled=已取消
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    result: Mapped[str] = mapped_column(String(1024), default="")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


# ============================================================================
# 阶段三 T3.1：会计科目与记账凭证（复式记账）
# ============================================================================


class AccountSubject(Base):
    """会计科目。

    此前 FinanceEntry 只是"期间 + 收/支 + 金额"的流水台账，出不了资产负债表，
    也无法核对借贷是否平衡。这里补上科目体系与凭证，FinanceEntry 保留为
    业务口径的收支汇总，二者并存不互相取代。
    """

    __tablename__ = "account_subjects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    # asset=资产, liability=负债, net_asset=净资产, income=收入, expense=费用
    category: Mapped[str] = mapped_column(String(16), index=True)
    # 余额方向：debit=借方增加（资产/费用），credit=贷方增加（负债/净资产/收入）
    direction: Mapped[str] = mapped_column(String(8), default="debit")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class Voucher(Base):
    """记账凭证：草稿可改，过账后锁定，冲销走作废而不是删除。"""

    __tablename__ = "vouchers"
    __table_args__ = (UniqueConstraint("org_id", "voucher_no", name="uq_voucher_org_no"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    voucher_no: Mapped[str] = mapped_column(String(32))
    voucher_date: Mapped[str] = mapped_column(String(10), index=True)
    summary: Mapped[str] = mapped_column(String(256), default="")
    total_debit: Mapped[float] = mapped_column(Money, default=0)
    total_credit: Mapped[float] = mapped_column(Money, default=0)
    # draft=草稿, posted=已过账, void=已作废
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    posted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    entries: Mapped[list["VoucherEntry"]] = relationship(back_populates="voucher")


class VoucherEntry(Base):
    """凭证分录：一条分录只能是借方或贷方，不能两边都填。"""

    __tablename__ = "voucher_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    voucher_id: Mapped[int] = mapped_column(ForeignKey("vouchers.id"), index=True)
    subject_code: Mapped[str] = mapped_column(String(16), index=True)
    summary: Mapped[str] = mapped_column(String(256), default="")
    # 借贷金额：整个复式记账、试算平衡表与合并报表都靠这两列求和。
    # 它们不含 amount/price/cost 这类词，阶段十二第一遍按命名族批量改类型时
    # 漏掉了——**平台最核心的两个金额列反而是最后改的**。教训写在这里：
    # 按命名批量处理必须回头核对剩下的清单，不能只看匹配到的那一批。
    debit: Mapped[float] = mapped_column(Money, default=0)
    credit: Mapped[float] = mapped_column(Money, default=0)

    voucher: Mapped[Voucher] = relationship(back_populates="entries")


# ============================================================================
# 阶段三 T3.2：成本核算（科室 / 诊次 / 床日）
# ============================================================================


class DepartmentCost(Base):
    """科室成本归集：期间 × 科室 × 成本项的直接成本。"""

    __tablename__ = "department_costs"
    __table_args__ = (
        UniqueConstraint("dept_id", "period", "cost_type", name="uq_dept_cost_period_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    dept_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    # labor=人员经费, drug=药品, consumable=卫生材料, depreciation=折旧, overhead=其他运行
    cost_type: Mapped[str] = mapped_column(String(16), index=True)
    amount: Mapped[float] = mapped_column(Money, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CostAllocationRule(Base):
    """成本分摊规则：行政后勤/医技科室的成本按比例分摊到临床科室。

    比例用百分数存，同一来源科室的比例之和应为 100；不强制校验为 100，
    因为分期建规则时中间态必然不足 100，改由分摊接口在计算时提示。
    """

    __tablename__ = "cost_allocation_rules"
    __table_args__ = (
        UniqueConstraint("from_dept_id", "to_dept_id", name="uq_alloc_from_to"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    from_dept_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), index=True)
    to_dept_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), index=True)
    ratio_pct: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ============================================================================
# 阶段三 T3.3 / T3.4：物资采购全流程与高值耗材追溯
# ============================================================================


class MaterialPurchase(Base):
    """物资采购：申请→审批→合同→到货验收。

    药品采购走 PurchaseOrder（/api/pharmacy），此处是**非药品物资**，
    两者审批链与入库对象不同，不合并。
    """

    __tablename__ = "material_purchases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    dept_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"), nullable=True)
    item_name: Mapped[str] = mapped_column(String(128))
    spec: Mapped[str] = mapped_column(String(64), default="")
    unit: Mapped[str] = mapped_column(String(16), default="件")
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    estimated_price: Mapped[float] = mapped_column(Money, default=0)
    reason: Mapped[str] = mapped_column(String(512), default="")
    # requested=待审批, approved=已审批, contracted=已签合同, received=已验收, cancelled=已取消
    status: Mapped[str] = mapped_column(String(16), default="requested", index=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"), nullable=True)
    contract_no: Mapped[str] = mapped_column(String(64), default="")
    contract_amount: Mapped[float] = mapped_column(Money, default=0)
    received_quantity: Mapped[int] = mapped_column(Integer, default=0)
    received_note: Mapped[str] = mapped_column(String(512), default="")
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class HighValueConsumable(Base):
    """高值耗材：一物一码，使用时绑定患者与手术，构成可追溯链。"""

    __tablename__ = "high_value_consumables"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    barcode: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    spec: Mapped[str] = mapped_column(String(64), default="")
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    supplier_id: Mapped[int | None] = mapped_column(ForeignKey("suppliers.id"), nullable=True)
    batch_no: Mapped[str] = mapped_column(String(64), default="")
    expire_date: Mapped[str] = mapped_column(String(10), default="", index=True)
    unit_price: Mapped[float] = mapped_column(Money, default=0)
    # in_stock=在库, used=已使用, returned=已退回, scrapped=已报废
    status: Mapped[str] = mapped_column(String(16), default="in_stock", index=True)
    used_patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), nullable=True, index=True)
    used_surgery_id: Mapped[int | None] = mapped_column(
        ForeignKey("surgery_requests.id"), nullable=True, index=True
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ============================================================================
# 阶段四 T4.1：县外就诊登记（县域就诊率 / 外转率的数据源）
# ============================================================================


class OutboundVisit(Base):
    """县外就诊登记：本县居民在县域外机构的门诊/住院记录。

    没有这张表，"县域就诊率"和"外转率"这两项紧密型医共体的头号监测指标就
    只有分子没有分母——平台只看得见县内发生的诊疗。数据来源既可人工登记，
    也可从医保结算数据批量导入（source 字段区分）。
    """

    __tablename__ = "outbound_visits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    visit_date: Mapped[str] = mapped_column(String(10), index=True)
    external_org_name: Mapped[str] = mapped_column(String(128))
    # city=市级, province=省级, other=其他（含省外）
    external_org_level: Mapped[str] = mapped_column(String(16), default="city", index=True)
    # outpatient=门急诊, inpatient=住院
    visit_type: Mapped[str] = mapped_column(String(16), default="outpatient", index=True)
    diagnosis_name: Mapped[str] = mapped_column(String(256), default="")
    total_amount: Mapped[float] = mapped_column(Money, default=0)
    insurance_pay: Mapped[float] = mapped_column(Money, default=0)
    # 关联转诊单：有值表示经县域内机构规范转出，无值即自行外出就医
    referral_id: Mapped[int | None] = mapped_column(ForeignKey("referrals.id"), nullable=True)
    # manual=人工登记, insurance_import=医保数据导入
    source: Mapped[str] = mapped_column(String(20), default="manual", index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ============================================================================
# 阶段四 T4.3：绩效自定义公式
# ============================================================================


class PerformanceFormula(Base):
    """自定义绩效公式：表达式引用平台指标变量，由受限求值器计算。

    表达式只允许数字、变量名、四则运算、括号与少量白名单函数，
    求值走 AST 白名单而不是 eval——绩效公式由管理员录入，等同于让用户
    往服务端塞代码，用 eval 就是远程执行漏洞。
    """

    __tablename__ = "performance_formulas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    expression: Mapped[str] = mapped_column(String(512))
    unit: Mapped[str] = mapped_column(String(16), default="")
    # 该指标是否越大越好（用于综合报告排序与达标判断）
    higher_is_better: Mapped[bool] = mapped_column(Boolean, default=True)
    # 计入综合绩效的权重；0 表示只观测不计分
    weight: Mapped[float] = mapped_column(Float, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ============================================================================
# 阶段五 T5.1：统一规则引擎
# ============================================================================


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


# ============================================================================
# 阶段五 T5.2：业务流程引擎
# ============================================================================


