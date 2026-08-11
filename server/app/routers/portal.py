"""居民端服务门户：电子健康档案向本人开放（121号文第五条）。

## 身份体系（重新设计）

原先靠"电子健康卡号 + 身份证号"双因子直接查档案——居民记不住卡号、且是一个
免登录的证件号查询面。现改为**账号体系**，与主流居民端一致：

1. **登录**：手机号验证码 或 微信网页授权 → 得到居民令牌（scope=portal）。
   登录只证明"这个手机号/微信是我的"，此时还看不到任何档案。
2. **实名绑定**：姓名 + 身份证号匹配患者主索引 → 账户与档案绑定。
   一份档案只能被一个账户绑定；绑定失败按次锁定，防证件号撞库。
   例外：登录手机号在患者主索引中**唯一**命中时自动完成绑定（手机号即注册联系方式）。
3. **查档案 / 评价**：`/api/portal/me/*` 带令牌访问，服务端从账户取患者，
   客户端再也不传身份标识。

居民令牌与业务端令牌互不越界：居民令牌带 scope=portal，deps.get_current_user
见到即拒；业务端令牌无 scope，current_resident 见到也拒。

旧的双因子接口（`/my-archive`、`/surveys`）保留为过渡兼容，可用
MEDPLAT_PORTAL_LEGACY_VERIFY=false 关闭（生产建议关闭）。
"""
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..privacy import mask_phone
from ..models import (
    ChronicPatient,
    Encounter,
    ExamReport,
    ExamRequest,
    HealthArticle,
    Patient,
    ResidentAccount,
    SatisfactionSurvey,
    SmsCode,
)
from ..security import create_token, decode_token, hash_password, revoked_tokens, verify_password
from ..sms import get_sms_provider
from ..state_store import LoginFailureTracker, SlidingWindowRateLimiter
from ..wechat import MockWeChatProvider, get_wechat_provider
from .chronic import guidance_for

router = APIRouter(prefix="/api/portal", tags=["居民端"])

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
# 单条验证码最多试错 5 次，超限即作废（防对已下发的码穷举）
MAX_CODE_ATTEMPTS = 5

# 双因子核验防撞库：同一证件号连续5次核验失败锁定10分钟
_verify_failures = LoginFailureTracker(fail_limit=5, lock_seconds=600)
# 验证码校验失败：按手机号计数
_code_failures = LoginFailureTracker(fail_limit=5, lock_seconds=600)
# 实名绑定失败：按账户计数，防登录后拿账户去撞他人身份证号
_bind_failures = LoginFailureTracker(fail_limit=5, lock_seconds=600)
# 验证码下发限流：单手机号 10 分钟 5 条、单来源 IP 10 分钟 20 条
_send_by_phone = SlidingWindowRateLimiter(max_events=5, window_seconds=600)
_send_by_ip = SlidingWindowRateLimiter(max_events=20, window_seconds=600)
# 同一手机号两条验证码之间的最小间隔
SEND_COOLDOWN_SECONDS = 60

_portal_bearer = HTTPBearer(auto_error=False)


def _reset_portal_failures() -> None:
    """测试辅助：清空核验/限流状态。"""
    for tracker in (_verify_failures, _code_failures, _bind_failures):
        tracker.clear_all()
    _send_by_phone.clear_all()
    _send_by_ip.clear_all()


def _naive_utcnow() -> datetime:
    """与模型中 DateTime 列一致的无时区 UTC 时刻（SQLite 不存时区）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ============================================================================
# 登录：手机号验证码
# ============================================================================


class SendCodeIn(BaseModel):
    phone: str = Field(min_length=11, max_length=11)
    # login=登录, bind=已登录账户补绑手机号
    purpose: str = Field(default="login", pattern="^(login|bind)$")


def _check_phone(phone: str) -> str:
    if not PHONE_RE.match(phone):
        raise HTTPException(status_code=422, detail="手机号格式不正确")
    return phone


@router.post("/auth/sms/code")
def send_sms_code(body: SendCodeIn, request: Request, db: Session = Depends(get_db)):
    """下发登录验证码。

    三重限流：单号冷却 60 秒、单号 10 分钟 5 条、单 IP 10 分钟 20 条。
    验证码只落散列；仅当通道为 console 且**非生产环境**时在响应中回显
    `debug_code`，用于本地联调与演示站——生产环境永远不回显。
    """
    phone = _check_phone(body.phone)
    client_ip = request.client.host if request.client else "unknown"
    if not _send_by_ip.allow(f"portal:sms:{client_ip}"):
        raise HTTPException(status_code=429, detail="发送过于频繁，请稍后再试")

    last = (
        db.query(SmsCode)
        .filter(SmsCode.phone == phone, SmsCode.purpose == body.purpose)
        .order_by(SmsCode.id.desc())
        .first()
    )
    if last is not None:
        elapsed = (_naive_utcnow() - last.created_at).total_seconds()
        if elapsed < SEND_COOLDOWN_SECONDS:
            raise HTTPException(
                status_code=429, detail=f"请{int(SEND_COOLDOWN_SECONDS - elapsed)}秒后再获取验证码"
            )
    if not _send_by_phone.allow(f"portal:sms:{phone}"):
        raise HTTPException(status_code=429, detail="该手机号今日获取过于频繁，请稍后再试")

    code = f"{secrets.randbelow(1_000_000):06d}"
    ttl = settings.sms_code_ttl_seconds
    db.add(
        SmsCode(
            phone=phone,
            purpose=body.purpose,
            code_hash=hash_password(code),
            expires_at=_naive_utcnow() + timedelta(seconds=ttl),
        )
    )
    db.commit()

    provider = get_sms_provider()
    content = f"【{settings.sms_sign_name}】验证码 {code}，{ttl // 60}分钟内有效，请勿转发。"
    if not provider.send(phone, content):
        raise HTTPException(status_code=502, detail="短信通道暂不可用，请稍后重试")

    result = {"sent": True, "expires_in": ttl, "cooldown_seconds": SEND_COOLDOWN_SECONDS}
    if provider.name == "console" and not settings.is_production:
        result["debug_code"] = code
    return result


def _consume_code(db: Session, phone: str, code: str, purpose: str) -> None:
    """校验并消费验证码；失败抛 4xx。成功后该码立即作废（一次性）。"""
    key = f"portal:code:{phone}"
    if _code_failures.locked_remaining(key) > 0:
        raise HTTPException(status_code=429, detail="验证失败次数过多，请10分钟后再试")
    record = (
        db.query(SmsCode)
        .filter(
            SmsCode.phone == phone,
            SmsCode.purpose == purpose,
            SmsCode.consumed.is_(False),
            SmsCode.expires_at > _naive_utcnow(),
        )
        .order_by(SmsCode.id.desc())
        .first()
    )
    if record is None:
        _code_failures.record_failure(key)
        raise HTTPException(status_code=400, detail="验证码不存在或已过期，请重新获取")
    if record.attempts >= MAX_CODE_ATTEMPTS:
        record.consumed = True
        db.commit()
        raise HTTPException(status_code=400, detail="验证码错误次数过多，请重新获取")
    if not verify_password(code, record.code_hash):
        record.attempts += 1
        db.commit()
        _code_failures.record_failure(key)
        raise HTTPException(status_code=400, detail="验证码错误")
    record.consumed = True
    db.commit()
    _code_failures.reset(key)


def _issue_token(account: ResidentAccount) -> str:
    return create_token(
        f"resident:{account.id}",
        role="resident",
        extra={"scope": "portal", "account_id": account.id},
        ttl_seconds=settings.portal_token_ttl_seconds,
    )


def _autobind_by_phone(db: Session, account: ResidentAccount) -> None:
    """登录手机号在患者主索引中唯一命中时自动完成实名绑定。

    "唯一"是关键：一个手机号常被登记为全家人的联系方式，命中多条时不猜，
    交给显式实名绑定。已被别的账户绑定的档案也跳过。
    """
    if account.patient_id or not account.phone:
        return
    matches = db.query(Patient).filter(Patient.phone == account.phone).limit(2).all()
    if len(matches) != 1:
        return
    patient = matches[0]
    taken = (
        db.query(ResidentAccount)
        .filter(ResidentAccount.patient_id == patient.id, ResidentAccount.id != account.id)
        .first()
    )
    if taken is None:
        account.patient_id = patient.id


def _login_result(db: Session, account: ResidentAccount) -> dict:
    account.last_login_at = _naive_utcnow()
    db.commit()
    patient = db.get(Patient, account.patient_id) if account.patient_id else None
    return {
        "access_token": _issue_token(account),
        "token_type": "bearer",
        "expires_in": settings.portal_token_ttl_seconds,
        "bound": patient is not None,
        "name": patient.name if patient else "",
        "nickname": account.nickname,
    }


def _account_by(db: Session, field, value: str, **create_kwargs) -> ResidentAccount:
    """按唯一标识取账户，不存在则创建。

    并发首登会同时插入两条同 phone/openid 记录，靠数据库唯一约束挡住，
    撞约束后回滚并回查既有账户（与患者建档的幂等处理同一套路）。
    """
    account = db.query(ResidentAccount).filter(field == value).first()
    if account is not None:
        return account
    account = ResidentAccount(**create_kwargs)
    db.add(account)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        account = db.query(ResidentAccount).filter(field == value).first()
        if account is None:  # pragma: no cover - 仅约束异常非本键冲突时
            raise
        return account
    db.refresh(account)
    return account


class SmsLoginIn(BaseModel):
    phone: str = Field(min_length=11, max_length=11)
    code: str = Field(min_length=4, max_length=8)


@router.post("/auth/sms/login")
def sms_login(body: SmsLoginIn, db: Session = Depends(get_db)):
    """手机号验证码登录：首次登录自动开户，命中唯一患者时顺带完成实名绑定。"""
    phone = _check_phone(body.phone)
    _consume_code(db, phone, body.code, "login")
    account = _account_by(db, ResidentAccount.phone, phone, phone=phone)
    if account.status != "active":
        raise HTTPException(status_code=403, detail="账户已停用，请联系服务机构")
    _autobind_by_phone(db, account)
    return _login_result(db, account)


# ============================================================================
# 登录：微信网页授权
# ============================================================================


@router.get("/auth/wechat/authorize")
def wechat_authorize():
    """返回微信授权页地址。

    mock 通道额外返回 `mock_code`，前端无需跳转即可就地完成一次授权，
    这样没有公众号的演示站也能走通完整登录流程。
    """
    provider = get_wechat_provider()
    state = secrets.token_urlsafe(12)
    result = {"provider": provider.name, "state": state, "authorize_url": provider.authorize_url(state)}
    if isinstance(provider, MockWeChatProvider):
        result["mock_code"] = provider.mock_code(state)
    return result


class WeChatLoginIn(BaseModel):
    code: str = Field(min_length=1)
    state: str = ""


@router.post("/auth/wechat/login")
def wechat_login(body: WeChatLoginIn, db: Session = Depends(get_db)):
    """微信授权码登录：首次授权自动开户，仍需实名绑定后才可见档案。"""
    info = get_wechat_provider().exchange_code(body.code)
    if info is None:
        raise HTTPException(status_code=400, detail="微信授权失败，请重新发起")
    account = _account_by(
        db,
        ResidentAccount.wechat_openid,
        info["openid"],
        wechat_openid=info["openid"],
        wechat_unionid=info.get("unionid", ""),
        nickname=info.get("nickname", ""),
    )
    if account.status != "active":
        raise HTTPException(status_code=403, detail="账户已停用，请联系服务机构")
    return _login_result(db, account)


# ============================================================================
# 居民令牌依赖
# ============================================================================


def current_resident(
    credentials: HTTPAuthorizationCredentials | None = Depends(_portal_bearer),
    db: Session = Depends(get_db),
) -> ResidentAccount:
    """解析居民令牌 → 账户。业务端令牌（无 scope=portal）在此被拒。"""
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    claims = decode_token(credentials.credentials)
    if claims is None or claims.get("scope") != "portal":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="登录状态无效，请重新登录")
    if (claims.get("jti") or credentials.credentials) in revoked_tokens:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="已退出登录")
    account = db.get(ResidentAccount, claims.get("account_id", 0))
    if account is None or account.status != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账户不存在或已停用")
    return account


def current_resident_patient(
    account: ResidentAccount = Depends(current_resident), db: Session = Depends(get_db)
) -> Patient:
    """要求账户已完成实名绑定，返回其绑定的患者档案。"""
    patient = db.get(Patient, account.patient_id) if account.patient_id else None
    if patient is None:
        raise HTTPException(status_code=403, detail="请先完成实名绑定")
    return patient


@router.post("/auth/logout")
def portal_logout(
    credentials: HTTPAuthorizationCredentials | None = Depends(_portal_bearer),
    account: ResidentAccount = Depends(current_resident),
):
    """退出登录：按 jti 拉黑当前令牌（与业务端登出同一黑名单）。"""
    claims = decode_token(credentials.credentials) or {}
    revoked_tokens.add(claims.get("jti") or credentials.credentials, ttl_seconds=settings.portal_token_ttl_seconds)
    return {"logged_out": True}


# ============================================================================
# 实名绑定与账户信息
# ============================================================================


class RealNameIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    id_card: str = Field(min_length=6, max_length=18)


@router.post("/auth/realname")
def bind_realname(
    body: RealNameIn,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """实名绑定：姓名 + 身份证号匹配患者主索引后与账户绑定。

    一份档案只允许一个账户绑定（409）；失败按账户计数锁定，防登录后撞库。
    绑定成功时顺带回填患者联系电话（原先为空的话）。
    """
    if account.patient_id:
        raise HTTPException(status_code=409, detail="该账户已完成实名绑定")
    key = f"portal:bind:{account.id}"
    if _bind_failures.locked_remaining(key) > 0:
        raise HTTPException(status_code=429, detail="绑定尝试过于频繁，请10分钟后再试")
    patient = (
        db.query(Patient)
        .filter(Patient.name == body.name.strip(), Patient.id_card == body.id_card.strip())
        .first()
    )
    if patient is None:
        _bind_failures.record_failure(key)
        raise HTTPException(status_code=404, detail="未找到该身份的健康档案，请先到就近机构建档")
    taken = (
        db.query(ResidentAccount)
        .filter(ResidentAccount.patient_id == patient.id, ResidentAccount.id != account.id)
        .first()
    )
    if taken is not None:
        raise HTTPException(status_code=409, detail="该健康档案已被其他账号绑定，请联系服务机构核实")
    account.patient_id = patient.id
    if account.phone and not patient.phone:
        patient.phone = account.phone
    db.commit()
    _bind_failures.reset(key)
    return {"bound": True, "name": patient.name, "ehc_no": patient.ehc_no}


class BindPhoneIn(BaseModel):
    phone: str = Field(min_length=11, max_length=11)
    code: str = Field(min_length=4, max_length=8)


@router.post("/auth/bind-phone")
def bind_phone(
    body: BindPhoneIn,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """微信登录的账户补绑手机号（purpose=bind 的验证码）。"""
    phone = _check_phone(body.phone)
    if account.phone:
        raise HTTPException(status_code=409, detail="该账户已绑定手机号")
    if db.query(ResidentAccount).filter(ResidentAccount.phone == phone).first() is not None:
        raise HTTPException(status_code=409, detail="该手机号已被其他账号绑定")
    _consume_code(db, phone, body.code, "bind")
    account.phone = phone
    _autobind_by_phone(db, account)
    db.commit()
    return {"phone": phone, "bound_patient": account.patient_id is not None}


class BindWeChatIn(BaseModel):
    code: str = Field(min_length=1)


@router.post("/auth/bind-wechat")
def bind_wechat(
    body: BindWeChatIn,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """手机号登录的账户补绑微信，之后两种方式登入同一账户。"""
    if account.wechat_openid:
        raise HTTPException(status_code=409, detail="该账户已绑定微信")
    info = get_wechat_provider().exchange_code(body.code)
    if info is None:
        raise HTTPException(status_code=400, detail="微信授权失败，请重新发起")
    if (
        db.query(ResidentAccount).filter(ResidentAccount.wechat_openid == info["openid"]).first()
        is not None
    ):
        raise HTTPException(status_code=409, detail="该微信已被其他账号绑定")
    account.wechat_openid = info["openid"]
    account.wechat_unionid = info.get("unionid", "")
    account.nickname = account.nickname or info.get("nickname", "")
    db.commit()
    return {"bound": True, "nickname": account.nickname}


@router.get("/me")
def portal_me(account: ResidentAccount = Depends(current_resident), db: Session = Depends(get_db)):
    """当前账户信息：手机号脱敏返回，实名信息只回姓名与健康卡号。"""
    patient = db.get(Patient, account.patient_id) if account.patient_id else None
    return {
        "account_id": account.id,
        "phone": mask_phone(account.phone or ""),
        "wechat_bound": bool(account.wechat_openid),
        "nickname": account.nickname,
        "bound": patient is not None,
        "name": patient.name if patient else "",
        "ehc_no": patient.ehc_no if patient else "",
    }


# ============================================================================
# 档案与评价（令牌方式）
# ============================================================================


def _build_archive(db: Session, patient: Patient) -> dict:
    encounters = (
        db.query(Encounter)
        .filter(Encounter.patient_id == patient.id)
        .order_by(Encounter.id.desc())
        .limit(50)
        .all()
    )
    reports = (
        db.query(ExamReport)
        .join(ExamRequest, ExamReport.request_id == ExamRequest.id)
        .filter(ExamRequest.patient_id == patient.id)
        .order_by(ExamReport.id.desc())
        .limit(50)
        .all()
    )
    chronic = db.query(ChronicPatient).filter(ChronicPatient.patient_id == patient.id).all()

    return {
        "name": patient.name,
        "ehc_no": patient.ehc_no,
        "encounters": [
            {"diagnosis_name": e.diagnosis_name, "encounter_type": e.encounter_type, "summary": e.summary}
            for e in encounters
        ],
        "exam_reports": [{"conclusion": r.conclusion, "critical": r.critical} for r in reports],
        "chronic_care": [
            {
                "disease": c.disease,
                "level": c.level,
                "next_followup_due": c.next_due,
                "guidance_points": guidance_for(db, c.disease),
            }
            for c in chronic
        ],
    }


@router.get("/me/archive")
def my_archive_token(
    patient: Patient = Depends(current_resident_patient), db: Session = Depends(get_db)
):
    """登录态查本人档案：客户端不再传任何身份标识。"""
    return _build_archive(db, patient)


class MySurveyIn(BaseModel):
    target_type: str = Field(pattern="^(contract|encounter|consultation)$")
    target_id: int = 0
    score: int = Field(ge=1, le=5)
    comment: str = ""


@router.post("/me/surveys", status_code=201)
def my_survey(
    body: MySurveyIn,
    patient: Patient = Depends(current_resident_patient),
    db: Session = Depends(get_db),
):
    """登录态提交满意度评价。"""
    survey = SatisfactionSurvey(
        target_type=body.target_type,
        target_id=body.target_id,
        patient_id=patient.id,
        score=body.score,
        comment=body.comment,
    )
    db.add(survey)
    db.commit()
    return {"id": survey.id, "submitted": True}


# ============================================================================
# 过渡兼容：电子健康卡号 + 身份证号双因子（已被账号体系取代）
# ============================================================================


def _require_legacy_enabled() -> None:
    if not settings.portal_legacy_verify:
        raise HTTPException(
            status_code=410, detail="该接口已停用，请改用手机号验证码或微信登录后访问 /api/portal/me/*"
        )


def _verify_patient(db: Session, ehc_no: str, id_card: str) -> Patient:
    """双因子核验（带速率限制）：锁定期 429；失败计数、成功清零。"""
    key = f"portal:{id_card}"
    if _verify_failures.locked_remaining(key) > 0:
        raise HTTPException(status_code=429, detail="核验尝试过于频繁，请10分钟后再试")
    patient = (
        db.query(Patient).filter(Patient.ehc_no == ehc_no, Patient.id_card == id_card).first()
    )
    if patient is None:
        _verify_failures.record_failure(key)
        raise HTTPException(status_code=403, detail="身份核验失败")
    _verify_failures.reset(key)
    return patient


class ArchiveQuery(BaseModel):
    ehc_no: str = Field(min_length=1)
    id_card: str = Field(min_length=1)


@router.get("/my-archive", deprecated=True)
def my_archive(ehc_no: str, id_card: str, db: Session = Depends(get_db)):
    """【已废弃】身份证号入 query 有日志泄露面，请改用登录态 GET /api/portal/me/archive。"""
    _require_legacy_enabled()
    patient = _verify_patient(db, ehc_no, id_card)
    return _build_archive(db, patient)


@router.post("/my-archive", deprecated=True)
def my_archive_post(body: ArchiveQuery, db: Session = Depends(get_db)):
    """【已废弃】请改用登录态 GET /api/portal/me/archive。"""
    _require_legacy_enabled()
    patient = _verify_patient(db, body.ehc_no, body.id_card)
    return _build_archive(db, patient)


class PortalSurveyCreate(BaseModel):
    ehc_no: str
    id_card: str
    target_type: str = Field(pattern="^(contract|encounter|consultation)$")
    target_id: int = 0
    score: int = Field(ge=1, le=5)
    comment: str = ""


@router.post("/surveys", status_code=201, deprecated=True)
def portal_submit_survey(body: PortalSurveyCreate, db: Session = Depends(get_db)):
    """【已废弃】请改用登录态 POST /api/portal/me/surveys。"""
    _require_legacy_enabled()
    patient = _verify_patient(db, body.ehc_no, body.id_card)
    survey = SatisfactionSurvey(
        target_type=body.target_type,
        target_id=body.target_id,
        patient_id=patient.id,
        score=body.score,
        comment=body.comment,
    )
    db.add(survey)
    db.commit()
    return {"id": survey.id, "submitted": True}


@router.get("/health-articles")
def published_articles(category: str | None = None, db: Session = Depends(get_db)):
    """健康宣教：居民端展示已发布文章（无需登录）。"""
    q = db.query(HealthArticle).filter(HealthArticle.status == "published")
    if category:
        q = q.filter(HealthArticle.category == category)
    return [
        {"id": a.id, "title": a.title, "category": a.category, "content": a.content}
        for a in q.order_by(HealthArticle.id.desc()).limit(50).all()
    ]
