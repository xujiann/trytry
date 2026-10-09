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
import contextlib
import json
import logging
import os
import re
import secrets
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from sqlalchemy import String, case, func
from sqlalchemy.orm import Session

from .. import clock, events
from ..clock import now_aware, now_naive, to_aware
from ..concurrency import upsert_unique
from ..config import settings
from ..visibility import GLOBAL_ROLES, assert_obj_org_writable, log_patient_access
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
    ReportRevision,
    SystemParam,
    User,
    Ward,
    utcnow,
)
from ..pii import PII_PREFIX, looks_like_ciphertext
from ..privacy import desensitize, mask_id_card, mask_phone
from ..schemas import EncounterCreate, ExamReportCreate, FollowUpCreate, PatientOut
from ..texttypes import NON_BLANK, normalize_gender
from ..vitals import bp_order_problem
from .chronic import FIELD_DISEASE, _evaluate_level
from .encounters import create_encounter
from .dataquality import id_card_invalid_reason
from .exams import EXAM_REQUEST_STATUS_NAMES, submit_report
from .inpatient import (AdmissionCreate, _mark_discharged, _release_bed, create_admission,
                        spawn_discharge_followup)
from .patients import create_patient_idempotent, id_card_match

#: 入站端点 → 交换日志的消息类型：请求在进处理函数之前就被拒时用它落日志（P2-521）。ADT / ORU 在处理函数里按事件细分，
#: 这里记基础类型
_INBOUND_TYPES = {
    "/api/integration/hl7v2/patient": "hl7v2_patient",
    "/api/integration/fhir/Patient": "fhir_patient",
    "/api/integration/fhir/Observation": "fhir_observation",
    "/api/integration/hl7v2/adt": "hl7v2_adt",
    "/api/integration/hl7v2/oru": "hl7v2_oru",
    "/api/integration/fhir/DiagnosticReport": "fhir_diagnostic_report",
    "/api/integration/fhir/Encounter": "fhir_encounter",
}


def _validation_summary(exc: RequestValidationError) -> list[dict[str, Any]]:
    """请求体校验失败落交换日志用的摘要：每条错误只留 type / loc / msg，丢掉 input 与 ctx（P2-1143，见 `_InboundRoute`）。"""
    return [{key: error.get(key) for key in ("type", "loc", "msg")} for error in exc.errors()]


class _InboundRoute(APIRoute):
    """入站端点在进处理函数之前被拒（请求体校验 422、未登录 401、角色 403）同样落交换日志（P2-521）。

    `_log_exchange` 写着「失败也留痕」、ORU 写着「全部入站落 ExchangeLog」，原先只有进了处理函数的（`_run_inbound`）才记：
    空消息、缺字段、FHIR 资源不是对象、令牌过期、账号角色不对，接口方收到的全是失败，监控页却只数得到那几条解析失败的，
    失败率看着比真实的低。处理函数里已经记过的异常带着标记（`_run_inbound`），这里不重记。

    请求体校验失败只记每条错误的 type / loc / msg（`_validation_summary`），不记 pydantic 附带的 input 与 ctx（P2-1143）：
    input 是整个请求体——字段名写错的 HL7 是整条报文（PID 的证件号、姓名、电话，ORU 还有 OBX 检验结果），发成数组的 FHIR
    是整个资源。交换日志没有保留期，任一机构的经办都读得到（`exchange_logs`），同样的原文在 ESB 那一侧只给管理员看（P0-49）。
    回给对接方的 422 响应不变（那是它自己发来的报文）。修前落下的存量不在迁移里改：运维按
    `SELECT id, created_at, source_system, message_type FROM exchange_logs WHERE error_detail LIKE '422: 请求体校验失败%'
    AND error_detail LIKE '%''input'':%'` 出清单，核对后把这些行的 error_detail 改写成不带报文的摘要（如
    `UPDATE exchange_logs SET error_detail = '422: 请求体校验失败（原文含报文，按 P2-1143 清除）' WHERE id IN (…)`）；
    成败、消息类型、来源系统不动，失败率统计不变。
    """

    def get_route_handler(self):
        handler = super().get_route_handler()
        message_type = _INBOUND_TYPES.get(self.path) if "POST" in self.methods else None
        if message_type is None:
            return handler

        async def logged(request: Request):
            try:
                return await handler(request)
            except (RequestValidationError, HTTPException) as exc:
                if not getattr(exc, "exchange_logged", False):
                    detail = (f"422: 请求体校验失败 {_validation_summary(exc)!r}" if isinstance(exc, RequestValidationError)
                              else f"{exc.status_code}: {exc.detail}")
                    await run_in_threadpool(_log_exchange, message_type, False, detail,
                                            request.headers.get("x-source-system", ""))
                raise

        return logged


logger = logging.getLogger("medplat.integration")

router = APIRouter(
    prefix="/api/integration",
    tags=["对接适配层"],
    dependencies=[Depends(require_roles("operator"))],
    route_class=_InboundRoute,
)

_GENDER_TO_FHIR = {"男": "male", "女": "female"}

ID_CARD_SYSTEM = "urn:oid:2.16.156.10011.1.3"  # 中国居民身份证号 OID
EHC_SYSTEM = "urn:medplat:ehc"
EXAM_ITEM_SYSTEM = "urn:medplat:exam-item"   # 检查检验项目的本地编码（出站 DiagnosticReport.code，P2-1079）
ICD10_SYSTEM = "http://hl7.org/fhir/sid/icd-10"


def _is_icd10_system(system: str) -> bool:
    """入站诊断的编码系统是不是 ICD-10（P2-1080）：FHIR 的 `coding.system`（http://hl7.org/fhir/sid/icd-10 及 -cm 等变体、
    国内对接常写的 ICD-10 / ICD10）、HL7 v2 表 0396 的 `I10` / `I10C`（DG1-3.3）。`I10P` 是 ICD-10 的手术操作码，不算。"""
    key = system.strip().lower()
    return "icd-10" in key or "icd10" in key or key in ("i10", "i10c")

_FHIR_EMPTY: tuple = ("", None, [], {})


def _fhir_compact(value: Any) -> Any:
    """出站 FHIR 资源里没有值的元素整个省掉（第十六批 T1-4）。

    FHIR R4 的 JSON 表示不许出现空串、空数组、空对象与 null——没有值的元素就不写。原先照抄库里的默认值：老档案
    没登记出生日期导出 `"birthDate": ""`、没电话导出 `"telecom": []`，只有诊断编码的就诊内联 `"text": ""`，
    没写所见的报告 `"presentedForm": []`；做校验的前置机整条拒收，这些档案到不了省平台。
    布尔 `False` 与数字 `0` 是值，不算空。
    """
    if isinstance(value, dict):
        kept = {k: _fhir_compact(v) for k, v in value.items()}
        return {k: v for k, v in kept.items() if v not in _FHIR_EMPTY}
    if isinstance(value, list):
        return [v for v in (_fhir_compact(item) for item in value) if v not in _FHIR_EMPTY]
    return value


class Hl7Message(BaseModel):
    message: str = Field(min_length=1, description="HL7 v2 ADT 消息原文（管道分隔）", pattern=NON_BLANK)


def _log_exchange(
    message_type: str, success: bool, error_detail: str = "", source_system: str = ""
) -> None:
    """交换日志落库：独立会话写入并提交，与业务事务解耦（失败也留痕）。

    写不进去（库抖动、连接池一时取不到连接、锁超时）时回滚、记错误日志后吞掉，不拖垮业务响应（P2-819）：「成功」那一笔
    落在业务已经提交之后，原先照抛——已落库的业务回成 500，对接方按规范重推，FHIR Observation 落两条随访，ORU 重推回 409
    「已报告」、LIS 那头两次都记失败；「失败」那一笔照抛则把原来的 4xx 换成 500。取舍与审计落库（`main._write_audit`）、
    调阅留痕同一句：丢的这条留痕记进错误日志，由日志告警兜底。
    """
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
    except Exception:  # noqa: BLE001 - 见 docstring：旁路留痕失败不拖垮业务响应
        with contextlib.suppress(Exception):
            db.rollback()
        logger.error("交换日志写入失败（业务响应不受影响，本条留痕丢失）：%s success=%s", message_type, success,
                     exc_info=True)
    finally:
        with contextlib.suppress(Exception):
            db.close()


def _run_inbound(message_type: str, source_system: str, fn):
    """入站转换统一包装：成功/失败均落交换日志；未预期解析异常转 422。"""
    try:
        result = fn()
    except HTTPException as exc:
        _log_exchange(message_type, False, f"{exc.status_code}: {exc.detail}", source_system)
        exc.exchange_logged = True   # type: ignore[attr-defined]  # 路由那一层（_InboundRoute）不再重记
        raise
    except Exception as exc:  # noqa: BLE001 - 解析异常统一捕获落日志
        _log_exchange(message_type, False, f"解析异常: {exc!r}", source_system)
        failed = HTTPException(status_code=422, detail="消息解析失败，已记录交换日志")
        failed.exchange_logged = True   # type: ignore[attr-defined]
        raise failed from exc
    _log_exchange(message_type, True, "", source_system)
    return result


#: FHIR 资源的作废态（P2-626）：来源系统录错撤回（entered-in-error）或取消（cancelled）——不是新数据
_VOIDED_FHIR_STATUSES = ("entered-in-error", "cancelled")


def _refuse_voided(resource: dict) -> None:
    """作废态的 Observation / Encounter / DiagnosticReport 拒收（P2-626）。

    三处入站原先都不读 `status`：录错之后发来的撤回照样当新数据再记一遍——随访多一次、慢病分级按错值升上去、就诊人次
    多一条（还触发慢专病就诊识别）、检查报告照样出具连同危急值闭环。撤回既往数据属人工更正，不在入站里自动冲销；
    这里只保证作废态不被当成正向数据落库，交换日志照记失败原因。没带 status 的照旧收（FHIR 必填，但上游常省略）。
    """
    status = str(resource.get("status") or "").strip().lower()
    if status in _VOIDED_FHIR_STATUSES:
        raise HTTPException(
            status_code=422, detail=f"资源状态为 {status}（已作废 / 已取消），不入站；撤回既往数据请走人工更正")


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

    只收建档类消息（MSH-9 为 ADT^A04 / A28 / A01，或不带 MSH-9 的简化消息），其余 422 拒收并指路、落交换日志
    （`_refuse_non_patient_event`，P2-1266）。
    """
    return _run_inbound("hl7v2_patient", x_source_system, lambda: _do_hl7v2_patient(body, db, user))


#: 简化建档口（本接口与 ESB 的 hl7v2_patient 转换）受理的 MSH-9（P2-1266）：A04 挂号建档、A28 新增人员信息同属建档。
#: A01 照旧收、只取 PID 建档——存量用例（本口与 ESB）都拿只带 PID 的 A01 当建档报文，而 /hl7v2/adt 的 A01 要 PV1 床位，
#: 这类报文改投过去也收不下；本口还收不收 A01（带 PV1 的入院信息在这里不落库）待裁定
_PATIENT_EVENTS = {"ADT^A01", "ADT^A04", "ADT^A28"}


def _refuse_non_patient_event(message: str) -> None:
    """简化建档口只收建档类消息：MSH-9 是别的事件时 422，detail 指路（P2-1266，第三十七批扫描 AA3-3）。

    原先全程不看 MSH-9：A08 改电话、A03 出院、ORU 检验结果发到这里一律 201 + `MSA|AA`，实际只按证件号查档或建档——
    对端收到 AA 不再重发，交换日志记成功，信息更新、出院、结果全丢。兄弟入口 /hl7v2/adt、/hl7v2/oru 各有白名单、其余
    一律 422 明确拒收，这里同一口径：/adt 白名单里的事件（A03 / A08）指路 /hl7v2/adt，ORU^R01 指路 /hl7v2/oru（只指向
    收得下它的入口），其余 ADT 事件与非 ADT 消息平台不受理。不带 MSH-9 的简化消息照旧收（接口说明写的就是「简化」消息，
    存量对接可能不带）；缺 MSH 段的由解析照旧报「缺少 MSH 消息头段」。
    """
    event = _hl7_event(message)
    if not event or event in _PATIENT_EVENTS:
        return
    code = event.split("^")[1] if event.startswith("ADT^") else ""
    accepted = "本接口只收 ADT^A04 / A28 / A01 建档消息（或不带 MSH-9 的简化消息）"
    if code in _ADT_EVENTS:
        detail = f"消息类型 {event}（{_ADT_EVENTS[code]}）不是建档消息：{accepted}，请改投 /api/integration/hl7v2/adt"
    elif event == "ORU^R01":   # 与 /hl7v2/oru 的受理口径同一句（`_do_hl7v2_oru`）
        detail = f"消息类型 {event}（检验结果）不是建档消息：{accepted}，请改投 /api/integration/hl7v2/oru"
    else:
        detail = f"不支持的消息类型 {event}：{accepted}，平台不受理该事件"
    raise HTTPException(status_code=422, detail=detail)


def parse_hl7v2_patient(message: str, *, any_event: bool = False) -> tuple[dict, str]:
    """HL7 v2 ADT 消息 → (患者字段字典, 消息控制ID)。

    纯转换逻辑，不触库：入站接口与 ESB 编排 transform 步骤共用同一实现
    （块1：集成平台总线复用本函数，避免解析口径分叉）。

    这两处拿到字段都只建档，所以只收建档类消息，别的事件 422（`_refuse_non_patient_event`，P2-1266）；ESB 那一侧
    照它解析失败的老路记失败、重试到死信。ADT 入站按自己的事件白名单判过之后才取 PID，传 `any_event=True`。
    """
    lines = [ln.strip() for ln in message.replace("\r", "\n").split("\n") if ln.strip()]
    msh_line = next((ln for ln in lines if ln.startswith("MSH|")), None)
    if msh_line is None:
        raise HTTPException(status_code=422, detail="缺少 MSH 消息头段")
    if not any_event:
        _refuse_non_patient_event(message)
    msh_fields = msh_line.split("|")
    control_id = msh_fields[9] if len(msh_fields) > 9 else ""
    pid_line = next((ln for ln in lines if ln.startswith("PID|")), None)
    if pid_line is None:
        raise HTTPException(status_code=422, detail="缺少 PID 患者标识段")

    fields = pid_line.split("|")

    def field(i: int) -> str:
        return fields[i] if i < len(fields) else ""

    id_card = _pid3_id_card(field(3))
    name = _pid5_name(field(5))
    birth_raw = field(7).strip()
    # 性别与建档同一口径（P2-1078，`normalize_gender`）：原先入站只认 M / F，PID-8 送 1 / 2（GB/T 2261.1）、小写、「男 / 女」
    # 一律记成「未知」，A08 又不拿「未知」覆盖，之后也改不回来
    gender = normalize_gender(field(8).split("^")[0]) or "未知"
    phone = _pid13_phone(field(13))

    if not id_card or len(id_card) < 15:
        raise HTTPException(status_code=422, detail="PID-3 身份证号缺失或格式不正确")
    if not name:
        raise HTTPException(status_code=422, detail="PID-5 患者姓名缺失")
    _refuse_cipher_prefix("PID-3 身份证号", id_card)   # 解析即拒、早于任何写库（P2-1723）
    _refuse_cipher_prefix("PID-13 联系电话", phone)

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


#: HL7 v2 的转义序列（按缺省编码字符 `^~\\&`）：分隔符写进数据要转义，读回来要还原（P2-724）
_HL7_ESCAPES = {"F": "|", "S": "^", "T": "&", "R": "~", "E": "\\", ".br": "\n", "H": "", "N": ""}
_HL7_ESCAPE_RE = re.compile(r"\\(F|S|T|R|E|\.br|H|N)\\")


def _hl7_unescape(text: str) -> str:
    """还原 HL7 v2 转义（P2-724）：`\\S\\` → `^`、`\\T\\` → `&`、`\\F\\` → `|`、`\\R\\` → `~`、`\\E\\` → `\\`、`\\.br\\` → 换行，
    高亮开关 `\\H\\ \\N\\` 去掉；认不得的原样留着。原先原样印进报告：「10\\S\\9/L」「男 130-175 \\T\\ 女 115-150」。"""
    return _HL7_ESCAPE_RE.sub(lambda m: _HL7_ESCAPES[m.group(1)], text)


def _hl7_null(raw: str) -> str:
    """一个字段 / 组件去首尾空白；HL7 的显式空值 `""`（两个双引号）按「没给」返回空串（P2-1760，第五十二批扫描 AP2-3）。

    原先当字面值落库：A04 / A01 建档电话存成两个双引号，A08 把已有手机号、姓名覆盖成 `""`，A01 的 DG1-3 为 `""` 时诊断
    编码与名称都是 `'""'`，FHIR 出站照样导出——同一条 PID 里 PID-7 / PID-8 的 `""` 早就当「没给」。落库、印进报告的文本
    （PID-5 / PID-13、PV1、DG1、OBR-4、OBX）先过它、再还原转义（P2-724）；A08 因此对 `""` 保持原值（「非空字段覆盖更新」）。
    HL7 本义是「删除接收方已有的值」，A08 要不要照此清空另待裁定，这里不做。标识（PID-3、OBR-2 / OBR-3）的 `""` 不像
    证件号 / 单号，本来就按缺失拒收，不经这里。"""
    value = raw.strip()
    return "" if value == '""' else value


def _pid5_name(raw: str) -> str:
    """PID-5（XPN，可重复）取第一个重复的姓、名、其余名三个组件（P2-724）。原先把全部 `^` 删掉拼起来：`张^三^^^^^L`（第 7
    组件是名称类型码）成了「张三L」，`张三~ZHANG^SAN` 成了「张三~ZHANGSAN」——A08 照此覆盖主索引姓名，居民按姓名实名绑定就找不到档案。
    `""` 按没给（P2-1760）。"""
    parts = raw.split("~")[0].split("^")
    return _hl7_unescape("".join(_hl7_null(part) for part in parts[:3])).strip()


def _refuse_cipher_prefix(label: str, value: object) -> None:
    """入站的证件号 / 电话以密文前缀 `pii1$` 开头的，按解析失败 422 拒收（P2-1723）。

    两列都是加密列（`EncryptedPII`），见前缀就当密文：写入直通、读出解密，解不开就抛。原先 A08 拿 PID-13 的 `pii1$x`
    先覆盖、提交，回读时才抛——回执 422「消息解析失败」，覆盖却已经提交，这位患者的清单、取档、360 视图从此 500，再推一条
    正常电话的 A08 也修不回来（取档那一步就抛）；A04 / A01、简化建档、FHIR 与 ESB 的建档同样落得进去。放在解析函数里，
    入站接口与 ESB 编排共用、拒在任何写库之前。detail 不带原值（交换日志任一机构的经办都读得到）。"""
    if looks_like_ciphertext(value):
        raise HTTPException(status_code=422, detail=f"{label}不得以 {PII_PREFIX} 开头（这是加密存储的密文前缀）")


def _pid13_phone(raw: str) -> str:
    """PID-13（XTN，可重复）优先取像手机号的那一项，没有取第一项（P2-724）。原先整串「座机~手机」落库，按手机号自动绑定对不上。
    `""` 按没给（P2-1760）；每一项的号码怎么取见 `_xtn_number`（P2-1761）。"""
    numbers = [number for number in (_xtn_number(rep) for rep in raw.split("~")) if number]
    return next((n for n in numbers if re.fullmatch(r"1[0-9]{10}", n)), numbers[0] if numbers else "")


#: XTN-2 用途（HL7 表 0201）/ XTN-3 设备类型（表 0202）里表示电子邮件的取值——这类重复不是电话（P2-1761）
_XTN_EMAIL_USES = {"NET"}
_XTN_EMAIL_EQUIPMENT = {"INTERNET", "X.400"}


def _xtn_number(rep: str) -> str:
    """PID-13 的一个重复（XTN）里的电话号码（P2-1761，第五十二批扫描 AP2-9）。

    XTN-1 是旧写法，v2.5 起号码多放在 XTN-12（未格式化号码）或 XTN-6 区号 + XTN-7 本地号码：`^PRN^CP^^86^^13987654321`、
    `^PRN^CP^^^^^^^^^13987654321`。原先只取 XTN-1（P2-724），这两种写法建档电话为空、A08 也改不了。XTN-1 为空时依次取
    XTN-12、XTN-7（前面拼上 XTN-6 区号，写成「区号-号码」）；XTN-5 国家码不拼。邮件类的重复（XTN-2 为 NET 或 XTN-3 为
    Internet / X.400）不是电话，返回空串。各组件先拆、再按没给处理 `""`、再还原转义（P2-724 / P2-1760）。
    """
    parts = [_hl7_unescape(_hl7_null(part)).strip() for part in rep.split("^")] + [""] * 12
    if parts[1].upper() in _XTN_EMAIL_USES or parts[2].upper() in _XTN_EMAIL_EQUIPMENT:
        return ""
    if parts[0] or parts[11]:
        return parts[0] or parts[11]
    return "-".join(part for part in (parts[5], parts[6]) if part) if parts[6] else ""


#: 身份证那一项的标识类型码：HL7 表 0203 的 NI（国家统一个人标识）/ NNCHN（中国国民身份号），与国内常见写法 ID
_ID_CARD_ID_TYPES = {"ID", "NI", "NNCHN"}


def _pid3_id_card(raw: str) -> str:
    """PID-3（患者标识列表）里取身份证号（P2-723）。

    PID-3 可重复：`证件号^^^CN^ID~病案号^^^HIS^MR`。原先不按 `~` 拆、只取第一个组件——整串「证件号~病案号」当证件号
    另建一份主档；病案号排在前面时拿病案号建档（16 位的）或整条 422（短的）。先取标识类型（CX.5）是身份证的那一项，
    其次校验位对得上的 18 位号、再次 15 位纯数字的老证号；都没有时照旧取第一项（长度够不够照旧由调用方判，P1-61）。
    """
    reps = [rep.split("^") for rep in raw.split("~") if rep.strip()]

    def rank(parts: list[str]) -> int:
        value = parts[0].strip()
        if len(parts) > 4 and parts[4].strip().upper() in _ID_CARD_ID_TYPES and value:
            return 0
        if len(value) == 18 and not id_card_invalid_reason(value):
            return 1
        if len(value) == 15 and value.isascii() and value.isdigit():
            return 2
        return 3

    return min(reps, key=rank)[0].strip() if reps else ""   # 同一档里取靠前的


def _is_id_card_identifier(ident: dict) -> bool:
    """FHIR identifier 是不是身份证号（P2-723）：system 认 OID 带不带 `urn:oid:` 前缀两种写法，也认 type 里的身份证类型码。"""
    system = str(ident.get("system") or "").strip()
    if system in (ID_CARD_SYSTEM, ID_CARD_SYSTEM.removeprefix("urn:oid:")):
        return True
    coding = (ident.get("type") or {}).get("coding") or [] if isinstance(ident.get("type"), dict) else []
    return any(isinstance(c, dict) and str(c.get("code") or "").upper() in _ID_CARD_ID_TYPES for c in coding)


def _do_hl7v2_patient(body: Hl7Message, db: Session, user: User):
    data, control_id = parse_hl7v2_patient(body.message)
    patient, created = _upsert_patient(db, data)
    # 终审轮（浙#21 消息确认机制）：返回 HL7 ACK 应答（MSA|AA|原消息控制ID）
    ack = _build_ack(control_id)
    return {"created": created, "ack": ack, "patient": desensitize(patient, user).model_dump()}


def _build_ack(control_id: str, code: str = "AA") -> str:
    """构造 HL7 v2 ACK 应答消息：AA=接收成功（浙#21 消息传输确认回执）。"""

    # MSH-7 带上时区偏移（P2-536）：HL7 v2 的时间戳不带偏移时按**发送方本地时间**解读，而这里取的是 UTC——
    # 接收方在东八区就把应答时间读早 8 小时
    ts = now_aware().strftime("%Y%m%d%H%M%S%z")
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
        # 去首尾空白（P2-790，HL7 一侧 PID-3 早就去了）：上游 CHAR 定长列补的尾随空格、换行原样入库，按证件号幂等建档
        # 认不出是同一个人，另建一份主档
        value = str(ident.get("value") or "").strip()
        if value:
            id_card = value
            if _is_id_card_identifier(ident):   # 原先只认带 urn:oid: 前缀的写法，认不出就落到最后一个（常是病案号）
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
    _refuse_cipher_prefix("identifier 身份证号", id_card)   # 同 PID-3 / PID-13，解析即拒（P2-1723）
    _refuse_cipher_prefix("telecom 电话", phone)

    # 性别同 PID-8（P2-1078）：原先只认全小写的 male / female，Male、FEMALE 都成了「未知」
    raw_gender = resource.get("gender")
    return {
        "name": name,
        "id_card": id_card,
        "gender": normalize_gender(raw_gender if isinstance(raw_gender, str) else "") or "未知",
        "birth_date": resource.get("birthDate", ""),
        "phone": phone,
    }


def _do_fhir_patient(resource: dict, db: Session, user: User):
    patient, created = _upsert_patient(db, parse_fhir_patient(resource))
    return {"created": created, "patient": desensitize(patient, user).model_dump()}


# LOINC 编码 → 随访指标字段。血糖三个编码：2339-0 是质量浓度（mg/dL），15074-8 / 14749-6 是摩尔浓度（mmol/L）
_LOINC_FIELDS = {"8480-6": "sbp", "8462-4": "dbp", "2339-0": "glucose", "15074-8": "glucose", "14749-6": "glucose"}
#: 血糖单位（UCUM，`valueQuantity.code`，没有再看 `unit`）→ 折成 mmol/L 的除数（P2-639）
_GLUCOSE_UNIT_DIVISOR = {"mmol/l": 1.0, "mg/dl": 18.0}
# 指标 → 慢病病种（用于定位随访归属档案）：挪进 `chronic.FIELD_DISEASE` 作唯一一份，桌面端录随访按同一份拆（P2-1541）


class FhirObservationFiledOut(BaseModel):
    """一个病种归档的一条随访：`values` 的产地都经 `float(quantity)`，恒 float。"""

    followup_id: int
    chronic_id: int
    disease: str
    values: dict[str, float]
    level: int


class FhirObservationInboundOut(FhirObservationFiledOut):
    """Observation 入站归档回执。

    血压、血糖一起报的按病种各归各的档案（P2-848）：顶层是第一个归档的病种，其余归档了的病种在 `others`，没有档案、
    没归档的病种在 `unfiled`。这两个键只在有值时出现（`response_model_exclude_unset`）——只报一个病种的回执字节不变。
    """

    others: list[FhirObservationFiledOut] = []
    unfiled: list[str] = []


@router.post("/fhir/Observation", status_code=201, response_model=FhirObservationInboundOut,
             response_model_exclude_unset=True)
def fhir_observation(
    resource: dict, db: Session = Depends(get_db), x_source_system: str = Header(default="")
):
    """FHIR R4 Observation 入站：血压（LOINC 8480-6/8462-4）或血糖（2339-0 / 15074-8 / 14749-6）→ 慢病随访。

    血糖按 `valueQuantity` 的单位折成 mmol/L：mg/dL ÷18，不带单位按 mmol/L，别的单位 422（P2-639）。

    subject.reference 形如 Patient/{ehc_no}；component 或 valueQuantity 提供数值。
    """
    return _run_inbound(
        "fhir_observation", x_source_system, lambda: _do_fhir_observation(resource, db)
    )


def _quantity_value(field: str, quantity: dict) -> float | None:
    """观测值折成随访字段的单位；没给数值返回 None。

    血糖一律折成 mmol/L（P2-639）：分级阈值按 mmol/L 配（`chronic_seed`：≥10.0 三级、≥7.0 二级），入站原先不读单位——
    按 LOINC 2339-0（质量浓度）上送的 99 mg/dL（约 5.5 mmol/L，正常）当 99 mmol/L 判成三级高危、建议上转，再经采集器
    进慢专病监测。mg/dL 按 ÷18 折算；认不出的单位 422（不猜）；不带单位的照旧按 mmol/L 收——对接规范一直这么收，
    已经接上的系统不断。血压不看单位（mmHg 以外的写法在县域对接里没见过）。
    """
    value = quantity.get("value")
    if value is None:
        return None
    if field != "glucose":
        return float(value)
    unit = str(quantity.get("code") or quantity.get("unit") or "").strip()
    if not unit:
        return float(value)
    divisor = _GLUCOSE_UNIT_DIVISOR.get(unit.lower())
    if divisor is None:
        raise HTTPException(status_code=422, detail=f"血糖单位 {unit} 无法识别：请按 mmol/L 或 mg/dL 上送")
    return round(float(value) / divisor, 2)


def _do_fhir_observation(resource: dict, db: Session):
    if resource.get("resourceType") != "Observation":
        raise HTTPException(status_code=422, detail="resourceType 必须为 Observation")
    _refuse_voided(resource)

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
        quantity = _quantity_value(field_name, comp.get("valueQuantity") or {}) if field_name else None
        if field_name and quantity is not None:
            values[field_name] = quantity
    top_field = _LOINC_FIELDS.get(loinc_code(resource.get("code")))
    if top_field and top_field not in values:
        top_value = _quantity_value(top_field, resource.get("valueQuantity") or {})
        if top_value is not None:
            values[top_field] = top_value

    if not values:
        raise HTTPException(status_code=422, detail="未识别到支持的观测指标（血压/血糖 LOINC）")

    # 按指标归病种、各归各的档案（P2-848，`FIELD_DISEASE` 本来就是「指标 → 随访归属档案」）：原先整条挂到第一个分量的
    # 病种——血压 + 血糖一起报，血糖记进高血压档案、糖尿病档案不记也不分级（分量顺序反过来就反过来）；只有糖尿病档案的
    # 患者连血糖一起 404。缺档案的那部分在回执里点名，不连累其余；一个都归不了的照旧 404
    groups: dict[str, dict[str, float]] = {}
    for field_name, value in values.items():
        groups.setdefault(FIELD_DISEASE[field_name], {})[field_name] = value
    chronics = {
        disease: db.query(ChronicPatient)
        .filter(ChronicPatient.patient_id == patient.id, ChronicPatient.disease == disease)
        .order_by(ChronicPatient.id)
        .first()
        for disease in groups
    }
    unfiled = [disease for disease, chronic in chronics.items() if chronic is None]
    if len(unfiled) == len(groups):
        raise HTTPException(status_code=404, detail=f"该患者无 {'、'.join(unfiled)} 慢病档案，无法归档随访")

    filed = []
    for disease, group in groups.items():
        chronic = chronics[disease]
        if chronic is None:
            continue
        # `group` 是运行期按 LOINC 映射拼出来的字段字典，键名在类型上不可知；
        # pydantic 会做校验，缺字段/多字段都会在这里报 422，不会静默走下去。
        followup_in = FollowUpCreate(**cast(Any, group), guidance="HL7/FHIR 对接自动归档")
        # 收缩压须高于舒张压（P2-1016，与界面录随访同一句）：设备把两项接反，原先照样定 3 级、改写档案分级
        problem = bp_order_problem(followup_in.sbp, followup_in.dbp)
        if problem:
            raise HTTPException(status_code=422, detail=problem)
        followup = FollowUp(chronic_id=chronic.id, **followup_in.model_dump())
        new_level = _evaluate_level(db, chronic.disease, followup_in)
        if new_level is not None:
            chronic.level = new_level
        db.add(followup)
        filed.append((disease, chronic, followup, group))
    db.commit()
    receipts = []
    for disease, chronic, followup, group in filed:
        db.refresh(followup)
        receipts.append({"followup_id": followup.id, "chronic_id": chronic.id, "disease": disease,
                         "values": group, "level": chronic.level})
    out: dict[str, Any] = dict(receipts[0])
    if receipts[1:]:
        out["others"] = receipts[1:]
    if unfiled:
        out["unfiled"] = unfiled
    return out


# FHIR R4 Patient 是**外部标准形状**（identifier/name/telecom 皆为标准定义的嵌套
# 数组），照 workflows.nodes 先例宽 dict 透传——给国际标准建窄模型等于替 HL7 另立
# 规格；当前导出的 7 键字段面由 test_integration_contract.py 逐键钉住（没值的元素省掉，见 `_fhir_compact`）。
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
    return _fhir_compact({
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
    })


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
        .order_by(ExchangeLog.message_type)
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
# OBX-8 异常标志（HL7 v2 表 0078）：H/L 偏高偏低，A 异常，HH/LL 危急高/低，AA 非数值结果的危急（表 0078：与数值结果
# 的危急界值同义）——HH/LL/AA 判危急值，进危急值闭环；< / > 超出仪器量程下限 / 上限，按异常计；N 正常，空 = 没给判断。
# 字段可重复（`~` 分隔，如 H~W），逐个判；每个重复按第 1 组件判（v2.7 起是 CWE `HH^Critical high^HL70078`，P2-1756）。
# 其余标志（变化趋势 U/D/B/W、药敏 S/R/I……）平台不据以判异常，结论里如实写
# 「标志未识别」、不当正常（P1-213：原先整串比对、只认五个，AA 的血培养记成「异常 0 项」、不进危急值闭环）
_ABNORMAL_FLAGS = {"H", "L", "A", "HH", "LL", "AA", "<", ">"}
_CRITICAL_FLAGS = {"HH", "LL", "AA"}
_NORMAL_FLAGS = {"N"}


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
      补录流程承担），差异特此写明；只能出本机构的住院（全域账号不限），与平台出院同一道机构门（P1-196）；
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
    data, control_id = parse_hl7v2_patient(body.message, any_event=True)   # 事件已按白名单判过（P2-1266）
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
            .filter(id_card_match(data["id_card"]))   # 证件号两种写法都认（P1-114）
            .order_by(Patient.id)
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
        diagnosis_code, diagnosis_name = _parse_dg1(body.message)
        # 按名找病区只在调用方能以其名义写入的机构里找（P2-727）：原先全县找，两家医院都有「内科病区」（县域里再常见不过）
        # 时本院对接账号的每一条 A01 都 422「在多家机构存在」，而这位账号本来只能收进本院的病区（下面 create_admission
        # 按病区机构判写权）。与 `assert_org_writable` 同一判据：全域角色照旧全县找、同名仍 422
        ward_query = db.query(Ward).filter(Ward.name == ward_name)
        if user.role not in GLOBAL_ROLES:
            ward_query = ward_query.filter(Ward.org_id == user.org_id)
        wards = ward_query.order_by(Ward.id).all()
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
                diagnosis_code=diagnosis_code,
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
        .filter(id_card_match(data["id_card"]))   # 证件号两种写法都认（P1-114）
        .order_by(Patient.id)
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
    # 哪家机构的住院，只能由那家（或全域账号）的对接账号推出院（P1-196）：A01 入院经 create_admission 判病区所属机构，
    # 平台出院判住院记录所属机构；A03 原先都不看——任一机构的对接账号按证件号就能让别家在院的患者出院、释放床位、
    # 停掉执行中的医嘱、派出院随访。只读判定，不影响下面「第一条写语句」的约定
    assert_obj_org_writable(db, user, admission)
    # 上面那句"有没有在院记录"是快路径；闸门是 `_mark_discharged` 那条带状态条件的
    # UPDATE，且必须是本次事务的第一条写语句。A03 与平台端点是**同一行的两个出院入口**，
    # 谁抢输都拿与顺序重复完全一致的 409（`_run_inbound` 照旧把它写进交换日志）。
    now = utcnow()
    if not _mark_discharged(db, admission.id, now):
        db.rollback()
        raise HTTPException(status_code=409, detail="该患者无在院记录，A03 出院拒收")
    db.refresh(admission)  # 中途有转床提交时，要释放的是新床
    db.query(InpatientOrder).filter(
        InpatientOrder.admission_id == admission.id, InpatientOrder.status == "active"
    ).update(
        {InpatientOrder.status: "stopped", InpatientOrder.stopped_at: now},
        synchronize_session=False,
    )
    _release_bed(db, admission.bed_id)
    # 出院随访与平台出院同一处派（P2-160）；给患者发的「出院随访安排」通知没跟着发：那段话把人指向平台上的住院
    # 费用清单，而 HIS 为出院来源的机构，费用多半在 HIS 里——发不发、怎么措辞要另定
    spawn_discharge_followup(db, admission)
    events.publish(db, events.ADMISSION_DISCHARGED, {
        "admission_id": admission.id,
        "patient_id": admission.patient_id,
        "org_id": admission.org_id,
        "diagnosis_name": admission.diagnosis_name or "",
        "discharged_on": clock.today().isoformat(),   # 本地业务日，与平台出院同一口径（P2-545）
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
    ward_name = _hl7_unescape(_hl7_null(parts[0]))   # `""` 按没给（P2-1760）
    bed_no = _hl7_unescape(_hl7_null(parts[2])) if len(parts) > 2 else ""
    if not ward_name or not bed_no:
        raise HTTPException(status_code=422, detail="PV1-3 须为 病区^房间^床号")
    return ward_name, bed_no


def _parse_pv1_doctor(message: str) -> str:
    """PV1-7 主治医师 `工号^姓^名`：取姓名组件（无姓名组件时回落首组件）。可重复（XCN），取第一个重复（P2-724）。"""
    pv1 = next((s for s in _hl7_segments(message) if s.startswith("PV1|")), None)
    parts = _hl7_field(pv1, 7).split("~")[0].split("^") if pv1 else [""]
    name = "".join(_hl7_null(p) for p in parts[1:3])   # `""` 按没给（P2-1760）
    return _hl7_unescape(name or _hl7_null(parts[0]))[:64]


def _parse_dg1(message: str) -> tuple[str, str]:
    """DG1-3 诊断 `编码^名称` → (编码, 名称)（名称缺失回落 DG1-4 描述，再回落编码）。可缺省。

    编码原先解析出来就丢了（P2-632）：住院就诊上没有 ICD 编码，按诊断编码匹配的慢专病识别、诊断编码必填 / 字典校验的
    数据质控、FHIR 出站的 reasonCode 都认不出这次住院——同一件事走 FHIR Encounter 入站是带编码的。"""
    dg1 = next((s for s in _hl7_segments(message) if s.startswith("DG1|")), None)
    if dg1 is None:
        return "", ""
    parts = [_hl7_null(part) for part in _hl7_field(dg1, 3).split("^")] + [""] * 6   # `""` 按没给（P2-1760）
    # DG1-3 是 CWE：`编码^名称^编码系统^备用编码^备用名称^备用编码系统`。主三元组明写了别的编码系统（如 SCT）、备用三元组是
    # ICD-10 时取备用的（P2-1080）：原先一律取第一组件，SNOMED 码当 ICD-10 落库、再按 ICD-10 导出，慢专病按诊断编码也认不出。
    # 主三元组没写编码系统的照旧当 ICD-10；一条 ICD-10 都没有时怎么办待裁定（与 P2-744 一并定）
    if parts[2].strip() and not _is_icd10_system(parts[2]) and parts[3].strip() and _is_icd10_system(parts[5]):
        parts = parts[3:]
    code = _hl7_unescape(parts[0])
    name = _hl7_unescape(parts[1])
    return code[:64], (name or _hl7_unescape(_hl7_null(_hl7_field(dg1, 4))) or code)[:256]


class OruReportOut(BaseModel):
    request_id: int
    report_id: int
    obx_count: int
    abnormal_count: int
    critical: bool


class OruInboundOut(BaseModel):
    event: str
    ack: str
    request_id: int
    report_id: int
    obx_count: int
    abnormal_count: int
    critical: bool
    # 一条消息带几张申请的结果时逐组列出（P2-722，只加尾键）；顶层是第一组的单号与各组合计
    reports: list[OruReportOut]


@router.post("/hl7v2/oru", status_code=201, response_model=OruInboundOut)
def hl7v2_oru(
    body: Hl7Message,
    db: Session = Depends(get_db),
    x_source_system: str = Header(default=""),
):
    """ORU^R01 检验/检查结果入站：OBR（申请信息）+ 多条 OBX（结果项）回写检查报告。

    - **申请单定位**：OBR-2（下单方单号）即平台申请单号（ExamRequest.id，对接规范
      映射表"检查检验申请→ServiceRequest"）；OBR-2 缺失回退 OBR-3（执行方单号），回退时必须带 PID
      段核对患者（P2-986）；
    - **OBX 逐条解析**：标识（OBX-3）/值（OBX-5，按 OBX-2 值类型取文字、附件类不收正文，P2-1758）/
      单位（OBX-6）/参考范围（OBX-7）/异常标志（OBX-8），逐行拼入报告 finding；异常标志 HH/LL/AA 判**危急值**，
      复用报告发布的危急值闭环（通知→确认→处置留痕）；标志按 `~` 拆开逐个判，认不得的
      在结论里写明、不当正常（P1-213）；
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


def _oru_groups(segments: list[str]) -> list[tuple[str | None, str, list[str]]]:
    """ORU^R01 按申请分组：每个 OBR 连同其后、下一个 OBR 之前的 OBX 是一组（HL7 的 ORDER_OBSERVATION 组，P2-722），
    返回 (本组的 PID 段, OBR 段, OBX 段列表)。

    一条消息可以带几张申请的结果（LIS 常把同一次采血的血常规、电解质放在一起发）。原先只认第一个 OBR、却把全文
    的 OBX 都算进去：电解质的血钾危急值写进了血常规的报告，电解质那张申请永远「待出报告」，ACK 照回 AA、LIS 不重发。

    一条消息也可以带几位患者（PATIENT_RESULT 组可重复，P2-1757）：每组的患者是这个 OBR 之前最近的那个 PID，前面没有
    PID 的为 None。原先全文只取第一个 PID、所有 OBR 都拿它核：乙的结果写进甲的申请单照样 201（防串单失效），合法的
    多患者批量消息整条 422、错因还说成「不一致」。
    """
    groups: list[tuple[str | None, str, list[str]]] = []
    pid: str | None = None
    for seg in segments:
        if seg.startswith("PID|"):
            pid = seg
        elif seg.startswith("OBR|"):
            groups.append((pid, seg, []))
        elif seg.startswith("OBX|"):
            if not groups:
                raise HTTPException(status_code=422, detail="OBX 结果段出现在 OBR 申请信息段之前，无法判断属于哪张申请")
            groups[-1][2].append(seg)
    return groups


def _oru_request(db: Session, obr: str, pid: str | None, index: int = 0) -> ExamRequest:
    """按 OBR 定位申请单并核对 PID（每一组拿本组的 PID 各自核，防串单，P2-1757）。

    `index`：一条消息几组时这是第几个 OBR，PID 对不上、没带证件号时点名（只有一组时传 0，错因不带前缀）。"""
    placer = _hl7_field(obr, 2)
    order_no = (placer or _hl7_field(obr, 3)).split("^")[0].strip()
    # 只认 ASCII（P1-97）：`isdigit()` 放行上标「²」、圈码「①」，下一行 int() 抛异常，被入站兜底成笼统的
    # 「消息解析失败」——对方系统看不出是单号不对
    if not (order_no.isascii() and order_no.isdigit()):
        raise HTTPException(status_code=422, detail="OBR-2/OBR-3 申请单号缺失或非平台单号")
    # 按 OBR-3 回退时必须带 PID、核对上申请单患者（P2-986）：OBR-3 是执行方（LIS）自己编的号，只是碰巧可能等于某张平台
    # 申请单号——不带 PID 就不核患者，LIS 回传一份平台上没有申请单的结果，样本号恰好等于别人的申请单号，结果连同危急值
    # 就写进那位患者的申请单，他自己的结果随后回传反而 409。带 OBR-2（平台单号）的照旧可以不带 PID
    if not placer and (pid is None or not _pid3_id_card(_hl7_field(pid, 3))):
        raise HTTPException(status_code=422,
                            detail="OBR-2（平台申请单号）缺失：按 OBR-3 定位申请单时须带 PID 段核对患者，结果拒收")
    request = db.get(ExamRequest, int(order_no))
    if request is None:
        raise HTTPException(
            status_code=404,
            detail=f"申请单 {order_no} 不存在，结果拒收（对接规范§四：请先创建检查申请）",
        )
    # PID 一致性核验（可选段）：报文声明的患者与申请单不一致时拒收，防串单
    if pid is not None:
        id_card = _pid3_id_card(_hl7_field(pid, 3))   # 与建档同一个取法（P2-723）
        where = f"第 {index} 个 OBR（申请单 {request.id}）：" if index else ""
        if id_card and len(id_card) < 15:
            # 取出的值不像证件号（与建档同一判据：不足 15 位，`parse_hl7v2_patient` 报「缺失或格式不正确」）——PID-3 只给了
            # 病案号 / 门诊号（`MR0012345^^^HIS^MR`）。原先拿它去核、报「PID 患者与申请单患者不一致」，错因说错（P2-1759）
            raise HTTPException(status_code=422, detail=f"{where}PID-3 未带身份证号，无法核对患者")
        if id_card:
            # 核的是**申请单患者本人**的证件号（两种写法都认，P1-114）。原先是「平台上另有一位持这个证件号的患者才拒收」：
            # 证件号不属于平台上任何人（院内自建档、没进平台的患者）的结果照样写进申请单患者名下——别人的检验结果、
            # 连同危急值闭环一起落到这位患者身上（P1-143）
            owns = (
                db.query(Patient.id)
                .filter(Patient.id == request.patient_id, id_card_match(id_card))
                .first()
            )
            if owns is None:
                raise HTTPException(status_code=422, detail=f"{where}PID 患者与申请单患者不一致，结果拒收")
    return request


#: `exam_reports.conclusion` 的列宽。结论逐项点名危急项（P2-1363）之后，危急项多的一组能拼到超宽：按列宽截断、末尾标「…」
#: （写法同 `exams._critical_action_text`）；原先这里硬切 `[:1024]`，截掉了也看不出
ORU_CONCLUSION_MAX = cast(String, ExamReport.__table__.c.conclusion.type).length or 1024


def _oru_conclusion_text(text: str) -> str:
    return text if len(text) <= ORU_CONCLUSION_MAX else text[:ORU_CONCLUSION_MAX - 1] + "…"


#: OBX-2 值类型里的编码类（`码^文字^码表^…`）：所见里印文字组件（P2-1758）
_OBX_CODED_TYPES = {"CE", "CWE", "CNE"}
#: OBX-2 值类型里的附件类：ED 封装数据（PDF 报告单的 base64）、RP 引用指针——正文不进所见（P2-1758；附件怎么收随 P2-1137）
_OBX_ATTACHMENT_TYPES = {"ED", "RP"}


def _obx_value(value_type: str, raw: str) -> str:
    """OBX-5 按 OBX-2 值类型取所见里印的文字（P2-1758）。

    原先不看值类型、整串还原转义就印：CE / CWE 印成「P^阳性^99LAB」、SN 印成「>^250」、重复值印成「第一行~第二行」；
    ED（PDF 报告单的 base64）整段灌进所见，把后面的结果行挤出 2048 字的截断——危急的血钾行没了。
    编码类取文字组件（第 2 组件，空则第 1）；SN（比较符^数值^分隔符或后缀^数值）各组件拼起来（「>250」「1:128」）；
    重复值用「；」连；附件类不印正文，只印一句说明（PDF 正文怎么收随 P2-1137）；其余类型照旧整个重复还原转义。
    都是先按分隔符拆、再还原转义（P2-724）。异常 / 危急照旧只按 OBX-8 判，与值类型无关。`""` 按没给（P2-1760）。
    """
    if value_type in _OBX_ATTACHMENT_TYPES:
        return f"附件类结果（{value_type}），平台未收" if _hl7_null(raw) else ""
    texts: list[str] = []
    for rep in raw.split("~"):
        parts = rep.split("^")
        if value_type in _OBX_CODED_TYPES:
            text = _hl7_unescape((_hl7_null(parts[1]) if len(parts) > 1 else "") or _hl7_null(parts[0]))
        elif value_type == "SN":
            text = "".join(_hl7_unescape(_hl7_null(part)) for part in parts[:4])
        else:
            text = _hl7_unescape(_hl7_null(rep))
        if text:
            texts.append(text)
    return "；".join(texts)


def _oru_report(obr: str, obx_segments: list[str], request: ExamRequest,
                reported_by: str) -> tuple[ExamReportCreate, int, int]:
    """一组 OBX 拼成一份报告，返回 (报告, 结果项数, 异常项数)。"""
    lines: list[str] = []
    abnormal = 0
    critical = False
    critical_items: list[str] = []   # 判成危急的结果项「项目 值 单位 [标志]」，结论里逐项点名（P2-1363）
    unrecognized: list[str] = []   # 没有一个认得的标志、又带着认不得的标志的结果项——判不了，不当正常
    for seg in obx_segments:
        code_parts = _hl7_field(seg, 3).split("^")
        # 先按分隔符拆、再还原转义（P2-724）；`""` 按没给（P2-1760）
        label = _hl7_unescape((_hl7_null(code_parts[1]) if len(code_parts) > 1 else "") or _hl7_null(code_parts[0]))
        value = _obx_value(_hl7_field(seg, 2).strip().upper(), _hl7_field(seg, 5))   # 按 OBX-2 值类型拆（P2-1758）
        unit = _hl7_unescape(_hl7_null(_hl7_field(seg, 6).split("^")[0]))
        ref_range = _hl7_unescape(_hl7_null(_hl7_field(seg, 7)))
        flag = _hl7_null(_hl7_field(seg, 8)).upper()
        # 每个重复先取第 1 组件再按表 0078 判（P2-1756）：v2.7 起 OBX-8 是 CWE（`HH^Critical high^HL70078`），本地 LIS 也有
        # `H^偏高` 这样带文字的写法——原先整串比对，一律当「标志不认得」：危急值不进闭环、异常计 0 项、居民照收「已出具」。
        # 同 OBX-3 / OBX-6 先拆组件（P2-724）；第 1 组件空着的照旧整个重复当认不得的标志。结论与所见里照旧印原文
        flags = {(_hl7_null(rep.split("^")[0]) or _hl7_null(rep)) for rep in flag.split("~")} - {""}
        if flags & _ABNORMAL_FLAGS:
            abnormal += 1
        if flags & _CRITICAL_FLAGS:
            critical = True
            critical_items.append(" ".join(part for part in (label, value, unit, f"[{flag}]") if part))
        if flags and not flags & (_ABNORMAL_FLAGS | _NORMAL_FLAGS):
            unrecognized.append(flag)
        line = f"{label}：{value}"
        if unit:
            line += f" {unit}"
        if ref_range:
            line += f"（参考 {ref_range}）"
        if flag:
            line += f" [{flag}]"
        lines.append(line)
    item_parts = _hl7_field(obr, 4).split("^")
    item_name = _hl7_unescape((_hl7_null(item_parts[1]) if len(item_parts) > 1 else "")) or request.item_name
    conclusion = f"{item_name}：共 {len(lines)} 项，异常 {abnormal} 项"
    if critical:
        # 点名是哪一项、多少（P2-1363）：危急值的站内信正文、定向广播、待办、超时催办、两端危急值清单给的都是这句结论——
        # 原先只写「含危急值」，村医在手机上确认接收时不知道是哪一项、多少，逐项结果只在所见里
        conclusion += f"，含危急值（{'、'.join(critical_items)}）"
    if unrecognized:
        conclusion += f"，另 {len(unrecognized)} 项的异常标志平台不认得（{'、'.join(dict.fromkeys(unrecognized))}），以原文为准"
    report = ExamReportCreate(
        finding="\n".join(lines)[:2048],
        conclusion=_oru_conclusion_text(conclusion),
        critical=critical,
        reported_by=reported_by,
    )
    return report, len(lines), abnormal


def _do_hl7v2_oru(body: Hl7Message, db: Session, event: str, source_system: str):
    if event != "ORU^R01":
        raise HTTPException(
            status_code=422,
            detail=f"不支持的消息类型 {event or '(缺失)'}：本接口仅受理 ORU^R01",
        )
    segments = _hl7_segments(body.message)
    ack = _build_ack(_hl7_control_id(body.message))

    if not any(s.startswith("OBR|") for s in segments):
        raise HTTPException(status_code=422, detail="缺少 OBR 申请信息段")
    groups = _oru_groups(segments)
    # 每组先全部定位、核对完再出报告（P2-722）：出报告是逐张提交的，第二组的申请单不存在 / 已出过报告，
    # 第一组已经出具、危急值已经通知，整条消息却回 AE、LIS 再发一遍就撞「已出具」
    planned: list[tuple[ExamRequest, ExamReportCreate, int, int]] = []
    for index, (pid, obr, obx_segments) in enumerate(groups, start=1):
        request = _oru_request(db, obr, pid, index if len(groups) > 1 else 0)   # 本组的 PID（P2-1757）
        if not obx_segments:
            raise HTTPException(status_code=422, detail="缺少 OBX 结果段" if len(groups) == 1
                                else f"第 {index} 个 OBR（申请单 {request.id}）下没有 OBX 结果段")
        if any(request.id == seen.id for seen, *_ in planned):
            raise HTTPException(status_code=422, detail=f"申请单 {request.id} 在同一条消息里出现两次")
        if request.status not in ("pending", "diagnosing"):   # 与出报告同一条件，提前判
            raise HTTPException(
                status_code=409,
                detail=f"当前状态 {EXAM_REQUEST_STATUS_NAMES.get(request.status, request.status)} 不可出报告"
                       + ("" if len(groups) == 1 else f"（申请单 {request.id}）"))
        report, obx_count, abnormal = _oru_report(obr, obx_segments, request, (source_system or "HL7-ORU")[:64])
        planned.append((request, report, obx_count, abnormal))
    results = []
    for request, report_in, obx_count, abnormal in planned:
        report = submit_report(request.id, report_in, db)
        results.append({"request_id": request.id, "report_id": report.id, "obx_count": obx_count,
                        "abnormal_count": abnormal, "critical": report.critical})
    first = results[0]
    return {
        "event": event,
        "ack": ack,
        "request_id": first["request_id"],
        "report_id": first["report_id"],
        # 一条消息几组时，顶层的数取合计、危急值任一组有即有；逐组明细在 reports 里
        "obx_count": sum(r["obx_count"] for r in results),
        "abnormal_count": sum(r["abnormal_count"] for r in results),
        "critical": any(r["critical"] for r in results),
        "reports": results,
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
      映射表 critical 的入站承载（FHIR R4 DiagnosticReport 无标准危急值字段，
      以命名扩展承载，出站导出用同一 URL 对称回写）；对接规范§二原先写成「critical→flag」，
      按规范送 `flag` 的照收却不判危急值，现已改写成这个扩展（P2-1267）；
    - 申请单不存在 404 拒收（口径同 ORU：规范§四"引用的资源不存在→先行创建"）；
      已出报告 409；
    - subject（可选）= `Patient/{ehc_no}`（与 Observation / Encounter 入站、DiagnosticReport 出站同口径）：
      给了就须是申请单患者本人，不一致 422 拒收（P1-195，与 ORU 的 PID 核验 P1-143 同一道防串单）。
    """
    return _run_inbound(
        "fhir_diagnostic_report",
        x_source_system,
        lambda: _do_fhir_diagnostic_report(resource, db, x_source_system),
    )


def _presented_finding(forms) -> str:
    """presentedForm → 所见：挑第一份带数据的文本件（contentType 为 text/*，没写按 text/plain），按它声明的 charset 解码，
    没写按 UTF-8（P2-1111）。原先只取第 [0] 份、一律按 UTF-8 解：声明 GB18030 / GBK 的、先放一份 PDF 再放文本的，整单
    422，报错还说成「不是合法的 base64」，结论与危急值标记跟着一起被拒。一份文本件都没有、只有 PDF 这类的，照旧拒收，
    但把原因说对（只收文本所见、PDF 这类要不要单收结论，见待裁定）。"""
    if forms is None:
        return ""
    if not isinstance(forms, list):
        raise HTTPException(status_code=422, detail="presentedForm 必须是数组")
    others: list[str] = []
    for index, form in enumerate(forms):
        if not isinstance(form, dict) or not form.get("data"):
            continue
        mime, _, params = str(form.get("contentType") or "text/plain").partition(";")
        if not mime.strip().lower().startswith("text/"):
            others.append(mime.strip())
            continue
        charset = "utf-8"
        for param in params.split(";"):
            key, _, value = param.partition("=")
            if key.strip().lower() == "charset" and value.strip():
                charset = value.strip().strip('"')
        try:
            raw = base64.b64decode(form["data"])
        except Exception:
            raise HTTPException(status_code=422, detail=f"presentedForm[{index}].data 不是合法的 base64") from None
        try:
            return raw.decode(charset)
        except LookupError:
            raise HTTPException(status_code=422, detail=f"presentedForm[{index}] 的字符集 {charset} 不认识") from None
        except UnicodeDecodeError:
            raise HTTPException(
                status_code=422, detail=f"presentedForm[{index}].data 按 {charset} 解不开（contentType 的 charset 与内容不符）"
            ) from None
    if others:
        raise HTTPException(
            status_code=422,
            detail=f"presentedForm 里没有文本件（{'、'.join(others)}）：所见请以 text/plain 送",
        )
    return ""


def _do_fhir_diagnostic_report(resource: dict, db: Session, source_system: str):
    if resource.get("resourceType") != "DiagnosticReport":
        raise HTTPException(status_code=422, detail="resourceType 必须为 DiagnosticReport")
    _refuse_voided(resource)
    based = resource.get("basedOn") or []
    reference = (based[0] or {}).get("reference", "") if based else ""
    if not reference.startswith("ServiceRequest/"):
        raise HTTPException(
            status_code=422, detail="basedOn[0].reference 必须为 ServiceRequest/{申请单id}"
        )
    request_id = reference.split("/", 1)[1]
    if not (request_id.isascii() and request_id.isdigit()):  # 只认 ASCII（P1-97）
        raise HTTPException(status_code=422, detail="ServiceRequest 引用的申请单号须为数字")
    request = db.get(ExamRequest, int(request_id))
    if request is None:
        raise HTTPException(
            status_code=404,
            detail=f"申请单 {request_id} 不存在，报告拒收（对接规范§四：请先创建检查申请）",
        )
    # subject 一致性核验（可选，口径同 ORU 的 PID 段）：原先 subject 写的是谁都不看，别人的报告——连同危急值闭环——
    # 照样落到申请单患者名下；同一类报告走 ORU 早就按 PID 核本人（P1-143）
    subject = resource.get("subject")
    subject_ref = str(subject.get("reference") or "").strip() if isinstance(subject, dict) else ""
    if (subject is not None and not isinstance(subject, dict)) or (
        subject_ref and not subject_ref.startswith("Patient/")
    ):
        raise HTTPException(status_code=422, detail="subject.reference 必须为 Patient/{ehc_no}")
    if subject_ref:
        owner = db.get(Patient, request.patient_id)
        if owner is None or not owner.ehc_no or subject_ref.split("/", 1)[1] != owner.ehc_no:
            raise HTTPException(status_code=422, detail="subject 患者与申请单患者不一致，报告拒收")
    conclusion = str(resource.get("conclusion") or "").strip()
    if not conclusion:
        raise HTTPException(status_code=422, detail="conclusion 缺失")
    finding = _presented_finding(resource.get("presentedForm"))
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
    resource: dict,
    db: Session = Depends(get_db),
    x_source_system: str = Header(default=""),
    user: User = Depends(get_current_user),
):
    """FHIR R4 Encounter 入站 → 就诊记录入档（映射表：Encounter+Condition）。

    - subject.reference = `Patient/{ehc_no}`（与 Observation 入站同口径）；
    - serviceProvider.reference = `Organization/{机构id}`；
    - class.code：AMB→门诊 / IMP→住院（缺省按门诊）；
    - reasonCode[0]：coding[0].code→diagnosis_code（ICD-10）、text→diagnosis_name；
    - participant[0].individual.display→doctor_name；
    - summary 留空，不写来源标记（P2-1249）。
    复用就诊登记路由逻辑（患者/机构校验 + 领域事件发布），入站落 ExchangeLog。
    就诊机构（`serviceProvider`）同样只能是对接账号自己的机构（P0-35，与 ADT 入站把
    `user` 传给 `create_admission` 同一口径）；跨机构批量同步用全域账号。
    """
    return _run_inbound(
        "fhir_encounter", x_source_system, lambda: _do_fhir_encounter(resource, db, user)
    )


def _do_fhir_encounter(resource: dict, db: Session, user: User):
    if resource.get("resourceType") != "Encounter":
        raise HTTPException(status_code=422, detail="resourceType 必须为 Encounter")
    _refuse_voided(resource)
    reference = (resource.get("subject") or {}).get("reference", "")
    if not reference.startswith("Patient/"):
        raise HTTPException(status_code=422, detail="subject.reference 必须为 Patient/{ehc_no}")
    patient = db.query(Patient).filter(Patient.ehc_no == reference.split("/", 1)[1]).first()
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    provider = (resource.get("serviceProvider") or {}).get("reference", "")
    org_part = provider.split("/", 1)[1] if provider.startswith("Organization/") else ""
    if not (org_part.isascii() and org_part.isdigit()):  # 只认 ASCII（P1-97）
        raise HTTPException(
            status_code=422, detail="serviceProvider.reference 必须为 Organization/{机构id}"
        )
    cls = resource.get("class") or {}
    class_code = str(cls.get("code", "") if isinstance(cls, dict) else "").upper() or "AMB"
    encounter_type = _CLASS_TO_ENCOUNTER_TYPE.get(class_code)
    if encounter_type is None:
        raise HTTPException(status_code=422, detail="class.code 仅支持 AMB（门诊）/IMP（住院）")
    reasons = resource.get("reasonCode") or []
    codings = [c for c in (reasons[0].get("coding") or []) if isinstance(c, dict)] if reasons else []
    # 按编码系统挑 ICD-10 那条（P2-1080）：原先取 coding[0]，SNOMED / 本地码排在前面就当 ICD-10 落库、再按 ICD-10 导出。
    # 一条 ICD-10 都没有的照旧取第一条（丢弃、透传还是拒收待裁定，与 P2-744 一并定）
    coding = next((c for c in codings if _is_icd10_system(str(c.get("system") or ""))), codings[0] if codings else {})
    diagnosis_code = str(coding.get("code", ""))[:64]
    diagnosis_name = str(
        (reasons[0].get("text") if reasons else "") or coding.get("display", "")
    )[:256]
    participants = resource.get("participant") or []
    doctor_name = str(
        ((participants[0].get("individual") or {}).get("display", "")) if participants else ""
    )[:64]
    # 摘要留空（P2-1249）：原先写死「FHIR Encounter 入站同步」——给系统看的来源标记落进了就诊摘要，居民端「我的档案」
    # 印成「摘要」、慢专病档案时间线当 detail 显示。入站留痕在交换日志（`_run_inbound`：报文类型、来源系统、时间、成败，
    # 不记落成哪条就诊）；R4 Encounter 没有摘要 / 备注元素，映射里也没定义摘要取自哪里。存量不动（迁移不改业务数据）
    encounter = create_encounter(
        EncounterCreate(
            patient_id=patient.id,
            org_id=int(org_part),
            doctor_name=doctor_name,
            encounter_type=encounter_type,
            diagnosis_code=diagnosis_code,
            diagnosis_name=diagnosis_name,
        ),
        db,
        user,
    )
    # 登记接口回的是就诊行出参（P2-1631 起按一批带上姓名，`encounters._encounters_out`），不再是 ORM 对象：按键取
    return {
        "encounter_id": encounter["id"],
        "patient_id": encounter["patient_id"],
        "org_id": encounter["org_id"],
        "encounter_type": encounter["encounter_type"],
    }


# ---------- 工程包 I1：FHIR 批量导出（增量水位，省平台前置机拉取） ----------

#: 每资源类型单轮导出上限：分批防单轮长事务/大文件，余量下一轮接着导
FHIR_EXPORT_BATCH_LIMIT = 1000
#: 增量水位（最后已导出主键）落既有 system_params 表（浙#45），不另建表
FHIR_EXPORT_WM_KEYS = {
    "Patient": "fhir_export_wm_patient",
    "Encounter": "fhir_export_wm_encounter",
    "DiagnosticReport": "fhir_export_wm_exam_report",
    # 报告修订史的水位（P2-102）：修订是原地改结论，主键水位看不见它
    "DiagnosticReportAmended": "fhir_export_wm_report_revision",
}


def _fhir_out_dir() -> Path:
    d = Path(settings.upload_dir) / "fhir_out"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _wm_get(db: Session, key: str) -> int:
    row = db.query(SystemParam).filter(SystemParam.key == key).first()
    raw = str(row.value) if row is not None else ""
    return int(raw) if raw.isascii() and raw.isdigit() else 0  # 只认 ASCII（P1-97）


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
    return _fhir_compact({
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
    })


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
        # FHIR 的 dateTime 带到时分就必须带时区（instant 同）；落库是 naive UTC，出口标上（P2-536）
        "period": {"start": to_aware(e.created_at).isoformat()},
    }
    if e.doctor_name:
        resource["participant"] = [{"individual": {"display": e.doctor_name}}]
    if e.diagnosis_code or e.diagnosis_name:
        resource["contained"] = [
            {
                "resourceType": "Condition",
                "id": "dx",
                # R4 的 Condition.subject 是 1..1，内联的同样要带（P2-1079）：做校验的前置机整条拒收
                "subject": {"reference": f"Patient/{ehc_no}"},
                "code": {
                    "coding": (
                        [{"system": ICD10_SYSTEM, "code": e.diagnosis_code}]
                        if e.diagnosis_code
                        else []
                    ),
                    "text": e.diagnosis_name,
                },
            }
        ]
        resource["diagnosis"] = [{"condition": {"reference": "#dx"}}]
    return _fhir_compact(resource)


def fhir_diagnostic_report_resource(
    report: ExamReport, request_id: int, ehc_no: str, status: str = "final", item_code: str = "", item_name: str = ""
) -> dict:
    """ExamReport → FHIR R4 DiagnosticReport（conclusion→conclusion、finding→presentedForm、
    critical→urn:medplat:critical 扩展，与入站承载对称）。修订后再导的一份 `status="amended"`（P2-102）。

    `code` 取申请单的检查项目（P2-1079，对接规范映射表 item_code→code）：R4 的 DiagnosticReport.code 是 1..1，原先不导——
    省平台看不出这是哪项检查，做校验的前置机整条拒收；basedOn 指向的 ServiceRequest 又从不导出。项目编码是本地码，
    系统写 `urn:medplat:exam-item`，名称进 text。"""
    return _fhir_compact({
        "resourceType": "DiagnosticReport",
        "id": str(report.id),
        "status": status,
        "code": {
            "coding": [{"system": EXAM_ITEM_SYSTEM, "code": item_code}] if item_code else [],
            "text": item_name,
        },
        "basedOn": [{"reference": f"ServiceRequest/{request_id}"}],
        "subject": {"reference": f"Patient/{ehc_no}"},
        "issued": to_aware(report.reported_at).isoformat(),
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
    })


def run_fhir_batch_export(db: Session) -> tuple[int, str]:
    """FHIR 批量导出（供 jobs.fhir_batch_export 调用）：按增量水位导 NDJSON。

    - 三类资源（对接规范§二映射表已实现的子集）：Patient / Encounter /
      DiagnosticReport（ExamReport），每类一个 `{类型}_{时间戳}_{起始id}.ndjson`
      （一行一个资源），落 `upload_dir/fhir_out/`；
    - 水位 = 最后已导出主键，存 system_params（key 见 FHIR_EXPORT_WM_KEYS）：
      只导 `id > 水位` 的增量，导完推进水位——重复执行幂等（无增量即不产文件）；
    - `manifest.jsonl` 每个产出文件追加一行（文件名/资源类型/行数/id 区间/时间），
      前置机按 manifest 拉取；
    - 修订过的检查报告再导一次（P2-102）：`DiagnosticReport_amended_*.ndjson`，资源 `status="amended"`、内容是修订后
      的当前版本，manifest 行带 `"kind": "amended"`、id 区间是修订史的主键，水位另记一个 key；
    - **落盘先于推进水位**（P2-1114）：NDJSON 先写进同目录的临时文件（独占创建）、fsync、`os.replace` 换名，manifest
      追加后 fsync，然后才提交水位。水位一推进，这批行就不会再导——原先写完不 fsync 就提交，文件与 manifest 还在页缓存
      里时掉电，重启后文件缺失或为空、manifest 少行，这批数据永远到不了省平台。与归档任务「落盘先于删行」同一口径
      （`jobs._fsync`）；落盘失败照实抛出，水位不动，下一轮重导；
    - **待办（不假装）**：映射表其余资源（Prescription→MedicationRequest、
      Referral→ServiceRequest、Consultation、CarePlan、Appointment 等）尚未
      纳入批量导出，扩展时在本函数追加资源类型并配套新水位 key。
    """
    # 惰性导入：模块级只有 jobs → routers 一个方向（jobs 那头也是惰性导入本函数）
    from ..jobs import _fsync

    out_dir = _fhir_out_dir()
    stamp = now_naive().strftime("%Y%m%d%H%M%S")
    total = 0
    parts: list[str] = []

    def _export(resource_type: str, rows: list[tuple[int, dict]], kind: str = "") -> None:
        """`kind="amended"`：修订后的再导——文件名与 manifest 行带上它，行号是修订史的主键，水位记修订史那一个。"""
        nonlocal total
        if not rows:
            return
        filename = f"{resource_type}_{kind + '_' if kind else ''}{stamp}_{rows[0][0]}.ndjson"
        # 落盘先于推进水位（P2-1114）：临时文件写完 fsync 再换名，前置机按文件名只会看到完整的文件；别把 _fsync 挪到
        # _wm_set 之后。临时文件随机后缀、独占创建：调度锁失效窗口里两路同秒导出，也不会写进同一个临时文件
        tmp = out_dir / f".{filename}.{secrets.token_hex(4)}.tmp"
        fh = tmp.open("x", encoding="utf-8")
        try:
            with fh:
                for _row_id, resource in rows:
                    fh.write(json.dumps(resource, ensure_ascii=False) + "\n")
                _fsync(fh)
            os.replace(tmp, out_dir / filename)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
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
                        **({"kind": kind} if kind else {}),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            _fsync(f)  # manifest 是前置机拉取的账本：这一行落盘之前不能推进水位（P2-1114）
        _wm_set(db, FHIR_EXPORT_WM_KEYS[resource_type + ("Amended" if kind == "amended" else "")], rows[-1][0])
        total += len(rows)
        parts.append(f"{resource_type}{'（修订）' if kind else ''} {len(rows)} 条")

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

    # 修订史先取、「已按新增导出过」的界先记下（P2-168）：原先两样都在本批新增导完之后才读——水位已推进过本批，
    # 先修订、后首次导出的报告同一批里按新增导一次（内容已是修订后的）、又按修订导一次 amended。
    # 先取修订史还顾住了另一头：本批新增导出的报告，内容一定含这里取到的每一条修订（修订与改报告同一次提交）；
    # 取完修订史之后才进来的修订，下一批照常按修订再导
    revisions = (
        db.query(ReportRevision.id, ReportRevision.report_id)
        .filter(ReportRevision.id > _wm_get(db, FHIR_EXPORT_WM_KEYS["DiagnosticReportAmended"]))
        .order_by(ReportRevision.id)
        .limit(FHIR_EXPORT_BATCH_LIMIT)
        .all()
    )
    exported_upto = _wm_get(db, FHIR_EXPORT_WM_KEYS["DiagnosticReport"])

    reports = (
        db.query(ExamReport, ExamRequest.id, Patient.ehc_no, ExamRequest.item_code, ExamRequest.item_name)
        .join(ExamRequest, ExamRequest.id == ExamReport.request_id)
        .join(Patient, Patient.id == ExamRequest.patient_id)
        .filter(ExamReport.id > exported_upto)
        .order_by(ExamReport.id)
        .limit(FHIR_EXPORT_BATCH_LIMIT)
        .all()
    )
    _export(
        "DiagnosticReport",
        [
            (r.id, fhir_diagnostic_report_resource(r, req_id, ehc_no, item_code=code, item_name=name))
            for r, req_id, ehc_no, code, name in reports
        ],
    )

    # 修订过的报告再导一次（P2-102）：修订是原地改结论（修订前的记进修订史），主键水位看不见它——省平台留着的一直是
    # 修订前的结论与危急值标记。按修订史自己的水位取；只导已经按新增导出过的报告（还没导的，到时导出的就是修订后的），
    # 一份报告这一批里修订几次只导一次当前版本；修订史水位推到这一批的最后一条（跳过的也算看过）。
    # 修订史与「导出过」的界都在上面、本批新增导出之前取（P2-168）
    if revisions:
        last_revision = {report_id: revision_id for revision_id, report_id in revisions}
        amended = (
            db.query(ExamReport, ExamRequest.id, Patient.ehc_no, ExamRequest.item_code, ExamRequest.item_name)
            .join(ExamRequest, ExamRequest.id == ExamReport.request_id)
            .join(Patient, Patient.id == ExamRequest.patient_id)
            .filter(ExamReport.id.in_([rid for rid in last_revision if rid <= exported_upto]))
            .all()
        )
        _export(
            "DiagnosticReport",
            sorted(
                (last_revision[r.id], fhir_diagnostic_report_resource(r, req_id, ehc_no, status="amended",
                                                                      item_code=code, item_name=name))
                for r, req_id, ehc_no, code, name in amended
            ),
            kind="amended",
        )
        _wm_set(db, FHIR_EXPORT_WM_KEYS["DiagnosticReportAmended"], revisions[-1][0])

    if not total:
        return 0, "无增量数据（水位未推进，不产文件）"
    return total, "导出 " + "、".join(parts) + " → fhir_out/（manifest 已更新）"
