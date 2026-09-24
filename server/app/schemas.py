from pydantic import BaseModel, Field, FiniteFloat
from .datetypes import DateStr, OptionalDateStr
from .numtypes import INT4_MAX
from .texttypes import NON_BLANK


class LoginRequest(BaseModel):
    # 列长（P1-91 第五层）：登录不论成败都写登录留痕 `login_logs.username`(64)，超长用户名原先在生产库上 500
    username: str = Field(max_length=64)
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=2, max_length=128, pattern=NON_BLANK)
    org_type: str = Field(pattern="^(lead_hospital|township|village|public_health)$")
    # L-4：city=市级协作医院（市级互认项目适用），county/township/village=县域层级
    level: str = Field(pattern="^(city|county|township|village)$")
    parent_id: int | None = None
    address: str = Field(default="", max_length=256)


class OrganizationOut(OrganizationCreate):
    id: int
    # 机构名 2–128 字是**建档入口**的约束。存量导入（scripts/import_legacy.py）原先只查非空，
    # 导进一个单字机构名，继承来的约束就让机构清单对所有人 500（P2-40，实测）；入口已同口径，
    # 出参照 P1-65 覆盖回 str——修之前导进来的坏值要在清单里看得见才谈得上改
    name: str

    model_config = {"from_attributes": True}


class PatientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    id_card: str = Field(min_length=15, max_length=18, pattern=NON_BLANK)
    gender: str = "未知"
    # 年龄全靠它现算（审方的儿童/老年规则、未满 14 周岁须监护人、慢专病的年龄纳入规则），
    # 算不出的一律当"不知道"放过：`2016/03/05` 建档的 10 岁孩子登记知情同意不要监护人（P1-61，实测）
    birth_date: OptionalDateStr = ""
    phone: str = ""


class PatientOut(PatientCreate):
    id: int
    ehc_no: str
    # 出参不带入参的日历校验（P1-63）：库里的存量坏日期要原样读出来，而不是让响应 500
    birth_date: str = ""
    # 证件号 15–18 位、姓名 1–64 字是**建档入口**的约束。HL7/FHIR/ESB 入站不走请求模型，
    # 只查"证件号至少 15 位"，而列能存 256 位：超长证件号的患者一入库，继承来的约束就让
    # 患者列表对所有人 500，入站接口自己也回 422 却已建档（P1-65，实测）
    name: str
    id_card: str

    model_config = {"from_attributes": True}


class CodeEntryCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    name: str = Field(min_length=1, max_length=256, pattern=NON_BLANK)


class CodeEntryOut(CodeEntryCreate):
    id: int
    system_id: int

    model_config = {"from_attributes": True}


class ReferralCreate(BaseModel):
    patient_id: int
    from_org_id: int
    to_org_id: int
    direction: str = Field(pattern="^(up|down)$")
    reason: str = Field(default="", max_length=512)


class ReferralOut(ReferralCreate):
    id: int
    status: str
    #: 状态中文（业务端口径，见 `routers/referrals.STATUS_LABELS`）。
    #: 加这个字段是为了把文案的权威收到后端一处——前端各存一份映射，
    #: 改一处漏一处只是时间问题。对既有调用方是**新增**字段，不动任何原有字段。
    status_label: str = ""

    model_config = {"from_attributes": True}


class ReferralStatusUpdate(BaseModel):
    status: str = Field(pattern="^(accepted|completed|rejected)$")


class EncounterCreate(BaseModel):
    patient_id: int
    org_id: int
    doctor_name: str = Field(default="", max_length=64)
    encounter_type: str = Field(default="outpatient", pattern="^(outpatient|inpatient)$")
    diagnosis_code: str = Field(default="", max_length=64)
    diagnosis_name: str = Field(default="", max_length=256)
    summary: str = Field(default="", max_length=1024)


class EncounterOut(EncounterCreate):
    id: int

    model_config = {"from_attributes": True}


class ExamRequestCreate(BaseModel):
    patient_id: int
    from_org_id: int
    center_type: str = Field(pattern="^(imaging|ecg|lab|pathology)$")
    # 检查项目必填（P1-110）：原先只卡长度，项目编码 / 名称为空串照样建单，检查中心收到一张不知道查什么的申请
    item_code: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    item_name: str = Field(min_length=1, max_length=128, pattern=NON_BLANK)
    clinical_info: str = Field(default="", max_length=512)
    # 互认：引用既往已报告申请的 id 即互认其结果；填写理由则记录不互认原因
    accept_recognition_of: int | None = None
    recognition_declined_reason: str = Field(default="", max_length=256)


class ExamRequestOut(BaseModel):
    id: int
    patient_id: int
    from_org_id: int
    center_type: str
    item_code: str
    item_name: str
    clinical_info: str
    status: str
    recognized_from_id: int | None
    recognition_declined_reason: str
    sample_status: str = ""
    claimed_by: str = ""

    model_config = {"from_attributes": True}


class ExamReportCreate(BaseModel):
    finding: str = Field(default="", max_length=2048)
    conclusion: str = Field(max_length=1024)
    critical: bool = False
    reported_by: str = Field(default="", max_length=64)


class ExamReportOut(ExamReportCreate):
    id: int
    request_id: int
    critical_status: str = ""

    model_config = {"from_attributes": True}


class RecognitionItemCreate(BaseModel):
    item_code: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    item_name: str = Field(min_length=1, max_length=128, pattern=NON_BLANK)
    center_type: str = Field(pattern="^(imaging|ecg|lab|pathology)$")
    mutual_scope: str = Field(default="county", pattern="^(county|city)$")
    active: bool = True


class RecognitionItemUpdate(BaseModel):
    # 改名与建档同口径（P2-40）：原先不带 min_length，改名为空串照收，出参校验在 commit 之后才失败
    item_name: str | None = Field(default=None, min_length=1, max_length=128, pattern=NON_BLANK)
    center_type: str | None = Field(default=None, pattern="^(imaging|ecg|lab|pathology)$")
    mutual_scope: str | None = Field(default=None, pattern="^(county|city)$")
    active: bool | None = None


class RecognitionItemOut(RecognitionItemCreate):
    id: int
    # 改档原先收得下空串（P2-40）：一条空名目录项就让整张互认目录 500、页面整页打不开（实测）。
    # 入口已同口径，出参照 P1-65 覆盖回 str，修之前存进去的空名要在目录里看得见才谈得上改
    item_name: str
    # 出参不带「不能只填空格」（P1-109）：修之前存进去的纯空白行要原样读出来，而不是让整个清单 500
    item_code: str = Field(min_length=1, max_length=64)

    model_config = {"from_attributes": True}


class CriticalActionOut(BaseModel):
    id: int
    report_id: int
    action: str
    actor: str

    model_config = {"from_attributes": True}


class CriticalResolveBody(BaseModel):
    note: str = Field(default="", max_length=512)


class DrugRuleCreate(BaseModel):
    # 药品编码必填（P1-110）：原先空串照收，建出一条只管「没有编码的药」的规则
    drug_code: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    max_daily_dose: FiniteFloat = Field(gt=0)
    dose_unit: str = Field(default="mg", max_length=16)
    note: str = Field(default="", max_length=256)
    # 相互作用冲突药品编码，逗号分隔（如 "D002,D003"）
    interactions: str = Field(default="", max_length=512)
    # 禁忌诊断关键词，逗号分隔（如 "消化性溃疡,出血"）
    contraindicated_diagnoses: str = Field(default="", max_length=512)
    # 特殊人群，逗号分隔，取值 pregnant/child/elderly
    special_groups: str = Field(default="", max_length=64)
    # 肝肾功能提示（不拦截，随处方返回供剂量调整参考）
    renal_hepatic_note: str = Field(default="", max_length=512)
    # 处方点评要点（事后点评规则化依据）
    review_points: str = Field(default="", max_length=512)
    # 抗菌药物标记与 DDD（限定日剂量，单位同 dose_unit）。
    # ddd 留 0 表示未维护，使用强度统计会把它计入"未覆盖"而不是按 0 参与计算。
    antibiotic: bool = False
    ddd: FiniteFloat = Field(default=0, ge=0)


class DrugRuleOut(DrugRuleCreate):
    id: int
    # 出参不带必填校验（P1-110）：修之前存进去的空串行要原样读出来，而不是让整个清单 500
    drug_code: str = Field(max_length=64)
    # 出参不要求有限值（P1-92）：PG 的浮点/金额列存得下 NaN，存量坏值要读成 null，而不是让整个响应 500
    max_daily_dose: float
    ddd: float = 0
    # 停用标记：停用的规则不参与审方与点评，但保留在库里可回溯
    active: bool = True

    model_config = {"from_attributes": True}


class PrescriptionItemIn(BaseModel):
    # 药品必填（P1-110）：原先编码为空串照收，没有规则对得上，这张处方自动审核通过（修前实测 auto_passed）
    drug_code: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    drug_name: str = Field(min_length=1, max_length=128, pattern=NON_BLANK)
    daily_dose: FiniteFloat = Field(gt=0)
    days: int = Field(default=1, ge=1, le=INT4_MAX)


class PrescriptionItemOut(PrescriptionItemIn):
    # 出参不要求有限值（P1-92）：PG 的浮点/金额列存得下 NaN，存量坏值要读成 null，而不是让整个响应 500
    daily_dose: float
    # 出参不带必填校验（P1-110）：修之前存进去的空串行要原样读出来，而不是让整个清单 500
    drug_code: str = Field(max_length=64)
    drug_name: str = Field(max_length=128)


class PrescriptionCreate(BaseModel):
    patient_id: int
    org_id: int
    diagnosis_name: str = Field(default="", max_length=256)
    items: list[PrescriptionItemIn] = Field(min_length=1)


class PrescriptionOut(BaseModel):
    id: int
    patient_id: int
    org_id: int
    diagnosis_name: str
    status: str
    review_comment: str
    items: list[PrescriptionItemOut]
    # 块2：非拦截性提示（肝肾功能剂量调整等），不影响审方状态
    advisories: list[str] = []

    model_config = {"from_attributes": True}


class PrescriptionReview(BaseModel):
    approve: bool
    comment: str = ""


class StockUpsert(BaseModel):
    org_id: int
    # 列长 / 列容量（P1-91 / P1-93 第四层）：入库按业务键查出已有库存再累加、改阈值、改药名，原先判据看不见
    # 与批次入库同口径（P1-98）：原先空药品编码照建库存行、空药名把已有库存的药名改空
    drug_code: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    drug_name: str = Field(min_length=1, max_length=128, pattern=NON_BLANK)
    quantity: int = Field(ge=0, le=INT4_MAX)
    threshold: int = Field(default=0, ge=0, le=INT4_MAX)


class StockOut(StockUpsert):
    id: int
    # 出参不带「不能只填空格」（P1-109）：修之前存进去的纯空白行要原样读出来，而不是让整个清单 500
    drug_code: str = Field(min_length=1, max_length=64)
    drug_name: str = Field(min_length=1, max_length=128)

    model_config = {"from_attributes": True}


class TransferCreate(BaseModel):
    drug_code: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    from_org_id: int
    to_org_id: int
    quantity: int = Field(gt=0, le=INT4_MAX)


class ChronicCreate(BaseModel):
    patient_id: int
    # 块1：病种取值改由 ChronicDiseaseType 目录校验（不再硬编码枚举）
    disease: str = Field(min_length=1, max_length=32, pattern=NON_BLANK)
    managed_by_org_id: int
    # 日期真源（P2-55）：原先裸 str，「2026/10/1」「10月1日」照存，之后按字符串比较的逾期判定对它失效；
    # 日期闸门原先只按名字认 `date`，这一格名叫 next_due，一直不在视野里（闸门已补认 `due`）
    next_due: OptionalDateStr = ""


class ChronicOut(ChronicCreate):
    id: int
    level: int
    # 出参不带入参的日历校验（P1-63）：换成日期真源之前存进去的「2026/10/1」要原样读出来，而不是让响应 500
    next_due: str = ""
    # 出参不带「不能只填空格」（P1-109）：修之前存进去的纯空白行要原样读出来，而不是让整个清单 500
    disease: str = Field(min_length=1, max_length=32)

    model_config = {"from_attributes": True}


class FollowUpCreate(BaseModel):
    # 生理测量值不收非正数（P1-101）：原先 0 / 负数照收——一次测量失败（设备回 0）就把 3 级高危降成 1 级
    # 「控制良好」、上转建议随之消失（分级按「越高越危」比阈值）。上界与住院体征 `clinical_docs.VitalIn` 同口径
    sbp: FiniteFloat | None = Field(default=None, gt=0, le=300)
    dbp: FiniteFloat | None = Field(default=None, gt=0, le=200)
    glucose: FiniteFloat | None = Field(default=None, gt=0)
    # 块1：通用指标（非血压血糖类），如 {"adherence_score": 4, "cat_score": 22}
    metrics: dict[str, FiniteFloat] = Field(default_factory=dict)
    guidance: str = Field(default="", max_length=1024)
    next_due: OptionalDateStr = ""  # 日期真源（P2-55），同 ChronicCreate.next_due


class FollowUpOut(FollowUpCreate):
    id: int
    chronic_id: int
    next_due: str = ""  # 出参不带入参的日历校验（P1-63），同 ChronicOut.next_due
    # 出参不要求有限值（P1-92）：PG 的浮点/金额列存得下 NaN，存量坏值要读成 null，而不是让整个响应 500
    sbp: float | None = None
    dbp: float | None = None
    glucose: float | None = None
    metrics: dict[str, float] = Field(default_factory=dict)

    model_config = {"from_attributes": True}


class InfectiousCaseCreate(BaseModel):
    org_id: int
    # 病种必填（P1-110）：原先编码与名称为空串照收，一例不知道是什么病的法定传染病报告照样进多点预警的分组
    disease_code: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    disease_name: str = Field(min_length=1, max_length=128, pattern=NON_BLANK)
    onset_date: DateStr


class InfectiousCaseOut(InfectiousCaseCreate):
    id: int
    category: str = ""
    # 出参不带入参的日历校验（P1-63）：库里的存量坏日期要原样读出来，而不是让响应 500
    onset_date: str
    # 出参不带必填校验（P1-110）：修之前存进去的空串行要原样读出来，而不是让整个清单 500
    disease_code: str = Field(max_length=64)
    disease_name: str = Field(max_length=128)

    model_config = {"from_attributes": True}


class InfectiousDiseaseOut(BaseModel):
    id: int
    code: str
    name: str
    category: str
    report_hours: int

    model_config = {"from_attributes": True}


class ConsultationCreate(BaseModel):
    patient_id: int
    from_org_id: int
    to_org_id: int
    question: str = Field(min_length=1, max_length=1024, pattern=NON_BLANK)


class ConsultationOut(ConsultationCreate):
    id: int
    status: str
    expert_name: str
    opinion: str
    rating: int
    # 出参不带「不能只填空格」（P1-109）：修之前存进去的纯空白行要原样读出来，而不是让整个清单 500
    question: str = Field(min_length=1, max_length=1024)

    model_config = {"from_attributes": True}


class ConsultationAccept(BaseModel):
    expert_name: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)


class ConsultationComplete(BaseModel):
    opinion: str = Field(min_length=1, max_length=2048, pattern=NON_BLANK)


class ConsultationRate(BaseModel):
    rating: int = Field(ge=1, le=5)


class ContractCreate(BaseModel):
    patient_id: int
    org_id: int
    doctor_name: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    package: str = Field(default="basic", pattern="^(basic|standard|premium)$")
    signed_date: OptionalDateStr = ""


class ContractOut(ContractCreate):
    id: int
    status: str
    # 出参不带入参的日历校验（P1-63）：库里的存量坏日期要原样读出来，而不是让响应 500
    signed_date: str = ""
    # 出参不带「不能只填空格」（P1-109）：修之前存进去的纯空白行要原样读出来，而不是让整个清单 500
    doctor_name: str = Field(min_length=1, max_length=64)

    model_config = {"from_attributes": True}


class ContractServiceCreate(BaseModel):
    service_type: str = Field(pattern="^(visit|consult|followup|referral)$")
    note: str = Field(default="", max_length=512)


class ContractServiceOut(ContractServiceCreate):
    id: int
    contract_id: int

    model_config = {"from_attributes": True}


class SlotCreate(BaseModel):
    org_id: int
    resource_type: str = Field(pattern="^(outpatient|exam|lab)$")
    resource_name: str = Field(min_length=1, max_length=128, pattern=NON_BLANK)
    # ⑨便捷寻医：门诊号源挂医师档案。检查/检验号源不对应某位医师，故可空。
    employee_id: int | None = None
    slot_date: DateStr
    slot_time: str = Field(default="", max_length=16)
    capacity: int = Field(default=1, ge=1, le=INT4_MAX)


class SlotOut(SlotCreate):
    id: int
    booked: int
    # 出参不带入参的日历校验（P1-63）：库里的存量坏日期要原样读出来，而不是让响应 500
    slot_date: str
    # 出参不带「不能只填空格」（P1-109）：修之前存进去的纯空白行要原样读出来，而不是让整个清单 500
    resource_name: str = Field(min_length=1, max_length=128)

    model_config = {"from_attributes": True}


class AppointmentCreate(BaseModel):
    slot_id: int
    patient_id: int


class AppointmentOut(AppointmentCreate):
    id: int
    status: str

    model_config = {"from_attributes": True}


class BatchCreate(BaseModel):
    batch_no: str = Field(min_length=1, max_length=32, pattern=NON_BLANK)
    center_org_id: int
    item_name: str = Field(min_length=1, max_length=128, pattern=NON_BLANK)
    quantity: int = Field(gt=0, le=INT4_MAX)


class BatchOut(BatchCreate):
    id: int
    status: str
    dispatched_to_org_id: int | None
    # 出参不带「不能只填空格」（P1-109）：修之前存进去的纯空白行要原样读出来，而不是让整个清单 500
    batch_no: str = Field(min_length=1, max_length=32)
    item_name: str = Field(min_length=1, max_length=128)

    model_config = {"from_attributes": True}


class WasteCreate(BaseModel):
    org_id: int
    waste_type: str = Field(pattern="^(infectious|sharp|pathological|pharmaceutical|chemical)$")
    weight_kg: FiniteFloat = Field(gt=0)
    collected_date: DateStr


class WasteOut(WasteCreate):
    id: int
    status: str
    handler_name: str

    model_config = {"from_attributes": True}


class WasteHandover(BaseModel):
    handler_name: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
