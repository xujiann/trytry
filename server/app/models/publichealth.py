"""ORM 模型 · 公共卫生：传染病、妇幼儿保、免疫规划、事件与监测、体检。

由原 `models.py`（3989 行 / 187 类）分域拆出，见 ADR-0008。
**类的先后顺序保持原文件不变**——本仓库没开 `from __future__ import annotations`，
`Mapped[SomeClass]` 这类注解在建类时就要求被引用的类已经定义。
"""
from datetime import datetime

from sqlalchemy import (
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
from ._base import utcnow


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


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
    # 总检（B2）：分项录完后由总检医师出结论；两列均空 = 尚未总检
    final_conclusion: Mapped[str] = mapped_column(String(1024), default="")
    final_doctor: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CheckupItem(Base):
    """体检分项结果（B2）：一次体检的逐项测值与参考范围。

    汇总口径不变：`PhysicalExam.summary`/`abnormal_items` 保留兼容（存量记录
    只有汇总），分项非空时 `has_abnormal` 由"汇总异常串非空 **或** 任一分项
    abnormal"共同决定。
    """

    __tablename__ = "checkup_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    checkup_id: Mapped[int] = mapped_column(ForeignKey("physical_exams.id"), index=True)
    item_code: Mapped[str] = mapped_column(String(64), index=True)
    item_name: Mapped[str] = mapped_column(String(128))
    # 结果值存字符串：定性项目（阴性/阳性）与定量项目（数值）同列
    result_value: Mapped[str] = mapped_column(String(64))
    unit: Mapped[str] = mapped_column(String(16), default="")
    ref_range: Mapped[str] = mapped_column(String(64), default="")
    abnormal: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
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
