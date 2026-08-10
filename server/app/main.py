"""紧密型县域医共体信息化平台 —— 第一期基础平台服务。

已实现模块（对应《县域医共体信息化平台建设规划》第一期）：
- 统一认证（JWT 令牌，角色权限）
- 医共体成员单位（机构）管理
- 患者主索引 EMPI / 电子健康卡号
- 统一编码字典（诊断、药品、耗材、收费"四统一"）
- 双向转诊状态流转
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .database import Base, SessionLocal, engine
from .models import User
from .routers import (
    appointments,
    auth,
    chronic,
    consultations,
    contracts,
    cssd,
    dictionaries,
    encounters,
    exams,
    infectious,
    medwaste,
    metrics,
    organizations,
    patients,
    performance,
    pharmacy,
    portal,
    prescriptions,
    referrals,
    users,
)
from .models import AuditLog
from .security import decode_token, hash_password


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(User).filter(User.username == "admin").first() is None:
            db.add(
                User(
                    username="admin",
                    password_hash=hash_password("admin123"),
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
    return {"status": "ok", "service": "medplat", "version": app.version}


_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(_STATIC_DIR / "index.html")
