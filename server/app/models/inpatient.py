"""ORM 模型 · 住院与手术：病区床位、医嘱、病案首页、手术、用血。

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
from ._base import Money, utcnow


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
