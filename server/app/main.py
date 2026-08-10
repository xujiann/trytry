"""紧密型县域医共体信息化平台 —— 第一期基础平台服务。

已实现模块（对应《县域医共体信息化平台建设规划》第一期）：
- 统一认证（JWT 令牌，角色权限）
- 医共体成员单位（机构）管理
- 患者主索引 EMPI / 电子健康卡号
- 统一编码字典（诊断、药品、耗材、收费"四统一"）
- 双向转诊状态流转
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .database import Base, SessionLocal, engine
from .models import User
from .routers import auth, dictionaries, organizations, patients, referrals
from .security import hash_password


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


@app.get("/api/health", tags=["平台"])
def health():
    return {"status": "ok", "service": "medplat", "version": app.version}
