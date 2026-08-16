"""患者主索引（EMPI）：以身份证号去重，生成全县唯一电子健康卡号。"""
import secrets

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from sqlalchemy.exc import IntegrityError

from ..visibility import GLOBAL_ROLES, assert_patient_visible, log_patient_access
from ..database import get_db
from ..deps import get_current_user, paginate, require_roles, resolve_business_date
from pydantic import BaseModel, Field

from ..models import ArchiveAuthorization, Organization, Patient, User
from ..privacy import desensitize, mask_id_card, mask_phone  # noqa: F401  公共脱敏模块（H1）
from ..schemas import PatientCreate, PatientOut
from ..datetypes import DateStr

router = APIRouter(
    prefix="/api/patients", tags=["患者主索引"], dependencies=[Depends(get_current_user)]
)


def _find_by_id_card(db: Session, id_card: str) -> Patient | None:
    return db.query(Patient).filter(Patient.id_card == id_card).first()


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
    """患者检索（L-3 分页：offset/limit，总数见 X-Total-Count 响应头）。"""
    query = db.query(Patient)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            (Patient.name.like(like)) | (Patient.id_card.like(like)) | (Patient.ehc_no.like(like))
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


@router.post(
    "/{patient_id}/authorizations",
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
    # 自授权闭链防护：给"本机构"签发授权等于自助开权限，是越权链的入口
    # （patient_basis 的 authorization 依据随即放行）。窗口为患者办理知情同意、
    # 授权**其他**机构（如转诊目标院）不受影响；只有把 grantee 指向自己机构时，
    # 才要求调用者对该患者已有可见关系——有关系则本就无需授权，此分支专挡凭空自授。
    if user.role not in GLOBAL_ROLES and body.grantee_org_id == user.org_id:
        assert_patient_visible(db, user, patient_id, resource="authorization_grant")
    auth = ArchiveAuthorization(patient_id=patient_id, created_by=user.id, **body.model_dump())
    db.add(auth)
    db.commit()
    return {"id": auth.id, "patient_id": patient_id, "scope": auth.scope, "status": auth.status}


@router.post(
    "/{patient_id}/authorizations/{auth_id}/revoke",
    dependencies=[Depends(require_roles("doctor", "operator"))],
)
def revoke_authorization(
    patient_id: int,
    auth_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    auth = db.get(ArchiveAuthorization, auth_id)
    if auth is None or auth.patient_id != patient_id:
        raise HTTPException(status_code=404, detail="授权记录不存在")
    # 归属校验：只有全域角色、被授权机构、或签发方机构（按签发人所在机构推定）
    # 方可撤销，否则任意机构可撤销他家持有的授权（与签发端同为自授权链的一环）。
    creator_org = (
        db.query(User.org_id).filter(User.id == auth.created_by).scalar()
        if auth.created_by
        else None
    )
    if not (
        user.role in GLOBAL_ROLES
        or user.org_id == auth.grantee_org_id
        or (creator_org is not None and user.org_id == creator_org)
    ):
        raise HTTPException(status_code=403, detail="无权撤销该授权记录")
    auth.status = "revoked"
    db.commit()
    return {"id": auth.id, "status": "revoked"}


@router.get("/{patient_id}/authorizations")
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


@router.get("/{patient_id}/authorizations/check")
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
    """
    log_patient_access(db, user, patient_id, "authorization", "consent_admin")
    current = resolve_business_date(today).isoformat()
    grants = (
        db.query(ArchiveAuthorization)
        .filter(
            ArchiveAuthorization.patient_id == patient_id,
            ArchiveAuthorization.grantee_org_id == org_id,
            ArchiveAuthorization.status == "active",
            ArchiveAuthorization.expire_date >= current,
        )
        .all()
    )
    allowed = any(g.scope == "all" or g.scope == scope for g in grants)
    return {"patient_id": patient_id, "org_id": org_id, "scope": scope, "allowed": allowed}
