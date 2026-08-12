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
    Admission,
    Appointment,
    AppointmentSlot,
    Bed,
    BillDetail,
    ChargeItem,
    ChargePriceChange,
    ChronicPatient,
    ContractService,
    Encounter,
    ExamReport,
    ExamRequest,
    FamilyDoctorContract,
    HealthArticle,
    Notification,
    Organization,
    Patient,
    PaymentOrder,
    OperatingRoom,
    Referral,
    ResidentAccount,
    ResidentFamilyMember,
    SatisfactionSurvey,
    Settlement,
    SmsCode,
    SurgeryRequest,
    SurgerySchedule,
    Ward,
    utcnow,
)
from ..security import create_token, decode_token, hash_password, revoked_tokens, verify_password
from ..sms import get_sms_provider
from ..state_store import LoginFailureTracker, SlidingWindowRateLimiter
from ..wechat import MockWeChatProvider, get_wechat_provider
from .appointments import book_slot, release_appointment
from .notifications import notification_out
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


def accessible_patient(
    db: Session, account: ResidentAccount, patient_id: int | None
) -> Patient:
    """解析本次请求要访问哪一份档案，并校验访问权。

    可访问集合 = 本人档案 ∪ 已代管的家庭成员档案。不传 patient_id 时默认本人。
    越权一律 403，且不区分"不存在"与"无权"，避免被用来探测他人档案是否存在。
    """
    if patient_id is None or patient_id == account.patient_id:
        patient = db.get(Patient, account.patient_id) if account.patient_id else None
        if patient is None:
            raise HTTPException(status_code=403, detail="请先完成实名绑定")
        return patient
    managed = (
        db.query(ResidentFamilyMember)
        .filter(
            ResidentFamilyMember.account_id == account.id,
            ResidentFamilyMember.patient_id == patient_id,
        )
        .first()
    )
    patient = db.get(Patient, patient_id) if managed else None
    if patient is None:
        raise HTTPException(status_code=403, detail="无权访问该档案")
    return patient


@router.get("/me/archive")
def my_archive_token(
    patient_id: int | None = None,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """登录态查档案：默认本人，传 patient_id 可切换到已代管的家庭成员。"""
    patient = accessible_patient(db, account, patient_id)
    return _build_archive(db, patient)


# ============================================================================
# 家庭成员代管
# ============================================================================

MAX_FAMILY_MEMBERS = 5


class FamilyMemberIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    id_card: str = Field(min_length=6, max_length=18)
    relation: str = Field(default="other", pattern="^(spouse|child|parent|other)$")
    # 被代管人档案上登记了不同手机号时，需填该号码收到的验证码（purpose=bind）
    code: str = ""


@router.get("/me/family")
def list_family(
    account: ResidentAccount = Depends(current_resident), db: Session = Depends(get_db)
):
    """我的家庭成员：含本人，供客户端做档案切换。"""
    rows = (
        db.query(ResidentFamilyMember, Patient)
        .join(Patient, ResidentFamilyMember.patient_id == Patient.id)
        .filter(ResidentFamilyMember.account_id == account.id)
        .order_by(ResidentFamilyMember.id)
        .all()
    )
    members = []
    if account.patient_id:
        me = db.get(Patient, account.patient_id)
        if me is not None:
            members.append(
                {"patient_id": me.id, "name": me.name, "ehc_no": me.ehc_no, "relation": "self", "is_self": True}
            )
    members += [
        {
            "patient_id": p.id,
            "name": p.name,
            "ehc_no": p.ehc_no,
            "relation": m.relation,
            "is_self": False,
            "member_id": m.id,
        }
        for m, p in rows
    ]
    return members


@router.post("/me/family", status_code=201)
def add_family_member(
    body: FamilyMemberIn,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """添加代管家庭成员。

    核验分两档，取决于被代管人自己有没有留手机号：

    - 档案上登记的手机号与本账户不同 → 说明本人自己能收短信，必须提供**该号码**
      的验证码，光知道姓名和身份证号不足以代管（否则等于开了个证件号查档口子）；
    - 档案未登记手机号或就是本账户手机号（老人、儿童常见）→ 姓名+身份证号核验即可。

    失败同样计入账户绑定锁定计数，防止拿账户逐个撞身份证号。
    """
    if not account.patient_id:
        raise HTTPException(status_code=403, detail="请先完成本人实名绑定")
    count = (
        db.query(ResidentFamilyMember)
        .filter(ResidentFamilyMember.account_id == account.id)
        .count()
    )
    if count >= MAX_FAMILY_MEMBERS:
        raise HTTPException(status_code=409, detail=f"最多代管{MAX_FAMILY_MEMBERS}位家庭成员")

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
    if patient.id == account.patient_id:
        raise HTTPException(status_code=409, detail="本人档案无需添加为家庭成员")

    if patient.phone and patient.phone != account.phone:
        if not body.code:
            raise HTTPException(
                status_code=428,
                detail=f"该成员已登记手机号 {mask_phone(patient.phone)}，请获取该号码的验证码后再代管",
            )
        _consume_code(db, patient.phone, body.code, "bind")

    member = ResidentFamilyMember(
        account_id=account.id, patient_id=patient.id, relation=body.relation
    )
    db.add(member)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该成员已在您的家庭成员中") from None
    _bind_failures.reset(key)
    return {"member_id": member.id, "patient_id": patient.id, "name": patient.name}


@router.delete("/me/family/{member_id}")
def remove_family_member(
    member_id: int,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """解除代管关系（只删关系，不动档案本身）。"""
    member = (
        db.query(ResidentFamilyMember)
        .filter(
            ResidentFamilyMember.id == member_id,
            ResidentFamilyMember.account_id == account.id,
        )
        .first()
    )
    if member is None:
        raise HTTPException(status_code=404, detail="家庭成员不存在")
    db.delete(member)
    db.commit()
    return {"removed": True}


# ============================================================================
# 在线服务：预约、签约、账单、转诊
# ============================================================================


@router.get("/me/slots")
def portal_slots(
    org_id: int | None = None,
    slot_date: str | None = None,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """可约号源：只列还有余号的，避免居民点进去才发现约满。"""
    query = db.query(AppointmentSlot).filter(AppointmentSlot.booked < AppointmentSlot.capacity)
    if org_id is not None:
        query = query.filter(AppointmentSlot.org_id == org_id)
    if slot_date:
        query = query.filter(AppointmentSlot.slot_date == slot_date)
    rows = query.order_by(AppointmentSlot.slot_date, AppointmentSlot.slot_time).limit(200).all()
    org_names = {o.id: o.name for o in db.query(Organization).all()}
    return [
        {
            "id": s.id,
            "org_id": s.org_id,
            "org_name": org_names.get(s.org_id, ""),
            "resource_type": s.resource_type,
            "resource_name": s.resource_name,
            "slot_date": s.slot_date,
            "slot_time": s.slot_time,
            "remaining": s.capacity - s.booked,
        }
        for s in rows
    ]


class PortalBookIn(BaseModel):
    slot_id: int
    # 不传即为本人预约；传则须是已代管的家庭成员
    patient_id: int | None = None


@router.post("/me/appointments", status_code=201)
def portal_book(
    body: PortalBookIn,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """居民自助预约：复用管理端的号源占位逻辑（黑名单/原子占号/重复判定一致）。"""
    patient = accessible_patient(db, account, body.patient_id)
    appointment = book_slot(db, body.slot_id, patient.id)
    return {"id": appointment.id, "slot_id": appointment.slot_id, "status": appointment.status}


def _my_patient_ids(db: Session, account: ResidentAccount) -> list[int]:
    """本人 + 代管成员的患者 id 集合，用于"我的"各类列表查询。"""
    ids = [account.patient_id] if account.patient_id else []
    ids += [
        m.patient_id
        for m in db.query(ResidentFamilyMember)
        .filter(ResidentFamilyMember.account_id == account.id)
        .all()
    ]
    return ids


@router.get("/me/appointments")
def portal_my_appointments(
    account: ResidentAccount = Depends(current_resident), db: Session = Depends(get_db)
):
    """我的预约：含代管家庭成员的预约。"""
    ids = _my_patient_ids(db, account)
    if not ids:
        return []
    rows = (
        db.query(Appointment, AppointmentSlot, Patient)
        .join(AppointmentSlot, Appointment.slot_id == AppointmentSlot.id)
        .join(Patient, Appointment.patient_id == Patient.id)
        .filter(Appointment.patient_id.in_(ids))
        .order_by(Appointment.id.desc())
        .limit(100)
        .all()
    )
    org_names = {o.id: o.name for o in db.query(Organization).all()}
    return [
        {
            "id": a.id,
            "patient_id": p.id,
            "patient_name": p.name,
            "org_name": org_names.get(s.org_id, ""),
            "resource_name": s.resource_name,
            "slot_date": s.slot_date,
            "slot_time": s.slot_time,
            "status": a.status,
        }
        for a, s, p in rows
    ]


@router.post("/me/appointments/{appointment_id}/cancel")
def portal_cancel(
    appointment_id: int,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """取消我的预约：只能取消本人与代管成员的，且复用管理端的号源释放逻辑。"""
    appointment = db.get(Appointment, appointment_id)
    if appointment is None or appointment.patient_id not in _my_patient_ids(db, account):
        raise HTTPException(status_code=404, detail="预约不存在")
    release_appointment(db, appointment)
    return {"id": appointment.id, "status": appointment.status}


@router.get("/me/contract")
def portal_my_contract(
    patient_id: int | None = None,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """我的家医签约：协议、服务包与履约记录。"""
    patient = accessible_patient(db, account, patient_id)
    contracts = (
        db.query(FamilyDoctorContract)
        .filter(FamilyDoctorContract.patient_id == patient.id)
        .order_by(FamilyDoctorContract.id.desc())
        .all()
    )
    org_names = {o.id: o.name for o in db.query(Organization).all()}
    result = []
    for c in contracts:
        services = (
            db.query(ContractService)
            .filter(ContractService.contract_id == c.id)
            .order_by(ContractService.id.desc())
            .limit(20)
            .all()
        )
        result.append(
            {
                "id": c.id,
                "org_name": org_names.get(c.org_id, ""),
                "doctor_name": c.doctor_name,
                "package": c.package,
                "signed_date": c.signed_date,
                "status": c.status,
                "services": [
                    {"service_type": s.service_type, "note": s.note,
                     "date": s.created_at.date().isoformat()}
                    for s in services
                ],
            }
        )
    return result


@router.get("/me/bills")
def portal_my_bills(
    patient_id: int | None = None,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """我的账单：结算单与对应支付单状态。"""
    patient = accessible_patient(db, account, patient_id)
    settlements = (
        db.query(Settlement)
        .filter(Settlement.patient_id == patient.id)
        .order_by(Settlement.id.desc())
        .limit(50)
        .all()
    )
    org_names = {o.id: o.name for o in db.query(Organization).all()}
    paid_ids = {
        o.settlement_id
        for o in db.query(PaymentOrder)
        .filter(
            PaymentOrder.settlement_id.in_([s.id for s in settlements] or [0]),
            PaymentOrder.status == "paid",
        )
        .all()
    }
    return [
        {
            "id": s.id,
            "org_name": org_names.get(s.org_id, ""),
            "bill_type": s.bill_type,
            "total_amount": s.total_amount,
            "insurance_pay": s.insurance_pay,
            "self_pay": s.self_pay,
            "paid": s.id in paid_ids,
            "date": s.created_at.date().isoformat(),
        }
        for s in settlements
    ]


@router.get("/me/referrals")
def portal_my_referrals(
    patient_id: int | None = None,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """我的转诊进度。"""
    patient = accessible_patient(db, account, patient_id)
    rows = (
        db.query(Referral)
        .filter(Referral.patient_id == patient.id)
        .order_by(Referral.id.desc())
        .limit(50)
        .all()
    )
    org_names = {o.id: o.name for o in db.query(Organization).all()}
    return [
        {
            "id": r.id,
            "direction": r.direction,
            "from_org": org_names.get(r.from_org_id, ""),
            "to_org": org_names.get(r.to_org_id, ""),
            "reason": r.reason,
            "status": r.status,
            "date": r.created_at.date().isoformat(),
        }
        for r in rows
    ]


@router.get("/me/admissions")
def portal_my_admissions(
    patient_id: int | None = None,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """我的住院记录：在院/已出院、病区床位、住院天数与费用是否结清。

    住院天数按"当日入当日出计 1 天"，与成本核算、运行效率的口径一致，
    免得同一次住院在三个地方显示三个天数。
    """
    patient = accessible_patient(db, account, patient_id)
    rows = (
        db.query(Admission)
        .filter(Admission.patient_id == patient.id)
        .order_by(Admission.id.desc())
        .limit(50)
        .all()
    )
    org_names = {o.id: o.name for o in db.query(Organization).all()}
    wards = {w.id: w.name for w in db.query(Ward).all()}
    beds = {b.id: b.bed_no for b in db.query(Bed).all()}
    # 未结清明细：settlement_id 为空即尚未纳入任何结算单
    unsettled = {
        admission_id
        for (admission_id,) in db.query(BillDetail.admission_id)
        .filter(
            BillDetail.patient_id == patient.id,
            BillDetail.admission_id.isnot(None),
            BillDetail.settlement_id.is_(None),
        )
        .distinct()
        .all()
    }
    result = []
    for a in rows:
        end = (a.discharged_at or utcnow()).date()
        result.append(
            {
                "id": a.id,
                "org_name": org_names.get(a.org_id, ""),
                "ward_name": wards.get(a.ward_id, ""),
                "bed_no": beds.get(a.bed_id, ""),
                "doctor_name": a.doctor_name,
                "diagnosis_name": a.diagnosis_name,
                "status": a.status,
                "admitted_date": a.admitted_at.date().isoformat(),
                "discharged_date": a.discharged_at.date().isoformat() if a.discharged_at else "",
                "days": max((end - a.admitted_at.date()).days, 1),
                "settled": a.id not in unsettled,
            }
        )
    return result


@router.get("/me/admissions/{admission_id}/bill")
def portal_admission_bill(
    admission_id: int,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """住院费用清单：按收费类别汇总 + 明细，附结算摘要。

    越权访问按 404 处理（不区分"不存在"与"不是你的"），与其他居民端接口同口径。
    """
    admission = db.get(Admission, admission_id)
    allowed = _my_patient_ids(db, account)
    if admission is None or admission.patient_id not in allowed:
        raise HTTPException(status_code=404, detail="住院记录不存在")

    details = (
        db.query(BillDetail)
        .filter(BillDetail.admission_id == admission_id)
        .order_by(BillDetail.id)
        .limit(500)
        .all()
    )
    categories = {c.code: c.category for c in db.query(ChargeItem).all()}
    by_category: dict[str, float] = {}
    for d in details:
        key = categories.get(d.item_code, "other")
        by_category[key] = round(by_category.get(key, 0) + d.amount, 2)

    settlements = (
        db.query(Settlement)
        .filter(Settlement.admission_id == admission_id)
        .order_by(Settlement.id.desc())
        .all()
    )
    return {
        "admission_id": admission_id,
        "diagnosis_name": admission.diagnosis_name,
        "status": admission.status,
        "total_amount": round(sum(d.amount for d in details), 2),
        "by_category": by_category,
        "items": [
            {
                "item_name": d.item_name,
                "unit_price": d.unit_price,
                "quantity": d.quantity,
                "amount": d.amount,
                "category": categories.get(d.item_code, "other"),
                "settled": d.settlement_id is not None,
                "date": d.created_at.date().isoformat(),
            }
            for d in details
        ],
        "settlements": [
            {
                "id": s.id,
                "total_amount": s.total_amount,
                "insurance_pay": s.insurance_pay,
                "self_pay": s.self_pay,
                "date": s.created_at.date().isoformat(),
            }
            for s in settlements
        ],
    }


@router.get("/me/surgeries")
def portal_my_surgeries(
    patient_id: int | None = None,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """我的手术安排：术式、状态与已排定的时间地点。

    只回患者该知道的：术式名、日期时段、手术间、状态。术中记录（出血量、
    术中所见、并发症）不在居民端展示——那是给医生看的专业文书，直接推给
    患者容易造成误读，需要时由医生当面解释。
    """
    patient = accessible_patient(db, account, patient_id)
    rows = (
        db.query(SurgeryRequest)
        .filter(SurgeryRequest.patient_id == patient.id)
        .order_by(SurgeryRequest.id.desc())
        .limit(50)
        .all()
    )
    schedules = {
        s.request_id: s
        for s in db.query(SurgerySchedule)
        .filter(SurgerySchedule.request_id.in_([r.id for r in rows] or [0]))
        .all()
    }
    rooms = {r.id: r.name for r in db.query(OperatingRoom).all()}
    org_names = {o.id: o.name for o in db.query(Organization).all()}
    return [
        {
            "id": r.id,
            "surgery_name": r.surgery_name,
            "org_name": org_names.get(r.org_id, ""),
            "surgeon_name": r.surgeon_name,
            "urgency": r.urgency,
            "status": r.status,
            "planned_date": r.planned_date,
            "scheduled_date": schedules[r.id].scheduled_date if r.id in schedules else "",
            "scheduled_time": (
                f"{schedules[r.id].start_time}-{schedules[r.id].end_time}"
                if r.id in schedules
                else ""
            ),
            "room_name": rooms.get(schedules[r.id].room_id, "") if r.id in schedules else "",
        }
        for r in rows
    ]


@router.get("/me/notifications")
def portal_my_notifications(
    unread_only: bool = False,
    limit: int = 50,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    """我的消息：报告出具、手术安排、随访提醒等。

    消息按**账户**而非患者归集——代管家属收到的是"孩子的报告出了"，
    发到代管人账户上才有意义。
    """
    query = db.query(Notification).filter(Notification.resident_account_id == account.id)
    if unread_only:
        query = query.filter(Notification.read_at.is_(None))
    rows = (
        query.order_by(Notification.read_at.isnot(None), Notification.id.desc())
        .limit(min(max(limit, 1), 200))
        .all()
    )
    return [notification_out(n) for n in rows]


@router.get("/me/notifications/unread-count")
def portal_unread_count(
    account: ResidentAccount = Depends(current_resident), db: Session = Depends(get_db)
):
    count = (
        db.query(Notification)
        .filter(
            Notification.resident_account_id == account.id, Notification.read_at.is_(None)
        )
        .count()
    )
    return {"unread": count}


@router.post("/me/notifications/{notification_id}/read")
def portal_mark_read(
    notification_id: int,
    account: ResidentAccount = Depends(current_resident),
    db: Session = Depends(get_db),
):
    notification = db.get(Notification, notification_id)
    if notification is None or notification.resident_account_id != account.id:
        raise HTTPException(status_code=404, detail="消息不存在")
    if notification.read_at is None:
        notification.read_at = utcnow()
        db.commit()
    return {"id": notification.id, "read": True}


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


# 收费类别的中文名：公示页给患者看，"drug"/"bed" 这种英文枚举不能直接抛出去
CHARGE_CATEGORY_NAMES = {
    "drug": "药品", "exam": "检查检验", "treatment": "治疗处置",
    "bed": "床位", "other": "其他",
}


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


@router.get("/price-list")
def public_price_list(
    category: str | None = None, keyword: str | None = None, db: Session = Depends(get_db)
):
    """医疗服务价格公示（浙#55，无需登录）。

    公示本来就是给不特定公众看的，加登录反而背离目的。只出**启用中**的项目：
    停用项目的价格挂在公示页上，患者按它来问价却收不到这个费，等于自找纠纷。

    每项附最近一次调价的时间与生效日期——价格公示的价值有一半在"什么时候
    开始是这个价"，只贴一个数字回答不了患者最常问的那句"上个月不是这个价"。
    """
    query = db.query(ChargeItem).filter(ChargeItem.active.is_(True))
    if category:
        query = query.filter(ChargeItem.category == category)
    if keyword:
        query = query.filter(ChargeItem.name.contains(keyword))
    items = query.order_by(ChargeItem.category, ChargeItem.code).limit(500).all()

    # 最近一次调价：一次查询取回全部相关记录，按项目取最新的那条
    latest: dict[int, ChargePriceChange] = {}
    if items:
        for change in (
            db.query(ChargePriceChange)
            .filter(ChargePriceChange.item_id.in_([i.id for i in items]))
            .order_by(ChargePriceChange.id.desc())
            .all()
        ):
            latest.setdefault(change.item_id, change)

    return [
        {
            "code": i.code,
            "name": i.name,
            "category": i.category,
            "category_name": CHARGE_CATEGORY_NAMES.get(i.category, i.category),
            "price": i.price,
            "last_adjusted_at": (
                latest[i.id].created_at.strftime("%Y-%m-%d") if i.id in latest else ""
            ),
            "effective_date": latest[i.id].effective_date if i.id in latest else "",
        }
        for i in items
    ]
