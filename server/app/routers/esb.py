"""集成平台底座 ESB（浙江省指南 M11 专项）：轻量服务总线与流程编排。

纯 Python 实现，不引入外部消息中间件（Kafka/RabbitMQ 等为外部依赖，
本模块以数据库表承载队列语义，单实例部署即可运行；多实例部署时消费端
以 status=processing 抢占，避免重复消费）。

- EsbEndpoint  接入方注册：code 唯一、令牌散列存储、按分钟限流、可停用；
               出站端点可配 endpoint_url + secret，消费时经 HTTP POST 真实投递
               （HMAC-SHA256 签名头），未配置地址保持"仅登记"
- EsbMessage   消息队列：queued → processing → succeeded / failed（重试）→ dead
- EsbFlow      编排流程：有序步骤 transform | route | validate | persist
- EsbFlowRun   编排执行记录：逐步结果与失败步骤定位

与 integration.py 打通：transform 步骤直接调用入站接口同款转换函数
（integration.parse_hl7v2_patient / parse_fhir_patient），解析口径唯一。
"""
import hashlib
import hmac
import json
import logging
import secrets
import threading
from datetime import datetime, timedelta
from typing import NoReturn

import httpx
from httpx import URL, InvalidURL  # 直接取名：出站用例会把模块里的 httpx 换成假投递，地址校验与异常类得用真的
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import ColumnElement, case, func
from sqlalchemy.orm import Session

from ..concurrency import add_amount, ensure_present, insert_or_conflict, move_row
from ..database import get_db
from ..deps import get_current_user, paginate, require_admin, require_roles, row_dict
from ..models import EsbEndpoint, EsbFlow, EsbFlowRun, EsbMessage, ExchangeLog, User, utcnow
from ..security import hash_password, verify_password
from ..state_store import SlidingWindowRateLimiter
from ..texttypes import NON_BLANK
from .integration import parse_fhir_patient, parse_hl7v2_patient
from .patients import create_patient_idempotent

router = APIRouter(prefix="/api/esb", tags=["集成平台"])
logger = logging.getLogger("medplat.esb")

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
# 出站投递的 HTTP 超时（秒）：投递失败走既有重试/死信机制，不必挂长
DELIVERY_TIMEOUT_SECONDS = 10
# 出站签名头：X-Esb-Signature = HMAC-SHA256(endpoint.secret, 报文体字节) 的十六进制
SIGNATURE_HEADER = "X-Esb-Signature"
# esb_outbound_worker 每轮消费的消息上限（分批，防单轮长事务与全表拉取）
OUTBOUND_BATCH_SIZE = 50


# ---------------------------------------------------------------------------
# 接入方注册（管理员）
# ---------------------------------------------------------------------------


def _check_endpoint_url(value: str | None) -> str | None:
    """投递地址存之前先认得出来（P1-176）：原先只限长度，`http://[::1/x` 这种存得进去，投递时 httpx 抛 `InvalidURL`——
    它不是 `httpx.HTTPError`，逃出重试 / 死信，手工消费 500、定时出站一轮一轮地失败。留空即「仅登记不投递」照旧。"""
    if not value:
        return value
    try:
        url = URL(value)
    except (InvalidURL, TypeError, ValueError) as exc:
        raise ValueError("投递地址不是合法的 URL") from exc
    if url.scheme not in ("http", "https") or not url.host:
        raise ValueError("投递地址须为 http:// 或 https:// 开头、带主机名的地址")
    return value


class EndpointCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    name: str = Field(min_length=1, max_length=128, pattern=NON_BLANK)
    system_type: str = Field(pattern="^(his|lis|pacs|insurance|provincial)$")
    direction: str = Field(default="inbound", pattern="^(inbound|outbound)$")
    rate_limit_per_min: int = Field(default=60, ge=1, le=100000)
    # 出站投递地址：留空即"仅登记"（消费成功但不投递）；签名密钥仅入库不回显
    endpoint_url: str | None = Field(default=None, max_length=512)
    secret: str | None = Field(default=None, max_length=128)
    active: bool = True

    @field_validator("endpoint_url")
    @classmethod
    def _endpoint_url(cls, value: str | None) -> str | None:
        return _check_endpoint_url(value)


class EndpointUpdate(BaseModel):
    # 改档与建档同口径（P1-98）：原先改名为空串照收
    name: str | None = Field(default=None, min_length=1, max_length=128, pattern=NON_BLANK)
    active: bool | None = None
    rate_limit_per_min: int | None = Field(default=None, ge=1, le=100000)
    endpoint_url: str | None = Field(default=None, max_length=512)
    secret: str | None = Field(default=None, max_length=128)

    @field_validator("endpoint_url")
    @classmethod
    def _endpoint_url(cls, value: str | None) -> str | None:
        return _check_endpoint_url(value)


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
        # 投递地址可回显；签名密钥与接入令牌同口径，注册后不再回显
        "endpoint_url": e.endpoint_url or "",
        "created_at": e.created_at.isoformat(),
    }


class EndpointOut(BaseModel):
    """字段与顺序精确镜像 `_endpoint_out`（签名密钥与令牌散列不出参；
    `endpoint_url` 空值以空串呈现，不是 null）。"""

    id: int
    code: str
    name: str
    system_type: str
    system_type_name: str
    direction: str
    direction_name: str
    active: bool
    rate_limit_per_min: int
    endpoint_url: str
    created_at: str


class EndpointWithTokenOut(EndpointOut):
    """注册/轮换回执：接入方字段之外多一个尾键 auth_token（明文仅此一次回显）。"""

    auth_token: str


@router.post(
    "/endpoints",
    response_model=EndpointWithTokenOut,
    status_code=201,
    dependencies=[Depends(require_admin)],
)
def create_endpoint(body: EndpointCreate, db: Session = Depends(get_db)):
    """注册接入方：返回的 auth_token 明文仅此一次可见（库内只留散列）。"""
    if db.query(EsbEndpoint).filter(EsbEndpoint.code == body.code).first():
        raise HTTPException(status_code=409, detail="该接入方编码已存在")
    token = secrets.token_urlsafe(24)
    endpoint = insert_or_conflict(db, EsbEndpoint(**body.model_dump(), auth_token_hash=hash_password(token)), "该接入方编码已存在")
    return {**_endpoint_out(endpoint), "auth_token": token}


@router.get("/endpoints", response_model=list[EndpointOut], dependencies=[Depends(get_current_user)])
def list_endpoints(
    system_type: str | None = None, active: bool | None = None, db: Session = Depends(get_db)
):
    q = db.query(EsbEndpoint)
    if system_type:
        q = q.filter(EsbEndpoint.system_type == system_type)
    if active is not None:
        q = q.filter(EsbEndpoint.active.is_(active))
    return [_endpoint_out(e) for e in q.order_by(EsbEndpoint.code).limit(500).all()]


@router.patch(
    "/endpoints/{endpoint_id}", response_model=EndpointOut, dependencies=[Depends(require_admin)]
)
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


@router.post(
    "/endpoints/{endpoint_id}/rotate-token",
    response_model=EndpointWithTokenOut,
    dependencies=[Depends(require_admin)],
)
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
    msg_type: str = Field(min_length=1, max_length=32, pattern=NON_BLANK)
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


class MessageOut(BaseModel):
    """字段与顺序精确镜像 `_message_out`。

    - `payload` 是接入方投进来的任意 JSON 对象（JSON 列），宽 dict 原样透传
      （workflows.nodes 先例）：嵌套结构与非 ASCII 键不猜不滤；
    - `next_retry_at` 键恒在、值随状态：queued/succeeded/dead 为 null，
      failed 为 ISO 字符串——不是条件键，无需 exclude_unset。
    """

    id: int
    endpoint_id: int
    endpoint_code: str
    msg_type: str
    payload: dict
    status: str
    status_name: str
    retry_count: int
    max_retries: int
    last_error: str
    next_retry_at: str | None
    created_at: str
    updated_at: str


class MessageProcessOut(MessageOut):
    """消费回执：消息全量字段加尾键 detail（成功说明或失败原因，两分支同形）。"""

    detail: str


@router.post("/messages", response_model=MessageOut, status_code=201)
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
    # 出站消息的类型编码要放进投递请求头（`X-Esb-Msg-Type`），请求头只收 ASCII（P2-412）：原先照收，投递时 httpx
    # 抛 UnicodeEncodeError（ValueError 的子类），按失败记一句「'ascii' codec can't encode…」一路重试到死信，这条消息
    # 永远投不出去。入站消息不投递，照旧不限
    if endpoint.direction == "outbound" and not body.msg_type.isascii():
        raise HTTPException(
            status_code=422, detail="出站消息的类型编码须为 ASCII 字符（投递时放在请求头 X-Esb-Msg-Type 里）"
        )
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


# 只给管理员（P0-49）：载荷是接入方投进来的原始报文（HL7 PID 段、FHIR Patient……），证件号、手机号都是明文——原先登录
# 即可，村医一页 500 条翻得到全县过总线的患者身份信息，而同一个人在 /api/patients 上对非管理员是掩码的（§4 出口脱敏）。
# 唯一的调用方是只对管理员开放的集成平台页（app.js 的 esb 页 roles: ["admin"]），接口与它同口径
@router.get("/messages", response_model=list[MessageOut], dependencies=[Depends(require_admin)])
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


def _deliver(endpoint: EsbEndpoint, msg_type: str, body: dict) -> str:
    """真实投递：转换后报文 POST 到端点 endpoint_url，HMAC-SHA256 签名头供对方验签。

    - 报文体 = body 的 JSON 字节（UTF-8，非 ASCII 不转义），签名对**这串字节**计算，
      对方收到后按同一字节流复算即可验签，不依赖 JSON 键序的再序列化稳定性；
    - `secret` 未配置则不带签名头（对接方未约定验签的最小配置）；
    - 响应 2xx 记投递成功（delivered）；非 2xx 与网络异常抛 ValueError，
      由调用方走既有 `_record_failure` 重试/死信机制，不另造一套。
    """
    # 类型编码进请求头，只能是 ASCII（P2-412）：入队时已拦住出站的，这里兜编排路由步骤转投的入站消息与存量行——
    # 说清楚为什么投不出去，不再是一句看不懂的「'ascii' codec can't encode…」
    if not msg_type.isascii():
        raise ValueError(f"消息类型编码「{msg_type}」含非 ASCII 字符，放不进投递请求头 X-Esb-Msg-Type")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Esb-Msg-Type": msg_type}
    if endpoint.secret:
        headers[SIGNATURE_HEADER] = hmac.new(
            endpoint.secret.encode("utf-8"), data, hashlib.sha256
        ).hexdigest()
    try:
        resp = httpx.post(
            endpoint.endpoint_url or "",
            content=data,
            headers=headers,
            timeout=DELIVERY_TIMEOUT_SECONDS,
        )
    except (httpx.HTTPError, InvalidURL) as exc:   # InvalidURL 不是 HTTPError 的子类（P1-176，存量的坏地址）
        raise ValueError(f"投递失败（网络异常）：{exc!r}") from exc
    if not 200 <= resp.status_code < 300:
        raise ValueError(f"投递失败：目标端返回 HTTP {resp.status_code}")
    return f"已投递（delivered）至 {endpoint.endpoint_url}，HTTP {resp.status_code}"


def _outbound_body(message: EsbMessage) -> dict:
    """出站报文：可转换类型（hl7v2_patient / fhir_patient）投递**转换后**的标准化
    字段字典（解析口径与入站同一实现），其它类型原样透传 payload。"""
    if message.msg_type in TRANSFORM_FORMATS:
        return _apply_transform(message.payload or {}, {"format": message.msg_type})
    return dict(message.payload or {})


def _apply_transform(payload: dict, config: dict) -> dict:
    """transform 步骤：复用 integration.py 入站解析（HL7 v2 / FHIR Patient）。

    解析失败一律以 ValueError 抛出（P1-176）：`_run_step` 的约定是「失败以 ValueError/HTTPException 抛出」，三条消费
    路径只接这两类、据此计失败走重试 / 死信。可解析函数碰上形状不对的报文（FHIR 的 `given` 写成字符串、`name` 写成
    对象、证件号写成数字……）抛的是 TypeError / KeyError——手工消费 500、消息原样待处理，定时出站那一轮整个中断，排在
    它后面的所有出站消息一条也投不出去。直连入站接口（`integration._run_inbound`）早就把这类异常收成 422。"""
    try:
        return _parse_payload(payload, config)
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - 报文形状千奇百怪，解析里抛什么都算这条报文解析失败
        raise ValueError(f"报文解析失败（{type(exc).__name__}：{exc}）") from exc


def _parse_payload(payload: dict, config: dict) -> dict:
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
        # 真实投递：目标配置了 endpoint_url 才 POST（报文取 transform 产物，
        # 未经 transform 时投原始 payload）；未配置保持"仅登记"现状并在结果说明。
        if target.endpoint_url:
            body = context.get("data") or context.get("payload") or {}
            detail = _deliver(target, str(context.get("msg_type", "")), body)
            return f"路由至 {target.name}（{target.code}），{detail}"
        return f"路由至 {target.name}（{target.code}）（未配置投递地址，仅登记不投递）"

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


def _claim(db: Session, message: EsbMessage, expect: ColumnElement[bool]) -> bool:
    """抢占这条消息：转「处理中」与判定压进同一条 UPDATE，返回这一路是否抢到（P2-405）。

    模块开头写着「多实例部署时消费端以 status=processing 抢占，避免重复消费」，三条消费路径原先却是锁外读了状态、再
    无条件写「处理中」：经办点「消费 / 重试」的同时定时出站正投着这一条、两位经办同时点、编排执行与手工消费撞在一起，
    几路都读到「待处理」、都往下走——同一条消息投两次，入站建档走两遍。调度器的任务锁只挡得住两轮定时出站互相重叠。
    抢占不单独提交：行锁持有到这一路消费结束，后到的一路等它提交、再按新状态重判（改到 0 行即抢输），出站投递中途
    崩溃的随事务回滚到抢之前。入站建档那一步会中途提交（`create_patient_idempotent`），之后别处读到的是「处理中」；
    手工消费不拒「处理中」——崩溃后卡在这一态的消息只有这条出路，要不要改成「处理中超过时限才可重领」待裁定（P2-406）。"""
    return move_row(db, EsbMessage, message.id, expect, status="processing")


def _as_read(message: EsbMessage) -> ColumnElement[bool]:
    """手工消费 / 编排执行的抢占条件：状态与重试次数还是刚才读到、判过的那样。

    只比状态不够：别处在这中间重试失败一次，状态仍是「失败待重试」，这一路照样会再投一次——次数一并比上。"""
    return (EsbMessage.status == message.status) & (EsbMessage.retry_count == message.retry_count)


def _due(now: datetime) -> ColumnElement[bool]:
    """定时出站可投的消息：待处理的，或到了下次重试时间的失败消息。选批次与逐条抢占用同一个条件——批次是锁外
    选的，逐条提交之后会话里的对象会按库里的最新状态重读，那时它可能已被别处投完，不能按重读到的状态比。"""
    return (EsbMessage.status == "queued") | ((EsbMessage.status == "failed") & (EsbMessage.next_retry_at <= now))


def _claim_lost(db: Session, message: EsbMessage) -> NoReturn:
    """手工一路抢输：回滚、按库里此刻的状态报 409。"""
    db.rollback()
    db.refresh(message)
    raise HTTPException(
        status_code=409,
        detail=f"这条消息刚被别处消费（当前状态 {MSG_STATUS.get(message.status, message.status)}），刷新后再看",
    )


def _process_message(db: Session, message: EsbMessage, endpoint: EsbEndpoint | None = None) -> str:
    """默认消费逻辑（未指定编排流程时）：按端点方向分流。

    出站端点（direction=outbound）：报文转换后经 `_deliver` 真实投递——
    - endpoint_url 已配置：POST 成功（2xx）记 delivered，失败抛 ValueError
      进既有重试/死信；
    - endpoint_url 为空：保持"仅登记"现状，消费成功并在结果里说明未投递。

    入站端点：沿用既有逻辑——
    - hl7v2_patient / fhir_patient：复用 integration 解析并幂等建档；
    - 其它类型：要求 payload 非空（作为通用透传消息的最小校验）。
    """
    if endpoint is not None and endpoint.direction == "outbound":
        if not message.payload:
            raise ValueError("消息载荷为空，无法处理")
        if not endpoint.endpoint_url:
            return "出站端点未配置投递地址（endpoint_url），仅登记不投递"
        return _deliver(endpoint, message.msg_type, _outbound_body(message))
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


def _unexpected(db: Session, message_id: int, exc: Exception) -> tuple[EsbMessage, EsbEndpoint | None, str]:
    """消费中的意外错误照样记成这条消息的一次失败、走重试 / 死信（P1-176）。

    三条消费路径原先只接 ValueError / HTTPException，别的异常一路 500：手工消费的消息原样待处理、重试计数与错误说明
    都是空的；编排里建档那一步已经提交的，消息卡在「处理中」，页面不给消费 / 重试；定时出站那一轮整个中断，排在它
    后面的所有出站消息一条也投不出去——「每条各自 commit：一条投挂不拖累同批其余消息」只对预料到的失败成立。
    先回滚：库报的错（真 PG 上超长的姓名、日期撞列宽）让会话处于失败状态，不回滚什么也写不进去；回滚后按编号重取消息与
    端点。错误全文进日志，消息上记类名与说明（`last_error` 是给页面上的人看的）。"""
    db.rollback()
    logger.exception("ESB 消息 %s 消费时出现未预期错误", message_id)
    message = ensure_present(db.get(EsbMessage, message_id), "消息")
    endpoint = db.get(EsbEndpoint, message.endpoint_id)
    return message, endpoint, f"未预期错误（{type(exc).__name__}：{exc}）"[:1024]


@router.post(
    "/messages/{message_id}/process",
    response_model=MessageProcessOut,
    dependencies=[Depends(require_roles("operator"))],  # 消费/重试=经办（admin 全通）
)
def process_message(message_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """消费一条消息：成功转 succeeded；失败重试计数 +1，达上限转死信。

    回执里的载荷只回给管理员（P0-49）：经办点「消费 / 重试」要的是结果，不是报文里的证件号与手机号——键照旧在，
    值为空对象（与消息清单只给管理员同一个理由）。"""
    message = db.get(EsbMessage, message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="消息不存在")
    if message.status in {"succeeded", "dead"}:
        raise HTTPException(status_code=409, detail=f"消息当前状态 {MSG_STATUS.get(message.status, message.status)} 不可再消费")
    endpoint = db.get(EsbEndpoint, message.endpoint_id)
    # 停用的出站接入方不手工投递（P2-180）：定时消费只取「出站且启用」的、编排的路由步骤拒停用目标，手工消费原先
    # 什么都不看——省平台维护期间停用了端点，经办逐条点「消费/重试」就逐条真投、失败三次进死信，恢复启用后一条
    # 也不再投。拦在改状态之前：消息原样留在队里，启用后照常消费。入站积压停用后能不能手工消化待裁定，此处不拦
    if endpoint is not None and endpoint.direction == "outbound" and not endpoint.active:
        raise HTTPException(status_code=409, detail="出站接入方已停用，不投递——启用后再消费")
    if not _claim(db, message, _as_read(message)):
        _claim_lost(db, message)
    try:
        detail = _process_message(db, message, endpoint)
    except (ValueError, HTTPException) as exc:
        error = exc.detail if isinstance(exc, HTTPException) else str(exc)
        _record_failure(db, message, str(error))
        _log_exchange(db, endpoint, message, False, str(error))
        db.commit()
        return {**_receipt(message, endpoint, user), "detail": str(error)}
    except Exception as exc:  # noqa: BLE001 - 意外错误同样记失败走重试 / 死信，见 _unexpected
        message, endpoint, unexpected = _unexpected(db, message_id, exc)
        _record_failure(db, message, unexpected)
        _log_exchange(db, endpoint, message, False, unexpected)
        db.commit()
        return {**_receipt(message, endpoint, user), "detail": unexpected}
    _record_success(message)
    _log_exchange(db, endpoint, message, True, "")
    db.commit()
    return {**_receipt(message, endpoint, user), "detail": detail}


def _receipt(message: EsbMessage, endpoint: EsbEndpoint | None, user: User) -> dict:
    """消费回执的消息部分：非管理员不回显载荷（P0-49，见 `process_message`）。"""
    out = _message_out(message, endpoint.code if endpoint else "")
    if user.role != "admin":
        out["payload"] = {}
    return out


def consume_pending_outbound(db: Session, batch_size: int = OUTBOUND_BATCH_SIZE) -> tuple[int, str]:
    """周期消费出站待投递消息（供 jobs.esb_outbound_worker 调用，也可测试直调）。

    口径：
    - 只挑**出站端点**（direction=outbound 且 active）的消息；
    - queued 立即可投；failed 须到达 next_retry_at（尊重既有指数退避），
      succeeded/dead/processing 一律不碰；
    - 每轮至多 batch_size 条（分批，防单轮长事务），按 id 先进先出；
    - 每条各自 commit：一条投挂不拖累同批其余消息；
    - 成败均落 ExchangeLog（与手工消费同一监控口径）；告警交由日志——
      本函数只返回摘要，失败计数由调用方（定时任务）记入 JobRun.message。
    """
    now = utcnow()
    rows = (
        db.query(EsbMessage)
        .join(EsbEndpoint, EsbEndpoint.id == EsbMessage.endpoint_id)
        .filter(
            EsbEndpoint.direction == "outbound",
            EsbEndpoint.active.is_(True),
            _due(now),
        )
        .order_by(EsbMessage.id)
        .limit(batch_size)
        .all()
    )
    delivered = failed = 0
    for message in rows:
        endpoint = db.get(EsbEndpoint, message.endpoint_id)
        # 逐条复查启用状态（P2-180）：上面只在取批次时看了一眼，一批最多 50 条、每条最长等 10 秒，中途停用的端点
        # 原先照样把这一批投完。每条之后都提交（会话里的对象随之过期），这里取到的是库里的最新状态
        if endpoint is None or not endpoint.active:
            continue
        message_id = message.id
        if not _claim(db, message, _due(now)):
            continue   # 选出之后被别处消费了（手工消费 / 编排执行），这一轮不再投（P2-405）
        try:
            _process_message(db, message, endpoint)
        except (ValueError, HTTPException) as exc:
            error = exc.detail if isinstance(exc, HTTPException) else str(exc)
            _record_failure(db, message, str(error))
            _log_exchange(db, endpoint, message, False, str(error))
            failed += 1
        except Exception as exc:  # noqa: BLE001 - 一条的意外错误不拖垮这一轮，见 _unexpected
            message, endpoint, unexpected = _unexpected(db, message_id, exc)
            _record_failure(db, message, unexpected)
            _log_exchange(db, endpoint, message, False, unexpected)
            failed += 1
        else:
            _record_success(message)
            _log_exchange(db, endpoint, message, True, "")
            delivered += 1
        db.commit()
    return delivered + failed, (
        f"出站消费 {delivered + failed} 条：成功 {delivered}，失败 {failed}（失败走重试/死信）"
    )


# ---------------------------------------------------------------------------
# 流程编排
# ---------------------------------------------------------------------------


class FlowCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    name: str = Field(min_length=1, max_length=128, pattern=NON_BLANK)
    steps: list[dict] = Field(min_length=1)
    active: bool = True


class FlowUpdate(BaseModel):
    # 改档与建档同口径（P1-98）：原先改名为空串、把步骤改成空数组照收——零步骤的流程跑起来什么也不做
    name: str | None = Field(default=None, min_length=1, max_length=128, pattern=NON_BLANK)
    steps: list[dict] | None = Field(default=None, min_length=1)
    active: bool | None = None


def _validate_steps(steps: list) -> None:
    for idx, step in enumerate(steps, start=1):
        if not isinstance(step, dict) or step.get("type") not in STEP_TYPES:
            raise HTTPException(
                status_code=422, detail=f"第 {idx} 步类型无效（须为 transform/route/validate/persist）"
            )
        if step.get("config") is not None and not isinstance(step["config"], dict):
            raise HTTPException(status_code=422, detail=f"第 {idx} 步 config 须为对象")
        # 配置项的形状也在存的时候查（P1-176）：原先 `{"required": 5}` 照收，跑起来 TypeError——建档那一步已经提交，
        # 消息卡在「处理中」，页面上消费 / 重试按钮都不给
        config = step.get("config") or {}
        required = config.get("required", [])
        if not isinstance(required, list) or not all(isinstance(f, str) for f in required):
            raise HTTPException(status_code=422, detail=f"第 {idx} 步 required 须为字段名数组")
        for key in ("format", "source_field", "target_endpoint", "entity"):
            if key in config and not isinstance(config[key], str):
                raise HTTPException(status_code=422, detail=f"第 {idx} 步 {key} 须为字符串")


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


class FlowOut(BaseModel):
    """字段与顺序精确镜像 `_flow_out`。

    `steps` 是建流程时提交的原样步骤数组（JSON 列）：`_validate_steps` 只约束
    type/config 两键，步骤 dict 里的其余自定义键原样保留——宽 dict 透传，
    逐字段建模会把它们静默滤掉（workflows.nodes 先例）。
    """

    id: int
    code: str
    name: str
    steps: list[dict]
    step_count: int
    active: bool
    created_at: str


@router.post("/flows", response_model=FlowOut, status_code=201, dependencies=[Depends(require_admin)])
def create_flow(body: FlowCreate, db: Session = Depends(get_db)):
    if db.query(EsbFlow).filter(EsbFlow.code == body.code).first():
        raise HTTPException(status_code=409, detail="该流程编码已存在")
    _validate_steps(body.steps)
    flow = insert_or_conflict(db, EsbFlow(**body.model_dump()), "该流程编码已存在")
    return _flow_out(flow)


@router.get("/flows", response_model=list[FlowOut], dependencies=[Depends(get_current_user)])
def list_flows(active: bool | None = None, db: Session = Depends(get_db)):
    q = db.query(EsbFlow)
    if active is not None:
        q = q.filter(EsbFlow.active.is_(active))
    return [_flow_out(f) for f in q.order_by(EsbFlow.code).limit(200).all()]


@router.patch("/flows/{flow_id}", response_model=FlowOut, dependencies=[Depends(require_admin)])
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


class FlowStepResultOut(BaseModel):
    """单步执行结果：`run_flow` 是唯一产地（落库快照亦出自它），恒为四键
    （成功/失败仅 status/detail 取值不同）——照 quality.defects 先例逐字段建模。"""

    step: int
    type: str
    status: str
    detail: str


class FlowRunResultOut(BaseModel):
    """`run_flow` 的执行回执（带消息侧终态）；列表行另有 flow_id/created_at，不同形。"""

    id: int
    flow_code: str
    message_id: int
    status: str
    step_results: list[FlowStepResultOut]
    error: str
    message_status: str
    retry_count: int


@router.post(
    "/flows/{code}/run",
    response_model=FlowRunResultOut,
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
        raise HTTPException(status_code=409, detail=f"消息当前状态 {MSG_STATUS.get(message.status, message.status)} 不可再消费")
    endpoint = db.get(EsbEndpoint, message.endpoint_id)

    if not _claim(db, message, _as_read(message)):
        _claim_lost(db, message)
    context: dict = {"payload": message.payload or {}, "msg_type": message.msg_type}
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
        except Exception as exc:  # noqa: BLE001 - 意外错误同样记这一步失败、消息走重试 / 死信，见 _unexpected
            message, endpoint, error = _unexpected(db, message_id, exc)
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


class FlowRunOut(BaseModel):
    """执行记录列表行：与执行回执不同形（有 flow_id/created_at，无消息终态两键）。"""

    id: int
    flow_id: int
    flow_code: str
    message_id: int
    status: str
    step_results: list[FlowStepResultOut]
    error: str
    created_at: str


# 同上只给管理员（P0-49）：逐步结果里有建档那一步落下的健康卡号，唯一的调用方同样是管理员才看得到的集成平台页
@router.get("/flow-runs", response_model=list[FlowRunOut], dependencies=[Depends(require_admin)])
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


class EsbStatsTotalsOut(BaseModel):
    """全局合计。计数键恒 int（COUNT 与 int 累加，声明成 float 会把 6 变 6.0，
    即改字节）；比率键恒 float（`round(x*100.0/n, 2)` 与兜底字面量 `0.0`
    两条分支都是浮点，不存在 Money 那种 int/float 并存）。"""

    total: int
    succeeded: int
    dead: int
    backlog: int
    success_rate_pct: float
    failure_rate_pct: float
    endpoints: int
    flows: int


class EsbStatsEndpointOut(BaseModel):
    """分端点行：int/float 之别同上。"""

    endpoint_id: int
    endpoint_code: str
    endpoint_name: str
    total: int
    succeeded: int
    dead: int
    queued: int
    failed: int
    backlog: int
    success_rate_pct: float
    failure_rate_pct: float


class EsbStatsOut(BaseModel):
    totals: EsbStatsTotalsOut
    by_endpoint: list[EsbStatsEndpointOut]


@router.get("/stats", response_model=EsbStatsOut, dependencies=[Depends(get_current_user)])
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
        .order_by(EsbMessage.endpoint_id)
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
