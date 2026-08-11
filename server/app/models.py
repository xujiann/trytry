"""核心数据模型：对应规划第一期"平台支撑层 + 数据中心层"基础实体。

- User            统一认证用户（医共体工作人员账号）
- Organization    医共体成员单位（牵头医院/乡镇卫生院/村卫生室等）
- Patient         患者主索引（EMPI，电子健康卡号为对外统一标识）
- CodeSystem/CodeEntry  统一编码字典（诊断、药品、耗材、收费"四统一"）
- Referral        双向转诊记录（上转/下转，状态流转）
"""
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
    """⑮基层缺药登记：登记→采购→配送到基层。"""

    __tablename__ = "drug_shortages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    drug_code: Mapped[str] = mapped_column(String(64))
    drug_name: Mapped[str] = mapped_column(String(128))
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    # registered=已登记, purchasing=采购中, delivered=已配送
    status: Mapped[str] = mapped_column(String(16), default="registered", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class InsuranceSettlement(Base):
    """⑲医保业务协同：结算记录（本地/异地）。"""

    __tablename__ = "insurance_settlements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # local=本地结算, remote=异地结算
    settle_type: Mapped[str] = mapped_column(String(16), default="local")
    total_amount: Mapped[float] = mapped_column(Float)
    insurance_pay: Mapped[float] = mapped_column(Float)
    self_pay: Mapped[float] = mapped_column(Float)
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

    child: Mapped[ChildRecord] = relationship(back_populates="visits")


class VaccinationRecord(Base):
    """㉕疫苗接种记录。"""

    __tablename__ = "vaccination_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    vaccine_code: Mapped[str] = mapped_column(String(64))
    vaccine_name: Mapped[str] = mapped_column(String(128))
    dose_no: Mapped[int] = mapped_column(Integer, default=1)
    vaccinated_date: Mapped[str] = mapped_column(String(10), default="")
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))


class VaccineContraindication(Base):
    __tablename__ = "vaccine_contraindications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    vaccine_code: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(256))
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
    position: Mapped[str] = mapped_column(String(64), default="")
    # active=在岗, seconded=派驻中, left=离职
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    # 终审轮：科室挂接（浙#9）
    dept_id: Mapped[int | None] = mapped_column(ForeignKey("departments.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Secondment(Base):
    """㉚人员派驻/下沉记录（支撑监测指标4）。"""

    __tablename__ = "secondments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    from_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    to_org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    start_date: Mapped[str] = mapped_column(String(10))
    end_date: Mapped[str] = mapped_column(String(10), default="")
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
    amount: Mapped[float] = mapped_column(Float)
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


class AppointmentBlacklist(Base):
    """⑫预约黑名单管理（多次爽约限制预约）。"""

    __tablename__ = "appointment_blacklist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), unique=True)
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
    total_cost: Mapped[float] = mapped_column(Float, default=0)
    drug_cost: Mapped[float] = mapped_column(Float, default=0)
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
    price: Mapped[float] = mapped_column(Float)
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
    unit_price: Mapped[float] = mapped_column(Float)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    amount: Mapped[float] = mapped_column(Float)
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
    total_amount: Mapped[float] = mapped_column(Float)
    insurance_pay: Mapped[float] = mapped_column(Float, default=0)
    self_pay: Mapped[float] = mapped_column(Float, default=0)
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
    """DRG 分组目录：编码/名称/基准权重/主诊断关键词（简化入组规则）。"""

    __tablename__ = "drg_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128))
    base_weight: Mapped[float] = mapped_column(Float)
    # 主诊断匹配关键词（逗号分隔），出院病例按关键词命中入组
    keywords: Mapped[str] = mapped_column(String(256), default="")
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
    price: Mapped[float] = mapped_column(Float, default=0)
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
    base_salary: Mapped[float] = mapped_column(Float)
    perf_bonus: Mapped[float] = mapped_column(Float, default=0)
    # 绩效系数（考核结果联动薪酬分配）
    perf_coefficient: Mapped[float] = mapped_column(Float, default=1.0)
    total: Mapped[float] = mapped_column(Float)
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
    amount: Mapped[float] = mapped_column(Float)
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
    requested_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
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
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
