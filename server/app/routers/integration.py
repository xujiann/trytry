"""对接适配层：HL7 v2 / FHIR R4 入站转换与出站导出（对接规范 M3-M4，工程包 I1 加深）。

- POST /api/integration/hl7v2/patient      简化 HL7 v2 ADT 消息 → 患者建档（EMPI 幂等）
- POST /api/integration/hl7v2/adt          ADT 事件细分：A01 入院/A03 出院/A04 建档/A08 更新
- POST /api/integration/hl7v2/oru          ORU^R01 检验结果：OBR+多条 OBX → 回写检查报告
- POST /api/integration/fhir/Patient       FHIR R4 Patient 资源 → 患者建档
- POST /api/integration/fhir/Observation   FHIR R4 Observation（血压/血糖）→ 慢病随访
- POST /api/integration/fhir/DiagnosticReport  FHIR R4 DiagnosticReport → 检查报告回写
- POST /api/integration/fhir/Encounter     FHIR R4 Encounter → 就诊记录入档
- GET  /api/integration/fhir/Patient/{ehc_no}  患者档案导出为 FHIR R4 Patient
- 定时任务 fhir_batch_export（jobs.py）：按增量水位把 Patient/Encounter/ExamReport
  序列化为 FHIR NDJSON 落 upload_dir/fhir_out/（含 manifest），供省平台前置机拉取。

M11 交换监控（#26）：
- 每次入站转换落 ExchangeLog（来源系统/消息类型/成功失败/错误详情），
  独立会话写入，业务失败不丢日志；
- 未预期的解析异常统一捕获：落日志后返回 422（不再 500 裸抛）；
- GET /api/integration/exchange-logs 提供日志查询与失败率统计。
"""
import base64
import json
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from .. import events
from ..clock import now_aware, now_naive
from ..concurrency import upsert_unique
from ..config import settings
from ..visibility import log_patient_access
from ..database import SessionLocal, get_db
from ..deps import get_current_user, require_roles
from ..models import (
    Admission,
    Bed,
    ChronicPatient,
    Encounter,
    ExamReport,
    ExamRequest,
    ExchangeLog,
    FollowUp,
    InpatientOrder,
    Patient,
    SystemParam,
    User,
    Ward,
    utcnow,
)
from ..pii import pii_filter
from ..privacy import desensitize, mask_id_card, mask_phone
from ..schemas import EncounterCreate, ExamReportCreate, FollowUpCreate, PatientOut
from .chronic import _evaluate_level
from .encounters import create_encounter
from .exams import submit_report
from .inpatient import AdmissionCreate, _release_bed, create_admission
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


class Hl7PatientInboundOut(BaseModel):
    """HL7 简化建档回执：patient 按调用者角色脱敏（H1 口径，同 AdtInboundOut 先例）。"""

    created: bool
    ack: str
    patient: PatientOut


@router.post("/hl7v2/patient", status_code=201, response_model=Hl7PatientInboundOut)
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


def parse_hl7v2_patient(message: str) -> tuple[dict, str]:
    """HL7 v2 ADT 消息 → (患者字段字典, 消息控制ID)。

    纯转换逻辑，不触库：入站接口与 ESB 编排 transform 步骤共用同一实现
    （块1：集成平台总线复用本函数，避免解析口径分叉）。
    """
    lines = [ln.strip() for ln in message.replace("\r", "\n").split("\n") if ln.strip()]
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

    return (
        {
            "name": name,
            "id_card": id_card,
            "gender": gender,
            "birth_date": birth_date,
            "phone": phone,
        },
        control_id,
    )


def _do_hl7v2_patient(body: Hl7Message, db: Session, user: User):
    data, control_id = parse_hl7v2_patient(body.message)
    patient, created = _upsert_patient(db, data)
    # 终审轮（浙#21 消息确认机制）：返回 HL7 ACK 应答（MSA|AA|原消息控制ID）
    ack = _build_ack(control_id)
    return {"created": created, "ack": ack, "patient": desensitize(patient, user).model_dump()}


def _build_ack(control_id: str, code: str = "AA") -> str:
    """构造 HL7 v2 ACK 应答消息：AA=接收成功（浙#21 消息传输确认回执）。"""

    ts = now_aware().strftime("%Y%m%d%H%M%S")
    return f"MSH|^~\\&|MEDPLAT|COUNTY|||{ts}||ACK|{control_id}|P|2.4\rMSA|{code}|{control_id}"


class FhirPatientInboundOut(BaseModel):
    """FHIR Patient 建档回执：patient 按调用者角色脱敏（H1 口径）。"""

    created: bool
    patient: PatientOut


@router.post("/fhir/Patient", status_code=201, response_model=FhirPatientInboundOut)
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


def parse_fhir_patient(resource: dict) -> dict:
    """FHIR R4 Patient 资源 → 患者字段字典（纯转换，入站接口与 ESB 编排共用）。"""
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

    return {
        "name": name,
        "id_card": id_card,
        "gender": _GENDER_FHIR.get(resource.get("gender", ""), "未知"),
        "birth_date": resource.get("birthDate", ""),
        "phone": phone,
    }


def _do_fhir_patient(resource: dict, db: Session, user: User):
    patient, created = _upsert_patient(db, parse_fhir_patient(resource))
    return {"created": created, "patient": desensitize(patient, user).model_dump()}


# LOINC 编码 → 随访指标字段
_LOINC_FIELDS = {"8480-6": "sbp", "8462-4": "dbp", "2339-0": "glucose"}
# 指标 → 慢病病种（用于定位随访归属档案）
_FIELD_DISEASE = {"sbp": "hypertension", "dbp": "hypertension", "glucose": "diabetes"}


class FhirObservationInboundOut(BaseModel):
    """Observation 入站归档回执：`values` 的产地都经 `float(quantity)`，恒 float。"""

    followup_id: int
    chronic_id: int
    disease: str
    values: dict[str, float]
    level: int


@router.post("/fhir/Observation", status_code=201, response_model=FhirObservationInboundOut)
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

    def loinc_code(codeable: dict | None) -> str:
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

    # `values` 是运行期按 LOINC 映射拼出来的字段字典，键名在类型上不可知；
    # pydantic 会做校验，缺字段/多字段都会在这里报 422，不会静默走下去。
    followup_in = FollowUpCreate(**cast(Any, values), guidance="HL7/FHIR 对接自动归档")
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


# FHIR R4 Patient 是**外部标准形状**（identifier/name/telecom 皆为标准定义的嵌套
# 数组），照 workflows.nodes 先例宽 dict 透传——给国际标准建窄模型等于替 HL7 另立
# 规格；当前导出的 7 键字段面由 test_integration_contract.py 逐键钉住。
@router.get("/fhir/Patient/{ehc_no}", response_model=dict[str, Any])
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
    # 先按"与 360 同级"加了关系判定，两条既有用例当场变红——**判断错了**：
    # 这条是出站对接接口，调用方是区域平台/接口引擎那一侧的对接账号，
    # 它按设计就要能导出全县任意患者，天然没有"业务关系"可言。
    # 用关系判定卡它，等于把对接功能关掉。
    #
    # 所以改回"可问责而非可阻断"：不拦，但每一次导出都留痕。
    # 遗留项（已登记）：真正对症的做法是给对接账号单独一类身份，
    # 并声明它的导出范围（哪个区域、哪些字段），而不是复用 operator 这个人的角色。
    # 平台现在没有这类账号，本批不造。
    log_patient_access(db, user, patient.id, "fhir_export", "export")
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


class ExchangeLogTypeStatOut(BaseModel):
    """按消息类型统计行：count/failed 恒 int（COUNT 与 `int(x or 0)`），
    failure_rate_pct 恒 float（真除法与兜底字面量 0.0 两条产地都是浮点）。"""

    message_type: str
    count: int
    failed: int
    failure_rate_pct: float


class ExchangeLogEntryOut(BaseModel):
    id: int
    source_system: str
    message_type: str
    direction: str
    success: bool
    error_detail: str
    at: str


class ExchangeLogsOut(BaseModel):
    """交换监控回执：`total`/`failed`/`by_type` 恒为全量口径，过滤参数只作用于
    `logs` 明细（test_integration_contract.py 专门钉过这一点）。"""

    total: int
    failed: int
    failure_rate_pct: float
    by_type: list[ExchangeLogTypeStatOut]
    logs: list[ExchangeLogEntryOut]


@router.get("/exchange-logs", response_model=ExchangeLogsOut)
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


# ---------- 工程包 I1：HL7 v2 入站深度（ADT 事件细分 + ORU 检验结果） ----------

# 受理的 ADT 事件白名单：其余事件（A02 转科、A11 撤销……）平台暂无对应动作，明确 422 拒收
_ADT_EVENTS = {"A01": "入院", "A03": "出院", "A04": "挂号建档", "A08": "信息更新"}
# OBX-8 异常标志：H/L 偏高偏低，A 异常，HH/LL 危急高/低（判危急值，进危急值闭环）
_ABNORMAL_FLAGS = {"H", "L", "A", "HH", "LL"}
_CRITICAL_FLAGS = {"HH", "LL"}


def _hl7_segments(message: str) -> list[str]:
    return [ln.strip() for ln in message.replace("\r", "\n").split("\n") if ln.strip()]


def _hl7_event(message: str) -> str:
    """MSH-9 消息类型（如 ADT^A01 / ORU^R01），取前两个组件；解析不了返回空串。"""
    msh = next((ln for ln in _hl7_segments(message) if ln.startswith("MSH|")), None)
    if msh is None:
        return ""
    fields = msh.split("|")
    raw = fields[8] if len(fields) > 8 else ""
    return "^".join(raw.split("^")[:2]).strip()


def _hl7_control_id(message: str) -> str:
    msh = next((ln for ln in _hl7_segments(message) if ln.startswith("MSH|")), None)
    fields = msh.split("|") if msh else []
    return fields[9] if len(fields) > 9 else ""


def _hl7_field(segment: str, index: int) -> str:
    fields = segment.split("|")
    return fields[index] if index < len(fields) else ""


class AdtInboundOut(BaseModel):
    """ADT 入站结果契约：patient 按调用者角色脱敏（H1 口径）。"""

    event: str
    event_name: str
    ack: str
    created: bool
    patient: PatientOut
    admission_id: int | None = None
    encounter_id: int | None = None
    detail: str


@router.post("/hl7v2/adt", status_code=201, response_model=AdtInboundOut)
def hl7v2_adt(
    body: Hl7Message,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    x_source_system: str = Header(default=""),
):
    """HL7 v2 ADT 入站（事件细分）：A01 入院 / A03 出院 / A04 挂号建档 / A08 信息更新。

    - **白名单**：仅上述四类事件，其它 ADT 事件与非 ADT 消息一律 422 明确拒收；
    - **A04**：与既有 /hl7v2/patient 等价（按 PID-3 身份证号幂等建档）；
    - **A01**：PID 建档/定位患者 + PV1-3（`病区^房间^床号`）定位病区床位，
      复用住院登记路由逻辑（占床原子分配、重复在院 409、住院 Encounter 同步入档）；
      PV1-7 主治医师、DG1 诊断一并落档；
    - **A03**：出院镜像同步——停止执行中医嘱、释放床位、置 discharged 并发布
      出院领域事件。平台侧发起的出院有"病案首页已填写、费用已结清"门禁；
      HIS 推送的 A03 是既成事实的镜像，不设门禁（质量门禁由 HIS 端与病案
      补录流程承担），差异特此写明；
    - **A08**：按 PID-3 定位患者，非空字段（姓名/性别/出生日期/电话）覆盖更新；
      档案不存在按对接规范§四口径 404 拒收（应先以 A04 建档）。

    全部入站（成功/失败）落 ExchangeLog，消息类型 hl7v2_adt_a01 等细分可查。
    """
    event = _hl7_event(body.message)
    log_type = f"hl7v2_{event.replace('^', '_').lower()}" if event else "hl7v2_adt"
    return _run_inbound(
        log_type[:32], x_source_system, lambda: _do_hl7v2_adt(body, db, user, event)
    )


def _adt_out(event: str, ack: str, patient: Patient, user: User, **extra) -> dict:
    return {
        "event": event,
        "event_name": _ADT_EVENTS[event.split("^")[1]],
        "ack": ack,
        "patient": desensitize(patient, user),
        **extra,
    }


def _do_hl7v2_adt(body: Hl7Message, db: Session, user: User, event: str):
    code = event.split("^")[1] if event.startswith("ADT^") and "^" in event else ""
    if code not in _ADT_EVENTS:
        raise HTTPException(
            status_code=422,
            detail=f"不支持的消息类型 {event or '(缺失)'}：本接口仅受理 ADT^A01/A03/A04/A08",
        )
    data, control_id = parse_hl7v2_patient(body.message)
    ack = _build_ack(control_id)

    if code == "A04":  # 挂号建档：现状等价（EMPI 幂等）
        patient, created = _upsert_patient(db, data)
        return _adt_out(
            event, ack, patient, user, created=created,
            detail=f"患者档案{'新建' if created else '已存在'}：{patient.ehc_no}",
        )

    if code == "A08":  # 信息更新：档案必须已存在，非空字段覆盖
        existing = (
            db.query(Patient)
            .filter(pii_filter(Patient.id_card_idx, Patient.id_card, data["id_card"]))
            .first()
        )
        if existing is None:
            raise HTTPException(
                status_code=404, detail="患者档案不存在，A08 更新拒收（请先以 A04 建档）"
            )
        for field_name in ("name", "birth_date", "phone"):
            if data[field_name]:
                setattr(existing, field_name, data[field_name])
        if data["gender"] != "未知":
            existing.gender = data["gender"]
        db.commit()
        db.refresh(existing)
        return _adt_out(event, ack, existing, user, created=False, detail="患者信息已更新")

    if code == "A01":  # 入院：复用住院登记路由（占床原子分配、重复在院 409）
        patient, created = _upsert_patient(db, data)
        ward_name, bed_no = _parse_pv1_location(body.message)
        doctor_name = _parse_pv1_doctor(body.message)
        diagnosis_name = _parse_dg1(body.message)
        wards = db.query(Ward).filter(Ward.name == ward_name).all()
        if not wards:
            raise HTTPException(status_code=404, detail=f"病区 {ward_name} 不存在")
        if len(wards) > 1:
            raise HTTPException(
                status_code=422, detail=f"病区名 {ward_name} 在多家机构存在，无法定位床位"
            )
        bed = (
            db.query(Bed)
            .filter(Bed.ward_id == wards[0].id, Bed.bed_no == bed_no)
            .first()
        )
        if bed is None:
            raise HTTPException(status_code=404, detail=f"床位 {ward_name}/{bed_no} 不存在")
        admission = create_admission(
            AdmissionCreate(
                patient_id=patient.id,
                ward_id=wards[0].id,
                bed_id=bed.id,
                doctor_name=doctor_name,
                diagnosis_name=diagnosis_name,
            ),
            db,
            user,
        )
        encounter = (
            db.query(Encounter)
            .filter(Encounter.patient_id == patient.id, Encounter.encounter_type == "inpatient")
            .order_by(Encounter.id.desc())
            .first()
        )
        return _adt_out(
            event, ack, patient, user, created=created,
            admission_id=admission["id"],
            encounter_id=encounter.id if encounter else None,
            detail=f"入院登记完成：{ward_name}/{bed_no} 床",
        )

    # A03 出院：镜像同步（不设病案首页/费用门禁，见端点 docstring）
    inpatient = (
        db.query(Patient)
        .filter(pii_filter(Patient.id_card_idx, Patient.id_card, data["id_card"]))
        .first()
    )
    if inpatient is None:
        raise HTTPException(status_code=404, detail="患者档案不存在，A03 出院拒收")
    admission = (
        db.query(Admission)
        .filter(Admission.patient_id == inpatient.id, Admission.status == "admitted")
        .first()
    )
    if admission is None:
        raise HTTPException(status_code=409, detail="该患者无在院记录，A03 出院拒收")
    db.query(InpatientOrder).filter(
        InpatientOrder.admission_id == admission.id, InpatientOrder.status == "active"
    ).update(
        {InpatientOrder.status: "stopped", InpatientOrder.stopped_at: utcnow()},
        synchronize_session=False,
    )
    _release_bed(db, admission.bed_id)
    admission.status = "discharged"
    admission.discharged_at = utcnow()
    events.publish(db, events.ADMISSION_DISCHARGED, {
        "admission_id": admission.id,
        "patient_id": admission.patient_id,
        "org_id": admission.org_id,
        "diagnosis_name": admission.diagnosis_name or "",
        "discharged_on": admission.discharged_at.date().isoformat(),
    })
    db.commit()
    return _adt_out(
        event, ack, inpatient, user, created=False,
        admission_id=admission.id, detail="出院镜像同步完成（床位已释放、执行中医嘱已停止）",
    )


def _parse_pv1_location(message: str) -> tuple[str, str]:
    """PV1-3 就诊位置 `病区^房间^床号` → (病区名, 床号)。"""
    pv1 = next((s for s in _hl7_segments(message) if s.startswith("PV1|")), None)
    if pv1 is None:
        raise HTTPException(status_code=422, detail="A01 入院消息缺少 PV1 就诊段")
    parts = _hl7_field(pv1, 3).split("^")
    ward_name = parts[0].strip()
    bed_no = parts[2].strip() if len(parts) > 2 else ""
    if not ward_name or not bed_no:
        raise HTTPException(status_code=422, detail="PV1-3 须为 病区^房间^床号")
    return ward_name, bed_no


def _parse_pv1_doctor(message: str) -> str:
    """PV1-7 主治医师 `工号^姓^名`：取姓名组件（无姓名组件时回落首组件）。"""
    pv1 = next((s for s in _hl7_segments(message) if s.startswith("PV1|")), None)
    parts = _hl7_field(pv1, 7).split("^") if pv1 else [""]
    name = "".join(p.strip() for p in parts[1:3])
    return (name or parts[0].strip())[:64]


def _parse_dg1(message: str) -> str:
    """DG1-3 诊断 `编码^名称`（名称缺失回落 DG1-4 描述，再回落编码）。可缺省。"""
    dg1 = next((s for s in _hl7_segments(message) if s.startswith("DG1|")), None)
    if dg1 is None:
        return ""
    parts = _hl7_field(dg1, 3).split("^")
    name = parts[1].strip() if len(parts) > 1 else ""
    return (name or _hl7_field(dg1, 4).strip() or parts[0].strip())[:256]


class OruInboundOut(BaseModel):
    event: str
    ack: str
    request_id: int
    report_id: int
    obx_count: int
    abnormal_count: int
    critical: bool


@router.post("/hl7v2/oru", status_code=201, response_model=OruInboundOut)
def hl7v2_oru(
    body: Hl7Message,
    db: Session = Depends(get_db),
    x_source_system: str = Header(default=""),
):
    """ORU^R01 检验/检查结果入站：OBR（申请信息）+ 多条 OBX（结果项）回写检查报告。

    - **申请单定位**：OBR-2（下单方单号）即平台申请单号（ExamRequest.id，对接规范
      映射表"检查检验申请→ServiceRequest"）；OBR-2 缺失回退 OBR-3（执行方单号）；
    - **OBX 逐条解析**：标识（OBX-3）/值（OBX-5）/单位（OBX-6）/参考范围（OBX-7）/
      异常标志（OBX-8），逐行拼入报告 finding；异常标志 HH/LL 判**危急值**，
      复用报告发布的危急值闭环（通知→确认→处置留痕）；
    - **找不到申请单一律 404 拒收，不建独立报告**：对接规范§四将 404 定义为
      "引用的资源不存在→检查外键是否先行创建"，且平台报告表与申请单一一对应
      （request_id 唯一非空外键），"无单报告"在数据模型上不存在——对接方应先
      POST /api/exams 创建申请再回传结果；
    - 已出报告的申请单再次回传 → 409（复用报告发布的唯一约束与冲突口径）。

    全部入站落 ExchangeLog（消息类型 hl7v2_oru_r01）。
    """
    event = _hl7_event(body.message)
    log_type = f"hl7v2_{event.replace('^', '_').lower()}" if event else "hl7v2_oru"
    return _run_inbound(
        log_type[:32],
        x_source_system,
        lambda: _do_hl7v2_oru(body, db, event, x_source_system),
    )


def _do_hl7v2_oru(body: Hl7Message, db: Session, event: str, source_system: str):
    if event != "ORU^R01":
        raise HTTPException(
            status_code=422,
            detail=f"不支持的消息类型 {event or '(缺失)'}：本接口仅受理 ORU^R01",
        )
    segments = _hl7_segments(body.message)
    ack = _build_ack(_hl7_control_id(body.message))

    obr = next((s for s in segments if s.startswith("OBR|")), None)
    if obr is None:
        raise HTTPException(status_code=422, detail="缺少 OBR 申请信息段")
    order_no = (_hl7_field(obr, 2) or _hl7_field(obr, 3)).split("^")[0].strip()
    if not order_no.isdigit():
        raise HTTPException(status_code=422, detail="OBR-2/OBR-3 申请单号缺失或非平台单号")
    request = db.get(ExamRequest, int(order_no))
    if request is None:
        raise HTTPException(
            status_code=404,
            detail=f"申请单 {order_no} 不存在，结果拒收（对接规范§四：请先创建检查申请）",
        )

    # PID 一致性核验（可选段）：报文声明的患者与申请单不一致时拒收，防串单
    pid = next((s for s in segments if s.startswith("PID|")), None)
    if pid is not None:
        id_card = _hl7_field(pid, 3).split("^")[0].strip()
        if id_card:
            patient = (
                db.query(Patient)
                .filter(pii_filter(Patient.id_card_idx, Patient.id_card, id_card))
                .first()
            )
            if patient is not None and patient.id != request.patient_id:
                raise HTTPException(status_code=422, detail="PID 患者与申请单患者不一致，结果拒收")

    lines: list[str] = []
    abnormal = 0
    critical = False
    for seg in (s for s in segments if s.startswith("OBX|")):
        code_parts = _hl7_field(seg, 3).split("^")
        label = (code_parts[1].strip() if len(code_parts) > 1 else "") or code_parts[0].strip()
        value = _hl7_field(seg, 5).strip()
        unit = _hl7_field(seg, 6).split("^")[0].strip()
        ref_range = _hl7_field(seg, 7).strip()
        flag = _hl7_field(seg, 8).strip().upper()
        if flag in _ABNORMAL_FLAGS:
            abnormal += 1
        if flag in _CRITICAL_FLAGS:
            critical = True
        line = f"{label}：{value}"
        if unit:
            line += f" {unit}"
        if ref_range:
            line += f"（参考 {ref_range}）"
        if flag:
            line += f" [{flag}]"
        lines.append(line)
    if not lines:
        raise HTTPException(status_code=422, detail="缺少 OBX 结果段")

    item_parts = _hl7_field(obr, 4).split("^")
    item_name = (item_parts[1].strip() if len(item_parts) > 1 else "") or request.item_name
    conclusion = f"{item_name}：共 {len(lines)} 项，异常 {abnormal} 项"
    if critical:
        conclusion += "，含危急值"
    report = submit_report(
        request.id,
        ExamReportCreate(
            finding="\n".join(lines)[:2048],
            conclusion=conclusion[:1024],
            critical=critical,
            reported_by=(source_system or "HL7-ORU")[:64],
        ),
        db,
    )
    return {
        "event": event,
        "ack": ack,
        "request_id": request.id,
        "report_id": report.id,
        "obx_count": len(lines),
        "abnormal_count": abnormal,
        "critical": report.critical,
    }


# ---------- 工程包 I1：FHIR 入站深度（DiagnosticReport / Encounter） ----------

CRITICAL_EXTENSION_URL = "urn:medplat:critical"
_CLASS_TO_ENCOUNTER_TYPE = {"AMB": "outpatient", "IMP": "inpatient"}


class FhirReportInboundOut(BaseModel):
    request_id: int
    report_id: int
    critical: bool
    request_status: str


@router.post("/fhir/DiagnosticReport", status_code=201, response_model=FhirReportInboundOut)
def fhir_diagnostic_report(
    resource: dict, db: Session = Depends(get_db), x_source_system: str = Header(default="")
):
    """FHIR R4 DiagnosticReport 入站 → 检查报告回写（映射表：ExamReport↔DiagnosticReport）。

    - basedOn[0].reference = `ServiceRequest/{申请单id}`（映射表：ExamRequest→ServiceRequest）；
    - conclusion→conclusion；presentedForm[0].data（base64 文本）→finding；
    - 危急值：extension `[{"url": "urn:medplat:critical", "valueBoolean": true}]`——
      映射表 critical→flag 的入站承载（FHIR R4 DiagnosticReport 无标准危急值字段，
      以命名扩展承载，出站导出用同一 URL 对称回写）；
    - 申请单不存在 404 拒收（口径同 ORU：规范§四"引用的资源不存在→先行创建"）；
      已出报告 409。
    """
    return _run_inbound(
        "fhir_diagnostic_report",
        x_source_system,
        lambda: _do_fhir_diagnostic_report(resource, db, x_source_system),
    )


def _do_fhir_diagnostic_report(resource: dict, db: Session, source_system: str):
    if resource.get("resourceType") != "DiagnosticReport":
        raise HTTPException(status_code=422, detail="resourceType 必须为 DiagnosticReport")
    based = resource.get("basedOn") or []
    reference = (based[0] or {}).get("reference", "") if based else ""
    if not reference.startswith("ServiceRequest/"):
        raise HTTPException(
            status_code=422, detail="basedOn[0].reference 必须为 ServiceRequest/{申请单id}"
        )
    request_id = reference.split("/", 1)[1]
    if not request_id.isdigit():
        raise HTTPException(status_code=422, detail="ServiceRequest 引用的申请单号须为数字")
    request = db.get(ExamRequest, int(request_id))
    if request is None:
        raise HTTPException(
            status_code=404,
            detail=f"申请单 {request_id} 不存在，报告拒收（对接规范§四：请先创建检查申请）",
        )
    conclusion = str(resource.get("conclusion") or "").strip()
    if not conclusion:
        raise HTTPException(status_code=422, detail="conclusion 缺失")
    finding = ""
    forms = resource.get("presentedForm") or []
    if forms and forms[0].get("data"):
        try:
            finding = base64.b64decode(forms[0]["data"]).decode("utf-8")
        except Exception:
            raise HTTPException(
                status_code=422, detail="presentedForm[0].data 不是合法的 base64 文本"
            ) from None
    critical = any(
        ext.get("url") == CRITICAL_EXTENSION_URL and ext.get("valueBoolean") is True
        for ext in resource.get("extension") or []
    )
    report = submit_report(
        request.id,
        ExamReportCreate(
            finding=finding[:2048],
            conclusion=conclusion[:1024],
            critical=critical,
            reported_by=(source_system or "FHIR")[:64],
        ),
        db,
    )
    return {
        "request_id": request.id,
        "report_id": report.id,
        "critical": report.critical,
        "request_status": request.status,
    }


class FhirEncounterInboundOut(BaseModel):
    encounter_id: int
    patient_id: int
    org_id: int
    encounter_type: str


@router.post("/fhir/Encounter", status_code=201, response_model=FhirEncounterInboundOut)
def fhir_encounter(
    resource: dict, db: Session = Depends(get_db), x_source_system: str = Header(default="")
):
    """FHIR R4 Encounter 入站 → 就诊记录入档（映射表：Encounter+Condition）。

    - subject.reference = `Patient/{ehc_no}`（与 Observation 入站同口径）；
    - serviceProvider.reference = `Organization/{机构id}`；
    - class.code：AMB→门诊 / IMP→住院（缺省按门诊）；
    - reasonCode[0]：coding[0].code→diagnosis_code（ICD-10）、text→diagnosis_name；
    - participant[0].individual.display→doctor_name。
    复用就诊登记路由逻辑（患者/机构校验 + 领域事件发布），入站落 ExchangeLog。
    """
    return _run_inbound(
        "fhir_encounter", x_source_system, lambda: _do_fhir_encounter(resource, db)
    )


def _do_fhir_encounter(resource: dict, db: Session):
    if resource.get("resourceType") != "Encounter":
        raise HTTPException(status_code=422, detail="resourceType 必须为 Encounter")
    reference = (resource.get("subject") or {}).get("reference", "")
    if not reference.startswith("Patient/"):
        raise HTTPException(status_code=422, detail="subject.reference 必须为 Patient/{ehc_no}")
    patient = db.query(Patient).filter(Patient.ehc_no == reference.split("/", 1)[1]).first()
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    provider = (resource.get("serviceProvider") or {}).get("reference", "")
    org_part = provider.split("/", 1)[1] if provider.startswith("Organization/") else ""
    if not org_part.isdigit():
        raise HTTPException(
            status_code=422, detail="serviceProvider.reference 必须为 Organization/{机构id}"
        )
    cls = resource.get("class") or {}
    class_code = str(cls.get("code", "") if isinstance(cls, dict) else "").upper() or "AMB"
    encounter_type = _CLASS_TO_ENCOUNTER_TYPE.get(class_code)
    if encounter_type is None:
        raise HTTPException(status_code=422, detail="class.code 仅支持 AMB（门诊）/IMP（住院）")
    reasons = resource.get("reasonCode") or []
    codings = (reasons[0].get("coding") or [{}]) if reasons else [{}]
    diagnosis_code = str(codings[0].get("code", ""))[:64]
    diagnosis_name = str(
        (reasons[0].get("text") if reasons else "") or codings[0].get("display", "")
    )[:256]
    participants = resource.get("participant") or []
    doctor_name = str(
        ((participants[0].get("individual") or {}).get("display", "")) if participants else ""
    )[:64]
    encounter = create_encounter(
        EncounterCreate(
            patient_id=patient.id,
            org_id=int(org_part),
            doctor_name=doctor_name,
            encounter_type=encounter_type,
            diagnosis_code=diagnosis_code,
            diagnosis_name=diagnosis_name,
            summary="FHIR Encounter 入站同步",
        ),
        db,
    )
    return {
        "encounter_id": encounter.id,
        "patient_id": encounter.patient_id,
        "org_id": encounter.org_id,
        "encounter_type": encounter.encounter_type,
    }


# ---------- 工程包 I1：FHIR 批量导出（增量水位，省平台前置机拉取） ----------

#: 每资源类型单轮导出上限：分批防单轮长事务/大文件，余量下一轮接着导
FHIR_EXPORT_BATCH_LIMIT = 1000
#: 增量水位（最后已导出主键）落既有 system_params 表（浙#45），不另建表
FHIR_EXPORT_WM_KEYS = {
    "Patient": "fhir_export_wm_patient",
    "Encounter": "fhir_export_wm_encounter",
    "DiagnosticReport": "fhir_export_wm_exam_report",
}


def _fhir_out_dir() -> Path:
    d = Path(settings.upload_dir) / "fhir_out"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _wm_get(db: Session, key: str) -> int:
    row = db.query(SystemParam).filter(SystemParam.key == key).first()
    return int(row.value) if row is not None and str(row.value).isdigit() else 0


def _wm_set(db: Session, key: str, value: int) -> None:
    upsert_unique(
        db,
        SystemParam,
        {"key": key},
        {"value": str(value), "description": "FHIR 批量导出增量水位（最后已导出主键）"},
    )


def fhir_patient_resource(p: Patient) -> dict:
    """Patient → FHIR R4（批量导出用，字段映射见对接规范§二）。

    **明文导出**：目标是省平台前置机的全量对接文件（落 upload_dir，由运维管控，
    与 A9 归档导出同口径），不是工作人员侧接口回显——接口面（含 GET
    /fhir/Patient/{ehc_no}）仍按 H1 走角色脱敏，两者口径刻意不同。
    """
    return {
        "resourceType": "Patient",
        "id": p.ehc_no,
        "identifier": [
            {"system": EHC_SYSTEM, "value": p.ehc_no},
            {"system": ID_CARD_SYSTEM, "value": p.id_card},
        ],
        "name": [{"text": p.name}],
        "gender": _GENDER_TO_FHIR.get(p.gender, "unknown"),
        "birthDate": p.birth_date,
        "telecom": ([{"system": "phone", "value": p.phone}] if p.phone else []),
    }


def fhir_encounter_resource(e: Encounter, ehc_no: str) -> dict:
    """Encounter → FHIR R4 Encounter（+内联 Condition 承载诊断，映射表"Encounter+Condition"）。"""
    resource: dict = {
        "resourceType": "Encounter",
        "id": str(e.id),
        "status": "finished",
        "class": {
            "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
            "code": "IMP" if e.encounter_type == "inpatient" else "AMB",
        },
        "subject": {"reference": f"Patient/{ehc_no}"},
        "serviceProvider": {"reference": f"Organization/{e.org_id}"},
        "period": {"start": e.created_at.isoformat()},
    }
    if e.doctor_name:
        resource["participant"] = [{"individual": {"display": e.doctor_name}}]
    if e.diagnosis_code or e.diagnosis_name:
        resource["contained"] = [
            {
                "resourceType": "Condition",
                "id": "dx",
                "code": {
                    "coding": (
                        [{"system": "http://hl7.org/fhir/sid/icd-10", "code": e.diagnosis_code}]
                        if e.diagnosis_code
                        else []
                    ),
                    "text": e.diagnosis_name,
                },
            }
        ]
        resource["diagnosis"] = [{"condition": {"reference": "#dx"}}]
    return resource


def fhir_diagnostic_report_resource(report: ExamReport, request_id: int, ehc_no: str) -> dict:
    """ExamReport → FHIR R4 DiagnosticReport（conclusion→conclusion、finding→presentedForm、
    critical→urn:medplat:critical 扩展，与入站承载对称）。"""
    return {
        "resourceType": "DiagnosticReport",
        "id": str(report.id),
        "status": "final",
        "basedOn": [{"reference": f"ServiceRequest/{request_id}"}],
        "subject": {"reference": f"Patient/{ehc_no}"},
        "issued": report.reported_at.isoformat(),
        "conclusion": report.conclusion,
        "presentedForm": (
            [
                {
                    "contentType": "text/plain",
                    "data": base64.b64encode(report.finding.encode("utf-8")).decode("ascii"),
                }
            ]
            if report.finding
            else []
        ),
        "extension": [{"url": CRITICAL_EXTENSION_URL, "valueBoolean": report.critical}],
    }


def run_fhir_batch_export(db: Session) -> tuple[int, str]:
    """FHIR 批量导出（供 jobs.fhir_batch_export 调用）：按增量水位导 NDJSON。

    - 三类资源（对接规范§二映射表已实现的子集）：Patient / Encounter /
      DiagnosticReport（ExamReport），每类一个 `{类型}_{时间戳}_{起始id}.ndjson`
      （一行一个资源），落 `upload_dir/fhir_out/`；
    - 水位 = 最后已导出主键，存 system_params（key 见 FHIR_EXPORT_WM_KEYS）：
      只导 `id > 水位` 的增量，导完推进水位——重复执行幂等（无增量即不产文件）；
    - `manifest.jsonl` 每个产出文件追加一行（文件名/资源类型/行数/id 区间/时间），
      前置机按 manifest 拉取；
    - **待办（不假装）**：映射表其余资源（Prescription→MedicationRequest、
      Referral→ServiceRequest、Consultation、CarePlan、Appointment 等）尚未
      纳入批量导出，扩展时在本函数追加资源类型并配套新水位 key。
    """
    out_dir = _fhir_out_dir()
    stamp = now_naive().strftime("%Y%m%d%H%M%S")
    total = 0
    parts: list[str] = []

    def _export(resource_type: str, rows: list[tuple[int, dict]]) -> None:
        nonlocal total
        if not rows:
            return
        filename = f"{resource_type}_{stamp}_{rows[0][0]}.ndjson"
        with (out_dir / filename).open("w", encoding="utf-8") as f:
            for _row_id, resource in rows:
                f.write(json.dumps(resource, ensure_ascii=False) + "\n")
        with (out_dir / "manifest.jsonl").open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "file": filename,
                        "resource_type": resource_type,
                        "rows": len(rows),
                        "from_id": rows[0][0],
                        "to_id": rows[-1][0],
                        "generated_at": now_aware().isoformat(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        _wm_set(db, FHIR_EXPORT_WM_KEYS[resource_type], rows[-1][0])
        total += len(rows)
        parts.append(f"{resource_type} {len(rows)} 条")

    patients = (
        db.query(Patient)
        .filter(Patient.id > _wm_get(db, FHIR_EXPORT_WM_KEYS["Patient"]))
        .order_by(Patient.id)
        .limit(FHIR_EXPORT_BATCH_LIMIT)
        .all()
    )
    _export("Patient", [(p.id, fhir_patient_resource(p)) for p in patients])

    encounters = (
        db.query(Encounter, Patient.ehc_no)
        .join(Patient, Patient.id == Encounter.patient_id)
        .filter(Encounter.id > _wm_get(db, FHIR_EXPORT_WM_KEYS["Encounter"]))
        .order_by(Encounter.id)
        .limit(FHIR_EXPORT_BATCH_LIMIT)
        .all()
    )
    _export(
        "Encounter", [(e.id, fhir_encounter_resource(e, ehc_no)) for e, ehc_no in encounters]
    )

    reports = (
        db.query(ExamReport, ExamRequest.id, Patient.ehc_no)
        .join(ExamRequest, ExamRequest.id == ExamReport.request_id)
        .join(Patient, Patient.id == ExamRequest.patient_id)
        .filter(ExamReport.id > _wm_get(db, FHIR_EXPORT_WM_KEYS["DiagnosticReport"]))
        .order_by(ExamReport.id)
        .limit(FHIR_EXPORT_BATCH_LIMIT)
        .all()
    )
    _export(
        "DiagnosticReport",
        [
            (r.id, fhir_diagnostic_report_resource(r, req_id, ehc_no))
            for r, req_id, ehc_no in reports
        ],
    )

    if not total:
        return 0, "无增量数据（水位未推进，不产文件）"
    return total, "导出 " + "、".join(parts) + " → fhir_out/（manifest 已更新）"
