"""核心数据模型：对应规划第一期"平台支撑层 + 数据中心层"基础实体。

- User            统一认证用户（医共体工作人员账号）
- Organization    医共体成员单位（牵头医院/乡镇卫生院/村卫生室等）
- Patient         患者主索引（EMPI，电子健康卡号为对外统一标识）
- CodeSystem/CodeEntry  统一编码字典（诊断、药品、耗材、收费"四统一"）
- Referral        双向转诊记录（上转/下转，状态流转）
"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
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
    # admin=平台管理员, doctor=医师, operator=经办人员
    role: Mapped[str] = mapped_column(String(32), default="operator")
    org_id: Mapped[int | None] = mapped_column(ForeignKey("organizations.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    # lead_hospital=牵头医院, township=乡镇卫生院/社区卫生服务中心, village=村卫生室, public_health=公卫机构
    org_type: Mapped[str] = mapped_column(String(32))
    # 层级：county=县级, township=乡级, village=村级
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
    reported_by: Mapped[str] = mapped_column(String(64), default="")
    reported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    request: Mapped[ExamRequest] = relationship(back_populates="report")


class DrugRule(Base):
    """合理用药规则库：集中审方的"系统审"依据。"""

    __tablename__ = "drug_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    drug_code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    max_daily_dose: Mapped[float] = mapped_column(Float)
    dose_unit: Mapped[str] = mapped_column(String(16), default="mg")
    note: Mapped[str] = mapped_column(String(256), default="")


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


class ChronicPatient(Base):
    """慢病建档：高血压/2型糖尿病/慢阻肺，智能分级分组。"""

    __tablename__ = "chronic_patients"
    __table_args__ = (
        UniqueConstraint("patient_id", "disease", name="uq_chronic_patient_disease"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    # hypertension=高血压, diabetes=2型糖尿病, copd=慢阻肺
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
    guidance: Mapped[str] = mapped_column(String(1024), default="")
    next_due: Mapped[str] = mapped_column(String(10), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    chronic: Mapped[ChronicPatient] = relationship(back_populates="followups")


class InfectiousCase(Base):
    """传染病病例报告：多点触发监测预警的数据来源。"""

    __tablename__ = "infectious_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    disease_code: Mapped[str] = mapped_column(String(64), index=True)
    disease_name: Mapped[str] = mapped_column(String(128))
    onset_date: Mapped[str] = mapped_column(String(10), index=True)
    reported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


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
