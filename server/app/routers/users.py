"""用户管理与审计日志（管理员），修改密码（本人）。"""
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import (
    ROLE_NAMES,
    get_current_user,
    paginate,
    require_admin,
    resolve_business_date,
)
from ..models import AuditLog, Organization, RoleChangeLog, User, utcnow
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


# 归档导出的批读大小：既不让单批占太多内存，也不至于把查询打得太碎
AUDIT_EXPORT_BATCH = 1000


@router.get("/audit/export", dependencies=[Depends(require_admin)])
def export_audit_logs(
    since_id: int = 0,
    until: str | None = None,
    db: Session = Depends(get_db),
):
    """审计日志归档导出：按 id 游标流式输出 NDJSON（每行一条记录）。

    T6.2 整改：此前是 `db.query(AuditLog).all()` 一次读进内存再序列化成单个
    JSON 响应。审计中间件对每一次写操作留痕，一个县域平台跑一年就是百万级行，
    开发库上看不出问题，生产上是一次 OOM。

    现按主键游标分批读取并逐行 yield：
    - `since_id` 增量归档（上次导到哪，下次从哪继续）；
    - `until` 可选按日期上界（YYYY-MM-DD，导出该日之前的记录）用于按月归档；
    - 首行是 meta 行（含本次导出的起止 id），便于归档端校验连续性。
    """
    if until:
        resolve_business_date(until)  # 复用统一的日期格式校验（非法 422）

    def rows():
        cursor = since_id
        last_id = since_id
        total = 0
        while True:
            query = db.query(AuditLog).filter(AuditLog.id > cursor)
            if until:
                query = query.filter(AuditLog.created_at < datetime.fromisoformat(f"{until}T00:00:00"))
            batch = query.order_by(AuditLog.id).limit(AUDIT_EXPORT_BATCH).all()
            if not batch:
                break
            for log in batch:
                yield json.dumps(
                    {
                        "id": log.id,
                        "user_id": log.user_id,
                        "username": log.username,
                        "method": log.method,
                        "path": log.path,
                        "status_code": log.status_code,
                        "at": log.created_at.isoformat(),
                    },
                    ensure_ascii=False,
                ) + "\n"
                total += 1
            cursor = last_id = batch[-1].id
            # 已读完的批次立即从会话中逐出，否则身份映射照样把全表留在内存里
            db.expunge_all()
        yield json.dumps(
            {"_meta": True, "total": total, "since_id": since_id, "last_id": last_id},
            ensure_ascii=False,
        ) + "\n"

    return StreamingResponse(
        rows(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="audit_logs.ndjson"'},
    )


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


@router.get("/audit/stats", dependencies=[Depends(require_admin)])
def audit_stats(days: int = 30, db: Session = Depends(get_db)):
    """审计统计（浙#46 日志图形化）：按日趋势、失败码分布、高频操作与用户 TOP。

    与 `/api/monitor/api-stats` 的分工：那边是**进程内**的全部请求（含读），
    随重启清零，用来看性能与错误；这边是**落库**的写操作留痕，跨实例、可追溯，
    用来看"谁在改什么"。两者不可互相替代，也不该合并——一个是运维视角，
    一个是审计视角，保留期与权限要求都不同。
    """
    days = max(1, min(days, 365))
    since = utcnow() - timedelta(days=days)
    base = db.query(AuditLog).filter(AuditLog.created_at >= since)

    # 按日趋势：成功与失败分开，只看总量看不出"改坏了多少次"
    daily: dict[str, dict[str, int]] = {}
    for created_at, status_code in base.with_entities(
        AuditLog.created_at, AuditLog.status_code
    ).all():
        day = created_at.strftime("%Y-%m-%d")
        bucket = daily.setdefault(day, {"date": day, "ok": 0, "failed": 0})
        bucket["failed" if status_code >= 400 else "ok"] += 1

    def _top(column, limit=10, failed_only=False):
        query = base
        if failed_only:
            query = query.filter(AuditLog.status_code >= 400)
        rows = (
            query.with_entities(column, func.count(AuditLog.id))
            .group_by(column)
            .order_by(func.count(AuditLog.id).desc())
            .limit(limit)
            .all()
        )
        return [{"key": k or "(空)", "count": c} for k, c in rows]

    status_rows = (
        base.filter(AuditLog.status_code >= 400)
        .with_entities(AuditLog.status_code, func.count(AuditLog.id))
        .group_by(AuditLog.status_code)
        .order_by(func.count(AuditLog.id).desc())
        .all()
    )
    total = base.count()
    failed = sum(c for _, c in status_rows)
    return {
        "days": days,
        "scope": "全部实例（审计落库，非进程内计数）",
        "total": total,
        "failed": failed,
        "failed_ratio_pct": round(failed / total * 100, 2) if total else 0.0,
        "daily": [daily[d] for d in sorted(daily)],
        "failed_status_codes": [{"status": s, "count": c} for s, c in status_rows],
        "top_users": _top(AuditLog.username),
        "top_paths": _top(AuditLog.path),
        "top_failed_paths": _top(AuditLog.path, failed_only=True),
    }


# ---------- 终审轮：用户角色变更与变更记录（浙#43） ----------


class RoleUpdate(BaseModel):
    role: str = Field(pattern="^(admin|director|doctor|pharmacist|public_health|operator)$")


@router.patch("/users/{user_id}/role", response_model=UserOut)
def change_user_role(
    user_id: int,
    body: RoleUpdate,
    db: Session = Depends(get_db),
    operator: User = Depends(require_admin),
):
    """角色调整（限管理员）：变更前后角色落 RoleChangeLog 留痕，且不可自降 admin。"""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if target.id == operator.id and body.role != "admin":
        raise HTTPException(status_code=422, detail="不可撤销自身管理员角色")
    if target.role == body.role:
        return target
    db.add(
        RoleChangeLog(
            user_id=target.id, old_role=target.role, new_role=body.role, changed_by=operator.id
        )
    )
    target.role = body.role
    # 角色变更即吊销既有令牌，避免旧角色令牌继续放行
    target.token_valid_from = utcnow()
    db.commit()
    db.refresh(target)
    return target


@router.get("/users/role-changes", dependencies=[Depends(require_admin)])
def list_role_changes(user_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(RoleChangeLog)
    if user_id is not None:
        q = q.filter(RoleChangeLog.user_id == user_id)
    return [
        {
            "id": r.id,
            "user_id": r.user_id,
            "old_role": r.old_role,
            "new_role": r.new_role,
            "changed_by": r.changed_by,
            "at": r.created_at.isoformat(),
        }
        for r in q.order_by(RoleChangeLog.id.desc()).limit(200).all()
    ]
