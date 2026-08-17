"""领域模型分块（见 app/models/__init__.py 说明）。
所有类共用 _shared 的 Base/Money/工具与 SQLAlchemy 名称；跨块引用一律走
字符串（ForeignKey("t.col") / relationship("Cls")），由 registry 惰性解析，
不依赖 import 顺序。"""
from ._shared import *  # noqa: F401,F403


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

    entries: Mapped[list["CodeEntry"]] = relationship(back_populates="system")


class CodeEntry(Base):
    __tablename__ = "code_entries"
    __table_args__ = (UniqueConstraint("system_id", "code", name="uq_entry_system_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    system_id: Mapped[int] = mapped_column(ForeignKey("code_systems.id"), index=True)
    code: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(256), index=True)

    system: Mapped[CodeSystem] = relationship(back_populates="entries")


class Encounter(Base):
    """就诊记录（电子病历摘要），健康档案与患者360视图的数据来源。"""

    __tablename__ = "encounters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    doctor_name: Mapped[str] = mapped_column(String(64), default="")
    # outpatient=门诊, inpatient=住院
    encounter_type: Mapped[str] = mapped_column(String(16), default="outpatient")
    diagnosis_code: Mapped[str] = mapped_column(String(64), default="")
    diagnosis_name: Mapped[str] = mapped_column(String(256), default="")
    summary: Mapped[str] = mapped_column(String(1024), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ExamRequest(Base):
    """共享诊断中心申请（影像/心电/检验/病理共用）："基层检查、上级诊断、结果互认"。"""

    __tablename__ = "exam_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    from_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    # imaging=影像, ecg=心电, lab=检验, pathology=病理
    center_type: Mapped[str] = mapped_column(String(16), index=True)
    item_code: Mapped[str] = mapped_column(String(64), index=True)
    item_name: Mapped[str] = mapped_column(String(128))
    clinical_info: Mapped[str] = mapped_column(String(512), default="")
    # pending=待诊断, diagnosing=诊断中, reported=已报告, recognized=互认既往结果
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    recognized_from_id: Mapped[int | None] = mapped_column(
        ForeignKey("exam_requests.id"), nullable=True
    )
    recognition_declined_reason: Mapped[str] = mapped_column(String(256), default="")
    # 检验样本物流（仅 lab）："" / collected=已采样 / in_transit=转运中 / received=中心核收
    sample_status: Mapped[str] = mapped_column(String(16), default="")
    # L-6 整改：诊断领取人（原子领取时记录，避免并发双领与责任不清）
    claimed_by: Mapped[str] = mapped_column(String(64), default="")
    # 承接诊断的中心机构：领取(claim)/出报告时置为诊断方所在机构。中心队列按
    # center_type 跨机构发现（不按此列过滤），但出报告须由承接中心本机构完成——
    # 据此堵住"他院医师出别家已领取报告"，并让报告可归属可审计。
    center_org_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id"), index=True, nullable=True
    )
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    report: Mapped["ExamReport | None"] = relationship(back_populates="request")


class ExamReport(Base):
    __tablename__ = "exam_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(
        ForeignKey("exam_requests.id"), unique=True, index=True
    )
    finding: Mapped[str] = mapped_column(String(2048), default="")
    conclusion: Mapped[str] = mapped_column(String(1024))
    critical: Mapped[bool] = mapped_column(Boolean, default=False)
    # 危急值闭环状态：""=非危急值, notified=已通知, acknowledged=医师已确认, resolved=已处置
    critical_status: Mapped[str] = mapped_column(String(16), default="", index=True)
    reported_by: Mapped[str] = mapped_column(String(64), default="")
    reported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    request: Mapped[ExamRequest] = relationship(back_populates="report")


class CriticalAction(Base):
    """危急值处置留痕：通知→确认→处置反馈全程记录（闭环管理）。"""

    __tablename__ = "critical_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("exam_reports.id"), index=True)
    # notified=系统通知, acknowledged=接收确认, resolved=处置反馈
    action: Mapped[str] = mapped_column(String(512))
    actor: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReportRevision(Base):
    """报告修订历史（M-6 整改）：每次修订记录修订前值，医疗文书修订可追溯。"""

    __tablename__ = "report_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("exam_reports.id"), index=True)
    prev_conclusion: Mapped[str] = mapped_column(String(1024), default="")
    prev_finding: Mapped[str] = mapped_column(String(2048), default="")
    prev_critical: Mapped[bool] = mapped_column(Boolean, default=False)
    revised_by: Mapped[str] = mapped_column(String(64), default="")
    reason: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RecognitionItem(Base):
    """检查检验结果互认项目目录：仅目录内 active 项目参与互认。"""

    __tablename__ = "recognition_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    item_name: Mapped[str] = mapped_column(String(128))
    # imaging=影像, ecg=心电, lab=检验, pathology=病理
    center_type: Mapped[str] = mapped_column(String(16), index=True)
    # county=县域内互认, city=市级互认
    mutual_scope: Mapped[str] = mapped_column(String(16), default="county")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DrugRule(Base):
    """合理用药规则库：集中审方的"系统审"依据。"""

    __tablename__ = "drug_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    drug_code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    max_daily_dose: Mapped[float] = mapped_column(Float)
    dose_unit: Mapped[str] = mapped_column(String(16), default="mg")
    note: Mapped[str] = mapped_column(String(256), default="")
    # 相互作用：与该药冲突的药品编码（逗号分隔），同方出现冲突药对转药师审
    interactions: Mapped[str] = mapped_column(String(512), default="")
    # 禁忌诊断关键词（逗号分隔）：诊断名命中即转药师审
    contraindicated_diagnoses: Mapped[str] = mapped_column(String(512), default="")
    # 特殊人群（逗号分隔：pregnant,child,elderly）：患者命中即转药师审
    special_groups: Mapped[str] = mapped_column(String(64), default="")
    # 块2：肝肾功能提示（不拦截，开方与点评时随处方返回，供剂量调整参考）
    renal_hepatic_note: Mapped[str] = mapped_column(String(512), default="")
    # 块2：处方点评要点（事后点评规则化依据）
    review_points: Mapped[str] = mapped_column(String(512), default="")
    # 抗菌药物标记与 DDD（限定日剂量，单位同 dose_unit）。
    # 使用强度 = Σ(日剂量×天数 ÷ DDD) × 100 ÷ 同期收治人天，是国家监测指标。
    # ddd 为 0 表示未维护，统计时**跳过并计入未覆盖数**——按缺省值硬算会把强度
    # 算成无穷大或悄悄漏掉，两种都比"明说没维护"更糟。
    antibiotic: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    ddd: Mapped[float] = mapped_column(Float, default=0)
    # D-4 同类：录错一条规则原先只能靠 import 覆盖，删不掉也停不掉，
    # 而通用规则引擎 `/api/rules/{key}` 是有停用的。停用不删行——规则改过什么、
    # 什么时候不再生效，是处方点评复核时要回溯的。
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


class Prescription(Base):
    """处方：全量进入集中审方中心，系统审+药师审双重审核。"""

    __tablename__ = "prescriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    diagnosis_name: Mapped[str] = mapped_column(String(256), default="")
    # auto_passed=系统审通过, pending_review=待药师审, approved=药师审通过, rejected=退回
    status: Mapped[str] = mapped_column(String(16), default="auto_passed", index=True)
    review_comment: Mapped[str] = mapped_column(String(1024), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    items: Mapped[list["PrescriptionItem"]] = relationship(back_populates="prescription")


class PrescriptionItem(Base):
    __tablename__ = "prescription_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prescription_id: Mapped[int] = mapped_column(ForeignKey("prescriptions.id"), index=True)
    drug_code: Mapped[str] = mapped_column(String(64))
    drug_name: Mapped[str] = mapped_column(String(128))
    daily_dose: Mapped[float] = mapped_column(Float)
    days: Mapped[int] = mapped_column(Integer, default=1)

    prescription: Mapped[Prescription] = relationship(back_populates="items")


class DrugStock(Base):
    """中心药房库存：县乡村药品余缺调度的基础。"""

    __tablename__ = "drug_stocks"
    __table_args__ = (UniqueConstraint("org_id", "drug_code", name="uq_stock_org_drug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    drug_code: Mapped[str] = mapped_column(String(64), index=True)
    drug_name: Mapped[str] = mapped_column(String(128))
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    # 低于该阈值触发缺药预警
    threshold: Mapped[int] = mapped_column(Integer, default=0)


class StockTransfer(Base):
    __tablename__ = "stock_transfers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    drug_code: Mapped[str] = mapped_column(String(64))
    from_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    to_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ChronicDiseaseType(Base):
    """慢病病种目录（块1）：病种编码/名称/分级规则/指导要点/随访周期。

    - level_rules：JSON 分级规则（指标名 + 阈值区间），结构见 app/chronic_seed.py
    - followup_interval_days：随访周期（天），用于随访后自动建议下次到期日
    启动时按 SEED_CHRONIC_DISEASE_TYPES 幂等种子化 8 个县域重点病种。
    """

    __tablename__ = "chronic_disease_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    level_rules: Mapped[dict] = mapped_column(JSON, default=dict)
    guidance: Mapped[str] = mapped_column(String(512), default="")
    followup_interval_days: Mapped[int] = mapped_column(Integer, default=90)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ChronicPatient(Base):
    """慢病建档：病种取自 ChronicDiseaseType 目录，智能分级分组。"""

    __tablename__ = "chronic_patients"
    __table_args__ = (
        UniqueConstraint("patient_id", "disease", name="uq_chronic_patient_disease"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    # 病种编码，取值见 chronic_disease_types.code（hypertension/diabetes/copd/chd/...）
    disease: Mapped[str] = mapped_column(String(32), index=True)
    # 1=控制良好, 2=需干预, 3=高危
    level: Mapped[int] = mapped_column(Integer, default=1, index=True)
    managed_by_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    next_due: Mapped[str] = mapped_column(String(10), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    followups: Mapped[list["FollowUp"]] = relationship(back_populates="chronic")


class FollowUp(Base):
    __tablename__ = "followups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chronic_id: Mapped[int] = mapped_column(ForeignKey("chronic_patients.id"), index=True)
    sbp: Mapped[float | None] = mapped_column(Float, nullable=True)
    dbp: Mapped[float | None] = mapped_column(Float, nullable=True)
    glucose: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 块1：通用指标 JSON（非血压血糖类，如 adherence_score 用药依从性、cat_score、mrs_score）
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    guidance: Mapped[str] = mapped_column(String(1024), default="")
    next_due: Mapped[str] = mapped_column(String(10), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    chronic: Mapped[ChronicPatient] = relationship(back_populates="followups")


class InfectiousDisease(Base):
    """法定传染病目录：甲/乙/丙类与报告时限（甲类2小时、乙丙类24小时）。"""

    __tablename__ = "infectious_diseases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    # A=甲类, B=乙类, C=丙类
    category: Mapped[str] = mapped_column(String(4), index=True)
    # 报告时限（小时）：甲类2，乙/丙类24
    report_hours: Mapped[int] = mapped_column(Integer, default=24)


class InfectiousCase(Base):
    """传染病病例报告：多点触发监测预警的数据来源。"""

    __tablename__ = "infectious_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    disease_code: Mapped[str] = mapped_column(String(64), index=True)
    disease_name: Mapped[str] = mapped_column(String(128))
    # 报告时自动按传染病目录回填：A/B/C，目录外为空串
    category: Mapped[str] = mapped_column(String(4), default="")
    onset_date: Mapped[str] = mapped_column(String(10), index=True)
    reported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PerformanceIndicator(Base):
    """绩效考核指标目录：维度权重可由管理层调节（按比例归一化到100）。"""

    __tablename__ = "performance_indicators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    weight: Mapped[float] = mapped_column(Float, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class Consultation(Base):
    """远程会诊：基层申请、上级接受、出具意见、申请方评价。"""

    __tablename__ = "consultations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    from_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    to_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    question: Mapped[str] = mapped_column(String(1024))
    # applied=已申请, accepted=已受理, completed=已完成, declined=已拒绝
    status: Mapped[str] = mapped_column(String(16), default="applied", index=True)
    expert_name: Mapped[str] = mapped_column(String(64), default="")
    opinion: Mapped[str] = mapped_column(String(2048), default="")
    # 1-5 星评价，0=未评价
    rating: Mapped[int] = mapped_column(Integer, default=0)
    # 会诊费用（指引⑤"费用管理"）。0 表示未计费——本院内部会诊常不计费，
    # 与"计了 0 元"是两回事，故用 fee_settled 区分而不是拿 0 当哨兵。
    fee: Mapped[float] = mapped_column(Money, default=0)
    fee_settled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    fee_note: Mapped[str] = mapped_column(String(256), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FamilyDoctorContract(Base):
    """家庭医生签约：协议、服务包、履约记录。"""

    __tablename__ = "fd_contracts"
    __table_args__ = (UniqueConstraint("patient_id", "org_id", name="uq_contract_patient_org"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    doctor_name: Mapped[str] = mapped_column(String(64))
    # basic=基础包, standard=标准包, premium=个性包（旧枚举，保留兼容）
    package: Mapped[str] = mapped_column(String(16), default="basic")
    # E1：签约挂内容化服务包（service_packages.id）；空=仍用旧枚举
    package_id: Mapped[int | None] = mapped_column(
        ForeignKey("service_packages.id"), nullable=True, index=True
    )
    # E1：重点人群标签（chronic/elderly/maternal/disabled/poverty…），用于按人头分配加权
    key_population: Mapped[str] = mapped_column(String(32), default="")
    signed_date: Mapped[str] = mapped_column(String(10), default="")
    # E1：协议到期日（YYYY-MM-DD），空=未设；用于续签/到期提醒
    expire_date: Mapped[str] = mapped_column(String(10), default="")
    # active=履约中, terminated=已解约
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    services: Mapped[list["ContractService"]] = relationship(back_populates="contract")


class ContractService(Base):
    __tablename__ = "fd_contract_services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    contract_id: Mapped[int] = mapped_column(ForeignKey("fd_contracts.id"), index=True)
    # visit=上门服务, consult=健康咨询, followup=随访, referral=转诊协助
    service_type: Mapped[str] = mapped_column(String(16))
    # E1：履约核销到具体服务包条目（service_package_items.id）；空=通用履约不计入应履约核销
    item_id: Mapped[int | None] = mapped_column(
        ForeignKey("service_package_items.id"), nullable=True, index=True
    )
    note: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    contract: Mapped[FamilyDoctorContract] = relationship(back_populates="services")


class AppointmentSlot(Base):
    """预约资源：机构发布分时段号源（门诊/检查/检验）。"""

    __tablename__ = "appointment_slots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # outpatient=门诊, exam=检查, lab=检验
    resource_type: Mapped[str] = mapped_column(String(16))
    resource_name: Mapped[str] = mapped_column(String(128))
    # ⑨便捷寻医：号源挂到医师档案上。原先只有 resource_name 自由文本，
    # "找王主任的号"只能靠字符串匹配——同名、写法不一都会漏。可空是因为
    # 检查与检验号源本就不对应某一位医师。
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id"), nullable=True, index=True
    )
    slot_date: Mapped[str] = mapped_column(String(10), index=True)
    slot_time: Mapped[str] = mapped_column(String(16), default="")
    capacity: Mapped[int] = mapped_column(Integer, default=1)
    booked: Mapped[int] = mapped_column(Integer, default=0)


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (
        UniqueConstraint("slot_id", "patient_id", name="uq_appointment_slot_patient"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slot_id: Mapped[int] = mapped_column(ForeignKey("appointment_slots.id"), index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    # booked=已预约, cancelled=已取消, fulfilled=已就诊
    status: Mapped[str] = mapped_column(String(16), default="booked")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SterilizationBatch(Base):
    """消毒供应中心：复用器械批次的清洗消毒灭菌、发放、回收全流程追溯。"""

    __tablename__ = "sterilization_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    center_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    item_name: Mapped[str] = mapped_column(String(128))
    quantity: Mapped[int] = mapped_column(Integer)
    # sterilizing=灭菌中, sterile=已灭菌, dispatched=已发放, recycled=已回收
    status: Mapped[str] = mapped_column(String(16), default="sterilizing", index=True)
    dispatched_to_org_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class MedicalWaste(Base):
    """医疗废弃物：收集、暂存、交接全过程实时监管与追溯。"""

    __tablename__ = "medical_wastes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # infectious=感染性, sharp=损伤性, pathological=病理性, pharmaceutical=药物性, chemical=化学性
    waste_type: Mapped[str] = mapped_column(String(16))
    weight_kg: Mapped[float] = mapped_column(Float)
    # collected=已收集, stored=已暂存, handed_over=已交接
    status: Mapped[str] = mapped_column(String(16), default="collected", index=True)
    handler_name: Mapped[str] = mapped_column(String(64), default="")
    collected_date: Mapped[str] = mapped_column(String(10), index=True)
    handed_over_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 追溯码：指引明文点名的技术手段。一包医废一码，扫码即知从哪来、
    # 现在在哪、谁交接的。全局唯一，由平台生成而非录入——让人填码，
    # 迟早会有两包医废共用一个码。
    trace_code: Mapped[str] = mapped_column(String(32), default="", unique=True, index=True)
    # 产生点位与暂存点位。产生点必填，暂存点在入暂存间时补
    source_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("waste_locations.id"), nullable=True, index=True
    )
    storage_location_id: Mapped[int | None] = mapped_column(
        ForeignKey("waste_locations.id"), nullable=True, index=True
    )
    # 转运人员挂员工档案而不是自由文本：转运人员管理是指引的子功能，
    # 靠 prompt 让人手打名字，既统计不出人次也追不了责。
    handler_employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id"), nullable=True, index=True
    )
    stored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------- 功能指引补齐：7/9/13-16/19-21/23-28/30-32/34 ----------


class EmergencyCase(Base):
    """⑦智慧急救：呼救→出车→到达→入院，院前院内信息互通。"""

    __tablename__ = "emergency_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), nullable=True)
    caller_phone: Mapped[str] = mapped_column(String(20), default="")
    location: Mapped[str] = mapped_column(String(256))
    symptom: Mapped[str] = mapped_column(String(512), default="")
    ambulance_no: Mapped[str] = mapped_column(String(32), default="")
    dest_org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    # 急救绿色通道类型：""=普通, chest_pain=胸痛, stroke=卒中, trauma=创伤
    channel_type: Mapped[str] = mapped_column(String(16), default="", index=True)
    # dispatched=已调度, en_route=转运中, arrived=已到院, admitted=已收治
    status: Mapped[str] = mapped_column(String(16), default="dispatched", index=True)
    # 抢救转归：""=未判定（非抢救病例或尚未结论）, success=抢救成功, failed=抢救无效。
    # 空串与 failed 必须分开——把"没填"当成"没救过来"，抢救成功率会被算低。
    rescue_outcome: Mapped[str] = mapped_column(String(16), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    vitals: Mapped[list["EmergencyVital"]] = relationship(back_populates="case")
    milestones: Mapped[list["EmergencyMilestone"]] = relationship(back_populates="case")


class EmergencyMilestone(Base):
    """急救绿道时间节点：发病→呼救→出车→到达现场→到院→开始救治，绿道时效分析依据。"""

    __tablename__ = "emergency_milestones"
    __table_args__ = (
        UniqueConstraint("case_id", "milestone", name="uq_emergency_milestone_case"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("emergency_cases.id"), index=True)
    # onset=发病, call=呼救, depart=出车, arrive_scene=到达现场,
    # arrive_hospital=到达医院, treatment=开始救治
    milestone: Mapped[str] = mapped_column(String(16))
    # 发生时刻（字符串，如 "2026-08-10 14:32"）
    occurred_at: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    case: Mapped["EmergencyCase"] = relationship(back_populates="milestones")


class EmergencyVital(Base):
    __tablename__ = "emergency_vitals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("emergency_cases.id"), index=True)
    heart_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    sbp: Mapped[float | None] = mapped_column(Float, nullable=True)
    dbp: Mapped[float | None] = mapped_column(Float, nullable=True)
    spo2: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    case: Mapped[EmergencyCase] = relationship(back_populates="vitals")


class OnlineConsult(Base):
    """⑨互联网+诊疗：在线咨询/复诊续方。"""

    __tablename__ = "online_consults"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    # consult=在线咨询, repeat_rx=复诊续方
    consult_type: Mapped[str] = mapped_column(String(16), default="consult")
    question: Mapped[str] = mapped_column(String(1024))
    reply: Mapped[str] = mapped_column(String(2048), default="")
    doctor_name: Mapped[str] = mapped_column(String(64), default="")
    # open=待回复, replied=已回复, closed=已结束
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    prescription_id: Mapped[int | None] = mapped_column(ForeignKey("prescriptions.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TcmDispenseOrder(Base):
    """⑭中药智能药学（共享中药房）：调配→煎煮→配送全程追溯。"""

    __tablename__ = "tcm_dispense_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    from_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    herbs: Mapped[str] = mapped_column(String(1024))
    doses: Mapped[int] = mapped_column(Integer, default=1)
    decoct: Mapped[bool] = mapped_column(Boolean, default=True)
    # ordered=已下单, dispensed=已调配, decocted=已煎煮, delivering=配送中, delivered=已送达
    status: Mapped[str] = mapped_column(String(16), default="ordered", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class TcmTechnique(Base):
    """㉑中医药适宜技术库。"""

    __tablename__ = "tcm_techniques"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    category: Mapped[str] = mapped_column(String(64), default="")
    indication: Mapped[str] = mapped_column(String(512), default="")
    description: Mapped[str] = mapped_column(String(1024), default="")


class DrugShortage(Base):
    """⑮基层缺药登记：登记→采购→配送→取药。

    `patient_id` 可空：既有按机构报缺（补库存），也有按患者登记（延伸处方、
    个性化治疗需求），两者共用一张表但含义不同——按患者登记的才谈得上
    "登记后不来取药"，也才进得了黑名单。

    末态分 collected 与 no_show 两个：都"结束了"，但一个是药送到人拿走了，
    一个是药白调了。混成一个 closed，缺药登记的履约率就永远算不出来。
    """

    __tablename__ = "drug_shortages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    patient_id: Mapped[int | None] = mapped_column(
        ForeignKey("patients.id"), nullable=True, index=True
    )
    drug_code: Mapped[str] = mapped_column(String(64))
    drug_name: Mapped[str] = mapped_column(String(128))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    # registered=已登记, purchasing=采购中, delivered=已配送,
    # collected=已取药, no_show=未取药, cancelled=已取消
    status: Mapped[str] = mapped_column(String(16), default="registered", index=True)
    close_reason: Mapped[str] = mapped_column(String(256), default="")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PathologySpecimen(Base):
    """④病理标本核收与标本管理。

    第七轮子功能级重审发现：`advance_sample` 明确 `center_type != "lab"` 即 422，
    病理标本整段没有状态机——而指引第 4 条头两项就是"病理标本核收""标本管理"。

    **不与检验样本共用一套流转**：检验是采样→冷链转运→中心核收，病理是
    核收→固定→取材→制片→阅片，节点数与含义都不同。硬套一套只会得到一个
    两头不像的状态机（与专病不复用慢病同一条理由）。

    **拒收是核心业务而非异常分支**：标本量不足、未加固定液、标识不清都要
    当场拒收并说明理由，否则后面做出来的片子是废的。
    """

    __tablename__ = "pathology_specimens"
    __table_args__ = (
        UniqueConstraint("specimen_no", name="uq_pathology_specimen_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("exam_requests.id"), index=True)
    # 标本号由平台生成（同追溯码的理由：唯一性是全部价值所在）
    specimen_no: Mapped[str] = mapped_column(String(32), index=True)
    site: Mapped[str] = mapped_column(String(128), default="")
    # 离体时间与固定时间：冷缺血时间是病理质控的核心指标
    excised_at: Mapped[str] = mapped_column(String(19), default="")
    fixed_at: Mapped[str] = mapped_column(String(19), default="")
    fixative: Mapped[str] = mapped_column(String(64), default="")
    # pending=待核收, received=已核收, embedded=已取材制片, slided=已制片, read=已阅片,
    # rejected=已拒收
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    reject_reason: Mapped[str] = mapped_column(String(256), default="")
    received_by: Mapped[str] = mapped_column(String(64), default="")
    # 承接病理室机构：核收时置为核收方机构。标本发现队列跨机构（不按此列过滤），
    # 但核收后的拒收/推进须由承接病理室本机构完成，堵住他院推进/作废别家标本。
    lab_org_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id"), index=True, nullable=True
    )
    block_count: Mapped[int] = mapped_column(Integer, default=0)
    slide_count: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


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


class InsuranceSettlement(Base):
    """⑲医保业务协同：结算记录（本地/异地）。"""

    __tablename__ = "insurance_settlements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # local=本地结算, remote=异地结算
    settle_type: Mapped[str] = mapped_column(String(16), default="local")
    total_amount: Mapped[float] = mapped_column(Money)
    insurance_pay: Mapped[float] = mapped_column(Money)
    self_pay: Mapped[float] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SpecialDiseaseApp(Base):
    """⑲特殊病种门诊治疗待遇申报。"""

    __tablename__ = "special_disease_apps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    disease_name: Mapped[str] = mapped_column(String(128))
    # applied=已申报, approved=已批准, rejected=已驳回
    status: Mapped[str] = mapped_column(String(16), default="applied")
    reason: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReferralCert(Base):
    """⑲转诊证明：基于已接诊/结案的转诊记录签发。"""

    __tablename__ = "referral_certs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    referral_id: Mapped[int] = mapped_column(ForeignKey("referrals.id"), unique=True)
    cert_no: Mapped[str] = mapped_column(String(32), unique=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Course(Base):
    """⑳远程医学教育课程（含㉑适宜技术培训）。"""

    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(256))
    # live=直播, vod=点播
    course_type: Mapped[str] = mapped_column(String(8), default="vod")
    # clinical=临床医学, tcm=中医药适宜技术, public_health=公共卫生
    category: Mapped[str] = mapped_column(String(16), default="clinical")
    speaker: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TrainingRecord(Base):
    __tablename__ = "training_records"
    __table_args__ = (UniqueConstraint("course_id", "user_id", name="uq_training_course_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    score: Mapped[float] = mapped_column(Float, default=0)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ElderlyAssessment(Base):
    """㉓老年健康：自理能力评估、认知筛查、中医体质辨识。"""

    __tablename__ = "elderly_assessments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    # 评估机构（第九轮补，理由同 visit_credentials）
    org_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id"), nullable=True, index=True
    )
    adl_score: Mapped[int] = mapped_column(Integer)
    cognitive_score: Mapped[int] = mapped_column(Integer, default=0)
    tcm_constitution: Mapped[str] = mapped_column(String(32), default="")
    # 依 ADL 自动分级：能力完好/轻度失能/中度失能/重度失能
    care_level: Mapped[str] = mapped_column(String(16), default="")
    assessed_date: Mapped[str] = mapped_column(String(10), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MaternalRecord(Base):
    """㉔妇幼保健：孕产妇建册与高危管理。"""

    __tablename__ = "maternal_records"
    __table_args__ = (UniqueConstraint("patient_id", name="uq_maternal_patient"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    # 建册管理机构：横向隔离根治（此前妇幼表无 org_id，只能靠患者维度兜底，
    # 而建册本身不构成服务关系，兜底对纯妇幼档案失效）。可空以容纳历史数据。
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=True)
    lmp: Mapped[str] = mapped_column(String(10), default="")
    edc: Mapped[str] = mapped_column(String(10), default="")
    gravidity: Mapped[int] = mapped_column(Integer, default=1)
    parity: Mapped[int] = mapped_column(Integer, default=0)
    high_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_factors: Mapped[str] = mapped_column(String(512), default="")
    # registered=已建册, delivered=已分娩, closed=产后访视结案
    status: Mapped[str] = mapped_column(String(16), default="registered", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    visits: Mapped[list["MaternalVisit"]] = relationship(back_populates="record")


class MaternalVisit(Base):
    __tablename__ = "maternal_visits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("maternal_records.id"), index=True)
    # prenatal=产前检查, postpartum=产后访视
    visit_type: Mapped[str] = mapped_column(String(16))
    gest_week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bp: Mapped[str] = mapped_column(String(16), default="")
    note: Mapped[str] = mapped_column(String(512), default="")
    visit_date: Mapped[str] = mapped_column(String(10), default="")

    record: Mapped[MaternalRecord] = relationship(back_populates="visits")


class ChildRecord(Base):
    """㉔儿童保健档案。"""

    __tablename__ = "child_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    gender: Mapped[str] = mapped_column(String(8), default="未知")
    birth_date: Mapped[str] = mapped_column(String(10))
    guardian_patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), nullable=True)
    # 建档管理机构（横向隔离，见 MaternalRecord.org_id 同理）
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    # 终审轮：高危儿管理（新筛异常自动标记，可人工标记/解除）
    high_risk: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    risk_note: Mapped[str] = mapped_column(String(256), default="")

    visits: Mapped[list["ChildVisit"]] = relationship(back_populates="child")


class ChildVisit(Base):
    __tablename__ = "child_visits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(ForeignKey("child_records.id"), index=True)
    # newborn=新生儿访视, checkup=健康体检
    visit_type: Mapped[str] = mapped_column(String(16))
    height_cm: Mapped[float | None] = mapped_column(Float, nullable=True)
    weight_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str] = mapped_column(String(512), default="")
    visit_date: Mapped[str] = mapped_column(String(10), default="")

    child: Mapped[ChildRecord] = relationship(back_populates="visits")


class VaccinationRecord(Base):
    """㉕疫苗接种记录。

    批号可空：既有存量记录没有批号，强制必填会让老数据无法迁移。
    但新接种一律建议带批号——出了问题按批号召回时，没批号的记录查不出来。
    """

    __tablename__ = "vaccination_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    vaccine_code: Mapped[str] = mapped_column(String(64))
    vaccine_name: Mapped[str] = mapped_column(String(128))
    dose_no: Mapped[int] = mapped_column(Integer, default=1)
    vaccinated_date: Mapped[str] = mapped_column(String(10), default="")
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("vaccine_batches.id"), nullable=True, index=True
    )
    # 冗余存一份批号：批次记录理论上不删，但接种史是要长期保存的，
    # 不该因为批次表的任何变动而查不出当年打的是哪一批。
    batch_no: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 接种部位与途径（AEFI 归因时要用）
    site: Mapped[str] = mapped_column(String(32), default="")
    vaccinator: Mapped[str] = mapped_column(String(64), default="")


class VaccineContraindication(Base):
    """接种禁忌。

    D-4：绝大多数接种禁忌是**暂时性**的（发热、急性病期、近期用过免疫球蛋白），
    原先这张表没有状态也没有有效期，登记后永久硬拦截且无解除接口——退了热也
    再打不了这支疫苗，只能改数据库。现补 `status` / `valid_until` / 解除留痕。

    **解除留痕不删行**：禁忌记录本身是接种史的一部分，"当时为什么没打"
    与"后来为什么又打了"都要留得住。
    """

    __tablename__ = "vaccine_contraindications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    vaccine_code: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(256))
    # permanent=长期禁忌（过敏史等）, temporary=暂时禁忌（发热等）
    contra_type: Mapped[str] = mapped_column(String(16), default="permanent", index=True)
    # active=生效中, lifted=已解除。过期（valid_until 已过）不改状态，
    # 由判定函数按日期算——定时任务改状态会让"何时失效"取决于任务跑没跑。
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    # 暂时禁忌的有效期末日（含当日）；空表示无期限，须人工解除
    valid_until: Mapped[str] = mapped_column(String(10), default="")
    lifted_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    lifted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    lift_reason: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PublicHealthEvent(Base):
    """㉖突发公共卫生事件应急处置指挥。"""

    __tablename__ = "ph_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(256))
    # I/II/III/IV 级响应
    level: Mapped[str] = mapped_column(String(4), default="IV")
    disease_name: Mapped[str] = mapped_column(String(128), default="")
    description: Mapped[str] = mapped_column(String(1024), default="")
    # active=处置中, closed=已结案
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    actions: Mapped[list["PhEventAction"]] = relationship(back_populates="event")


class PhEventAction(Base):
    __tablename__ = "ph_event_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("ph_events.id"), index=True)
    action: Mapped[str] = mapped_column(String(512))
    actor: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    event: Mapped[PublicHealthEvent] = relationship(back_populates="actions")


class HealthMonitorRecord(Base):
    """㉘其他卫生业务协同：营养/环境/职业/放射/学校卫生监测。"""

    __tablename__ = "health_monitor_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # nutrition/environment/occupational/radiation/school
    domain: Mapped[str] = mapped_column(String(16), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    indicator: Mapped[str] = mapped_column(String(128))
    value: Mapped[float] = mapped_column(Float)
    threshold: Mapped[float] = mapped_column(Float)
    exceeded: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    record_date: Mapped[str] = mapped_column(String(10), default="")


class Employee(Base):
    """㉚人力资源统一协同管理。"""

    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    # 职称：初级/中级/副高/正高
    title: Mapped[str] = mapped_column(String(32), default="")
    # 职称等级：国家监测指标要求区分"中级及以上"，而 title 是自由文本
    #（"主治医师"/"主管护师"/"副主任医师"各院写法不一）。**不从文本推断等级**——
    # 靠关键词猜，一个"助理全科医生"就能把统计带偏。由录入方明确选。
    # none=未填, junior=初级, intermediate=中级, deputy_senior=副高, senior=正高
    title_level: Mapped[str] = mapped_column(String(16), default="none", index=True)
    position: Mapped[str] = mapped_column(String(64), default="")
    # active=在岗, seconded=派驻中, left=离职
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    # 终审轮：科室挂接（浙#9）
    dept_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Secondment(Base):
    """人员派驻/下沉台账（支撑国家监测指标"派驻 6 个月以上人数"）。

    原先只有起止日期，答得了"现在有几个人在派"，答不了"派满 6 个月的有几个"，
    也分不清长期派驻与临时支援——而监测指标问的恰恰是前者。
    """

    __tablename__ = "secondments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    from_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    to_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    start_date: Mapped[str] = mapped_column(String(10))
    # 空串表示仍在派。不额外加 status 字段——两处表达同一件事，早晚会打架。
    end_date: Mapped[str] = mapped_column(String(10), default="")
    # long_term=长期派驻, support=短期支援, rounds=巡诊, other=其他。
    # 监测指标只认长期派驻，混在一起统计会把巡诊也算成下沉。
    assignment_type: Mapped[str] = mapped_column(String(16), default="long_term", index=True)
    position: Mapped[str] = mapped_column(String(64), default="")
    note: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FinanceEntry(Base):
    """㉛财务统一协同管理：独立建账、集中核算。"""

    __tablename__ = "finance_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    # income=收入, expense=支出
    category: Mapped[str] = mapped_column(String(8))
    item: Mapped[str] = mapped_column(String(128), default="")
    amount: Mapped[float] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Asset(Base):
    """㉜物资统一协同管理（非医疗设备、办公用品）。"""

    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    # equipment=非医疗设备, office=办公用品
    category: Mapped[str] = mapped_column(String(16), default="office")
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    # in_use=在用, idle=闲置, scrapped=报废
    status: Mapped[str] = mapped_column(String(16), default="in_use")
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


class DutyRoster(Base):
    """①-④共享中心排班管理。"""

    __tablename__ = "duty_rosters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    center_type: Mapped[str] = mapped_column(String(16), index=True)
    duty_date: Mapped[str] = mapped_column(String(10), index=True)
    shift: Mapped[str] = mapped_column(String(16), default="全天")
    doctor_name: Mapped[str] = mapped_column(String(64))


class QcRecord(Base):
    """①-④共享中心质控记录。"""

    __tablename__ = "qc_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    center_type: Mapped[str] = mapped_column(String(16), index=True)
    item: Mapped[str] = mapped_column(String(128))
    # pass=合格, fail=不合格
    result: Mapped[str] = mapped_column(String(8))
    note: Mapped[str] = mapped_column(String(512), default="")
    record_date: Mapped[str] = mapped_column(String(10), default="")


class ReportTemplate(Base):
    """①-④共享中心报告模板管理。"""

    __tablename__ = "report_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    center_type: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(String(2048), default="")


class CssdRequest(Base):
    """⑥消毒供应物品申领：基层申领→中心关联批次发放。"""

    __tablename__ = "cssd_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    item_name: Mapped[str] = mapped_column(String(128))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    # requested=已申领, fulfilled=已发放
    status: Mapped[str] = mapped_column(String(16), default="requested", index=True)
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("sterilization_batches.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ConsultExpert(Base):
    """⑤远程会诊专家管理。"""

    __tablename__ = "consult_experts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    specialty: Mapped[str] = mapped_column(String(64), default="")
    available: Mapped[bool] = mapped_column(Boolean, default=True)


class ServiceBlacklist(Base):
    """通用服务黑名单（⑫预约、⑮缺药登记）。

    指引第 12 与第 15 条各要一个黑名单。原先只有预约那一个，补第二个时
    有两条路：再建一张几乎一样的表，或者给现有的加一个业务域。选后者——
    第三个域（比如上门护理反复放空）迟早会来，届时又要第三张表。

    唯一约束是 (domain, patient_id)：同一人可以同时在预约黑名单和缺药
    黑名单里，两者互不影响。
    """

    __tablename__ = "service_blacklists"
    __table_args__ = (
        UniqueConstraint("domain", "patient_id", name="uq_service_blacklist"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # appointment=预约爽约, shortage=缺药登记后不取药
    domain: Mapped[str] = mapped_column(String(16), default="appointment", index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    reason: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class HealthArticle(Base):
    """⑨⑩健康宣教内容管理。"""

    __tablename__ = "health_articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(256))
    category: Mapped[str] = mapped_column(String(32), default="general")
    content: Mapped[str] = mapped_column(String(4096), default="")
    # draft=草稿, published=已发布（居民端可见）
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SatisfactionSurvey(Base):
    """⑪㉟满意度调查（签约服务/就诊等通用评价）。"""

    __tablename__ = "satisfaction_surveys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # contract=家医签约, encounter=就诊, consultation=会诊
    target_type: Mapped[str] = mapped_column(String(16), index=True)
    target_id: Mapped[int] = mapped_column(Integer)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    score: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------- 浙江省指南 M7：住院与床位（ADT/住院医嘱/病案首页） ----------


class Ward(Base):
    """病区：床位资源库的组织单元（挂接机构）。"""

    __tablename__ = "wards"
    __table_args__ = (UniqueConstraint("org_id", "name", name="uq_ward_org_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    beds: Mapped[list["Bed"]] = relationship(back_populates="ward")


class Bed(Base):
    """床位资源：占用状态由入出转流程原子维护。"""

    __tablename__ = "beds"
    __table_args__ = (UniqueConstraint("ward_id", "bed_no", name="uq_bed_ward_no"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ward_id: Mapped[int] = mapped_column(ForeignKey("wards.id"), index=True)
    bed_no: Mapped[str] = mapped_column(String(16))
    # free=空闲, occupied=占用
    status: Mapped[str] = mapped_column(String(16), default="free", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    ward: Mapped[Ward] = relationship(back_populates="beds")


class Admission(Base):
    """住院登记（ADT）：入院→转科/转床→出院，床位占用原子分配。"""

    __tablename__ = "admissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    ward_id: Mapped[int] = mapped_column(ForeignKey("wards.id"))
    bed_id: Mapped[int] = mapped_column(ForeignKey("beds.id"))
    doctor_name: Mapped[str] = mapped_column(String(64), default="")
    diagnosis_name: Mapped[str] = mapped_column(String(256), default="")
    # admitted=在院, discharged=已出院
    status: Mapped[str] = mapped_column(String(16), default="admitted", index=True)
    admitted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    discharged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))

    orders: Mapped[list["InpatientOrder"]] = relationship(back_populates="admission")
    case_summary: Mapped["CaseSummary | None"] = relationship(back_populates="admission")


class InpatientOrder(Base):
    """住院医嘱：长期/临时，开立/停止（限医师）。"""

    __tablename__ = "inpatient_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admission_id: Mapped[int] = mapped_column(ForeignKey("admissions.id"), index=True)
    # long=长期医嘱, temp=临时医嘱
    order_type: Mapped[str] = mapped_column(String(8))
    content: Mapped[str] = mapped_column(String(512))
    # active=执行中, stopped=已停止
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_by_name: Mapped[str] = mapped_column(String(64), default="")
    stopped_by_name: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    admission: Mapped[Admission] = relationship(back_populates="orders")


class CaseSummary(Base):
    """病案首页（最小数据集）：出院诊断、手术、费用汇总、转归，出院时填写。"""

    __tablename__ = "case_summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admission_id: Mapped[int] = mapped_column(ForeignKey("admissions.id"), unique=True, index=True)
    discharge_diagnosis: Mapped[str] = mapped_column(String(256))
    operation: Mapped[str] = mapped_column(String(256), default="")
    total_cost: Mapped[float] = mapped_column(Money, default=0)
    drug_cost: Mapped[float] = mapped_column(Money, default=0)
    # 治愈/好转/未愈/死亡/其他
    outcome: Mapped[str] = mapped_column(String(16), default="好转")
    note: Mapped[str] = mapped_column(String(1024), default="")
    # M12 DRGs：结案时按主诊断关键词自动入组（未入组为空串/0）
    drg_code: Mapped[str] = mapped_column(String(16), default="", index=True)
    drg_weight: Mapped[float] = mapped_column(Float, default=0)
    created_by_name: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    admission: Mapped[Admission] = relationship(back_populates="case_summary")


# ---------- 浙江省指南 M8：费用结算（计费引擎最小集） ----------


class ChargeItem(Base):
    """收费项目目录：价格管理与公示，编码关联四统一 charge 字典。"""

    __tablename__ = "charge_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    # drug=药品, exam=检查检验, treatment=治疗处置, bed=床位, other=其他
    category: Mapped[str] = mapped_column(String(16), default="other")
    price: Mapped[float] = mapped_column(Money)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BillDetail(Base):
    """费用明细：门诊按就诊（encounter）、住院按住院登记（admission）累计。"""

    __tablename__ = "bill_details"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    admission_id: Mapped[int | None] = mapped_column(
        ForeignKey("admissions.id"), nullable=True, index=True
    )
    encounter_id: Mapped[int | None] = mapped_column(
        ForeignKey("encounters.id"), nullable=True, index=True
    )
    item_code: Mapped[str] = mapped_column(String(64), index=True)
    item_name: Mapped[str] = mapped_column(String(128))
    # 计费时价格快照（此后目录调价不影响已计费明细）
    unit_price: Mapped[float] = mapped_column(Money)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    amount: Mapped[float] = mapped_column(Money)
    # 结算后回填：未结清明细 settlement_id 为空
    settlement_id: Mapped[int | None] = mapped_column(
        ForeignKey("settlements.id"), nullable=True, index=True
    )
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Settlement(Base):
    """结算单：汇总未结清明细→医保分担（联动 InsuranceSettlement）→结清。"""

    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # outpatient=门诊结算, inpatient=住院（出院）结算
    bill_type: Mapped[str] = mapped_column(String(16))
    admission_id: Mapped[int | None] = mapped_column(
        ForeignKey("admissions.id"), nullable=True, index=True
    )
    encounter_id: Mapped[int | None] = mapped_column(
        ForeignKey("encounters.id"), nullable=True
    )
    total_amount: Mapped[float] = mapped_column(Money)
    insurance_pay: Mapped[float] = mapped_column(Money, default=0)
    self_pay: Mapped[float] = mapped_column(Money, default=0)
    # 关联医保结算记录（复用 insurance 域，insurance_pay>0 时生成）
    insurance_settlement_id: Mapped[int | None] = mapped_column(
        ForeignKey("insurance_settlements.id"), nullable=True
    )
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------- 浙江省指南 M9：质量安全（病人安全/病历质控/院感） ----------


class AdverseEvent(Base):
    """不良事件上报：上报（可匿名）→ 审核 → 整改，全程留痕。"""

    __tablename__ = "adverse_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # medication=用药, device=器械, fall=跌倒, pressure_sore=压疮,
    # transfusion=输血, identification=查对, other=其他
    event_type: Mapped[str] = mapped_column(String(16), index=True)
    # I=警告(死亡/严重伤害), II=不良后果, III=未造成后果, IV=隐患事件
    level: Mapped[str] = mapped_column(String(4))
    anonymous: Mapped[bool] = mapped_column(Boolean, default=False)
    reporter_name: Mapped[str] = mapped_column(String(64), default="")
    description: Mapped[str] = mapped_column(String(2048))
    # reported=已上报, reviewed=已审核, rectified=已整改
    status: Mapped[str] = mapped_column(String(16), default="reported", index=True)
    review_note: Mapped[str] = mapped_column(String(1024), default="")
    reviewed_by: Mapped[str] = mapped_column(String(64), default="")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rectify_note: Mapped[str] = mapped_column(String(1024), default="")
    rectified_by: Mapped[str] = mapped_column(String(64), default="")
    rectified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RecordQc(Base):
    """病历质控：对就诊记录/病案首页抽检评分，缺陷项记录，自动定级。"""

    __tablename__ = "record_qcs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # encounter=门急诊病历, case_summary=病案首页
    target_type: Mapped[str] = mapped_column(String(16), index=True)
    target_id: Mapped[int] = mapped_column(Integer, index=True)
    score: Mapped[int] = mapped_column(Integer)
    # 甲/乙/丙（≥90 甲、≥80 乙、其余丙）
    grade: Mapped[str] = mapped_column(String(4), default="")
    # 缺陷项描述（分号分隔）
    defects: Mapped[str] = mapped_column(String(1024), default="")
    qc_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class InfectionReport(Base):
    """院感上报：医院感染病例登记与核实（#70 院感提醒数据源）。"""

    __tablename__ = "infection_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    # respiratory=呼吸道, surgical_site=手术部位, urinary=泌尿道,
    # bloodstream=血流, gastrointestinal=消化道, other=其他
    infection_site: Mapped[str] = mapped_column(String(16), index=True)
    pathogen: Mapped[str] = mapped_column(String(128), default="")
    note: Mapped[str] = mapped_column(String(1024), default="")
    # reported=已上报, confirmed=已确认院感, excluded=已排除
    status: Mapped[str] = mapped_column(String(16), default="reported", index=True)
    reported_by: Mapped[str] = mapped_column(String(64), default="")
    report_date: Mapped[str] = mapped_column(String(10), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------- 浙江省指南 M12：DRGs 简化版 ----------


class DrgGroup(Base):
    """DRG 分组目录：编码/名称/MDC/基准权重/主诊断关键词/主手术关键词。"""

    __tablename__ = "drg_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    base_weight: Mapped[float] = mapped_column(Float)
    # 主诊断匹配关键词（逗号分隔），出院病例按关键词命中入组
    keywords: Mapped[str] = mapped_column(String(256), default="")
    # 块3：主要诊断大类（MDCB/MDCE/... ），供 DRG 统计按 MDC 汇总
    mdc: Mapped[str] = mapped_column(String(8), default="", index=True)
    mdc_name: Mapped[str] = mapped_column(String(64), default="")
    # 块3：主手术关键词（逗号分隔），与病案首页 operation 匹配
    procedure_keywords: Mapped[str] = mapped_column(String(256), default="")
    # 块3：True=外科操作组，未命中主手术不得入组
    require_procedure: Mapped[bool] = mapped_column(Boolean, default=False)
    # 块3：QY 兜底组标志，统计中单列且不计入 CMI
    is_fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


# ---------- 浙江省指南 M11：集成平台底座（交换监控） ----------


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


class Referral(Base):
    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    from_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    to_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    # up=上转, down=下转
    direction: Mapped[str] = mapped_column(String(8))
    reason: Mapped[str] = mapped_column(String(512), default="")
    # pending=待接诊, accepted=已接诊, completed=已结案, rejected=已退回
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


# ============================================================================
# 终审轮（对照两份指南逐子功能核对）补全模型
# ============================================================================


