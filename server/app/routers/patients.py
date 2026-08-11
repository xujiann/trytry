"""患者主索引（EMPI）：以身份证号去重，生成全县唯一电子健康卡号。"""
import secrets

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from sqlalchemy.exc import IntegrityError

from ..database import get_db
from ..deps import get_current_user, paginate, require_roles, resolve_business_date
from pydantic import BaseModel, Field

from ..models import ArchiveAuthorization, Organization, Patient, User
from ..privacy import desensitize, mask_id_card, mask_phone  # noqa: F401  公共脱敏模块（H1）
from ..schemas import PatientCreate, PatientOut

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
    expire_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


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
    auth = ArchiveAuthorization(patient_id=patient_id, created_by=user.id, **body.model_dump())
    db.add(auth)
    db.commit()
    return {"id": auth.id, "patient_id": patient_id, "scope": auth.scope, "status": auth.status}


@router.post(
    "/{patient_id}/authorizations/{auth_id}/revoke",
    dependencies=[Depends(require_roles("doctor", "operator"))],
)
def revoke_authorization(patient_id: int, auth_id: int, db: Session = Depends(get_db)):
    auth = db.get(ArchiveAuthorization, auth_id)
    if auth is None or auth.patient_id != patient_id:
        raise HTTPException(status_code=404, detail="授权记录不存在")
    auth.status = "revoked"
    db.commit()
    return {"id": auth.id, "status": "revoked"}


@router.get("/{patient_id}/authorizations")
def list_authorizations(patient_id: int, db: Session = Depends(get_db)):
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
):
    """调阅授权校验：该机构是否持有患者未过期的有效授权（跨域调阅对接的校验入口）。"""
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
