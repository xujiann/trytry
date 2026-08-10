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
