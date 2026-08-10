"""对接适配层：HL7 v2 / FHIR R4 入站转换与出站导出（对接规范 M3-M4）。

- POST /api/integration/hl7v2/patient      简化 HL7 v2 ADT 消息 → 患者建档（EMPI 幂等）
- POST /api/integration/fhir/Patient       FHIR R4 Patient 资源 → 患者建档
- POST /api/integration/fhir/Observation   FHIR R4 Observation（血压/血糖）→ 慢病随访
- GET  /api/integration/fhir/Patient/{ehc_no}  患者档案导出为 FHIR R4 Patient
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_roles
from ..models import ChronicPatient, FollowUp, Patient
from ..schemas import FollowUpCreate, PatientOut
from .chronic import _evaluate_level
from .patients import _generate_ehc_no

router = APIRouter(
    prefix="/api/integration",
    tags=["对接适配层"],
    dependencies=[Depends(require_roles("operator"))],
)

_GENDER_HL7 = {"M": "男", "F": "女"}
_GENDER_FHIR = {"male": "男", "female": "女"}
_GENDER_TO_FHIR = {"男": "male", "女": "female"}

ID_CARD_SYSTEM = "urn:oid:2.16.156.10011.1.3"  # 中国居民身份证号 OID
EHC_SYSTEM = "urn:medplat:ehc"


class Hl7Message(BaseModel):
    message: str = Field(min_length=1, description="HL7 v2 ADT 消息原文（管道分隔）")


def _upsert_patient(db: Session, data: dict) -> tuple[Patient, bool]:
    """按身份证号幂等建档：已存在返回既有档案。"""
    existing = db.query(Patient).filter(Patient.id_card == data["id_card"]).first()
    if existing:
        return existing, False
    patient = Patient(ehc_no=_generate_ehc_no(db), **data)
    db.add(patient)
    db.commit()
    db.refresh(patient)
    return patient, True


@router.post("/hl7v2/patient", status_code=201)
def hl7v2_patient(body: Hl7Message, db: Session = Depends(get_db)):
    """解析简化 HL7 v2 ADT 消息：取 PID 段建档。

    字段约定（PID 段管道分隔）：PID-3 身份证号、PID-5 姓名（FN^GN 或纯文本）、
    PID-7 出生日期（YYYYMMDD）、PID-8 性别（M/F）、PID-13 联系电话。
    """
    lines = [ln.strip() for ln in body.message.replace("\r", "\n").split("\n") if ln.strip()]
    if not any(ln.startswith("MSH|") for ln in lines):
        raise HTTPException(status_code=422, detail="缺少 MSH 消息头段")
    pid_line = next((ln for ln in lines if ln.startswith("PID|")), None)
    if pid_line is None:
        raise HTTPException(status_code=422, detail="缺少 PID 患者标识段")

    fields = pid_line.split("|")

    def field(i: int) -> str:
        return fields[i] if i < len(fields) else ""

    id_card = field(3).split("^")[0].strip()
    name = field(5).replace("^", "").strip()
    birth_raw = field(7).strip()
    gender = _GENDER_HL7.get(field(8).strip(), "未知")
    phone = field(13).split("^")[0].strip()

    if not id_card or len(id_card) < 15:
        raise HTTPException(status_code=422, detail="PID-3 身份证号缺失或格式不正确")
    if not name:
        raise HTTPException(status_code=422, detail="PID-5 患者姓名缺失")

    birth_date = ""
    if len(birth_raw) >= 8 and birth_raw[:8].isdigit():
        birth_date = f"{birth_raw[:4]}-{birth_raw[4:6]}-{birth_raw[6:8]}"

    patient, created = _upsert_patient(
        db,
        {
            "name": name,
            "id_card": id_card,
            "gender": gender,
            "birth_date": birth_date,
            "phone": phone,
        },
    )
    return {"created": created, "patient": PatientOut.model_validate(patient).model_dump()}


@router.post("/fhir/Patient", status_code=201)
def fhir_patient(resource: dict, db: Session = Depends(get_db)):
    """FHIR R4 Patient 资源入站：identifier（身份证）+ name + gender + birthDate + telecom。"""
    if resource.get("resourceType") != "Patient":
        raise HTTPException(status_code=422, detail="resourceType 必须为 Patient")

    id_card = ""
    for ident in resource.get("identifier", []):
        if ident.get("value"):
            id_card = ident["value"]
            if ident.get("system") == ID_CARD_SYSTEM:
                break
    if not id_card or len(id_card) < 15:
        raise HTTPException(status_code=422, detail="identifier 中缺少有效身份证号")

    names = resource.get("name", [])
    name = ""
    if names:
        name = names[0].get("text") or "".join(
            [names[0].get("family", "")] + names[0].get("given", [])
        )
    if not name:
        raise HTTPException(status_code=422, detail="name 缺失")

    phone = ""
    for telecom in resource.get("telecom", []):
        if telecom.get("system") == "phone" and telecom.get("value"):
            phone = telecom["value"]
            break

    patient, created = _upsert_patient(
        db,
        {
            "name": name,
            "id_card": id_card,
            "gender": _GENDER_FHIR.get(resource.get("gender", ""), "未知"),
            "birth_date": resource.get("birthDate", ""),
            "phone": phone,
        },
    )
    return {"created": created, "patient": PatientOut.model_validate(patient).model_dump()}


# LOINC 编码 → 随访指标字段
_LOINC_FIELDS = {"8480-6": "sbp", "8462-4": "dbp", "2339-0": "glucose"}
# 指标 → 慢病病种（用于定位随访归属档案）
_FIELD_DISEASE = {"sbp": "hypertension", "dbp": "hypertension", "glucose": "diabetes"}


@router.post("/fhir/Observation", status_code=201)
def fhir_observation(resource: dict, db: Session = Depends(get_db)):
    """FHIR R4 Observation 入站：血压（LOINC 8480-6/8462-4）或血糖（2339-0）→ 慢病随访。

    subject.reference 形如 Patient/{ehc_no}；component 或 valueQuantity 提供数值。
    """
    if resource.get("resourceType") != "Observation":
        raise HTTPException(status_code=422, detail="resourceType 必须为 Observation")

    reference = (resource.get("subject") or {}).get("reference", "")
    if not reference.startswith("Patient/"):
        raise HTTPException(status_code=422, detail="subject.reference 必须为 Patient/{ehc_no}")
    ehc_no = reference.split("/", 1)[1]
    patient = db.query(Patient).filter(Patient.ehc_no == ehc_no).first()
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")

    def loinc_code(codeable: dict) -> str:
        for coding in (codeable or {}).get("coding", []):
            if coding.get("code"):
                return coding["code"]
        return ""

    values: dict[str, float] = {}
    for comp in resource.get("component", []):
        field_name = _LOINC_FIELDS.get(loinc_code(comp.get("code")))
        quantity = (comp.get("valueQuantity") or {}).get("value")
        if field_name and quantity is not None:
            values[field_name] = float(quantity)
    top_field = _LOINC_FIELDS.get(loinc_code(resource.get("code")))
    top_value = (resource.get("valueQuantity") or {}).get("value")
    if top_field and top_value is not None and top_field not in values:
        values[top_field] = float(top_value)

    if not values:
        raise HTTPException(status_code=422, detail="未识别到支持的观测指标（血压/血糖 LOINC）")

    disease = _FIELD_DISEASE[next(iter(values))]
    chronic = (
        db.query(ChronicPatient)
        .filter(ChronicPatient.patient_id == patient.id, ChronicPatient.disease == disease)
        .first()
    )
    if chronic is None:
        raise HTTPException(status_code=404, detail=f"该患者无 {disease} 慢病档案，无法归档随访")

    followup_in = FollowUpCreate(**values, guidance="HL7/FHIR 对接自动归档")
    followup = FollowUp(chronic_id=chronic.id, **followup_in.model_dump())
    new_level = _evaluate_level(chronic.disease, followup_in)
    if new_level is not None:
        chronic.level = new_level
    db.add(followup)
    db.commit()
    db.refresh(followup)
    return {
        "followup_id": followup.id,
        "chronic_id": chronic.id,
        "disease": disease,
        "values": values,
        "level": chronic.level,
    }


@router.get("/fhir/Patient/{ehc_no}")
def export_fhir_patient(ehc_no: str, db: Session = Depends(get_db)):
    """患者档案出站：导出 FHIR R4 Patient 资源。"""
    patient = db.query(Patient).filter(Patient.ehc_no == ehc_no).first()
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    return {
        "resourceType": "Patient",
        "id": patient.ehc_no,
        "identifier": [
            {"system": EHC_SYSTEM, "value": patient.ehc_no},
            {"system": ID_CARD_SYSTEM, "value": patient.id_card},
        ],
        "name": [{"text": patient.name}],
        "gender": _GENDER_TO_FHIR.get(patient.gender, "unknown"),
        "birthDate": patient.birth_date,
        "telecom": (
            [{"system": "phone", "value": patient.phone}] if patient.phone else []
        ),
    }
