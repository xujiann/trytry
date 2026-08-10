"""用户管理与审计日志（管理员），修改密码（本人）。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import ROLE_NAMES, get_current_user, require_admin
from ..models import AuditLog, Organization, User
from ..security import hash_password, verify_password

router = APIRouter(prefix="/api", tags=["用户与审计"])


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6)
    full_name: str = ""
    role: str = Field(default="operator", pattern="^(admin|director|doctor|pharmacist|public_health|operator)$")
    org_id: int | None = None


class UserOut(BaseModel):
    id: int
    username: str
    full_name: str
    role: str
    org_id: int | None

    model_config = {"from_attributes": True}


class ChangePassword(BaseModel):
    current_password: str
    new_password: str = Field(min_length=6)


@router.post("/users", response_model=UserOut, status_code=201, dependencies=[Depends(require_admin)])
def create_user(body: UserCreate, db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == body.username).first():
        raise HTTPException(status_code=409, detail="用户名已存在")
    if body.org_id is not None and db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="所属机构不存在")
    user = User(
        username=body.username,
        password_hash=hash_password(body.password),
        full_name=body.full_name,
        role=body.role,
        org_id=body.org_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.get("/users", response_model=list[UserOut], dependencies=[Depends(require_admin)])
def list_users(db: Session = Depends(get_db)):
    return db.query(User).order_by(User.id).all()


@router.get("/users/roles", dependencies=[Depends(get_current_user)])
def list_roles():
    return ROLE_NAMES


@router.post("/auth/change-password")
def change_password(
    body: ChangePassword,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="当前密码不正确")
    user.password_hash = hash_password(body.new_password)
    db.commit()
    return {"changed": True}


@router.get("/audit", dependencies=[Depends(require_admin)])
def list_audit_logs(limit: int = 100, username: str | None = None, db: Session = Depends(get_db)):
    query = db.query(AuditLog)
    if username:
        query = query.filter(AuditLog.username == username)
    logs = query.order_by(AuditLog.id.desc()).limit(min(limit, 500)).all()
    return [
        {
            "id": log.id,
            "username": log.username,
            "method": log.method,
            "path": log.path,
            "status_code": log.status_code,
            "at": log.created_at.isoformat(),
        }
        for log in logs
    ]
