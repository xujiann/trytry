"""E4 公共卫生短板补全：严重精神障碍管理、结核病督导服药、卫生监督协管、
公卫 12 类项目登记与完成度。

模型只承载表结构，横向隔离与角色守卫在 routers/enhance_pubhealth.py 内实现。
凡带 patient_id + org_id 的表（精神障碍/结核患者及其随访）会被 visibility
的 `_relation_tables` 自动纳入"患者↔机构"服务关系，故精神/结核档案的
患者可见性开箱即按机构服务关系推导，无需另行登记。
"""
from datetime import datetime

from ._shared import (Boolean, DateTime, Float, ForeignKey, Integer, Money, String,
                      UniqueConstraint, Mapped, mapped_column, relationship, utcnow)
from ..database import Base


class PsychPatient(Base):
    """严重精神障碍患者管理：0-5 级危险性评估、关锁情况、监护人信息。"""

    __tablename__ = "psych_patients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    diagnosis: Mapped[str] = mapped_column(String(128), default="")
    # 0-5 级危险性评估：0=无, 1-5 逐级升高
    danger_level: Mapped[str] = mapped_column(String(2), default="0")
    guardian: Mapped[str] = mapped_column(String(64), default="")
    guardian_phone: Mapped[str] = mapped_column(String(32), default="")
    # 关锁情况：曾被关锁/约束
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    # in_manage=在管, lost=失访, transferred=转出, deceased=死亡
    status: Mapped[str] = mapped_column(String(16), default="in_manage")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PsychFollowup(Base):
    """严重精神障碍患者随访：用药依从性、病情稳定性。"""

    __tablename__ = "psych_followups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    psych_id: Mapped[int] = mapped_column(ForeignKey("psych_patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    followup_date: Mapped[str] = mapped_column(String(10), default="")
    # good=规律, partial=间断, none=未服药
    medication_adherence: Mapped[str] = mapped_column(String(8), default="")
    stability: Mapped[str] = mapped_column(String(16), default="")
    note: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TbPatient(Base):
    """结核病患者管理：分型、治疗方案、疗程。"""

    __tablename__ = "tb_patients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # 分型：pulmonary=肺结核, extrapulmonary=肺外结核, mdr=耐多药 等
    tb_type: Mapped[str] = mapped_column(String(32), default="")
    treatment_start: Mapped[str] = mapped_column(String(10), default="")
    regimen: Mapped[str] = mapped_column(String(64), default="")
    # in_treatment=治疗中, cured=治愈, completed=完成疗程, failed=失败, lost=丢失
    status: Mapped[str] = mapped_column(String(16), default="in_treatment")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TbFollowup(Base):
    """结核病督导服药（DOT）：每次督导服药是否服药、痰检结果。"""

    __tablename__ = "tb_followups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tb_id: Mapped[int] = mapped_column(ForeignKey("tb_patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    followup_date: Mapped[str] = mapped_column(String(10), default="")
    taken: Mapped[bool] = mapped_column(Boolean, default=False)
    sputum_result: Mapped[str] = mapped_column(String(16), default="")
    note: Mapped[str] = mapped_column(String(512), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TbContact(Base):
    """结核病密切接触者筛查。"""

    __tablename__ = "tb_contacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tb_id: Mapped[int] = mapped_column(ForeignKey("tb_patients.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    relation: Mapped[str] = mapped_column(String(32), default="")
    screened: Mapped[bool] = mapped_column(Boolean, default=False)
    result: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class HealthSupervision(Base):
    """卫生监督协管巡查：食品/饮用水/学校/职业/放射等领域巡查记录。"""

    __tablename__ = "health_supervisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # food=食品, water=饮用水, school=学校卫生, occupation=职业卫生, radiation=放射卫生
    domain: Mapped[str] = mapped_column(String(16))
    target: Mapped[str] = mapped_column(String(128))
    inspect_date: Mapped[str] = mapped_column(String(10), default="")
    findings: Mapped[str] = mapped_column(String(512), default="")
    passed: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PublicHealthProject(Base):
    """公卫 12 类项目登记表（全域目录）：项目编码/名称/类别。

    admin 维护的全局目录，不带 org_id，不做机构隔离。
    """

    __tablename__ = "pubhealth_projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(32), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PublicHealthProjectStat(Base):
    """公卫项目完成度：某机构某年度某项目的目标数与完成数。"""

    __tablename__ = "pubhealth_project_stats"
    __table_args__ = (
        UniqueConstraint("project_id", "org_id", "year", name="uq_ph_project_stat"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("pubhealth_projects.id"), index=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    year: Mapped[str] = mapped_column(String(4))
    target_count: Mapped[int] = mapped_column(Integer, default=0)
    done_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
