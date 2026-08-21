"""ORM 模型 · 人力资源与培训：员工、科室、排班、课程、实训。

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
from sqlalchemy.orm import Mapped, mapped_column

from ..database import Base
from ._base import utcnow


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


class DutyRoster(Base):
    """①-④共享中心排班管理。"""

    __tablename__ = "duty_rosters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    center_type: Mapped[str] = mapped_column(String(16), index=True)
    duty_date: Mapped[str] = mapped_column(String(10), index=True)
    shift: Mapped[str] = mapped_column(String(16), default="全天")
    doctor_name: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


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
