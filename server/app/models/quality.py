"""ORM 模型 · 质量与绩效：不良事件、病历质控、院感上报、绩效指标。

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


class PerformanceIndicator(Base):
    """绩效考核指标目录：维度权重可由管理层调节（按比例归一化到100）。"""

    __tablename__ = "performance_indicators"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    weight: Mapped[float] = mapped_column(Float, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


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


class QcLot(Base):
    """检验室内质控（IQC）质控品批号：项目 × 批号 × 靶值/标准差。

    **与 `QcRecord` 法域不同，互不替代**：`QcRecord` 是①-④共享中心的运行质量
    台账（人工登记合格/不合格），本表与 `QcMeasurement` 是检验科**室内质控**的
    数值体系——靶值/SD 定基线，测定值录入即按 Westgard 规则判定失控。
    既有 `QcRecord` 台账保留不动。
    """

    __tablename__ = "qc_lots"
    __table_args__ = (
        UniqueConstraint("org_id", "item_code", "lot_no", name="uq_qc_lot_org_item_lot"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    org_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), index=True)
    # 检验项目编码/名称（如 K-POTASSIUM 血清钾）
    item_code: Mapped[str] = mapped_column(String(64), index=True)
    item_name: Mapped[str] = mapped_column(String(128))
    lot_no: Mapped[str] = mapped_column(String(64), index=True)
    # 靶值与标准差（定值质控品说明书或前 20 次测定累积均值/SD）
    target_value: Mapped[float] = mapped_column(Float)
    sd: Mapped[float] = mapped_column(Float)
    # 停用的批号不再接受测定值录入（换批后旧批停用）
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class QcMeasurement(Base):
    """IQC 测定值：录入时即按 Westgard 基础四规则判定，结果落列。

    - 1-2s 超±2SD → 警告（warning，不算失控）；
    - 1-3s / 2-2s / R-4s 命中 → 失控（out_of_control），命中规则记 violated_rules；
    - 失控须处理（原因 + 纠正措施）：未处理期间继续录入，响应里给警示。
    """

    __tablename__ = "qc_measurements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lot_id: Mapped[int] = mapped_column(ForeignKey("qc_lots.id"), index=True)
    value: Mapped[float] = mapped_column(Float)
    # 测定时刻（YYYY-MM-DD HH:MM），与录入时刻分开：补录时二者不同
    measured_at: Mapped[str] = mapped_column(String(16), default="")
    operator: Mapped[str] = mapped_column(String(64), default="")
    # 1-2s 警告命中（不算失控，Westgard 里是"启动其他规则检查"的信号）
    warning: Mapped[bool] = mapped_column(Boolean, default=False)
    out_of_control: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # 命中的失控规则，分号分隔（如 "1-3s" / "2-2s;R-4s"）
    violated_rules: Mapped[str] = mapped_column(String(64), default="")
    # 失控处理留痕：原因 + 纠正措施（handled=False 的失控点是未闭环欠账）
    handled: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    handle_reason: Mapped[str] = mapped_column(String(512), default="")
    corrective_action: Mapped[str] = mapped_column(String(512), default="")
    handled_by: Mapped[str] = mapped_column(String(64), default="")
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


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
