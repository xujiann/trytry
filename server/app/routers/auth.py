"""统一认证：登录（防爆破锁定）、登出（令牌黑名单）。"""
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import User
from ..schemas import LoginRequest, TokenResponse
from ..security import create_token, revoked_tokens, verify_password

router = APIRouter(prefix="/api/auth", tags=["统一认证"])

# 登录防爆破：同一用户名连续失败达到阈值后锁定（内存实现，多实例部署需换集中存储）
FAIL_LIMIT = 5
LOCK_SECONDS = 600
_login_failures: dict[str, dict] = {}

_bearer = HTTPBearer(auto_error=False)


def _reset_login_failures() -> None:
    """测试辅助：清空锁定状态。"""
    _login_failures.clear()


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    record = _login_failures.get(body.username)
    now = time.time()
    if record and record.get("locked_until", 0) > now:
        remain = int(record["locked_until"] - now)
        raise HTTPException(status_code=423, detail=f"账号已锁定，请 {remain // 60 + 1} 分钟后重试")

    user = db.query(User).filter(User.username == body.username).first()
    if user is None or not verify_password(body.password, user.password_hash):
        record = _login_failures.setdefault(body.username, {"count": 0, "locked_until": 0})
        record["count"] += 1
        if record["count"] >= FAIL_LIMIT:
            record["locked_until"] = now + LOCK_SECONDS
            record["count"] = 0
            raise HTTPException(status_code=423, detail="连续失败次数过多，账号锁定10分钟")
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    _login_failures.pop(body.username, None)
    return TokenResponse(access_token=create_token(user.username, user.role), role=user.role)


@router.post("/logout", dependencies=[Depends(get_current_user)])
def logout(credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)):
    """登出：当前令牌加入黑名单立即失效。"""
    if credentials is not None:
        revoked_tokens.add(credentials.credentials)
    return {"logged_out": True}
