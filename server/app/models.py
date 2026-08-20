"""核心数据模型：对应规划第一期"平台支撑层 + 数据中心层"基础实体。

- User            统一认证用户（医共体工作人员账号）
- Organization    医共体成员单位（牵头医院/乡镇卫生院/村卫生室等）
- Patient         患者主索引（EMPI，电子健康卡号为对外统一标识）
- CodeSystem/CodeEntry  统一编码字典（诊断、药品、耗材、收费"四统一"）
- Referral        双向转诊记录（上转/下转，状态流转）
"""
from datetime import datetime

from sqlalchemy import (
    JSON,
    Numeric,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .clock import now_naive
from .database import Base

#: 金额列类型（阶段十二）。
#:
#: 原先 43 个金额列用 `Float`：SQLite 存 REAL、PostgreSQL 存 double precision，
#: 库侧 SUM 会累积浮点误差——阶段七的 `test_fund.py` 里不得不写
#: `abs(distributed - 200000) < 1.0` 的容差断言才能通过。国产库（达梦/人大金仓）
#: 对浮点的处理与 SQLite 又不相同，信创适配时这批列是首当其冲的。
#:
#: 改为 `Numeric(14, 2)`：DDL 上是定点数，PostgreSQL 与国产库的 SUM/AVG 都是精确的，
#: 14 位精度对县域金额量级（百亿以下，精确到分）绰绰有余。
#:
#: **`asdecimal=False` 是一个明确的取舍**：Python 侧仍收发 float，不返回 Decimal。
#: 打开它会让全平台上百处 `Decimal * float` 的算式直接 TypeError，
#: 而那需要连同每一处金额计算一起改。所以这一步拿到的是**存储与库侧聚合的精确性**，
#: Python 侧的逐笔运算仍是浮点——端到端 Decimal 留作后续，理由与代价记在
#: `docs/信创适配与部署.md`，不含糊带过。
Money = Numeric(14, 2, asdecimal=False)


def utcnow() -> datetime:
    """所有 `created_at` 类列的默认值。

    P0-2：这里原先返回 aware（`datetime.now(timezone.utc)`），而调度器与居民端
    各自维护的 naive 版本写进的是同一批 `DateTime` 列——同一张表的两列可能一列
    aware 一列 naive。阶段二 `/api/monitor/overview` 拿 aware 值去比调度器写的
    naive `next_run_at`，直接 TypeError。现统一委托给 `clock.now_naive()`：
    **落库一律 naive UTC**。

    名字保留是因为它出现在近两百处列定义的 `default=` 里，改名的收益抵不上
    改动面；语义已经统一，新代码请直接用 `clock.now_naive()`。
    """
    return now_naive()


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


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
    # basic=基础包, standard=标准包, premium=个性包
    package: Mapped[str] = mapped_column(String(16), default="basic")
    signed_date: Mapped[str] = mapped_column(String(10), default="")
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    record: Mapped[MaternalRecord] = relationship(back_populates="visits")


class ChildRecord(Base):
    """㉔儿童保健档案。"""

    __tablename__ = "child_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    gender: Mapped[str] = mapped_column(String(8), default="未知")
    birth_date: Mapped[str] = mapped_column(String(10))
    guardian_patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), nullable=True)
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


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


class DeliveryRecord(Base):
    """㉔分娩服务：住院分娩记录（联动孕产妇档案状态流转）。"""

    __tablename__ = "delivery_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("maternal_records.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    delivery_date: Mapped[str] = mapped_column(String(10))
    # natural=自然分娩, cesarean=剖宫产
    delivery_mode: Mapped[str] = mapped_column(String(16), default="natural")
    newborn_count: Mapped[int] = mapped_column(Integer, default=1)
    outcome: Mapped[str] = mapped_column(String(256), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class NewbornScreening(Base):
    """㉔新生儿疾病筛查：遗传代谢病/听力/先心病，异常自动纳入高危儿管理。"""

    __tablename__ = "newborn_screenings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    child_id: Mapped[int] = mapped_column(ForeignKey("child_records.id"), index=True)
    # metabolic=遗传代谢病, hearing=听力, chd=先天性心脏病
    item: Mapped[str] = mapped_column(String(16))
    # normal=未见异常, abnormal=阳性/可疑
    result: Mapped[str] = mapped_column(String(16), default="normal")
    screen_date: Mapped[str] = mapped_column(String(10), default="")
    note: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WomenHealthRecord(Base):
    """㉔婚前保健/孕前保健/妇女保健/避孕节育服务记录。"""

    __tablename__ = "women_health_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    # premarital=婚前保健, preconception=孕前保健, gynecology=妇女保健, contraception=避孕节育
    record_type: Mapped[str] = mapped_column(String(16), index=True)
    # 服务机构（第九轮补，理由同 visit_credentials）
    org_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id"), nullable=True, index=True
    )
    exam_date: Mapped[str] = mapped_column(String(10), default="")
    result: Mapped[str] = mapped_column(String(512), default="")
    advice: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MedicalCert(Base):
    """法定医学证明（浙#7、㉔）：出生医学证明签发、死亡医学证明、出生缺陷儿登记。"""

    __tablename__ = "medical_certs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # birth=出生医学证明, death=死亡医学证明, defect=出生缺陷儿登记
    cert_type: Mapped[str] = mapped_column(String(8), index=True)
    cert_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    gender: Mapped[str] = mapped_column(String(8), default="未知")
    event_date: Mapped[str] = mapped_column(String(10))
    # 死因诊断 / 缺陷诊断 / 出生备注
    detail: Mapped[str] = mapped_column(String(512), default="")
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), nullable=True)
    child_id: Mapped[int | None] = mapped_column(ForeignKey("child_records.id"), nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PhysicalExam(Base):
    """成人一般常规健康体检记录（浙#5）：体检套餐、结论与异常项标记。"""

    __tablename__ = "physical_exams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    package_name: Mapped[str] = mapped_column(String(128), default="常规体检")
    exam_date: Mapped[str] = mapped_column(String(10), default="")
    summary: Mapped[str] = mapped_column(String(1024), default="")
    # 分号分隔的异常项列表，非空自动置 has_abnormal
    abnormal_items: Mapped[str] = mapped_column(String(512), default="")
    has_abnormal: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PrescriptionComment(Base):
    """⑱处方点评：药师对已审处方的事后点评（合理/不合理）与问题留痕。"""

    __tablename__ = "prescription_comments"
    __table_args__ = (UniqueConstraint("prescription_id", name="uq_rx_comment_prescription"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    prescription_id: Mapped[int] = mapped_column(ForeignKey("prescriptions.id"), index=True)
    # reasonable=合理, unreasonable=不合理
    grade: Mapped[str] = mapped_column(String(16), index=True)
    # 问题类型：适应证不适宜/用法用量不适宜/重复用药/相互作用等（分号分隔）
    issues: Mapped[str] = mapped_column(String(256), default="")
    comment: Mapped[str] = mapped_column(String(1024), default="")
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Supplier(Base):
    """㉜㉝供应商管理（药品耗材/非医疗物资共用）。"""

    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    contact: Mapped[str] = mapped_column(String(64), default="")
    license_no: Mapped[str] = mapped_column(String(64), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PurchaseOrder(Base):
    """㉝采购管理：采购申请→审批→到货验收（药品验收自动入库）。"""

    __tablename__ = "purchase_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    supplier_id: Mapped[int] = mapped_column(ForeignKey("suppliers.id"))
    # drug=药品, material=耗材/物资
    item_type: Mapped[str] = mapped_column(String(16), default="drug")
    item_code: Mapped[str] = mapped_column(String(64))
    item_name: Mapped[str] = mapped_column(String(128))
    quantity: Mapped[int] = mapped_column(Integer)
    # pending=待审批, approved=已审批, received=已验收入库, rejected=已驳回
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    note: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class StockTake(Base):
    """㉝存货盘点：账面数/实盘数差异留痕并调整库存。"""

    __tablename__ = "stock_takes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    drug_code: Mapped[str] = mapped_column(String(64))
    book_qty: Mapped[int] = mapped_column(Integer)
    actual_qty: Mapped[int] = mapped_column(Integer)
    diff: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(256), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class KnowledgeEntry(Base):
    """统一知识库：药物政策/临床指南/转诊知识/质管制度规范/中医养生（含有效期管理）。"""

    __tablename__ = "knowledge_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # drug_policy=药物政策, clinical_guideline=临床指南, referral=转诊知识,
    # regulation=质量制度规范, tcm_health=中医养生
    category: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(256), index=True)
    body: Mapped[str] = mapped_column(String(4096), default="")
    # 有效期（质管资料）：过期条目查询默认过滤并标记
    expire_date: Mapped[str] = mapped_column(String(10), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ExamResource(Base):
    """浙#18 检查资源要素：设备/项目/价格/时长/注意事项档案。"""

    __tablename__ = "exam_resources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    center_type: Mapped[str] = mapped_column(String(16), index=True)
    item_name: Mapped[str] = mapped_column(String(128))
    device: Mapped[str] = mapped_column(String(128), default="")
    price: Mapped[float] = mapped_column(Money, default=0)
    duration_min: Mapped[int] = mapped_column(Integer, default=15)
    notes: Mapped[str] = mapped_column(String(512), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class DualChannelApp(Base):
    """⑲双通道药品申报：申报→管理层审核。"""

    __tablename__ = "dual_channel_apps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    drug_name: Mapped[str] = mapped_column(String(128))
    reason: Mapped[str] = mapped_column(String(512), default="")
    # pending=待审核, approved=通过, rejected=驳回
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    review_comment: Mapped[str] = mapped_column(String(512), default="")
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Department(Base):
    """浙#9 科室信息基础库：机构-科室唯一标识，人员可挂接。"""

    __tablename__ = "departments"
    __table_args__ = (UniqueConstraint("org_id", "code", name="uq_dept_org_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    code: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(64))
    # clinical=临床, medtech=医技, admin=行政后勤
    category: Mapped[str] = mapped_column(String(16), default="clinical")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class EmployeeChange(Base):
    """㉚人员变动管理：入职/转正/调动/离职留痕并联动员工状态。"""

    __tablename__ = "employee_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    # hire=入职, regularize=转正, transfer=调动, leave=离职
    change_type: Mapped[str] = mapped_column(String(16), index=True)
    to_org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    detail: Mapped[str] = mapped_column(String(256), default="")
    effective_date: Mapped[str] = mapped_column(String(10), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class StaffContract(Base):
    """㉚合同管理：劳动/聘用合同起止与到期提醒。"""

    __tablename__ = "staff_contracts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    contract_no: Mapped[str] = mapped_column(String(64), unique=True)
    start_date: Mapped[str] = mapped_column(String(10))
    end_date: Mapped[str] = mapped_column(String(10))
    # active=履行中, expired=已到期, terminated=已解除
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PayrollRecord(Base):
    """㉚薪酬福利管理 / ㉟绩效薪酬分配：月度薪酬 = 基础 + 绩效×系数。"""

    __tablename__ = "payroll_records"
    __table_args__ = (UniqueConstraint("employee_id", "period", name="uq_payroll_emp_period"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    base_salary: Mapped[float] = mapped_column(Money)
    # 绩效奖金也是金额，同样漏在阶段十二第一遍之外（同表的 base_salary/total
    # 都已是 Money，唯独它不是——按命名批量改最典型的漏法）
    perf_bonus: Mapped[float] = mapped_column(Money, default=0)
    # 绩效系数（考核结果联动薪酬分配）
    perf_coefficient: Mapped[float] = mapped_column(Float, default=1.0)
    total: Mapped[float] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Budget(Base):
    """㉛预算管理：年度收支预算编制与执行对比。"""

    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("org_id", "year", "category", name="uq_budget_org_year_cat"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    year: Mapped[str] = mapped_column(String(4), index=True)
    # income=收入预算, expense=支出预算
    category: Mapped[str] = mapped_column(String(8))
    amount: Mapped[float] = mapped_column(Money)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AssetMovement(Base):
    """㉜物资出入库管理：入库/领用/归还/报废联动数量与状态。"""

    __tablename__ = "asset_movements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"), index=True)
    # inbound=入库, issue=领用出库, return=归还, scrap=报废出库
    movement_type: Mapped[str] = mapped_column(String(16))
    quantity: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(256), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SystemParam(Base):
    """浙#45 系统参数配置：平台运行参数集中管理（管理员维护）。"""

    __tablename__ = "system_params"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    value: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(String(256), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class RoleChangeLog(Base):
    """浙#43 用户角色变更记录：变更前后角色与操作人留痕。"""

    __tablename__ = "role_change_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    old_role: Mapped[str] = mapped_column(String(16))
    new_role: Mapped[str] = mapped_column(String(16))
    changed_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class BloodStock(Base):
    """浙#71 血液管理：血型×成分库存台账。"""

    __tablename__ = "blood_stocks"
    __table_args__ = (UniqueConstraint("blood_type", "component", name="uq_blood_type_component"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # A/B/AB/O
    blood_type: Mapped[str] = mapped_column(String(4), index=True)
    # rbc=红细胞, plasma=血浆, platelet=血小板
    component: Mapped[str] = mapped_column(String(16))
    quantity_ml: Mapped[int] = mapped_column(Integer, default=0)


class TransfusionRequest(Base):
    """浙#71 临床用血申请：申请→审批→发血（血站对接为对接项）。"""

    __tablename__ = "transfusion_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    blood_type: Mapped[str] = mapped_column(String(4))
    component: Mapped[str] = mapped_column(String(16))
    quantity_ml: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(512), default="")
    # pending=待审批, approved=已审批, issued=已发血, rejected=已驳回
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
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


class LiveSession(Base):
    """⑳直播申请/审核/反馈流程（音视频通道为对接项）。"""

    __tablename__ = "live_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(256))
    speaker: Mapped[str] = mapped_column(String(64), default="")
    planned_at: Mapped[str] = mapped_column(String(16), default="")
    # pending=待审核, approved=已排期, rejected=已驳回, finished=已结束
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    review_comment: Mapped[str] = mapped_column(String(256), default="")
    # 课程录制（指引⑳）：直播结束后回放地址。空表示未录制或尚未上传。
    recording_url: Mapped[str] = mapped_column(String(512), default="")
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LiveFeedback(Base):
    """⑳直播反馈：评分与意见。

    一人一场只留一条，重复提交按覆盖——改主意是正常的，
    累计多条会让均分被反复提交的人带偏。
    """

    __tablename__ = "live_feedbacks"
    __table_args__ = (
        UniqueConstraint("session_id", "user_id", name="uq_live_feedback"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("live_sessions.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    rating: Mapped[int] = mapped_column(Integer, default=0)
    comment: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


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


class QcRule(Base):
    """数据质控规则（块3）：规则引擎按 rule_type + config 扫描存量数据并给出违规明细。

    - rule_type：required=必填项 / range=数值区间 / enum=取值枚举 /
      cross_ref=引用字典或目录 / logic=命名逻辑校验（身份证校验位、日期先后等）
    - target_table：被检表名（与模型 __tablename__ 对应，见 dataquality._TABLE_MODELS）
    - config：规则参数 JSON，结构见 app/data/qc_rules_seed.py 注释
    - severity：error=必须整改 / warn=提示
    """

    __tablename__ = "qc_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    target_table: Mapped[str] = mapped_column(String(64), index=True)
    rule_type: Mapped[str] = mapped_column(String(16), index=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    # error=错误（必须整改）, warn=警告（提示）
    severity: Mapped[str] = mapped_column(String(8), default="error", index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ============================================================================
# 块4：指引细目补齐（⑭中药制剂 / ⑥消毒成本 / ⑳课件资源 / ㉑实训 /
#       ㉔产前筛查 / ㉟绩效整改 / ⑨上门服务）
# ============================================================================


class TcmFormula(Base):
    """⑭中药制剂配方：院内制剂的处方组成、工艺与适应症（编码唯一）。"""

    __tablename__ = "tcm_formulas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    # 剂型：pill=丸剂, powder=散剂, paste=膏剂, granule=颗粒剂, decoction=合剂/汤剂
    dosage_form: Mapped[str] = mapped_column(String(16), default="decoction", index=True)
    composition: Mapped[str] = mapped_column(String(1024), default="")
    process: Mapped[str] = mapped_column(String(1024), default="")
    indication: Mapped[str] = mapped_column(String(512), default="")
    # 有效期（月），批次生产时据此推算效期
    shelf_life_months: Mapped[int] = mapped_column(Integer, default=12)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    batches: Mapped[list["TcmPreparationBatch"]] = relationship(back_populates="formula")


class TcmPreparationBatch(Base):
    """⑭中药制剂批次：批号、产量、生产与效期，过期批次禁止发放。"""

    __tablename__ = "tcm_preparation_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    formula_id: Mapped[int] = mapped_column(ForeignKey("tcm_formulas.id"), index=True)
    batch_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    unit: Mapped[str] = mapped_column(String(16), default="剂")
    produced_date: Mapped[str] = mapped_column(String(10), default="")
    expire_date: Mapped[str] = mapped_column(String(10), default="", index=True)
    # produced=已生产, released=已发放, recalled=已召回
    status: Mapped[str] = mapped_column(String(16), default="produced", index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    formula: Mapped[TcmFormula] = relationship(back_populates="batches")


class CssdCostItem(Base):
    """⑥消毒供应成本核算：灭菌批次的成本项，单件成本 = 批次成本合计 / 件数。"""

    __tablename__ = "cssd_cost_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("sterilization_batches.id"), index=True)
    # labor=人工, material=耗材, energy=能耗, equipment=设备折旧, other=其他
    cost_type: Mapped[str] = mapped_column(String(16), index=True)
    amount: Mapped[float] = mapped_column(Money, default=0)
    note: Mapped[str] = mapped_column(String(256), default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CourseMaterial(Base):
    """⑳课件资源：课程下的课件条目，可挂附件（owner_type=course_material）并计点播量。"""

    __tablename__ = "course_materials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    title: Mapped[str] = mapped_column(String(256))
    # slide=课件, video=视频, doc=文档, link=外链
    material_type: Mapped[str] = mapped_column(String(16), default="slide", index=True)
    url: Mapped[str] = mapped_column(String(512), default="")
    play_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TrainingPlan(Base):
    """㉑适宜技术实训计划：名额有限、可报名、可考核。"""

    __tablename__ = "training_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(256))
    technique_id: Mapped[int | None] = mapped_column(
        ForeignKey("tcm_techniques.id"), nullable=True
    )
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    plan_date: Mapped[str] = mapped_column(String(10), default="")
    capacity: Mapped[int] = mapped_column(Integer, default=30)
    # 已占名额（第十轮）：报名/退报名原子增减，占额判定走这个计数列而不是
    # COUNT(*)——原先 `COUNT >= capacity` 是 check-then-act，并发下多人同时数到
    # "还差一个"一起挤进来，实测容量 2 报上 3 人。做法与疫苗批次占额一致。
    enrolled_count: Mapped[int] = mapped_column(Integer, default=0)
    trainer: Mapped[str] = mapped_column(String(64), default="")
    # open=报名中, closed=已截止, finished=已结训
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TrainingEnrollment(Base):
    """㉑实训报名：同一计划同一学员唯一。"""

    __tablename__ = "training_enrollments"
    __table_args__ = (UniqueConstraint("plan_id", "user_id", name="uq_enroll_plan_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("training_plans.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    # enrolled=已报名, cancelled=已取消
    status: Mapped[str] = mapped_column(String(16), default="enrolled", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TrainingAssessment(Base):
    """㉑实训考核记录：须先报名方可录入成绩，60 分及格。"""

    __tablename__ = "training_assessments"
    __table_args__ = (UniqueConstraint("plan_id", "user_id", name="uq_assess_plan_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("training_plans.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    score: Mapped[float] = mapped_column(Float, default=0)
    passed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    comment: Mapped[str] = mapped_column(String(512), default="")
    assessor: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PrenatalScreening(Base):
    """㉔产前筛查与诊断：唐筛/无创/超声筛查记录，高危结果自动标记孕产妇档案。"""

    __tablename__ = "prenatal_screenings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_id: Mapped[int] = mapped_column(ForeignKey("maternal_records.id"), index=True)
    # down=唐氏血清学筛查, nipt=无创产前基因检测, ultrasound=超声结构筛查, diagnosis=产前诊断
    screen_type: Mapped[str] = mapped_column(String(16), index=True)
    screen_date: Mapped[str] = mapped_column(String(10), default="")
    gest_week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # low_risk=低风险, high_risk=高风险, critical=临界风险
    result: Mapped[str] = mapped_column(String(16), default="low_risk", index=True)
    indicator: Mapped[str] = mapped_column(String(256), default="")
    conclusion: Mapped[str] = mapped_column(String(512), default="")
    # 是否已触发孕产妇档案高危标记
    flagged_high_risk: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ImprovementTask(Base):
    """㉟绩效自评改进：问题 → 责任人 → 期限 → 整改 → 完成确认（验证闭环）。"""

    __tablename__ = "improvement_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # 关联绩效指标 key（可空，来自绩效自评发现的问题）
    indicator_key: Mapped[str] = mapped_column(String(32), default="", index=True)
    problem: Mapped[str] = mapped_column(String(512))
    measures: Mapped[str] = mapped_column(String(1024), default="")
    owner_name: Mapped[str] = mapped_column(String(64))
    due_date: Mapped[str] = mapped_column(String(10), index=True)
    # open=待整改, in_progress=整改中, completed=已完成待确认, verified=已确认关闭
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    completion_note: Mapped[str] = mapped_column(String(512), default="")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verify_comment: Mapped[str] = mapped_column(String(512), default="")
    verified_by: Mapped[str] = mapped_column(String(64), default="")
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class HomeVisitOrder(Base):
    """⑨上门服务调度（送医送护上门）：申请 → 派单 → 完成，关联家医签约。"""

    __tablename__ = "home_visit_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    contract_id: Mapped[int | None] = mapped_column(ForeignKey("fd_contracts.id"), nullable=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # nursing=上门护理, doctor=上门诊疗, rehab=康复指导, sampling=上门采样
    service_type: Mapped[str] = mapped_column(String(16), index=True)
    demand: Mapped[str] = mapped_column(String(512), default="")
    address: Mapped[str] = mapped_column(String(256), default="")
    expect_date: Mapped[str] = mapped_column(String(10), default="")
    # applied=待派单, dispatched=已派单, completed=已完成, cancelled=已取消
    status: Mapped[str] = mapped_column(String(16), default="applied", index=True)
    assignee_name: Mapped[str] = mapped_column(String(64), default="")
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    service_note: Mapped[str] = mapped_column(String(512), default="")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# 块1：集成平台底座 ESB（浙江指南 M11）——接入方注册 / 消息队列 / 流程编排
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 块2：电子病历结构化与环节质控（浙江指南 M9 深化）
# ---------------------------------------------------------------------------


class MedicalRecord(Base):
    """结构化电子病历：一次就诊一份（encounter_id 唯一），医师书写。

    提交/更新即触发环节质控（实时评分），评分与等级回写本表，
    作为 qc-summary 统计口径的唯一数据源。
    """

    __tablename__ = "medical_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    encounter_id: Mapped[int] = mapped_column(ForeignKey("encounters.id"), unique=True, index=True)
    # 冗余机构/医师：统计维度固定为病历书写时的归属，后续人员变动不影响历史口径
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    doctor_name: Mapped[str] = mapped_column(String(64), default="", index=True)
    chief_complaint: Mapped[str] = mapped_column(String(256), default="")  # 主诉
    present_illness: Mapped[str] = mapped_column(String(2048), default="")  # 现病史
    past_history: Mapped[str] = mapped_column(String(1024), default="")  # 既往史
    physical_exam: Mapped[str] = mapped_column(String(1024), default="")  # 体格检查
    diagnosis_basis: Mapped[str] = mapped_column(String(1024), default="")  # 诊断依据
    treatment_plan: Mapped[str] = mapped_column(String(1024), default="")  # 治疗方案
    # 环节质控结果快照（每次提交/复评刷新）
    qc_score: Mapped[int] = mapped_column(Integer, default=100, index=True)
    qc_grade: Mapped[str] = mapped_column(String(4), default="甲", index=True)
    qc_defects: Mapped[list] = mapped_column(JSON, default=list)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RecordQcRule(Base):
    """环节质控规则：check_field 指向病历字段（含派生字段），rule 决定执行器。

    rule ∈ required（必填）| min_length（下限字数）| max_length（上限字数）|
    keyword_present（须含关键词之一）；config 携带阈值与触发条件；
    命中即按 deduct_points 扣分（100 分制）。
    """

    __tablename__ = "record_qc_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    check_field: Mapped[str] = mapped_column(String(32), index=True)
    rule: Mapped[str] = mapped_column(String(24), index=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    deduct_points: Mapped[int] = mapped_column(Integer, default=5)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# 块3：统一支付与日终对账（浙江指南 M8 深化）
# ---------------------------------------------------------------------------


class PaymentOrder(Base):
    """支付单：一次结算可分多笔渠道支付（现金/银行卡/医保/线上）。

    trade_no 为支付通道返回的外部流水号，是日终对账的比对主键。
    """

    __tablename__ = "payment_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    settlement_id: Mapped[int] = mapped_column(ForeignKey("settlements.id"), index=True)
    # cash=现金, card=银行卡, insurance=医保基金, online=线上支付
    channel: Mapped[str] = mapped_column(String(16), index=True)
    amount: Mapped[float] = mapped_column(Money, default=0)
    # pending=待支付, paid=已支付, refunded=已全额退款, failed=支付失败
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    trade_no: Mapped[str] = mapped_column(String(64), default="", index=True)
    # 已退金额（部分退款累计；等于 amount 时状态转 refunded）
    refunded_amount: Mapped[float] = mapped_column(Money, default=0)
    fail_reason: Mapped[str] = mapped_column(String(256), default="")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class ReconciliationBatch(Base):
    """日终对账单：某自然日本地支付单与通道流水的比对结果汇总。"""

    __tablename__ = "reconciliation_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[str] = mapped_column(String(10), index=True)
    total_orders: Mapped[int] = mapped_column(Integer, default=0)
    total_amount: Mapped[float] = mapped_column(Money, default=0)
    matched: Mapped[int] = mapped_column(Integer, default=0)
    unmatched: Mapped[int] = mapped_column(Integer, default=0)
    diff_amount: Mapped[float] = mapped_column(Money, default=0)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ReconciliationDiff(Base):
    """对账差异明细：本地有通道无 / 通道有本地无 / 金额不一致。"""

    __tablename__ = "reconciliation_diffs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_batches.id"), index=True)
    # 通道单边流水（missing_local）无本地支付单，order_id 为空
    order_id: Mapped[int | None] = mapped_column(ForeignKey("payment_orders.id"), nullable=True)
    trade_no: Mapped[str] = mapped_column(String(64), default="", index=True)
    # missing_local=通道有本地无, missing_remote=本地有通道无, amount_mismatch=金额不一致
    diff_type: Mapped[str] = mapped_column(String(20), index=True)
    local_amount: Mapped[float] = mapped_column(Money, default=0)
    remote_amount: Mapped[float] = mapped_column(Money, default=0)
    detail: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ============================================================================
# 居民端账号体系：手机号验证码 / 微信网页授权登录（取代电子健康卡号+身份证核验）
# ============================================================================


class ResidentAccount(Base):
    """居民账户：登录凭据（手机号/微信 openid）与患者主索引的绑定关系。

    账户与档案是两件事——先登录拿到账户身份，再实名绑定到 Patient 才看得到档案。
    未绑定的账户只能看健康宣教，这样验证码泄露也不会直接泄露他人档案。

    phone / wechat_openid 用可空唯一列而非空串：SQLite 与 PostgreSQL 的唯一索引
    都允许多个 NULL，既能让"只有微信没有手机号"的账户共存，又能靠数据库约束
    挡住并发首登产生的重复账户（撞约束后回查既有账户即可）。
    """

    __tablename__ = "resident_accounts"
    __table_args__ = (
        # "一份档案只绑一个账户"下沉到数据库：应用层查重是 check-then-act，
        # 并发下两个账户可同时绑上同一份档案（与基金池 D-2 同形）。部分唯一
        # 索引放行多个 NULL（未绑定账户共存），绑定列上唯一。
        Index(
            "uq_resident_account_patient",
            "patient_id",
            unique=True,
            sqlite_where=text("patient_id IS NOT NULL"),
            postgresql_where=text("patient_id IS NOT NULL"),
        ),
    )

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


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
from .spd.models import *  # noqa: E402,F401,F403
