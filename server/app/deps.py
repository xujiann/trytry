from datetime import date, timezone

from fastapi import Depends, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .database import get_db
from .models import User
from .security import decode_token, revoked_tokens

_bearer = HTTPBearer(auto_error=False)


def paginate(query, response: Response, offset: int = 0, limit: int = 100, max_limit: int = 500):
    """L-3（上轮 L4）分页整改：统一 offset/limit 分页，总数经 X-Total-Count 响应头返回。

    - 缺省行为与既有接口兼容（offset=0 时等价于原先的 .limit(N)）；
    - limit 超过 max_limit 时按 max_limit 截断，防一次拉全表。
    """
    response.headers["X-Total-Count"] = str(query.count())
    return query.offset(max(offset, 0)).limit(min(max(limit, 1), max_limit)).all()


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


def get_current_user(
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
    if token_issued_before_baseline(claims, user):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="密码已修改，令牌失效，请重新登录"
        )
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
    """角色守卫：admin 始终放行，其余角色需在允许清单内。"""

    def checker(user: User = Depends(get_current_user)) -> User:
        if user.role != "admin" and user.role not in roles:
            allowed = "、".join(ROLE_NAMES.get(r, r) for r in roles)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=f"需要以下角色之一：{allowed}"
            )
        return user

    return checker
