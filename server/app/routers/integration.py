"""对接适配层：HL7 v2 / FHIR R4 入站转换与出站导出（对接规范 M3-M4）。

- POST /api/integration/hl7v2/patient      简化 HL7 v2 ADT 消息 → 患者建档（EMPI 幂等）
- POST /api/integration/fhir/Patient       FHIR R4 Patient 资源 → 患者建档
- POST /api/integration/fhir/Observation   FHIR R4 Observation（血压/血糖）→ 慢病随访
- GET  /api/integration/fhir/Patient/{ehc_no}  患者档案导出为 FHIR R4 Patient

M11 交换监控（#26）：
- 每次入站转换落 ExchangeLog（来源系统/消息类型/成功失败/错误详情），
  独立会话写入，业务失败不丢日志；
- 未预期的解析异常统一捕获：落日志后返回 422（不再 500 裸抛）；
- GET /api/integration/exchange-logs 提供日志查询与失败率统计。
"""
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from ..database import SessionLocal, get_db
from ..deps import get_current_user, require_roles
from ..models import ChronicPatient, ExchangeLog, FollowUp, Patient, User
from ..privacy import desensitize, mask_id_card, mask_phone
from ..schemas import FollowUpCreate
from .chronic import _evaluate_level
from .patients import create_patient_idempotent

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


def _log_exchange(
    message_type: str, success: bool, error_detail: str = "", source_system: str = ""
) -> None:
    """交换日志落库：独立会话写入并提交，与业务事务解耦（失败也留痕）。"""
    db = SessionLocal()
    try:
        db.add(
            ExchangeLog(
                source_system=source_system[:64],
                message_type=message_type,
                direction="inbound",
                success=success,
                error_detail=error_detail[:1024],
            )
        )
        db.commit()
    finally:
        db.close()


def _run_inbound(message_type: str, source_system: str, fn):
    """入站转换统一包装：成功/失败均落交换日志；未预期解析异常转 422。"""
    try:
        result = fn()
    except HTTPException as exc:
        _log_exchange(message_type, False, f"{exc.status_code}: {exc.detail}", source_system)
        raise
    except Exception as exc:  # noqa: BLE001 - 解析异常统一捕获落日志
        _log_exchange(message_type, False, f"解析异常: {exc!r}", source_system)
        raise HTTPException(status_code=422, detail="消息解析失败，已记录交换日志") from exc
    _log_exchange(message_type, True, "", source_system)
    return result


def _upsert_patient(db: Session, data: dict) -> tuple[Patient, bool]:
    """按身份证号幂等建档：已存在返回既有档案（并发冲突由唯一约束兜底，M6）。"""
    return create_patient_idempotent(db, data)


@router.post("/hl7v2/patient", status_code=201)
def hl7v2_patient(
    body: Hl7Message,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    x_source_system: str = Header(default=""),
):
    """解析简化 HL7 v2 ADT 消息：取 PID 段建档。

    字段约定（PID 段管道分隔）：PID-3 身份证号、PID-5 姓名（FN^GN 或纯文本）、
    PID-7 出生日期（YYYYMMDD）、PID-8 性别（M/F）、PID-13 联系电话。
    响应中的患者敏感字段按调用者角色统一脱敏（H1）。
    """
    return _run_inbound("hl7v2_patient", x_source_system, lambda: _do_hl7v2_patient(body, db, user))


def _do_hl7v2_patient(body: Hl7Message, db: Session, user: User):
    lines = [ln.strip() for ln in body.message.replace("\r", "\n").split("\n") if ln.strip()]
    msh_line = next((ln for ln in lines if ln.startswith("MSH|")), None)
    if msh_line is None:
        raise HTTPException(status_code=422, detail="缺少 MSH 消息头段")
    msh_fields = msh_line.split("|")
    control_id = msh_fields[9] if len(msh_fields) > 9 else ""
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
    # 终审轮（浙#21 消息确认机制）：返回 HL7 ACK 应答（MSA|AA|原消息控制ID）
    ack = _build_ack(control_id)
    return {"created": created, "ack": ack, "patient": desensitize(patient, user).model_dump()}


def _build_ack(control_id: str, code: str = "AA") -> str:
    """构造 HL7 v2 ACK 应答消息：AA=接收成功（浙#21 消息传输确认回执）。"""
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"MSH|^~\\&|MEDPLAT|COUNTY|||{ts}||ACK|{control_id}|P|2.4\rMSA|{code}|{control_id}"


@router.post("/fhir/Patient", status_code=201)
def fhir_patient(
    resource: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    x_source_system: str = Header(default=""),
):
    """FHIR R4 Patient 资源入站：identifier（身份证）+ name + gender + birthDate + telecom。

    响应中的患者敏感字段按调用者角色统一脱敏（H1）。
    """
    return _run_inbound("fhir_patient", x_source_system, lambda: _do_fhir_patient(resource, db, user))


def _do_fhir_patient(resource: dict, db: Session, user: User):
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
    return {"created": created, "patient": desensitize(patient, user).model_dump()}


# LOINC 编码 → 随访指标字段
_LOINC_FIELDS = {"8480-6": "sbp", "8462-4": "dbp", "2339-0": "glucose"}
# 指标 → 慢病病种（用于定位随访归属档案）
_FIELD_DISEASE = {"sbp": "hypertension", "dbp": "hypertension", "glucose": "diabetes"}


@router.post("/fhir/Observation", status_code=201)
def fhir_observation(
    resource: dict, db: Session = Depends(get_db), x_source_system: str = Header(default="")
):
    """FHIR R4 Observation 入站：血压（LOINC 8480-6/8462-4）或血糖（2339-0）→ 慢病随访。

    subject.reference 形如 Patient/{ehc_no}；component 或 valueQuantity 提供数值。
    """
    return _run_inbound(
        "fhir_observation", x_source_system, lambda: _do_fhir_observation(resource, db)
    )


def _do_fhir_observation(resource: dict, db: Session):
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
    new_level = _evaluate_level(db, chronic.disease, followup_in)
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
def export_fhir_patient(
    ehc_no: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """患者档案出站：导出 FHIR R4 Patient 资源。

    H1 整改：出站导出统一走脱敏——非 admin 角色身份证号/电话一律掩码，
    与 /api/patients 同角色返回口径一致；明文导出仅限 admin（审计留痕）。
    """
    patient = db.query(Patient).filter(Patient.ehc_no == ehc_no).first()
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    id_card = patient.id_card if user.role == "admin" else mask_id_card(patient.id_card)
    phone = patient.phone if user.role == "admin" else mask_phone(patient.phone)
    return {
        "resourceType": "Patient",
        "id": patient.ehc_no,
        "identifier": [
            {"system": EHC_SYSTEM, "value": patient.ehc_no},
            {"system": ID_CARD_SYSTEM, "value": id_card},
        ],
        "name": [{"text": patient.name}],
        "gender": _GENDER_TO_FHIR.get(patient.gender, "unknown"),
        "birthDate": patient.birth_date,
        "telecom": ([{"system": "phone", "value": phone}] if phone else []),
    }


# ---------- M11 交换监控 ----------


@router.get("/exchange-logs")
def exchange_logs(
    message_type: str | None = None,
    success: bool | None = None,
    source_system: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """交换日志监控：明细查询 + 总量/失败率/按消息类型统计。"""
    q = db.query(ExchangeLog)
    if message_type:
        q = q.filter(ExchangeLog.message_type == message_type)
    if success is not None:
        q = q.filter(ExchangeLog.success.is_(success))
    if source_system:
        q = q.filter(ExchangeLog.source_system == source_system)
    logs = q.order_by(ExchangeLog.id.desc()).limit(min(max(limit, 1), 500)).all()

    total = db.query(func.count(ExchangeLog.id)).scalar() or 0
    failed = (
        db.query(func.count(ExchangeLog.id)).filter(ExchangeLog.success.is_(False)).scalar() or 0
    )
    by_type_rows = (
        db.query(
            ExchangeLog.message_type,
            func.count(ExchangeLog.id).label("n"),
            func.sum(case((ExchangeLog.success.is_(False), 1), else_=0)).label("failed"),
        )
        .group_by(ExchangeLog.message_type)
        .all()
    )
    return {
        "total": total,
        "failed": failed,
        "failure_rate_pct": round(failed * 100.0 / total, 2) if total else 0.0,
        "by_type": [
            {
                "message_type": r.message_type,
                "count": r.n,
                "failed": int(r.failed or 0),
                "failure_rate_pct": round((r.failed or 0) * 100.0 / r.n, 2) if r.n else 0.0,
            }
            for r in by_type_rows
        ],
        "logs": [
            {
                "id": log.id,
                "source_system": log.source_system,
                "message_type": log.message_type,
                "direction": log.direction,
                "success": log.success,
                "error_detail": log.error_detail,
                "at": log.created_at.isoformat(),
            }
            for log in logs
        ],
    }
