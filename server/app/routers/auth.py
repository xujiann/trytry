"""统一认证：登录（防爆破锁定）、登出（令牌黑名单）。"""
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import User
from ..schemas import LoginRequest, TokenResponse
from ..security import create_token, decode_token, revoked_tokens, verify_password
from ..state_store import LoginFailureTracker

router = APIRouter(prefix="/api/auth", tags=["统一认证"])

# 登录防爆破（M4 整改）：默认进程内存实现；配置 MEDPLAT_REDIS_URL 后
# 自动切换 Redis 共享存储，多实例部署下锁定状态全局生效。
FAIL_LIMIT = 5
LOCK_SECONDS = 600
_login_failures = LoginFailureTracker(fail_limit=FAIL_LIMIT, lock_seconds=LOCK_SECONDS)

_bearer = HTTPBearer(auto_error=False)


def _reset_login_failures() -> None:
    """测试辅助：清空锁定状态。"""
    _login_failures.clear_all()


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    remain = _login_failures.locked_remaining(body.username)
    if remain > 0:
        raise HTTPException(status_code=423, detail=f"账号已锁定，请 {remain // 60 + 1} 分钟后重试")

    user = db.query(User).filter(User.username == body.username).first()
    if user is None or not verify_password(body.password, user.password_hash):
        if _login_failures.record_failure(body.username):
            raise HTTPException(status_code=423, detail="连续失败次数过多，账号锁定10分钟")
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    _login_failures.reset(body.username)
    return TokenResponse(access_token=create_token(user.username, user.role), role=user.role)


@router.post("/logout", dependencies=[Depends(get_current_user)])
def logout(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)):
    """登出：当前令牌加入黑名单立即失效。

    L-9 整改：按令牌 jti 拉黑（Redis/内存中不再存完整令牌明文），
    TTL 取令牌剩余寿命；无 jti 的历史令牌退回按原文拉黑。
    """
    if credentials is not None:
        claims = decode_token(credentials.credentials)
        if claims and claims.get("jti"):
            ttl = max(int(claims.get("exp", 0) - time.time()), 60)
            revoked_tokens.add(claims["jti"], ttl_seconds=ttl)
        else:  # pragma: no cover - 兼容无 jti 的历史令牌
            revoked_tokens.add(credentials.credentials)
    return {"logged_out": True}
