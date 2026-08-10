from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .database import get_db
from .models import User
from .security import decode_token, revoked_tokens

_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未提供令牌")
    if credentials.credentials in revoked_tokens:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="令牌已登出失效")
    claims = decode_token(credentials.credentials)
    if claims is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="令牌无效或已过期")
    user = db.query(User).filter(User.username == claims["sub"]).first()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在")
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要管理员权限")
    return user


ROLE_NAMES = {
    "admin": "平台管理员",
    "director": "管理层",
    "doctor": "医师",
    "pharmacist": "药师",
    "public_health": "公卫人员",
    "operator": "经办人员",
}


def require_roles(*roles: str):
    """角色守卫：admin 始终放行，其余角色需在允许清单内。"""

    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role != "admin" and user.role not in roles:
            allowed = "、".join(ROLE_NAMES.get(r, r) for r in roles)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=f"需要以下角色之一：{allowed}"
            )
        return user

    return checker
