"""集成平台底座 ESB（浙江省指南 M11 专项）：轻量服务总线与流程编排。

纯 Python 实现，不引入外部消息中间件（Kafka/RabbitMQ 等为外部依赖，
本模块以数据库表承载队列语义，单实例部署即可运行；多实例部署时消费端
以 status=processing 抢占，避免重复消费）。

- EsbEndpoint  接入方注册：code 唯一、令牌散列存储、按分钟限流、可停用
- EsbMessage   消息队列：queued → processing → succeeded / failed（重试）→ dead
- EsbFlow      编排流程：有序步骤 transform | route | validate | persist
- EsbFlowRun   编排执行记录：逐步结果与失败步骤定位

与 integration.py 打通：transform 步骤直接调用入站接口同款转换函数
（integration.parse_hl7v2_patient / parse_fhir_patient），解析口径唯一。
"""
import secrets
import threading
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from ..concurrency import add_amount, insert_or_conflict
from ..database import get_db
from ..deps import get_current_user, paginate, require_admin, require_roles, row_dict
from ..models import EsbEndpoint, EsbFlow, EsbFlowRun, EsbMessage, ExchangeLog, utcnow
from ..security import hash_password, verify_password
from ..state_store import SlidingWindowRateLimiter
from .integration import parse_fhir_patient, parse_hl7v2_patient
from .patients import create_patient_idempotent

router = APIRouter(prefix="/api/esb", tags=["集成平台"])

SYSTEM_TYPES = {
    "his": "医院信息系统",
    "lis": "检验系统",
    "pacs": "影像系统",
    "insurance": "医保系统",
    "provincial": "省级平台",
}
DIRECTIONS = {"inbound": "入站", "outbound": "出站"}
MSG_STATUS = {
    "queued": "待处理",
    "processing": "处理中",
    "succeeded": "成功",
    "failed": "失败待重试",
    "dead": "死信",
}
STEP_TYPES = {"transform": "转换", "route": "路由", "validate": "校验", "persist": "落库"}
# 支持的转换格式 → 复用 integration.py 的入站解析实现
TRANSFORM_FORMATS = {"hl7v2_patient": "HL7 v2 ADT 患者", "fhir_patient": "FHIR R4 Patient"}
# 重试退避基数（秒）：第 n 次失败后 next_retry_at = now + BACKOFF_SECONDS * 2^(n-1)
BACKOFF_SECONDS = 60


# ---------------------------------------------------------------------------
# 接入方注册（管理员）
# ---------------------------------------------------------------------------


class EndpointCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    system_type: str = Field(pattern="^(his|lis|pacs|insurance|provincial)$")
    direction: str = Field(default="inbound", pattern="^(inbound|outbound)$")
    rate_limit_per_min: int = Field(default=60, ge=1, le=100000)
    active: bool = True


class EndpointUpdate(BaseModel):
    name: str | None = None
    active: bool | None = None
    rate_limit_per_min: int | None = Field(default=None, ge=1, le=100000)


def _endpoint_out(e: EsbEndpoint) -> dict:
    return {
        "id": e.id,
        "code": e.code,
        "name": e.name,
        "system_type": e.system_type,
        "system_type_name": SYSTEM_TYPES.get(e.system_type, e.system_type),
        "direction": e.direction,
        "direction_name": DIRECTIONS.get(e.direction, e.direction),
        "active": e.active,
        "rate_limit_per_min": e.rate_limit_per_min,
        "created_at": e.created_at.isoformat(),
    }


@router.post("/endpoints", status_code=201, dependencies=[Depends(require_admin)])
def create_endpoint(body: EndpointCreate, db: Session = Depends(get_db)):
    """注册接入方：返回的 auth_token 明文仅此一次可见（库内只留散列）。"""
    if db.query(EsbEndpoint).filter(EsbEndpoint.code == body.code).first():
        raise HTTPException(status_code=409, detail="该接入方编码已存在")
    token = secrets.token_urlsafe(24)
    endpoint = insert_or_conflict(db, EsbEndpoint(**body.model_dump(), auth_token_hash=hash_password(token)), "该接入方编码已存在")
    return {**_endpoint_out(endpoint), "auth_token": token}


@router.get("/endpoints", dependencies=[Depends(get_current_user)])
def list_endpoints(
    system_type: str | None = None, active: bool | None = None, db: Session = Depends(get_db)
):
    q = db.query(EsbEndpoint)
    if system_type:
        q = q.filter(EsbEndpoint.system_type == system_type)
    if active is not None:
        q = q.filter(EsbEndpoint.active.is_(active))
    return [_endpoint_out(e) for e in q.order_by(EsbEndpoint.code).limit(500).all()]


@router.patch("/endpoints/{endpoint_id}", dependencies=[Depends(require_admin)])
def update_endpoint(endpoint_id: int, body: EndpointUpdate, db: Session = Depends(get_db)):
    endpoint = db.get(EsbEndpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="接入方不存在")
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(endpoint, field, value)
    db.commit()
    _reset_limiter(endpoint.id)
    return _endpoint_out(endpoint)


@router.post("/endpoints/{endpoint_id}/rotate-token", dependencies=[Depends(require_admin)])
def rotate_endpoint_token(endpoint_id: int, db: Session = Depends(get_db)):
    """令牌轮换：旧令牌立即失效，新令牌明文仅此一次返回。"""
    endpoint = db.get(EsbEndpoint, endpoint_id)
    if endpoint is None:
        raise HTTPException(status_code=404, detail="接入方不存在")
    token = secrets.token_urlsafe(24)
    endpoint.auth_token_hash = hash_password(token)
    db.commit()
    return {**_endpoint_out(endpoint), "auth_token": token}


# ---------------------------------------------------------------------------
# 入队鉴权与限流
# ---------------------------------------------------------------------------

# 每个端点一套滑动窗口限速器；端点限额变更或重启后重建
_LIMITERS: dict[int, tuple[int, SlidingWindowRateLimiter]] = {}
_LIMITER_LOCK = threading.Lock()


def _reset_limiter(endpoint_id: int) -> None:
    with _LIMITER_LOCK:
        _LIMITERS.pop(endpoint_id, None)


def _allow(endpoint: EsbEndpoint) -> bool:
    with _LIMITER_LOCK:
        cached = _LIMITERS.get(endpoint.id)
        if cached is None or cached[0] != endpoint.rate_limit_per_min:
            cached = (
                endpoint.rate_limit_per_min,
                SlidingWindowRateLimiter(
                    max_events=endpoint.rate_limit_per_min, window_seconds=60
                ),
            )
            _LIMITERS[endpoint.id] = cached
        limiter = cached[1]
    return limiter.allow(str(endpoint.id))


def _authenticate_endpoint(db: Session, code: str, token: str) -> EsbEndpoint:
    """接入方鉴权：编码/令牌任一不符一律 401（不区分原因，避免探测）。"""
    endpoint = db.query(EsbEndpoint).filter(EsbEndpoint.code == code).first() if code else None
    if endpoint is None or not token or not verify_password(token, endpoint.auth_token_hash):
        raise HTTPException(status_code=401, detail="接入方编码或令牌无效")
    if not endpoint.active:
        raise HTTPException(status_code=403, detail="接入方已停用")
    return endpoint


# ---------------------------------------------------------------------------
# 消息队列
# ---------------------------------------------------------------------------


class MessageIn(BaseModel):
    msg_type: str = Field(min_length=1, max_length=32)
    payload: dict = Field(default_factory=dict)
    max_retries: int = Field(default=3, ge=0, le=10)


def _message_out(m: EsbMessage, endpoint_code: str = "") -> dict:
    return {
        "id": m.id,
        "endpoint_id": m.endpoint_id,
        "endpoint_code": endpoint_code,
        "msg_type": m.msg_type,
        "payload": m.payload,
        "status": m.status,
        "status_name": MSG_STATUS.get(m.status, m.status),
        "retry_count": m.retry_count,
        "max_retries": m.max_retries,
        "last_error": m.last_error,
        "next_retry_at": m.next_retry_at.isoformat() if m.next_retry_at else None,
        "created_at": m.created_at.isoformat(),
        "updated_at": m.updated_at.isoformat(),
    }


@router.post("/messages", status_code=201)
def enqueue_message(
    body: MessageIn,
    db: Session = Depends(get_db),
    x_esb_endpoint: str = Header(default=""),
    x_esb_token: str = Header(default=""),
):
    """消息入队：接入方以 X-Esb-Endpoint + X-Esb-Token 鉴权（非平台账号 JWT）。

    鉴权失败 401、停用 403、超出端点分钟限额 429。
    """
    endpoint = _authenticate_endpoint(db, x_esb_endpoint, x_esb_token)
    if not _allow(endpoint):
        raise HTTPException(
            status_code=429,
            detail=f"超出接入方限流配额（{endpoint.rate_limit_per_min} 条/分钟），请稍后重试",
        )
    message = EsbMessage(endpoint_id=endpoint.id, **body.model_dump())
    db.add(message)
    db.commit()
    db.refresh(message)
    return _message_out(message, endpoint.code)


@router.get("/messages", dependencies=[Depends(get_current_user)])
def list_messages(
    response: Response,
    status: str | None = None,
    endpoint_id: int | None = None,
    msg_type: str | None = None,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """消息队列查询：按状态/端点/类型过滤，分页（总数见 X-Total-Count 响应头）。"""
    if status and status not in MSG_STATUS:
        raise HTTPException(status_code=422, detail="未知消息状态")
    q = db.query(EsbMessage)
    if status:
        q = q.filter(EsbMessage.status == status)
    if endpoint_id is not None:
        q = q.filter(EsbMessage.endpoint_id == endpoint_id)
    if msg_type:
        q = q.filter(EsbMessage.msg_type == msg_type)
    rows = paginate(q.order_by(EsbMessage.id.desc()), response, offset, limit)
    codes = row_dict(db.query(EsbEndpoint.id, EsbEndpoint.code).all())
    return [_message_out(m, codes.get(m.endpoint_id, "")) for m in rows]


def _record_success(message: EsbMessage) -> None:
    message.status = "succeeded"
    message.last_error = ""
    message.next_retry_at = None
    message.updated_at = utcnow()


def _record_failure(db: Session, message: EsbMessage, error: str) -> None:
    """失败记账：重试次数 +1，达到上限转死信（保留最后错误，不再排下次重试）。

    重试次数走原子累加：同一条消息被两个 worker 同时重投时，`+=` 会丢更新，
    次数涨不上去，这条消息就永远进不了死信队列——**一直重投下去**。
    """
    add_amount(db, EsbMessage, message.id, "retry_count", 1)
    db.flush()
    db.refresh(message)  # 上面走的是 Core UPDATE，要按新次数判死信
    message.last_error = error[:1024]
    message.updated_at = utcnow()
    if message.retry_count >= message.max_retries:
        message.status = "dead"
        message.next_retry_at = None
    else:
        message.status = "failed"
        message.next_retry_at = utcnow() + timedelta(
            seconds=BACKOFF_SECONDS * (2 ** (message.retry_count - 1))
        )


def _log_exchange(db: Session, endpoint: EsbEndpoint | None, message: EsbMessage, success: bool, detail: str) -> None:
    """消费结果落既有交换日志（与 M11 交换监控同一口径，不另起统计表）。"""
    db.add(
        ExchangeLog(
            source_system=(endpoint.code if endpoint else "")[:64],
            message_type=f"esb_{message.msg_type}"[:32],
            direction=endpoint.direction if endpoint else "inbound",
            success=success,
            error_detail=detail[:1024],
        )
    )


def _apply_transform(payload: dict, config: dict) -> dict:
    """transform 步骤：复用 integration.py 入站解析（HL7 v2 / FHIR Patient）。"""
    fmt = config.get("format", "")
    if fmt not in TRANSFORM_FORMATS:
        raise ValueError(f"未知转换格式 {fmt or '(空)'}")
    if fmt == "hl7v2_patient":
        raw = payload.get(config.get("source_field", "message"))
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("HL7 v2 转换需要 payload 中的报文文本字段")
        data, control_id = parse_hl7v2_patient(raw)
        return {**data, "control_id": control_id}
    resource = payload.get(config.get("source_field", "resource"), payload)
    if not isinstance(resource, dict):
        raise ValueError("FHIR 转换需要 payload 中的资源对象")
    return parse_fhir_patient(resource)


def _run_step(db: Session, step: dict, context: dict) -> str:
    """执行单个编排步骤；返回可读结果说明，失败以 ValueError/HTTPException 抛出。"""
    step_type = step.get("type", "")
    config = step.get("config") or {}
    if step_type not in STEP_TYPES:
        raise ValueError(f"未知步骤类型 {step_type or '(空)'}")

    if step_type == "transform":
        context["data"] = _apply_transform(context["payload"], config)
        return f"转换 {TRANSFORM_FORMATS[config.get('format', '')]}，产出 {len(context['data'])} 个字段"

    if step_type == "validate":
        data = context.get("data") or context["payload"]
        missing = [
            f for f in config.get("required", []) if not str(data.get(f, "") or "").strip()
        ]
        if missing:
            raise ValueError(f"必填字段缺失：{'、'.join(missing)}")
        return f"校验通过（{len(config.get('required', []))} 项必填）"

    if step_type == "route":
        target_code = config.get("target_endpoint", "")
        target = db.query(EsbEndpoint).filter(EsbEndpoint.code == target_code).first()
        if target is None:
            raise ValueError(f"路由目标接入方 {target_code or '(空)'} 不存在")
        if not target.active:
            raise ValueError(f"路由目标接入方 {target_code} 已停用")
        context["routed_to"] = target.code
        return f"路由至 {target.name}（{target.code}）"

    # persist
    entity = config.get("entity", "patient")
    if entity == "patient":
        data = context.get("data")
        if not data:
            raise ValueError("落库步骤须先经 transform 产出标准化数据")
        patient, created = create_patient_idempotent(
            db,
            {
                "name": data.get("name", ""),
                "id_card": data.get("id_card", ""),
                "gender": data.get("gender", "未知"),
                "birth_date": data.get("birth_date", ""),
                "phone": data.get("phone", ""),
            },
        )
        context["patient_id"] = patient.id
        return f"患者档案{'新建' if created else '已存在'}：{patient.ehc_no}"
    if entity == "exchange_log":
        db.add(
            ExchangeLog(
                source_system=str(config.get("source_system", ""))[:64],
                message_type=str(config.get("message_type", "esb_flow"))[:32],
                direction="inbound",
                success=True,
                error_detail="",
            )
        )
        return "交换日志已落库"
    raise ValueError(f"未知落库实体 {entity}")


def _process_message(db: Session, message: EsbMessage) -> str:
    """默认消费逻辑（未指定编排流程时）：按 msg_type 走内置转换校验。

    - hl7v2_patient / fhir_patient：复用 integration 解析并幂等建档；
    - 其它类型：要求 payload 非空（作为通用透传消息的最小校验）。
    """
    if message.msg_type in TRANSFORM_FORMATS:
        data = _apply_transform(message.payload, {"format": message.msg_type})
        patient, created = create_patient_idempotent(
            db,
            {
                "name": data["name"],
                "id_card": data["id_card"],
                "gender": data["gender"],
                "birth_date": data["birth_date"],
                "phone": data["phone"],
            },
        )
        return f"患者档案{'新建' if created else '已存在'}：{patient.ehc_no}"
    if not message.payload:
        raise ValueError("消息载荷为空，无法处理")
    return f"透传消息已处理（{len(message.payload)} 个字段）"


@router.post(
    "/messages/{message_id}/process",
    dependencies=[Depends(require_roles("operator"))],  # 消费/重试=经办（admin 全通）
)
def process_message(message_id: int, db: Session = Depends(get_db)):
    """消费一条消息：成功转 succeeded；失败重试计数 +1，达上限转死信。"""
    message = db.get(EsbMessage, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="消息不存在")
    if message.status in {"succeeded", "dead"}:
        raise HTTPException(status_code=409, detail=f"消息当前状态 {message.status} 不可再消费")
    endpoint = db.get(EsbEndpoint, message.endpoint_id)
    message.status = "processing"
    db.flush()
    try:
        detail = _process_message(db, message)
    except (ValueError, HTTPException) as exc:
        error = exc.detail if isinstance(exc, HTTPException) else str(exc)
        _record_failure(db, message, str(error))
        _log_exchange(db, endpoint, message, False, str(error))
        db.commit()
        return {**_message_out(message, endpoint.code if endpoint else ""), "detail": str(error)}
    _record_success(message)
    _log_exchange(db, endpoint, message, True, "")
    db.commit()
    return {**_message_out(message, endpoint.code if endpoint else ""), "detail": detail}


# ---------------------------------------------------------------------------
# 流程编排
# ---------------------------------------------------------------------------


class FlowCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    steps: list[dict] = Field(min_length=1)
    active: bool = True


class FlowUpdate(BaseModel):
    name: str | None = None
    steps: list[dict] | None = None
    active: bool | None = None


def _validate_steps(steps: list) -> None:
    for idx, step in enumerate(steps, start=1):
        if not isinstance(step, dict) or step.get("type") not in STEP_TYPES:
            raise HTTPException(
                status_code=422, detail=f"第 {idx} 步类型无效（须为 transform/route/validate/persist）"
            )
        if step.get("config") is not None and not isinstance(step["config"], dict):
            raise HTTPException(status_code=422, detail=f"第 {idx} 步 config 须为对象")


def _flow_out(f: EsbFlow) -> dict:
    return {
        "id": f.id,
        "code": f.code,
        "name": f.name,
        "steps": f.steps,
        "step_count": len(f.steps or []),
        "active": f.active,
        "created_at": f.created_at.isoformat(),
    }


@router.post("/flows", status_code=201, dependencies=[Depends(require_admin)])
def create_flow(body: FlowCreate, db: Session = Depends(get_db)):
    if db.query(EsbFlow).filter(EsbFlow.code == body.code).first():
        raise HTTPException(status_code=409, detail="该流程编码已存在")
    _validate_steps(body.steps)
    flow = insert_or_conflict(db, EsbFlow(**body.model_dump()), "该流程编码已存在")
    return _flow_out(flow)


@router.get("/flows", dependencies=[Depends(get_current_user)])
def list_flows(active: bool | None = None, db: Session = Depends(get_db)):
    q = db.query(EsbFlow)
    if active is not None:
        q = q.filter(EsbFlow.active.is_(active))
    return [_flow_out(f) for f in q.order_by(EsbFlow.code).limit(200).all()]


@router.patch("/flows/{flow_id}", dependencies=[Depends(require_admin)])
def update_flow(flow_id: int, body: FlowUpdate, db: Session = Depends(get_db)):
    flow = db.get(EsbFlow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="流程不存在")
    payload = body.model_dump(exclude_unset=True)
    if payload.get("steps") is not None:
        _validate_steps(payload["steps"])
    for field, value in payload.items():
        if value is not None:
            setattr(flow, field, value)
    db.commit()
    return _flow_out(flow)


@router.post(
    "/flows/{code}/run",
    dependencies=[Depends(require_roles("operator"))],
)
def run_flow(code: str, message_id: int, db: Session = Depends(get_db)):
    """按流程编排消费指定消息：逐步执行并记录每步结果，失败步骤之后不再执行。"""
    flow = db.query(EsbFlow).filter(EsbFlow.code == code).first()
    if flow is None:
        raise HTTPException(status_code=404, detail="流程不存在")
    if not flow.active:
        raise HTTPException(status_code=409, detail="流程已停用")
    message = db.get(EsbMessage, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="消息不存在")
    if message.status in {"succeeded", "dead"}:
        raise HTTPException(status_code=409, detail=f"消息当前状态 {message.status} 不可再消费")
    endpoint = db.get(EsbEndpoint, message.endpoint_id)

    message.status = "processing"
    db.flush()
    context: dict = {"payload": message.payload or {}}
    step_results: list[dict] = []
    error = ""
    for idx, step in enumerate(flow.steps or [], start=1):
        step_type = step.get("type", "")
        try:
            detail = _run_step(db, step, context)
        except (ValueError, HTTPException) as exc:
            error = str(exc.detail if isinstance(exc, HTTPException) else exc)
            step_results.append(
                {"step": idx, "type": step_type, "status": "failed", "detail": error}
            )
            break
        step_results.append(
            {"step": idx, "type": step_type, "status": "succeeded", "detail": detail}
        )

    if error:
        _record_failure(db, message, f"第 {len(step_results)} 步（{step_results[-1]['type']}）失败：{error}")
    else:
        _record_success(message)
    _log_exchange(db, endpoint, message, not error, error)
    run = EsbFlowRun(
        flow_id=flow.id,
        message_id=message.id,
        status="failed" if error else "succeeded",
        step_results=step_results,
        error=error[:1024],
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return {
        "id": run.id,
        "flow_code": flow.code,
        "message_id": message.id,
        "status": run.status,
        "step_results": run.step_results,
        "error": run.error,
        "message_status": message.status,
        "retry_count": message.retry_count,
    }


@router.get("/flow-runs", dependencies=[Depends(get_current_user)])
def list_flow_runs(
    response: Response,
    flow_id: int | None = None,
    message_id: int | None = None,
    status: str | None = None,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    q = db.query(EsbFlowRun)
    if flow_id is not None:
        q = q.filter(EsbFlowRun.flow_id == flow_id)
    if message_id is not None:
        q = q.filter(EsbFlowRun.message_id == message_id)
    if status:
        q = q.filter(EsbFlowRun.status == status)
    rows = paginate(q.order_by(EsbFlowRun.id.desc()), response, offset, limit)
    codes = row_dict(db.query(EsbFlow.id, EsbFlow.code).all())
    return [
        {
            "id": r.id,
            "flow_id": r.flow_id,
            "flow_code": codes.get(r.flow_id, ""),
            "message_id": r.message_id,
            "status": r.status,
            "step_results": r.step_results,
            "error": r.error,
            "created_at": r.created_at.isoformat(),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 统计看板
# ---------------------------------------------------------------------------


@router.get("/stats", dependencies=[Depends(get_current_user)])
def esb_stats(db: Session = Depends(get_db)):
    """总线统计口径：

    - backlog 积压 = queued + failed（待重试仍属积压）+ processing（消费中）
    - success_rate_pct = succeeded / 已终结消息数（succeeded + dead），
      未终结（queued/failed/processing）不计入分母，避免积压压低成功率；
    - failure_rate_pct = dead / 已终结消息数（与成功率互补）。
    """
    rows = (
        db.query(
            EsbMessage.endpoint_id,
            func.count(EsbMessage.id).label("total"),
            func.sum(case((EsbMessage.status == "succeeded", 1), else_=0)).label("succeeded"),
            func.sum(case((EsbMessage.status == "dead", 1), else_=0)).label("dead"),
            func.sum(case((EsbMessage.status == "queued", 1), else_=0)).label("queued"),
            func.sum(case((EsbMessage.status == "failed", 1), else_=0)).label("failed"),
            func.sum(case((EsbMessage.status == "processing", 1), else_=0)).label("processing"),
        )
        .group_by(EsbMessage.endpoint_id)
        .all()
    )
    endpoints = {e.id: e for e in db.query(EsbEndpoint).all()}
    by_endpoint = []
    # 后面还要塞 success_rate_pct 等 float，值类型不能被推断成 int
    totals: dict[str, float] = {"total": 0, "succeeded": 0, "dead": 0, "backlog": 0}
    for r in rows:
        succeeded, dead = int(r.succeeded or 0), int(r.dead or 0)
        finished = succeeded + dead
        backlog = int(r.queued or 0) + int(r.failed or 0) + int(r.processing or 0)
        endpoint = endpoints.get(r.endpoint_id)
        by_endpoint.append(
            {
                "endpoint_id": r.endpoint_id,
                "endpoint_code": endpoint.code if endpoint else "",
                "endpoint_name": endpoint.name if endpoint else "",
                "total": r.total,
                "succeeded": succeeded,
                "dead": dead,
                "queued": int(r.queued or 0),
                "failed": int(r.failed or 0),
                "backlog": backlog,
                "success_rate_pct": round(succeeded * 100.0 / finished, 2) if finished else 0.0,
                "failure_rate_pct": round(dead * 100.0 / finished, 2) if finished else 0.0,
            }
        )
        totals["total"] += r.total
        totals["succeeded"] += succeeded
        totals["dead"] += dead
        totals["backlog"] += backlog
    finished_all = totals["succeeded"] + totals["dead"]
    totals["success_rate_pct"] = (
        round(totals["succeeded"] * 100.0 / finished_all, 2) if finished_all else 0.0
    )
    totals["failure_rate_pct"] = (
        round(totals["dead"] * 100.0 / finished_all, 2) if finished_all else 0.0
    )
    totals["endpoints"] = db.query(func.count(EsbEndpoint.id)).scalar() or 0
    totals["flows"] = db.query(func.count(EsbFlow.id)).scalar() or 0
    return {"totals": totals, "by_endpoint": by_endpoint}
