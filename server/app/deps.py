import time
from datetime import date, timedelta, timezone
from typing import Iterable, TypeVar

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session

from .clock import now_naive
from .config import settings
from .database import get_db
from .models import OrgGroup, OrgGroupMember, User
from .security import active_sessions, decode_token, revoked_tokens

_bearer = HTTPBearer(auto_error=False)

#: 等保 E1 口令最长使用期（天）。刻意用常量而不进 config：这是合规口径不是部署偏好，
#: 做成环境变量只会诱导现场把它调成 36500"关掉"。超期判定见 get_current_user；
#: password_updated_at 为 None（迁移前的开发库/未回填数据）不判超期，避免误伤。
PASSWORD_MAX_AGE_DAYS = 90

#: 428 强制改密的豁免路径：改密与登出必须放行，否则用户被锁进"改不了密"的死循环。
PASSWORD_CHANGE_EXEMPT_PATHS = {"/api/auth/change-password", "/api/auth/logout"}

_K = TypeVar("_K")
_V = TypeVar("_V")


def row_dict(rows: Iterable[Row[tuple[_K, _V]]]) -> dict[_K, _V]:
    """把两列查询结果（键, 值）收成字典。

    仓库里有三十多处 `dict(db.query(X.a, func.count(...)).group_by(...).all())`——
    统计接口的标准写法。直接喂给 `dict()` 在类型上是不成立的：`.all()` 给的是
    `list[Row[tuple[K, V]]]`，而 `dict()` 要 `Iterable[tuple[K, V]]`。
    Row 在运行期确实能解包成元组，所以代码一直是对的，只是类型上说不通，
    于是每处都留下"需要标注 + 参数类型不符"两条报错。

    收成一个函数而不是在每处加 `# type: ignore`：一来 ignore 是把话咽回去、
    不是把话说清楚，二来这本就是同一个模式重复三十多遍，早该有个名字。
    返回类型由 K/V 推导，调用点不必再手写标注。
    """
    return {key: value for key, value in rows}


def paginate(query, response: Response, offset: int = 0, limit: int = 100, max_limit: int = 500):
    """L-3（上轮 L4）分页整改：统一 offset/limit 分页，总数经 X-Total-Count 响应头返回。

    - 缺省行为与既有接口兼容（offset=0 时等价于原先的 .limit(N)）；
    - limit 超过 max_limit 时按 max_limit 截断，防一次拉全表。
    """
    response.headers["X-Total-Count"] = str(query.count())
    return query.offset(max(offset, 0)).limit(min(max(limit, 1), max_limit)).all()


def resolve_org_scope(
    db: Session, group_id: int | None = None, org_id: int | None = None
) -> list[int] | None:
    """把"按分组/按机构"统一解析为机构 id 列表；两者都不给则返回 None（全域）。

    返回 None 而不是"全部机构 id"——统计接口拿到 None 就不加过滤，
    既省一次全表查询，也让"没筛选"和"筛出了 0 家"在代码里不会长得一样。
    后者返回空列表，统计结果应当是 0 而不是全量。

    分组与机构同时给出时取**交集**：这是唯一不会让人意外的语义
    （"这个片区里的这家机构"），也让 UI 可以先选片区再选机构而不必清空前者。
    """
    if group_id is None:
        return None if org_id is None else [org_id]
    if db.get(OrgGroup, group_id) is None:
        raise HTTPException(status_code=404, detail="机构分组不存在")
    ids = [
        m.org_id
        for m in db.query(OrgGroupMember).filter(OrgGroupMember.group_id == group_id).all()
    ]
    if org_id is not None:
        return [org_id] if org_id in ids else []
    return ids


def resolve_business_date(today: str | None) -> date:
    """L-2（上轮 L3）整改：预警/超期类接口统一由服务端注入当前日期为默认。

    `today` 覆盖参数仅供**测试与管理排查**使用（见 docs/接口对接规范.md），
    生产对接方不应传入；格式非法返回 422，不再直接信任客户端字符串比较。
    """
    if today is None:
        return date.today()
    try:
        return date.fromisoformat(today)
    except ValueError:
        raise HTTPException(status_code=422, detail="today 参数须为 YYYY-MM-DD 格式") from None


def token_issued_before_baseline(claims: dict, user: User) -> bool:
    """M-4 整改：令牌签发时刻(iat)早于用户改密基线即视为已吊销。

    无 iat 声明的旧令牌在基线设定后同样拒绝（保守处理）。
    """
    if user.token_valid_from is None:
        return False
    baseline = user.token_valid_from.replace(tzinfo=timezone.utc).timestamp()
    return float(claims.get("iat", 0)) < baseline


def _enforce_session_idle_timeout(claims: dict) -> None:
    """等保 E1 空闲超时（简化口径）：距最近一次活动超过 idle_timeout 即拒。

    "最近活动"记在 state_store（`SessionRegistry.touch`，内存/Redis 双实现）；
    从未记录过活动的令牌（登录后第一个请求）以签发时刻 iat 为基准。
    开关为 0 时上层直接旁路，本函数不会被调用。多实例部署需配 MEDPLAT_REDIS_URL，
    否则活动记录不跨实例（与令牌黑名单同一约定）。
    """
    jti = str(claims.get("jti") or claims.get("sub") or "")
    reference = active_sessions.last_seen(jti)
    if reference is None:
        reference = float(claims.get("iat", 0))
    if time.time() - reference > settings.session_idle_timeout_seconds:
        # 闲置淘汰立即释放并发名额，不占坑到令牌自然过期
        active_sessions.remove(str(claims.get("sub", "")), jti)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="会话已闲置超时，请重新登录"
        )
    active_sessions.touch(jti)


def _enforce_password_lifecycle(user: User, request: Request) -> None:
    """等保 E1 口令生命周期：重置后首登、或超过 90 天未改密 → 428 强制改密。

    428 Precondition Required：语义就是"先完成前置动作（改密）再来"，
    与 401（令牌问题）、403（权限问题）都不同，前端可据此直接跳改密页。
    """
    if request.url.path in PASSWORD_CHANGE_EXEMPT_PATHS:
        return
    if user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail="口令已被管理员重置，请先修改口令（POST /api/auth/change-password）后再继续操作",
        )
    if (
        user.password_updated_at is not None
        and now_naive() - user.password_updated_at > timedelta(days=PASSWORD_MAX_AGE_DAYS)
    ):
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail=f"口令已超过 {PASSWORD_MAX_AGE_DAYS} 天未修改，"
                   "请先修改口令（POST /api/auth/change-password）后再继续操作",
        )


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未提供令牌")
    claims = decode_token(credentials.credentials)
    if claims is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="令牌无效或已过期")
    # L-9 整改：黑名单按 jti 判定（存储中不再落完整令牌明文）；无 jti 的旧令牌按原文兜底
    if (claims.get("jti") or credentials.credentials) in revoked_tokens:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="令牌已登出失效")
    # 居民端令牌（scope=portal）不得进入业务接口：居民账户不在 users 表内，
    # 这里显式拒绝，避免有人建一个叫 "resident:1" 的业务账号来撞 sub。
    if claims.get("scope") == "portal":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="居民端令牌不可用于业务接口")
    user = db.query(User).filter(User.username == claims["sub"]).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在")
    # 等保 E1 账号停用：每请求现查 users.status，停用即时生效，不等令牌过期。
    # 403 而非 401——问题不在令牌，是账号被管理员禁用，重新登录也无济于事。
    # 放在令牌基线判定**之前**：停用时会同步推基线吊销令牌，若先判基线，
    # 停用账号收到的是"密码已修改请重新登录"——指引用户去做一件做不成的事。
    if user.status == "disabled":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已停用，请联系管理员")
    if token_issued_before_baseline(claims, user):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="密码已修改，令牌失效，请重新登录"
        )
    # 等保 E1 会话空闲超时（0=关闭，零开销旁路）
    if settings.session_idle_timeout_seconds > 0:
        _enforce_session_idle_timeout(claims)
    # 等保 E1 口令生命周期（重置后首登 / 90 天超期 → 428；改密与登出豁免）
    _enforce_password_lifecycle(user, request)
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


# ============================================================================
# 业务写接口「接口 → 最小角色」矩阵（H2 整改，admin 全通；详见 docs/接口对接规范.md 附录）
# 原则：operator（经办人员）不得执行诊疗性质操作（接诊、出报告、开处方、随访）。
#
# | 业务 | 接口 | 允许角色 |
# |---|---|---|
# | 转诊申请 POST /api/referrals                       | doctor, operator |
# | 转诊接诊/结案/退回 PATCH /api/referrals/{id}/status | doctor |
# | 就诊记录 POST /api/encounters                      | doctor, operator |
# | 检查申请 POST /api/exams、样本物流 sample/advance   | doctor, operator |
# | 诊断领取/出报告/报告修改 claim、report、PATCH reports | doctor |
# | 处方开具 POST /api/prescriptions                   | doctor |
# | 处方审核 POST /api/prescriptions/{id}/review        | pharmacist |
# | 慢病建档/随访 POST /api/chronic(/followups)         | doctor, public_health |
# | 传染病报告 POST /api/infectious/cases              | doctor, public_health |
# | 急救调度/进程/体征 POST /api/emergency/*            | operator, doctor |
# | 医保结算/转诊证明 POST /api/insurance/settlements 等 | operator |
# | 特病申报 POST /api/insurance/special-diseases       | operator, doctor |
# | 特病审核 .../review                                 | director（L-11：申报/审核职责分离） |
# | 远程会诊申请/评价                                   | doctor, operator |
# | 远程会诊受理/拒绝/出具意见                          | doctor |
# | 互联网+诊疗咨询建立/结束                            | operator, doctor |
# | 互联网+诊疗医师回复 reply                           | doctor |
# | 家医签约/解约/履约 POST /api/contracts*             | doctor, public_health |
# | 老年评估/妇幼建档随访/疫苗接种与禁忌                | doctor, public_health |
# | 公卫事件/处置/监测指标 POST /api/publichealth/*     | public_health, doctor |
# | 号源预约/取消/核销 POST /api/appointments*          | operator, doctor |
# | 库存调拨 POST /api/pharmacy/transfers               | operator, pharmacist |
# | 短缺登记/流转 POST /api/medication/shortages*       | operator, pharmacist |
# | 中药代煎建单 POST /api/tcm/dispense-orders          | doctor |
# | 中药代煎流转 .../advance                            | operator, pharmacist |
# | 消毒批次/申领 POST /api/cssd/*                      | operator |
# | 医废收集/交接 POST /api/medwaste*                   | operator |
# | 健康宣教发布 POST /api/education/articles*          | public_health, operator |
# | 满意度代录 POST /api/surveys                        | operator |
# | 人事/资产 POST /api/mgmt/employees|secondments|assets | director, operator |
# | 财务/公文 POST /api/mgmt/finance|docs               | director, operator（发文 director） |
# | 号源发布/字典/规则/用户/机构等平台配置              | admin |
# ============================================================================
ROLE_NAMES = {
    "admin": "平台管理员",
    "director": "管理层",
    "doctor": "医师",
    "pharmacist": "药师",
    "public_health": "公卫人员",
    "operator": "经办人员",
}


def require_roles(*roles: str):
    """角色守卫。

    阶段十一在这里做了一处**不改签名的扩展**：

    - 六个内置角色（含 admin）判定完全照旧，走内存比较，**不查库**——
      每个请求多查一次库换不来任何东西，而 550 个端点每个都要过这一关；
    - 自定义角色（不在 `ROLE_NAMES` 里的）改查权限点：该角色被授了本接口
      对应的权限点才放行。

    这样既拿到了"自定义角色"这个能力，又保证现有部署零感知升级——
    一次性把 248 处 `require_roles` 改成 `require_permission("...")`，
    收益是统一，代价是 248 个改动点里只要错一个就是越权。
    """

    def checker(
        request: Request,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> User:
        if user.role == "admin" or user.role in roles:
            return user
        if user.role not in ROLE_NAMES:  # 自定义角色：查权限点
            if _custom_role_allows(db, user.role, request):
                return user
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"自定义角色「{user.role}」未被授予此操作的权限",
            )
        allowed = "、".join(ROLE_NAMES.get(r, r) for r in roles)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=f"需要以下角色之一：{allowed}"
        )

    return checker


def _custom_role_allows(db: Session, role_key: str, request: Request) -> bool:
    """自定义角色是否被授予当前请求对应的权限点。

    权限点用**路由模板**而非实际路径（`/api/x/{id}` 而不是 `/api/x/7`），
    否则每个 id 都会变成一个权限点，配到天荒地老。
    """
    from .models import Permission, Role, RolePermission

    route = request.scope.get("route")
    template = getattr(route, "path", None) or request.url.path
    code = f"{request.method}:{template}"
    return (
        db.query(RolePermission.id)
        .join(Role, Role.id == RolePermission.role_id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .filter(
            Role.key == role_key,
            Role.active.is_(True),
            Permission.code == code,
        )
        .first()
        is not None
    )
