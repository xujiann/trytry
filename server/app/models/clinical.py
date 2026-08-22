"""ORM 模型 · 诊疗过程：就诊、检查检验、病历文书、会诊、知情同意。

由原 `models.py`（3989 行 / 187 类）分域拆出，见 ADR-0008。
**类的先后顺序保持原文件不变**——本仓库没开 `from __future__ import annotations`，
`Mapped[SomeClass]` 这类注解在建类时就要求被引用的类已经定义。
"""
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ._base import Money, utcnow


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


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
    # 领取该申请单的**共享诊断中心机构**。`claimed_by` 存的是展示名（full_name 或
    # username），既不稳定也推不出机构，于是"中心与这位患者有服务关系"这件事
    # 在模型里一直没有落点——中心医师写完报告却打不开自己写的那份报告。
    # 列名以 `org_id` 结尾是刻意的：`visibility._relation_tables()` 按该后缀推导
    # 患者↔机构关系表，命名对了就自动被算进服务关系，不必再维护一份手工清单。
    claimed_org_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id"), nullable=True, index=True
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


class ConsultExpert(Base):
    """⑤远程会诊专家管理。"""

    __tablename__ = "consult_experts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"))
    specialty: Mapped[str] = mapped_column(String(64), default="")
    available: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


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
    # 护理执行联动（P1-24a）：本条护理记录若是执行某条住院医嘱产生的，挂上医嘱 id。
    # 可空——日常巡视、病情观察类护理记录本来就不对应任何医嘱，强制关联是错的语义。
    inpatient_order_id: Mapped[int | None] = mapped_column(
        ForeignKey("inpatient_orders.id"), nullable=True, index=True
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
