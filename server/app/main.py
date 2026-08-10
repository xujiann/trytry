"""紧密型县域医共体信息化平台 —— 第一期基础平台服务。

已实现模块（对应《县域医共体信息化平台建设规划》第一期）：
- 统一认证（JWT 令牌，角色权限）
- 医共体成员单位（机构）管理
- 患者主索引 EMPI / 电子健康卡号
- 统一编码字典（诊断、药品、耗材、收费"四统一"）
- 双向转诊状态流转
"""
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .config import settings
from .database import Base, SessionLocal, engine
from .models import User
from .routers import (
    admin_mgmt,
    appointments,
    auth,
    chronic,
    consultations,
    contracts,
    cssd,
    dictionaries,
    education,
    eldercare,
    emergency,
    encounters,
    exams,
    infectious,
    insurance,
    integration,
    maternal,
    medication,
    medwaste,
    metrics,
    organizations,
    patients,
    performance,
    pharmacy,
    portal,
    prescriptions,
    publichealth,
    referrals,
    service_extras,
    tcm,
    telemedicine,
    todos,
    users,
    vaccination,
)
from . import ws
from .models import AuditLog
from .security import decode_token, hash_password


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(User).filter(User.username == "admin").first() is None:
            # 公网部署时通过 MEDPLAT_ADMIN_PASSWORD 指定初始密码，避免默认口令暴露
            initial_password = settings.admin_password
            db.add(
                User(
                    username="admin",
                    password_hash=hash_password(initial_password),
                    full_name="平台管理员",
                    role="admin",
                )
            )
            db.commit()
    finally:
        db.close()
    yield


app = FastAPI(
    title="县域医共体信息化平台",
    version="0.1.0",
    description="第一期：基础平台与数据中心（统一认证、机构、EMPI、编码字典、双向转诊）",
    lifespan=lifespan,
)

app.include_router(auth.router)
app.include_router(organizations.router)
app.include_router(patients.router)
app.include_router(dictionaries.router)
app.include_router(referrals.router)
app.include_router(encounters.router)
app.include_router(exams.router)
app.include_router(prescriptions.router)
app.include_router(pharmacy.router)
app.include_router(chronic.router)
app.include_router(infectious.router)
app.include_router(consultations.router)
app.include_router(contracts.router)
app.include_router(appointments.router)
app.include_router(cssd.router)
app.include_router(medwaste.router)
app.include_router(performance.router)
app.include_router(metrics.router)
app.include_router(portal.router)
app.include_router(users.router)
app.include_router(emergency.router)
app.include_router(telemedicine.router)
app.include_router(tcm.router)
app.include_router(medication.router)
app.include_router(insurance.router)
app.include_router(integration.router)
app.include_router(education.router)
app.include_router(eldercare.router)
app.include_router(maternal.router)
app.include_router(vaccination.router)
app.include_router(publichealth.router)
app.include_router(admin_mgmt.router)
app.include_router(service_extras.router)
app.include_router(todos.router)
app.include_router(ws.router)

_access_logger = logging.getLogger("medplat.access")
if not _access_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _access_logger.addHandler(_handler)
    _access_logger.setLevel(logging.INFO)
    _access_logger.propagate = False


@app.middleware("http")
async def security_headers_middleware(request, call_next):
    """安全响应头：等保整改基线（防 MIME 嗅探/点击劫持/来源泄露）。"""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


@app.middleware("http")
async def request_log_middleware(request, call_next):
    """结构化 JSON 请求日志：method/path/status/耗时/追踪ID（X-Request-ID 透传或生成）。"""
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    if settings.log_json:
        _access_logger.info(
            json.dumps(
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": duration_ms,
                },
                ensure_ascii=False,
            )
        )
    return response


_AUDITED_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
# 登录请求不落审计（避免与口令尝试混淆，登录安全事件由专用日志承担）
_AUDIT_EXEMPT = {"/api/auth/login"}


@app.middleware("http")
async def audit_middleware(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if (
        request.method in _AUDITED_METHODS
        and path.startswith("/api/")
        and path not in _AUDIT_EXEMPT
    ):
        username, user_id = "", None
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            claims = decode_token(auth[7:])
            if claims:
                username = claims.get("sub", "")
        db = SessionLocal()
        try:
            if username:
                user = db.query(User).filter(User.username == username).first()
                user_id = user.id if user else None
            db.add(
                AuditLog(
                    user_id=user_id,
                    username=username or "anonymous",
                    method=request.method,
                    path=path,
                    status_code=response.status_code,
                )
            )
            db.commit()
        finally:
            db.close()
    return response


@app.get("/api/health", tags=["平台"])
def health():
    """健康检查：附带数据库连通性探测。"""
    db_status = "ok"
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - 任何数据库异常均判定为不可用
        db_status = "error"
    payload = {
        "status": "ok" if db_status == "ok" else "degraded",
        "service": "medplat",
        "version": app.version,
        "database": db_status,
    }
    return payload


_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/m", include_in_schema=False)
def mobile_index():
    """居民端移动版 H5 入口。"""
    return FileResponse(_STATIC_DIR / "m" / "index.html")
