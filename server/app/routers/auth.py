"""统一认证：登录（防爆破锁定 + TOTP 双因素 + 并发上限）、登出、TOTP 开通/关闭。

等保 E1 扩展（本文件新增的三层，全部**默认旁路**、不惊扰存量部署）：

- **登录留痕**：成功/失败/锁定触发均落 `login_logs`（见 record_login_event），
  失败原因用代号写死，供 director/admin 端查询与爆破画像；
- **TOTP 双因素**：`MEDPLAT_TOTP_REQUIRED_ROLES` 列出的角色登录须带 totp_code
  （±1 时间窗）；已配置角色但未开通的用户放行并在响应里提示
  `totp_setup_required: true`——渐进启用，不把存量账号锁在门外。开关为空=全旁路；
- **并发会话上限**：`MEDPLAT_SESSION_MAX_CONCURRENT` > 0 时，活跃令牌数达上限
  拒绝新登录（409）。登出与空闲淘汰即时释放名额；0=不限、零开销。
"""
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import totp
from ..config import settings
from ..database import get_db
from ..deps import clear_auth_cookies, get_current_user, set_auth_cookies, wants_cookie_auth
from ..models import LoginLog, User
from ..schemas import LoginRequest, TokenResponse
from ..security import (
    AUTH_COOKIE,
    CSRF_COOKIE,
    TOKEN_TTL_SECONDS,
    active_sessions,
    create_token,
    decode_token,
    revoked_tokens,
    verify_password,
)
from ..state_store import LoginFailureTracker, SlidingWindowRateLimiter

router = APIRouter(prefix="/api/auth", tags=["统一认证"])

# 登录防爆破（M4 整改）：默认进程内存实现；配置 MEDPLAT_REDIS_URL 后
# 自动切换 Redis 共享存储，多实例部署下锁定状态全局生效。
FAIL_LIMIT = 5
LOCK_SECONDS = 600
_login_failures = LoginFailureTracker(fail_limit=FAIL_LIMIT, lock_seconds=LOCK_SECONDS)

# 终审轮（浙#80 资源控制）：登录失败按来源 IP 限速——60 秒内同 IP 失败超过 50 次
# 直接 429（防分布式撞库跨账号绕过按用户名的锁定）。成功登录不计数。
IP_FAIL_LIMIT = 50
IP_FAIL_WINDOW_SECONDS = 60
_ip_fail_limiter = SlidingWindowRateLimiter(
    max_events=IP_FAIL_LIMIT, window_seconds=IP_FAIL_WINDOW_SECONDS
)

_bearer = HTTPBearer(auto_error=False)

#: TOTP 密钥的"已生成待验证"前缀：setup 后先带此前缀落库，本人验证一次通过
#: 才去前缀启用——防止把密钥直接写成启用态，用户没扫码就被自己锁在门外。
TOTP_PENDING_PREFIX = "pending:"


def _reset_login_failures() -> None:
    """测试辅助：清空锁定、限速与会话登记状态。"""
    _login_failures.clear_all()
    _ip_fail_limiter.clear_all()
    active_sessions.clear_all()


def record_login_event(
    db: Session,
    *,
    username: str,
    ip: str,
    success: bool,
    user_id: int | None = None,
    fail_reason: str = "",
    channel: str = "password",
) -> None:
    """登录留痕落库（等保 E1）。**立即 commit**：失败路径随后就 raise，
    留痕不能跟着请求事务一起回滚——记的就是"这次没成"。
    居民端通道复用本函数时传 channel="sms"/"wechat"（居民账号不在 users 表，
    user_id 传 None、username 传通道内标识）。
    """
    db.add(
        LoginLog(
            username=username,
            user_id=user_id,
            ip=ip,
            success=success,
            fail_reason=fail_reason,
            channel=channel,
        )
    )
    db.commit()


def _totp_required_roles() -> set[str]:
    """解析 MEDPLAT_TOTP_REQUIRED_ROLES（逗号分隔；空=双因素整体关闭）。"""
    return {r.strip() for r in settings.totp_required_roles.split(",") if r.strip()}


def _totp_enabled(user: User) -> bool:
    """已启用 = 有密钥且不带 pending 前缀（pending 是"生成了还没验证"的中间态）。"""
    secret = user.totp_secret or ""
    return bool(secret) and not secret.startswith(TOTP_PENDING_PREFIX)


class LoginIn(LoginRequest):
    # TOTP 动态验证码：仅当账号角色被要求双因素且已开通时校验；其余情况忽略
    totp_code: str | None = None


class LoginOut(TokenResponse):
    # 渐进启用提示：角色被要求双因素但尚未开通时为 true；其余情况不出现在响应里
    # （login 路由 response_model_exclude_none，默认配置下响应字节与升级前完全一致）
    totp_setup_required: bool | None = None


@router.post("/login", response_model=LoginOut, response_model_exclude_none=True)
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    client_ip = request.client.host if request.client else "unknown"

    def fail(status_code: int, detail: str, reason: str, user_id: int | None = None):
        record_login_event(
            db, username=body.username, ip=client_ip, success=False,
            user_id=user_id, fail_reason=reason,
        )
        return HTTPException(status_code=status_code, detail=detail)

    remain = _login_failures.locked_remaining(body.username)
    if remain > 0:
        raise fail(423, f"账号已锁定，请 {remain // 60 + 1} 分钟后重试", "locked")

    user = db.query(User).filter(User.username == body.username).first()
    if user is None or not verify_password(body.password, user.password_hash):
        if not _ip_fail_limiter.allow(client_ip):
            raise fail(429, "该来源登录失败过于频繁，请稍后重试", "ip_throttled")
        if _login_failures.record_failure(body.username):
            raise fail(423, "连续失败次数过多，账号锁定10分钟", "lock_triggered",
                       user.id if user else None)
        raise fail(401, "用户名或密码错误", "bad_credentials", user.id if user else None)

    # 停用校验放在口令验证**之后**：口令都不对的人不该探测出"这个号被停用了"
    if user.status == "disabled":
        raise fail(403, "账号已停用，请联系管理员", "disabled", user.id)

    # TOTP 双因素（等保 E1）：开关为空 = 全部旁路；角色命中且已开通才强制校验；
    # 命中但未开通 → 放行 + totp_setup_required 提示（渐进启用，不锁死存量账号）
    totp_setup_required: bool | None = None
    if user.role in _totp_required_roles():
        if _totp_enabled(user):
            if not body.totp_code:
                raise fail(401, "该账号已启用动态口令，请同时提供 totp_code", "totp_required", user.id)
            if not totp.verify(user.totp_secret or "", body.totp_code):
                raise fail(401, "动态验证码不正确或已过期", "totp_invalid", user.id)
        else:
            totp_setup_required = True

    # 并发会话上限（等保 E1）：0=不限（零开销旁路）。名额随登出/闲置淘汰/令牌过期释放
    if settings.session_max_concurrent > 0:
        if active_sessions.active_count(user.username) >= settings.session_max_concurrent:
            raise fail(
                409,
                f"该账号活跃会话已达上限（{settings.session_max_concurrent}），"
                "请先在其他终端登出后重试",
                "concurrent_limit",
                user.id,
            )

    _login_failures.reset(body.username)
    token = create_token(user.username, user.role)
    if settings.session_max_concurrent > 0:
        claims = decode_token(token) or {}
        ttl = max(int(claims.get("exp", 0) - time.time()), 60)
        active_sessions.register(user.username, str(claims.get("jti", "")), ttl_seconds=ttl)
    record_login_event(db, username=user.username, ip=client_ip, success=True, user_id=user.id)
    # G3 双模会话（P1-23）：前端声明 X-Token-Transport: cookie 时，同一枚令牌
    # 同时进 HttpOnly Cookie 并配发 CSRF Cookie（双提交）。响应体 access_token
    # 保持原样返回，Header 模式对接方零感知；opt-in 的取舍见 deps.wants_cookie_auth。
    if wants_cookie_auth(request):
        set_auth_cookies(
            response, token,
            cookie_name=AUTH_COOKIE, csrf_cookie_name=CSRF_COOKIE, max_age=TOKEN_TTL_SECONDS,
        )
    return LoginOut(
        access_token=token, role=user.role, totp_setup_required=totp_setup_required
    )


class LogoutOut(BaseModel):
    logged_out: bool


@router.post("/logout", response_model=LogoutOut, dependencies=[Depends(get_current_user)])
def logout(
    request: Request,
    response: Response,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
):
    """登出：当前令牌加入黑名单立即失效。

    L-9 整改：按令牌 jti 拉黑（Redis/内存中不再存完整令牌明文），
    TTL 取令牌剩余寿命；无 jti 的历史令牌退回按原文拉黑。
    等保 E1：同时从会话登记中移除，即时释放并发名额。
    G3：Cookie 会话的令牌从 Cookie 里取（与 get_current_user 同一优先级：
    header 优先），并顺带清掉两个会话 Cookie。
    """
    token = credentials.credentials if credentials is not None else request.cookies.get(AUTH_COOKIE, "")
    if token:
        claims = decode_token(token)
        if claims and claims.get("jti"):
            ttl = max(int(claims.get("exp", 0) - time.time()), 60)
            revoked_tokens.add(claims["jti"], ttl_seconds=ttl)
            active_sessions.remove(str(claims.get("sub", "")), claims["jti"])
        else:  # pragma: no cover - 兼容无 jti 的历史令牌
            revoked_tokens.add(token)
    clear_auth_cookies(response, cookie_name=AUTH_COOKIE, csrf_cookie_name=CSRF_COOKIE)
    return LogoutOut(logged_out=True)


# ---------- TOTP 双因素：本人开通 / 关闭（等保 E1） ----------


class TotpSetupOut(BaseModel):
    secret: str
    otpauth_uri: str
    note: str


class TotpCodeIn(BaseModel):
    code: str


class TotpStatusOut(BaseModel):
    enabled: bool


@router.post("/totp/setup", response_model=TotpSetupOut)
def totp_setup(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """本人开通第一步：生成密钥（pending 态）并返回 otpauth URI 供令牌 App 扫码。

    重复调用会重新生成（覆盖未验证的旧 pending 密钥——扫错了码重来是常事）；
    已启用的账号须先关闭再换绑，防止误操作把在用的密钥顶掉。
    """
    if _totp_enabled(user):
        raise HTTPException(status_code=409, detail="动态口令已启用；如需更换请先验证码关闭后重新开通")
    secret = totp.generate_secret()
    user.totp_secret = TOTP_PENDING_PREFIX + secret
    db.commit()
    return TotpSetupOut(
        secret=secret,
        otpauth_uri=totp.otpauth_uri(user.username, secret),
        note="请用令牌App扫码或录入密钥，再调用 /api/auth/totp/activate 验证一次以完成启用",
    )


@router.post("/totp/activate", response_model=TotpStatusOut)
def totp_activate(
    body: TotpCodeIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """本人开通第二步：验一次当前验证码，通过即启用（密钥去 pending 前缀）。"""
    if _totp_enabled(user):
        return TotpStatusOut(enabled=True)
    if not user.totp_secret or not user.totp_secret.startswith(TOTP_PENDING_PREFIX):
        raise HTTPException(status_code=409, detail="尚未生成密钥，请先调用 /api/auth/totp/setup")
    secret = user.totp_secret[len(TOTP_PENDING_PREFIX):]
    if not totp.verify(secret, body.code):
        raise HTTPException(status_code=400, detail="动态验证码不正确，请确认令牌App时间与密钥无误")
    user.totp_secret = secret
    db.commit()
    return TotpStatusOut(enabled=True)


@router.post("/totp/disable", response_model=TotpStatusOut)
def totp_disable(
    body: TotpCodeIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """本人关闭：须验当前验证码——拿到会话令牌不等于拿到手机，关闭也要过第二因素。"""
    if not _totp_enabled(user):
        raise HTTPException(status_code=409, detail="动态口令未启用")
    if not totp.verify(user.totp_secret or "", body.code):
        raise HTTPException(status_code=400, detail="动态验证码不正确")
    user.totp_secret = None
    db.commit()
    return TotpStatusOut(enabled=False)
