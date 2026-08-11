from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str


class OrganizationCreate(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    org_type: str = Field(pattern="^(lead_hospital|township|village|public_health)$")
    # L-4：city=市级协作医院（市级互认项目适用），county/township/village=县域层级
    level: str = Field(pattern="^(city|county|township|village)$")
    parent_id: int | None = None
    address: str = ""


class OrganizationOut(OrganizationCreate):
    id: int

    model_config = {"from_attributes": True}


class PatientCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    id_card: str = Field(min_length=15, max_length=18)
    gender: str = "未知"
    birth_date: str = ""
    phone: str = ""


class PatientOut(PatientCreate):
    id: int
    ehc_no: str

    model_config = {"from_attributes": True}


class CodeEntryCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=256)


class CodeEntryOut(CodeEntryCreate):
    id: int
    system_id: int

    model_config = {"from_attributes": True}


class ReferralCreate(BaseModel):
    patient_id: int
    from_org_id: int
    to_org_id: int
    direction: str = Field(pattern="^(up|down)$")
    reason: str = ""


class ReferralOut(ReferralCreate):
    id: int
    status: str

    model_config = {"from_attributes": True}


class ReferralStatusUpdate(BaseModel):
    status: str = Field(pattern="^(accepted|completed|rejected)$")


class EncounterCreate(BaseModel):
    patient_id: int
    org_id: int
    doctor_name: str = ""
    encounter_type: str = Field(default="outpatient", pattern="^(outpatient|inpatient)$")
    diagnosis_code: str = ""
    diagnosis_name: str = ""
    summary: str = ""


class EncounterOut(EncounterCreate):
    id: int

    model_config = {"from_attributes": True}


class ExamRequestCreate(BaseModel):
    patient_id: int
    from_org_id: int
    center_type: str = Field(pattern="^(imaging|ecg|lab|pathology)$")
    item_code: str
    item_name: str
    clinical_info: str = ""
    # 互认：引用既往已报告申请的 id 即互认其结果；填写理由则记录不互认原因
    accept_recognition_of: int | None = None
    recognition_declined_reason: str = ""


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
    finding: str = ""
    conclusion: str
    critical: bool = False
    reported_by: str = ""


class ExamReportOut(ExamReportCreate):
    id: int
    request_id: int
    critical_status: str = ""

    model_config = {"from_attributes": True}


class RecognitionItemCreate(BaseModel):
    item_code: str = Field(min_length=1, max_length=64)
    item_name: str = Field(min_length=1, max_length=128)
    center_type: str = Field(pattern="^(imaging|ecg|lab|pathology)$")
    mutual_scope: str = Field(default="county", pattern="^(county|city)$")
    active: bool = True


class RecognitionItemUpdate(BaseModel):
    item_name: str | None = None
    center_type: str | None = Field(default=None, pattern="^(imaging|ecg|lab|pathology)$")
    mutual_scope: str | None = Field(default=None, pattern="^(county|city)$")
    active: bool | None = None


class RecognitionItemOut(RecognitionItemCreate):
    id: int

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
    drug_code: str
    max_daily_dose: float = Field(gt=0)
    dose_unit: str = "mg"
    note: str = ""
    # 相互作用冲突药品编码，逗号分隔（如 "D002,D003"）
    interactions: str = ""
    # 禁忌诊断关键词，逗号分隔（如 "消化性溃疡,出血"）
    contraindicated_diagnoses: str = ""
    # 特殊人群，逗号分隔，取值 pregnant/child/elderly
    special_groups: str = ""
    # 肝肾功能提示（不拦截，随处方返回供剂量调整参考）
    renal_hepatic_note: str = ""
    # 处方点评要点（事后点评规则化依据）
    review_points: str = ""


class DrugRuleOut(DrugRuleCreate):
    id: int

    model_config = {"from_attributes": True}


class PrescriptionItemIn(BaseModel):
    drug_code: str
    drug_name: str
    daily_dose: float = Field(gt=0)
    days: int = Field(default=1, ge=1)


class PrescriptionCreate(BaseModel):
    patient_id: int
    org_id: int
    diagnosis_name: str = ""
    items: list[PrescriptionItemIn] = Field(min_length=1)


class PrescriptionOut(BaseModel):
    id: int
    patient_id: int
    org_id: int
    diagnosis_name: str
    status: str
    review_comment: str
    items: list[PrescriptionItemIn]
    # 块2：非拦截性提示（肝肾功能剂量调整等），不影响审方状态
    advisories: list[str] = []

    model_config = {"from_attributes": True}


class PrescriptionReview(BaseModel):
    approve: bool
    comment: str = ""


class StockUpsert(BaseModel):
    org_id: int
    drug_code: str
    drug_name: str
    quantity: int = Field(ge=0)
    threshold: int = Field(default=0, ge=0)


class StockOut(StockUpsert):
    id: int

    model_config = {"from_attributes": True}


class TransferCreate(BaseModel):
    drug_code: str
    from_org_id: int
    to_org_id: int
    quantity: int = Field(gt=0)


class ChronicCreate(BaseModel):
    patient_id: int
    # 块1：病种取值改由 ChronicDiseaseType 目录校验（不再硬编码枚举）
    disease: str = Field(min_length=1, max_length=32)
    managed_by_org_id: int
    next_due: str = ""


class ChronicOut(ChronicCreate):
    id: int
    level: int

    model_config = {"from_attributes": True}


class FollowUpCreate(BaseModel):
    sbp: float | None = None
    dbp: float | None = None
    glucose: float | None = None
    # 块1：通用指标（非血压血糖类），如 {"adherence_score": 4, "cat_score": 22}
    metrics: dict[str, float] = Field(default_factory=dict)
    guidance: str = ""
    next_due: str = ""


class FollowUpOut(FollowUpCreate):
    id: int
    chronic_id: int

    model_config = {"from_attributes": True}


class InfectiousCaseCreate(BaseModel):
    org_id: int
    disease_code: str
    disease_name: str
    onset_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class InfectiousCaseOut(InfectiousCaseCreate):
    id: int
    category: str = ""

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
    question: str = Field(min_length=1, max_length=1024)


class ConsultationOut(ConsultationCreate):
    id: int
    status: str
    expert_name: str
    opinion: str
    rating: int

    model_config = {"from_attributes": True}


class ConsultationAccept(BaseModel):
    expert_name: str = Field(min_length=1)


class ConsultationComplete(BaseModel):
    opinion: str = Field(min_length=1, max_length=2048)


class ConsultationRate(BaseModel):
    rating: int = Field(ge=1, le=5)


class ContractCreate(BaseModel):
    patient_id: int
    org_id: int
    doctor_name: str = Field(min_length=1)
    package: str = Field(default="basic", pattern="^(basic|standard|premium)$")
    signed_date: str = ""


class ContractOut(ContractCreate):
    id: int
    status: str

    model_config = {"from_attributes": True}


class ContractServiceCreate(BaseModel):
    service_type: str = Field(pattern="^(visit|consult|followup|referral)$")
    note: str = ""


class ContractServiceOut(ContractServiceCreate):
    id: int
    contract_id: int

    model_config = {"from_attributes": True}


class SlotCreate(BaseModel):
    org_id: int
    resource_type: str = Field(pattern="^(outpatient|exam|lab)$")
    resource_name: str = Field(min_length=1)
    slot_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    slot_time: str = ""
    capacity: int = Field(default=1, ge=1)


class SlotOut(SlotCreate):
    id: int
    booked: int

    model_config = {"from_attributes": True}


class AppointmentCreate(BaseModel):
    slot_id: int
    patient_id: int


class AppointmentOut(AppointmentCreate):
    id: int
    status: str

    model_config = {"from_attributes": True}


class BatchCreate(BaseModel):
    batch_no: str = Field(min_length=1, max_length=32)
    center_org_id: int
    item_name: str = Field(min_length=1)
    quantity: int = Field(gt=0)


class BatchOut(BatchCreate):
    id: int
    status: str
    dispatched_to_org_id: int | None

    model_config = {"from_attributes": True}


class WasteCreate(BaseModel):
    org_id: int
    waste_type: str = Field(pattern="^(infectious|sharp|pathological|pharmaceutical|chemical)$")
    weight_kg: float = Field(gt=0)
    collected_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class WasteOut(WasteCreate):
    id: int
    status: str
    handler_name: str

    model_config = {"from_attributes": True}


class WasteHandover(BaseModel):
    handler_name: str = Field(min_length=1)
