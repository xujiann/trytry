"""患者主索引（EMPI）：以身份证号去重，生成全县唯一电子健康卡号。"""
import secrets

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from sqlalchemy.exc import IntegrityError

from ..database import get_db
from ..deps import get_current_user
from ..models import Patient, User
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


@router.post("", response_model=PatientOut, status_code=201)
def register_patient(body: PatientCreate, db: Session = Depends(get_db)):
    # 主索引幂等：同一身份证号返回既有档案，不重复建档（并发竞态由唯一约束+重查兜底）
    patient, _created = create_patient_idempotent(db, body.model_dump())
    return patient


@router.get("", response_model=list[PatientOut])
def search_patients(
    keyword: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(Patient)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            (Patient.name.like(like)) | (Patient.id_card.like(like)) | (Patient.ehc_no.like(like))
        )
    return [desensitize(p, user) for p in query.order_by(Patient.id).limit(100).all()]


@router.get("/{ehc_no}", response_model=PatientOut)
def get_patient(ehc_no: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    patient = db.query(Patient).filter(Patient.ehc_no == ehc_no).first()
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    return desensitize(patient, user)
