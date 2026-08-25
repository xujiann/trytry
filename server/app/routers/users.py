"""用户管理与审计日志（管理员），修改密码（本人）。"""
import hmac
import json
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_ as sa_and
from sqlalchemy import func
from sqlalchemy import or_ as sa_or
from sqlalchemy.orm import Session

from ..concurrency import insert_or_conflict
from ..config import settings
from ..visibility import assert_obj_org_writable, assert_org_visible, visible_org_ids
from ..database import get_db
from ..deps import (
    ROLE_NAMES,
    get_current_user,
    paginate,
    require_admin,
    require_roles,
    resolve_business_date,
    set_auth_cookies,
)
from ..audit_chain import verify_chain
from ..models import AuditLog, LoginLog, Organization, RoleChangeLog, User, utcnow
from ..security import (
    AUTH_COOKIE,
    CSRF_COOKIE,
    TOKEN_TTL_SECONDS,
    active_sessions,
    create_token,
    decode_token,
    hash_password,
    validate_password_strength,
    verify_password,
)

router = APIRouter(prefix="/api", tags=["用户与审计"])


def _check_password(value: str) -> str:
    reason = validate_password_strength(value)
    if reason:
        raise ValueError(reason)
    return value


def _check_role_exists(db: Session, role: str) -> None:
    """角色须在 `roles` 表里且启用中。

    改为查表而不是写死正则，是为了让自定义角色也能建号；但**不放松校验**——
    随手打错一个角色名就建出一个谁也匹配不上的账号，比拒绝更麻烦。
    """
    from ..models import Role

    row = db.query(Role).filter(Role.key == role).first()
    if row is None:
        raise HTTPException(status_code=422, detail=f"角色 {role} 不存在，请先在角色管理中创建")
    if not row.active:
        raise HTTPException(status_code=422, detail=f"角色 {role} 已停用")


class UserCreate(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str
    full_name: str = ""
    # 阶段十一：角色不再写死正则——自定义角色也要能建号。
    # 合法性改为对 `roles` 表现查（见 _check_role_exists），非法角色仍 422。
    role: str = Field(default="operator", min_length=2, max_length=32)
    org_id: int | None = None
    # 等保 E1：建号即要求首登改密（初始口令是管理员代设的临时口令时置 true）。
    # 默认 false 而非 true——大量既有对接/测试以"建号即用"为前提，强制默认 true
    # 会破坏向后兼容（CLAUDE.md 第 1/7 条）；管理端 UI 建号时应显式传 true。
    must_change_password: bool = False

    # 密码复杂度：≥8位且含字母数字
    _password_strength = field_validator("password")(_check_password)


class UserOut(BaseModel):
    id: int
    username: str
    full_name: str
    role: str
    org_id: int | None
    # 等保 E1：账号状态（active/disabled），管理端据此渲染停用/启用按钮
    status: str

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
    _check_role_exists(db, body.role)
    user = insert_or_conflict(
        db,
        User(
            username=body.username,
            password_hash=hash_password(body.password),
            full_name=body.full_name,
            role=body.role,
            org_id=body.org_id,
            # 等保 E1 口令生命周期：建号时刻即口令基线，90 天超期从此起算
            password_updated_at=utcnow(),
            must_change_password=body.must_change_password,
        ),
        "用户名已存在",
    )
    return user


@router.get("/users", response_model=list[UserOut], dependencies=[Depends(require_admin)])
def list_users(db: Session = Depends(get_db)):
    return db.query(User).order_by(User.id).all()


@router.get("/users/roles", dependencies=[Depends(get_current_user)])
def list_roles():
    return ROLE_NAMES


class SelectableUserOut(BaseModel):
    """"选个人"控件用的最小披露形状。

    刻意**不回 `username`**：那是登录句柄，把它枚举给全体在册账号，等于把撞库
    要用的用户名字典白送出去。选人只需要"认得出是谁"——显示名 + 角色 + 机构，
    同名的人靠这两项区分，不靠登录名。
    也**不回 `status`**：列表本身只回在用账号（停用的人不该还能被加进团队），
    再回一个恒为 "active" 的字段没有意义（与 spd 标签列表同一口径）。
    """

    id: int
    #: 显示名：`full_name` 非空取它，否则回落到 `username`（全仓库既有惯例）
    name: str
    role: str
    org_id: int | None
    #: 未挂机构时为空串，不是 null——省得前端每处都判一次
    org_name: str


@router.get("/users/selectable", response_model=list[SelectableUserOut])
def list_selectable_users(
    response: Response,
    keyword: str = "",
    role: str | None = None,
    org_id: int | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """可被选为团队成员/村医的在册账号（登录即可读，按可见范围收窄）。

    **为什么单开一个接口，而不是放宽 `GET /users`**：那个是 `require_admin` 的
    管理清单，回的是完整账号档案（含登录名与账号状态）。慢专病配置域的写操作
    放给 `CONFIG_ROLES=("director","doctor")`，于是 doctor 能建团队、却列不出可选
    的人——管理端配置页只能退化成让人手填 user_id。放宽那个接口等于把管理清单
    开给全体登录用户；这里改成另开一个**最小披露 + 按可见范围收窄**的只读入口，
    权限面只扩到"看得见同机构同事的显示名"，不扩到账号档案。

    收窄口径与明细数据同一档（`visibility.visible_org_ids`）：
    - `admin` / `director` 是全域角色，看全部（他们本来就能跨机构配团队）；
    - 其余角色只看**本机构**在册账号；未挂机构的账号看不到任何人（也不被任何人看到）。

    `keyword` 匹配的是**显示名**——`full_name` 非空时只匹配它，为空时才匹配
    `username`。不无条件放开按登录名搜：那会变成一个用户名探测口子
    （输入猜测值、看有没有人跳出来），而本接口的整个设计前提就是不外泄登录名。
    """
    allowed = visible_org_ids(db, user)
    query = (
        db.query(User, Organization.name)
        .outerjoin(Organization, User.org_id == Organization.id)
        .filter(User.status == "active")
    )
    if allowed is not None:
        # 空列表（没挂机构的业务账号）→ in_([]) 天然不匹配任何行，与"看不到任何机构明细"一致
        query = query.filter(User.org_id.in_(allowed))
    if org_id is not None:
        assert_org_visible(db, user, org_id)
        query = query.filter(User.org_id == org_id)
    if role:
        query = query.filter(User.role == role)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            sa_or(
                sa_and(User.full_name != "", User.full_name.like(like)),
                sa_and(User.full_name == "", User.username.like(like)),
            )
        )
    rows = paginate(query.order_by(User.id), response, offset, limit)
    return [
        {
            "id": u.id,
            "name": u.full_name or u.username,
            "role": u.role,
            "org_id": u.org_id,
            "org_name": org_name or "",
        }
        for u, org_name in rows
    ]


class ChangePasswordOut(BaseModel):
    changed: bool
    tokens_revoked: bool


def _refresh_cookie_session(request: Request, response: Response, user: User) -> None:
    """Cookie 会话的改密续期：重签一枚令牌，刷新 HttpOnly 令牌 Cookie。

    改密推基线后旧令牌立即失效。Header 模式的对接方自己持有令牌，重新登录即可；
    Cookie 模式不行——令牌在 HttpOnly Cookie 里，前端**既读不到也删不掉**，改密后
    浏览器会一直回带一枚死 Cookie：业务接口 401，连 `/api/auth/logout` 都因为依赖
    `get_current_user` 而 401，用户没有任何自清通道（除非手动清浏览器数据）。

    两条修法里取"重签续期"而不是"放宽 logout 的鉴权让它能清 Cookie"：改密本就是
    本人已经通过鉴权的操作，没有理由把他踢下线再让他自己捡回来；而为了一个纯清理
    动作在认证边界上开口子（让 logout 接受失效令牌），代价明显更大。

    只在**本次请求确实走 Cookie 鉴权**时下发，Header 模式响应逐字节不变。
    """
    if request.headers.get("authorization"):
        return  # Header 模式：令牌由对接方自己持有，不下发 Cookie
    if not request.cookies.get(AUTH_COOKIE):
        return
    token = create_token(user.username, user.role)
    # 名额刚被 clear_user 清空，续期的这枚要重新占一个，否则 Cookie 会话不计数、
    # 并发上限形同虚设
    if settings.session_max_concurrent > 0:
        claims = decode_token(token) or {}
        ttl = max(int(claims.get("exp", 0) - time.time()), 60)
        active_sessions.register(user.username, str(claims.get("jti", "")), ttl_seconds=ttl)
    set_auth_cookies(
        response,
        token,
        cookie_name=AUTH_COOKIE,
        csrf_cookie_name=CSRF_COOKIE,
        max_age=TOKEN_TTL_SECONDS,
        # CSRF token 不轮换：它与口令无关，换掉只会让改密响应到达前已发出的
        # 并发写请求全部 403（前端每次现读 Cookie，但请求头里是发出时的旧值）
        csrf_token=request.cookies.get(CSRF_COOKIE) or None,
    )


@router.post("/auth/change-password", response_model=ChangePasswordOut)
def change_password(
    body: ChangePassword,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="当前密码不正确")
    user.password_hash = hash_password(body.new_password)
    # M-4 整改：改密即吊销该用户既有全部令牌（iat 早于基线的令牌一律拒绝）
    user.token_valid_from = utcnow()
    # 等保 E1 口令生命周期：改密成功刷新基线、解除 428 强制改密
    user.password_updated_at = utcnow()
    user.must_change_password = False
    db.commit()
    # 推基线只让旧令牌"验不过"，会话登记里的名额还挂在账号上（理由见 clear_user）
    active_sessions.clear_user(user.username)
    _refresh_cookie_session(request, response, user)
    return ChangePasswordOut(changed=True, tokens_revoked=True)


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


@router.get("/audit/verify", dependencies=[Depends(require_admin)])
def verify_audit_chain(
    start_id: int = 0,
    limit: int = 5000,
    anchor_id: int | None = None,
    anchor_hash: str | None = None,
    db: Session = Depends(get_db),
):
    """校验审计哈希链（阶段十一）。

    **能力边界写在返回里，不藏在文档里**：这条链能发现"某条历史记录被改过"，
    但拦不住有库权限且知道平台密钥的人重算整条链。真正的不可抵赖要靠外部存证
    或只追加存储，属部署形态而非应用能力。把它说成"审计不可篡改"是夸大。

    起点若不是链首（start_id > 首条），只能校验这一段内部的自洽——
    这一点也如实报出，免得有人拿一段抽查结果当全量结论。

    **外部锚点对账（P1-21）**：`anchor_id` + `anchor_hash` 传入某条锚点记录
    （audit_anchor 任务写的 audit_anchors.jsonl / webhook 外发副本）的
    tail_id / tail_entry_hash，校验"该行仍在库中、entry_hash 与锚点一致、
    且其后链连续"——这是唯一能抓住"末尾截断"的口径：锚点所指行不在库里，
    即疑似最新 N 条被删（或该段已归档，以归档 manifest 续查）。
    不带锚点入参时响应字节与既有完全一致（新增字段仅在对账时出现）。
    """
    if (anchor_id is None) != (anchor_hash is None):
        raise HTTPException(status_code=422, detail="anchor_id 与 anchor_hash 须成对提供")
    anchor: dict | None = None
    if anchor_id is not None:
        # 对账从锚点行起验：锚点行本身要在且哈希一致，其后（若有）链要连续
        start_id = anchor_id
        row = db.get(AuditLog, anchor_id)
        if row is None:
            anchor = {
                "anchor_id": anchor_id,
                "anchor_match": False,
                "anchor_reason": "锚点所指的行已不在库中——疑似末尾截断"
                                 "（若该段已归档，请核对归档 manifest 后续查）",
            }
        elif not hmac.compare_digest(row.entry_hash, anchor_hash or ""):
            anchor = {
                "anchor_id": anchor_id,
                "anchor_match": False,
                "anchor_reason": "该行 entry_hash 与外部锚点不符——锚点时刻之后该行被改动",
            }
        else:
            anchor = {"anchor_id": anchor_id, "anchor_match": True, "anchor_reason": ""}
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.id >= start_id)
        .order_by(AuditLog.id)
        .limit(limit)
        .all()
    )
    if not rows:
        empty = {"checked": 0, "valid": True, "note": "该区间没有审计记录"}
        if anchor is not None:
            empty["valid"] = anchor["anchor_match"]
            empty.update(anchor)
        return empty
    # 未启用哈希链之前的历史记录（entry_hash 为空）不参与校验，单列报出
    legacy = [r for r in rows if not r.entry_hash]
    chained = [r for r in rows if r.entry_hash]
    result = verify_chain(chained) if chained else {"valid": True, "broken_at": None, "reason": ""}
    body = {
        "checked": len(chained),
        "legacy_unchained": len(legacy),
        "from_id": chained[0].id if chained else None,
        "to_id": chained[-1].id if chained else None,
        "partial_segment": bool(chained) and chained[0].prev_hash != "",
        **result,
        "caliber": "哈希链能发现历史记录被改动；但拦不住有库权限且知道平台密钥者"
                   "重算整条链——不可抵赖需外部存证或只追加存储",
    }
    if anchor is not None:
        body["valid"] = bool(body["valid"]) and anchor["anchor_match"]
        body.update(anchor)
    return body


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
    role: str = Field(min_length=2, max_length=32)


# 同上：守卫放 dependencies=[]，保证非管理员拿到的是 403 而不是一份字段清单。
@router.patch(
    "/users/{user_id}/role", response_model=UserOut, dependencies=[Depends(require_admin)]
)
def change_user_role(
    user_id: int,
    body: RoleUpdate,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
    operator: User = Depends(get_current_user),
):
    """角色调整（限管理员）：变更前后角色落 RoleChangeLog 留痕，且不可自降 admin。"""
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    assert_obj_org_writable(db, user, target)
    _check_role_exists(db, body.role)
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


# ---------- 等保 E1：账号停用/启用、口令重置、TOTP 重置、登录留痕查询 ----------


class UserStatusUpdate(BaseModel):
    # active=在用, disabled=停用（列注释同款口径；裸字符串遵循仓库约定，不用 Enum）
    status: str = Field(pattern="^(active|disabled)$")


@router.patch(
    "/users/{user_id}/status", response_model=UserOut, dependencies=[Depends(require_admin)]
)
def set_user_status(
    user_id: int,
    body: UserStatusUpdate,
    db: Session = Depends(get_db),
    operator: User = Depends(get_current_user),
):
    """停用/启用员工账号（限管理员）。停用**即时生效**：deps 每请求校验 status，
    既有令牌下一次请求就被拒，不等过期；同时把令牌基线推到当前，重新启用后
    旧令牌也不复活，必须重新登录。
    """
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    assert_obj_org_writable(db, operator, target)
    if body.status == "disabled":
        # 最后一个可用 admin 的保护放在"不可停用自己"之前：两条规则同时命中时，
        # 报"最后一个管理员"才说到点子上——那不是操作习惯问题，是要把平台锁死。
        if target.role == "admin":
            others = (
                db.query(User)
                .filter(User.role == "admin", User.status == "active", User.id != target.id)
                .count()
            )
            if others == 0:
                raise HTTPException(status_code=422, detail="不可停用最后一个可用的管理员账号")
        if target.id == operator.id:
            raise HTTPException(status_code=422, detail="不可停用自己的账号")
        # 停用即吊销既有令牌：status 校验挡当下，令牌基线防"启用后旧令牌复活"
        target.token_valid_from = utcnow()
    target.status = body.status
    db.commit()
    db.refresh(target)
    if body.status == "disabled":
        # 令牌都作废了，别让它们继续占着并发名额——否则重新启用后本人还要先撞
        # 一轮 409 才登得进来（理由见 SessionRegistry.clear_user）
        active_sessions.clear_user(target.username)
    return target


class AdminPasswordReset(BaseModel):
    new_password: str

    _password_strength = field_validator("new_password")(_check_password)


class PasswordResetOut(BaseModel):
    reset: bool
    must_change_password: bool


@router.post(
    "/users/{user_id}/reset-password",
    response_model=PasswordResetOut,
    dependencies=[Depends(require_admin)],
)
def admin_reset_password(
    user_id: int,
    body: AdminPasswordReset,
    db: Session = Depends(get_db),
    operator: User = Depends(get_current_user),
):
    """管理员重置口令（等保 E1）：临时口令只该用一次——置 must_change_password，
    该用户此后除改密/登出外一律 428，直到本人改成只有自己知道的口令；
    同时吊销既有令牌（重置口令的常见场景正是"号可能已失陷"）。
    """
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    assert_obj_org_writable(db, operator, target)
    target.password_hash = hash_password(body.new_password)
    target.must_change_password = True
    target.password_updated_at = utcnow()
    target.token_valid_from = utcnow()
    db.commit()
    # 同上：基线一推旧令牌全废，名额也该一起放掉，否则用户拿着临时口令都登不进来
    active_sessions.clear_user(target.username)
    return PasswordResetOut(reset=True, must_change_password=True)


class TotpResetOut(BaseModel):
    reset: bool


@router.post(
    "/users/{user_id}/totp/reset",
    response_model=TotpResetOut,
    dependencies=[Depends(require_admin)],
)
def admin_reset_totp(
    user_id: int,
    db: Session = Depends(get_db),
    operator: User = Depends(get_current_user),
):
    """管理员重置他人 TOTP（换手机/令牌丢失时的解困通道）：清空密钥，
    该用户下次登录按"未开通"处理（若其角色被要求双因素，会收到 setup 提示）。
    """
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    assert_obj_org_writable(db, operator, target)
    target.totp_secret = None
    db.commit()
    return TotpResetOut(reset=True)


class LoginLogOut(BaseModel):
    id: int
    username: str
    user_id: int | None
    ip: str
    success: bool
    fail_reason: str
    channel: str
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get(
    "/audit/logins",
    response_model=list[LoginLogOut],
    dependencies=[Depends(require_roles("director"))],
)
def list_login_logs(
    response: Response,
    offset: int = 0,
    limit: int = 100,
    username: str | None = None,
    success: bool | None = None,
    channel: str | None = None,
    db: Session = Depends(get_db),
):
    """登录留痕查询（director/admin；等保 E1）。分页走统一 offset/limit，
    总数经 X-Total-Count 响应头返回。"""
    query = db.query(LoginLog)
    if username:
        query = query.filter(LoginLog.username == username)
    if success is not None:
        query = query.filter(LoginLog.success.is_(success))
    if channel:
        query = query.filter(LoginLog.channel == channel)
    return paginate(query.order_by(LoginLog.id.desc()), response, offset, limit)


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
