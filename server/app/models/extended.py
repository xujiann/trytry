"""领域模型分块（见 app/models/__init__.py 说明）。
所有类共用 _shared 的 Base/Money/工具与 SQLAlchemy 名称；跨块引用一律走
字符串（ForeignKey("t.col") / relationship("Cls")），由 registry 惰性解析，
不依赖 import 顺序。"""
from ._shared import *  # noqa: F401,F403


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


