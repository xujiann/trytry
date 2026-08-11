"""成人健康体检记录（浙#5）：体检登记、异常项标记、异常清单与患者体检史。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import Organization, Patient, PhysicalExam

router = APIRouter(prefix="/api/checkups", tags=["健康体检"], dependencies=[Depends(get_current_user)])


class CheckupCreate(BaseModel):
    patient_id: int
    org_id: int
    package_name: str = "常规体检"
    exam_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    summary: str = ""
    abnormal_items: str = ""


class CheckupOut(CheckupCreate):
    id: int
    has_abnormal: bool

    model_config = {"from_attributes": True}


@router.post(
    "",
    response_model=CheckupOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # 体检报告录入
)
def create_checkup(body: CheckupCreate, db: Session = Depends(get_db)):
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    exam = PhysicalExam(**body.model_dump(), has_abnormal=bool(body.abnormal_items.strip()))
    db.add(exam)
    db.commit()
    db.refresh(exam)
    return exam


@router.get("", response_model=list[CheckupOut])
def list_checkups(
    patient_id: int | None = None, has_abnormal: bool | None = None, db: Session = Depends(get_db)
):
    query = db.query(PhysicalExam)
    if patient_id is not None:
        query = query.filter(PhysicalExam.patient_id == patient_id)
    if has_abnormal is not None:
        query = query.filter(PhysicalExam.has_abnormal.is_(has_abnormal))
    return query.order_by(PhysicalExam.id.desc()).limit(200).all()


@router.get("/abnormal")
def abnormal_checkups(db: Session = Depends(get_db)):
    """异常项清单：供慢病筛查建档与随访干预衔接（医防协同）。"""
    return [
        {
            "id": e.id,
            "patient_id": e.patient_id,
            "exam_date": e.exam_date,
            "abnormal_items": e.abnormal_items,
        }
        for e in db.query(PhysicalExam)
        .filter(PhysicalExam.has_abnormal.is_(True))
        .order_by(PhysicalExam.id.desc())
        .limit(200)
        .all()
    ]
