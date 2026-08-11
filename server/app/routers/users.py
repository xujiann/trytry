"""用户管理与审计日志（管理员），修改密码（本人）。"""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import ROLE_NAMES, get_current_user, paginate, require_admin
from ..models import AuditLog, Organization, User, utcnow
from ..security import hash_password, validate_password_strength, verify_password

router = APIRouter(prefix="/api", tags=["用户与审计"])


def _check_password(value: str) -> str:
    reason = validate_password_strength(value)
    if reason:
        raise ValueError(reason)
    return value


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str
    full_name: str = ""
    role: str = Field(default="operator", pattern="^(admin|director|doctor|pharmacist|public_health|operator)$")
    org_id: int | None = None

    # 密码复杂度：≥8位且含字母数字
    _password_strength = field_validator("password")(_check_password)


class UserOut(BaseModel):
    id: int
    username: str
    full_name: str
    role: str
    org_id: int | None

    model_config = {"from_attributes": True}


class ChangePassword(BaseModel):
    current_password: str
    new_password: str

    _password_strength = field_validator("new_password")(_check_password)


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
    # M-4 整改：改密即吊销该用户既有全部令牌（iat 早于基线的令牌一律拒绝）
    user.token_valid_from = utcnow()
    db.commit()
    return {"changed": True, "tokens_revoked": True}


@router.get("/audit/export", dependencies=[Depends(require_admin)])
def export_audit_logs(db: Session = Depends(get_db)):
    """审计日志归档导出：全量 JSON（供按月归档与防篡改校验使用）。"""
    logs = db.query(AuditLog).order_by(AuditLog.id).all()
    return {
        "total": len(logs),
        "logs": [
            {
                "id": log.id,
                "user_id": log.user_id,
                "username": log.username,
                "method": log.method,
                "path": log.path,
                "status_code": log.status_code,
                "at": log.created_at.isoformat(),
            }
            for log in logs
        ],
    }


@router.get("/audit", dependencies=[Depends(require_admin)])
def list_audit_logs(
    response: Response,
    limit: int = 100,
    offset: int = 0,
    username: str | None = None,
    db: Session = Depends(get_db),
):
    """审计日志（L-3 分页：offset/limit，总数见 X-Total-Count 响应头）。"""
    query = db.query(AuditLog)
    if username:
        query = query.filter(AuditLog.username == username)
    logs = paginate(query.order_by(AuditLog.id.desc()), response, offset, limit)
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
