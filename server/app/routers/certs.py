"""法定医学证明（浙#7、㉔出生医学证明签发）：出生/死亡医学证明签发与出生缺陷儿登记。"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import ChildRecord, MedicalCert, Organization, Patient, User
from ..datetypes import DateStr

router = APIRouter(prefix="/api/certs", tags=["法定医学证明"], dependencies=[Depends(get_current_user)])

_PREFIX = {"birth": "B", "death": "D", "defect": "Q"}
_TYPE_NAMES = {"birth": "出生医学证明", "death": "死亡医学证明", "defect": "出生缺陷儿登记"}


class CertCreate(BaseModel):
    cert_type: str = Field(pattern="^(birth|death|defect)$")
    name: str = Field(min_length=1)
    gender: str = "未知"
    event_date: DateStr
    detail: str = ""
    org_id: int
    patient_id: int | None = None
    child_id: int | None = None


@router.post(
    "",
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # 证明签发/缺陷登记
)
def issue_cert(
    body: CertCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="签发机构不存在")
    if body.cert_type == "death":
        if body.patient_id is None:
            raise HTTPException(status_code=422, detail="死亡医学证明须关联患者档案")
        if db.get(Patient, body.patient_id) is None:
            raise HTTPException(status_code=404, detail="患者不存在")
        if not body.detail:
            raise HTTPException(status_code=422, detail="死亡医学证明须填写死因诊断")
    if body.cert_type == "defect" and not body.detail:
        raise HTTPException(status_code=422, detail="出生缺陷儿登记须填写缺陷诊断")
    if body.child_id is not None and db.get(ChildRecord, body.child_id) is None:
        raise HTTPException(status_code=404, detail="儿童档案不存在")
    # 证明编号：类型前缀 + 年份 + 6位顺序号
    seq = (db.query(func.count(MedicalCert.id)).filter(MedicalCert.cert_type == body.cert_type).scalar() or 0) + 1
    cert_no = f"{_PREFIX[body.cert_type]}{date.today().year}{seq:06d}"
    cert = MedicalCert(cert_no=cert_no, created_by=user.id, **body.model_dump())
    db.add(cert)
    db.commit()
    return {
        "id": cert.id,
        "cert_type": cert.cert_type,
        "cert_type_name": _TYPE_NAMES[cert.cert_type],
        "cert_no": cert.cert_no,
        "name": cert.name,
        "event_date": cert.event_date,
    }


@router.get("")
def list_certs(
    cert_type: str | None = None, patient_id: int | None = None, db: Session = Depends(get_db)
):
    query = db.query(MedicalCert)
    if cert_type:
        query = query.filter(MedicalCert.cert_type == cert_type)
    if patient_id is not None:
        query = query.filter(MedicalCert.patient_id == patient_id)
    return [
        {
            "id": c.id,
            "cert_type": c.cert_type,
            "cert_no": c.cert_no,
            "name": c.name,
            "gender": c.gender,
            "event_date": c.event_date,
            "detail": c.detail,
            "org_id": c.org_id,
        }
        for c in query.order_by(MedicalCert.id.desc()).limit(200).all()
    ]


@router.get("/stats")
def cert_stats(db: Session = Depends(get_db)):
    """签发统计：按证明类型计数（上报省平台为对接项）。"""
    rows = db.query(MedicalCert.cert_type, func.count(MedicalCert.id)).group_by(MedicalCert.cert_type).all()
    return {t: n for t, n in rows}
