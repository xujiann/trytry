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
    level: str = Field(pattern="^(county|township|village)$")
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

    model_config = {"from_attributes": True}


class ExamReportCreate(BaseModel):
    finding: str = ""
    conclusion: str
    critical: bool = False
    reported_by: str = ""


class ExamReportOut(ExamReportCreate):
    id: int
    request_id: int

    model_config = {"from_attributes": True}


class DrugRuleCreate(BaseModel):
    drug_code: str
    max_daily_dose: float = Field(gt=0)
    dose_unit: str = "mg"
    note: str = ""


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
    disease: str = Field(pattern="^(hypertension|diabetes|copd)$")
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

    model_config = {"from_attributes": True}
