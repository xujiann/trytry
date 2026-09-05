"""ORM 模型 · 慢病与专病管理：病种目录、入组、随访、双通道。

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
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..database import Base
from ._base import Money, utcnow


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


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


class SpecialDiseaseApp(Base):
    """⑲特殊病种门诊治疗待遇申报。"""

    __tablename__ = "special_disease_apps"
    __table_args__ = (
        # 部分唯一索引：同患者同病种同时只有一条**已申报待批**记录；批准/驳回后可再申报。
        Index(
            "uq_special_disease_app_applied",
            "patient_id", "disease_name",
            unique=True,
            sqlite_where=text("status = 'applied'"),
            postgresql_where=text("status = 'applied'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    disease_name: Mapped[str] = mapped_column(String(128))
    # applied=已申报, approved=已批准, rejected=已驳回
    status: Mapped[str] = mapped_column(String(16), default="applied")
    reason: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DualChannelApp(Base):
    """⑲双通道药品申报：申报→管理层审核。"""

    __tablename__ = "dual_channel_apps"
    __table_args__ = (
        # 部分唯一索引：同患者同药品同时只有一条**待审核**申报。审核后（通过/驳回）
        # 可以再申报，故只锁 pending 一态。
        Index(
            "uq_dual_channel_pending",
            "patient_id", "drug_name",
            unique=True,
            sqlite_where=text("status = 'pending'"),
            postgresql_where=text("status = 'pending'"),
        ),
    )

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
    __table_args__ = (
        # 部分唯一索引："同患者同专病同时只有一条**在管**记录"（模型 docstring 与
        # disease_programs.enroll 的 409 都这么写）。接口层"先查在管再建"是 check-then-act，
        # 并发下静默写出两条：program_stats 双计、出组只能翻掉一条，剩下那条永远挡着复发再入组。
        # 只锁 enrolled 一态——完成/退出后复发再入组是常态，全量唯一会拒掉它。
        Index(
            "uq_disease_enrollment_program_patient_enrolled",
            "program_id", "patient_id",
            unique=True,
            sqlite_where=text("status = 'enrolled'"),
            postgresql_where=text("status = 'enrolled'"),
        ),
    )

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
