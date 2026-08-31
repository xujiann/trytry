"""患者主索引（EMPI）：以身份证号去重，生成全县唯一电子健康卡号。"""
import secrets

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from sqlalchemy.exc import IntegrityError

from ..visibility import active_authorization_grants, log_patient_access
from ..database import get_db
from ..deps import get_current_user, paginate, require_roles, resolve_business_date
from pydantic import BaseModel, Field

from ..models import ArchiveAuthorization, Organization, Patient, User
from ..pii import pii_filter, pii_index_match
from ..privacy import desensitize, mask_id_card, mask_phone  # noqa: F401  公共脱敏模块（H1）
from ..schemas import PatientCreate, PatientOut
from ..datetypes import DateStr

router = APIRouter(
    prefix="/api/patients", tags=["患者主索引"], dependencies=[Depends(get_current_user)]
)


def _find_by_id_card(db: Session, id_card: str) -> Patient | None:
    return db.query(Patient).filter(pii_filter(Patient.id_card_idx, Patient.id_card, id_card)).first()


def create_patient_idempotent(db: Session, data: dict) -> tuple[Patient, bool]:
    """EMPI 幂等建档：同身份证号返回既有档案；并发建档以唯一约束兜底（M6）。

    返回 (patient, created)。
    """
    existing = _find_by_id_card(db, data["id_card"])
    if existing:
        return existing, False
    patient = Patient(ehc_no=_generate_ehc_no(db), **data)
    db.add(patient)
    try:
        db.commit()
    except IntegrityError:
        # 并发建档触发 uq_patient_id_card：回滚后重查，幂等返回既有档案
        db.rollback()
        existing = _find_by_id_card(db, data["id_card"])
        if existing is None:  # pragma: no cover - 仅约束异常非本键冲突时
            raise
        return existing, False
    db.refresh(patient)
    return patient, True


def _generate_ehc_no(db: Session) -> str:
    while True:
        candidate = "EHC" + secrets.token_hex(6).upper()
        if db.query(Patient).filter(Patient.ehc_no == candidate).first() is None:
            return candidate


@router.post(
    "",
    response_model=PatientOut,
    status_code=201,
    # L-10 整改：建档纳入角色矩阵（经办/医师/公卫），药师/管理层不建档
    dependencies=[Depends(require_roles("operator", "doctor", "public_health"))],
)
def register_patient(body: PatientCreate, db: Session = Depends(get_db)):
    # 主索引幂等：同一身份证号返回既有档案，不重复建档（并发竞态由唯一约束+重查兜底）
    patient, _created = create_patient_idempotent(db, body.model_dump())
    return patient


@router.get("", response_model=list[PatientOut])
def search_patients(
    response: Response,
    keyword: str = "",
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """患者检索（L-3 分页：offset/limit，总数见 X-Total-Count 响应头）。

    个保法注销口径（工程包 E2）：已注销档案（deactivated_at 非空）不出现在
    检索结果里——检索是新业务的入口，注销后不应再被"找到"；按 ehc_no 直取与
    既有业务历史（就诊/账单等）**照常可查**，医疗记录法定保留、不物理删除。
    """
    query = db.query(Patient).filter(Patient.deactivated_at.is_(None))
    if keyword:
        like = f"%{keyword}%"
        # PII 加密开态的降级口径（工程包 E3，文档见 app/pii.py）：证件号模糊检索
        # 对密文行不可用，追加索引列等值让**全值**证件号仍可命中；前缀/中缀不支持。
        # 关态该等值分支是 like 的子集，结果集不变。
        query = query.filter(
            (Patient.name.like(like))
            | (Patient.id_card.like(like))
            | (Patient.ehc_no.like(like))
            | pii_index_match(Patient.id_card_idx, keyword)
        )
    rows = paginate(query.order_by(Patient.id), response, offset, limit)
    return [desensitize(p, user) for p in rows]


@router.get("/{ehc_no}", response_model=PatientOut)
def get_patient(ehc_no: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    patient = db.query(Patient).filter(Patient.ehc_no == ehc_no).first()
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    return desensitize(patient, user)


# ---------- 终审轮：档案调阅授权（浙#59 患者授权模型） ----------


class AuthorizationCreate(BaseModel):
    grantee_org_id: int
    scope: str = Field(default="all", pattern="^(all|encounter|exam)$")
    expire_date: DateStr


class AuthorizationGrantedOut(BaseModel):
    """授权发放回执四键——与撤销回执（两键）不同形，两个模型，不互相注入。"""

    id: int
    patient_id: int
    scope: str
    status: str


class AuthorizationRevokedOut(BaseModel):
    id: int
    status: str


class AuthorizationOut(BaseModel):
    """授权清单行。`expire_date` 是 `String(10)` 非空列：空串=不设到期日
    （可见性侧按有效算，见 `visibility.active_authorization_grants`），恒 str 非 null。"""

    id: int
    grantee_org_id: int
    scope: str
    expire_date: str
    status: str


class AuthorizationCheckOut(BaseModel):
    """调阅授权校验：四键恒在，`allowed` 是唯一判定结论。"""

    patient_id: int
    org_id: int
    scope: str
    allowed: bool


@router.post(
    "/{patient_id}/authorizations",
    response_model=AuthorizationGrantedOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "operator"))],  # 授权代录（患者知情）
)
def grant_authorization(
    patient_id: int,
    body: AuthorizationCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if db.get(Patient, patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.grantee_org_id) is None:
        raise HTTPException(status_code=404, detail="被授权机构不存在")
    auth = ArchiveAuthorization(patient_id=patient_id, created_by=user.id, **body.model_dump())
    db.add(auth)
    db.commit()
    return {"id": auth.id, "patient_id": patient_id, "scope": auth.scope, "status": auth.status}


@router.post(
    "/{patient_id}/authorizations/{auth_id}/revoke",
    response_model=AuthorizationRevokedOut,
    dependencies=[Depends(require_roles("doctor", "operator"))],
)
def revoke_authorization(patient_id: int, auth_id: int, db: Session = Depends(get_db)):
    auth = db.get(ArchiveAuthorization, auth_id)
    if auth is None or auth.patient_id != patient_id:
        raise HTTPException(status_code=404, detail="授权记录不存在")
    auth.status = "revoked"
    db.commit()
    return {"id": auth.id, "status": "revoked"}


@router.get("/{patient_id}/authorizations", response_model=list[AuthorizationOut])
def list_authorizations(
    patient_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """该患者授权了哪些机构。

    这份清单本身就是隐私（等于"这个人在哪几家机构看过病"），但它是窗口办理
    知情同意的入口：患者本人就在柜台前，而此刻本机构往往还没有他的任何记录，
    要求先有业务关系会把这项业务办不成。故取"可问责而非可阻断"——只留痕。
    """
    log_patient_access(db, user, patient_id, "authorization", "consent_admin")
    if db.get(Patient, patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    return [
        {
            "id": a.id,
            "grantee_org_id": a.grantee_org_id,
            "scope": a.scope,
            "expire_date": a.expire_date,
            "status": a.status,
        }
        for a in db.query(ArchiveAuthorization)
        .filter(ArchiveAuthorization.patient_id == patient_id)
        .order_by(ArchiveAuthorization.id.desc())
        .all()
    ]


@router.get("/{patient_id}/authorizations/check", response_model=AuthorizationCheckOut)
def check_authorization(
    patient_id: int,
    org_id: int,
    scope: str = "all",
    today: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """调阅授权校验：该机构是否持有患者未过期的有效授权（跨域调阅对接的校验入口）。

    需先对该患者有可见性。原先 org_id 随便填、患者随便挑，等于可以枚举
    "这个人授权过哪几家机构"——那是在问他在哪看过病。

    与上面同一条口径：只留痕不阻断。签发授权的机构要能回看自己发出去的授权
    还生不生效，那是正当的，也正是这条接口本来的用途。

    **有效期判定不在这里实现**，调 `visibility.active_authorization_grants`——
    与 `assert_patient_visible` 真正把门用的是同一份。此前这里自己拼
    `status=active AND expire_date >= 今天`，与可见性那侧的
    "`expire_date` 为空 = 不设到期日、按有效算"对不上：`expire_date` 是
    `String(10) default=""` 的非空列，空串可达，于是同一条长期授权在可见性侧
    能调阅、在这条校验接口上却报 `allowed=false`。范围（scope）仍在这里判——
    那是本接口独有的问题，不是同一判定的第二份。
    """
    log_patient_access(db, user, patient_id, "authorization", "consent_admin")
    current = resolve_business_date(today).isoformat()
    grants = active_authorization_grants(db, patient_id, org_id, today=current)
    allowed = any(g.scope == "all" or g.scope == scope for g in grants)
    return {"patient_id": patient_id, "org_id": org_id, "scope": scope, "allowed": allowed}
