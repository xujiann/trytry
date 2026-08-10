"""共享诊断中心（影像/心电/检验/病理）：基层检查、上级诊断、结果互认、危急值管理。"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import ExamReport, ExamRequest, Organization, Patient, User
from ..schemas import ExamReportCreate, ExamReportOut, ExamRequestCreate, ExamRequestOut

router = APIRouter(prefix="/api/exams", tags=["共享诊断中心"], dependencies=[Depends(get_current_user)])

# 同一患者同一项目在此天数内已有报告的，提示可互认
RECOGNITION_WINDOW_DAYS = 30


def _find_recognizable(db: Session, patient_id: int, item_code: str) -> ExamRequest | None:
    since = datetime.now(timezone.utc) - timedelta(days=RECOGNITION_WINDOW_DAYS)
    return (
        db.query(ExamRequest)
        .join(ExamReport, ExamReport.request_id == ExamRequest.id)
        .filter(
            ExamRequest.patient_id == patient_id,
            ExamRequest.item_code == item_code,
            ExamRequest.status == "reported",
            ExamReport.reported_at >= since.replace(tzinfo=None),
        )
        .order_by(ExamRequest.id.desc())
        .first()
    )


@router.get("/recognition-check")
def recognition_check(patient_id: int, item_code: str, db: Session = Depends(get_db)):
    """开单前互认检查：近期已有同项目报告则返回可互认的申请单。"""
    existing = _find_recognizable(db, patient_id, item_code)
    if existing is None:
        return {"recognizable": False}
    return {
        "recognizable": True,
        "request_id": existing.id,
        "item_name": existing.item_name,
        "conclusion": existing.report.conclusion if existing.report else "",
    }


@router.post("", response_model=ExamRequestOut, status_code=201)
def create_request(
    body: ExamRequestCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.from_org_id) is None:
        raise HTTPException(status_code=404, detail="申请机构不存在")

    data = body.model_dump(exclude={"accept_recognition_of"})
    request = ExamRequest(**data, created_by=user.id)

    if body.accept_recognition_of is not None:
        source = db.get(ExamRequest, body.accept_recognition_of)
        if source is None or source.status != "reported":
            raise HTTPException(status_code=422, detail="被互认的申请单不存在或尚无报告")
        if source.patient_id != body.patient_id or source.item_code != body.item_code:
            raise HTTPException(status_code=422, detail="互认必须是同一患者的同一检查项目")
        request.status = "recognized"
        request.recognized_from_id = source.id
    db.add(request)
    db.commit()
    db.refresh(request)
    return request


@router.get("", response_model=list[ExamRequestOut])
def list_requests(
    center_type: str | None = None, status: str | None = None, db: Session = Depends(get_db)
):
    query = db.query(ExamRequest)
    if center_type:
        query = query.filter(ExamRequest.center_type == center_type)
    if status:
        query = query.filter(ExamRequest.status == status)
    return query.order_by(ExamRequest.id.desc()).limit(200).all()


@router.post(
    "/{request_id}/claim",
    response_model=ExamRequestOut,
    dependencies=[Depends(require_roles("doctor"))],
)
def claim_request(request_id: int, db: Session = Depends(get_db)):
    request = db.get(ExamRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="申请单不存在")
    if request.status != "pending":
        raise HTTPException(status_code=409, detail=f"当前状态 {request.status} 不可领取")
    request.status = "diagnosing"
    db.commit()
    db.refresh(request)
    return request


@router.post(
    "/{request_id}/report",
    response_model=ExamReportOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor"))],
)
def submit_report(request_id: int, body: ExamReportCreate, db: Session = Depends(get_db)):
    request = db.get(ExamRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="申请单不存在")
    if request.status not in ("pending", "diagnosing"):
        raise HTTPException(status_code=409, detail=f"当前状态 {request.status} 不可出报告")
    report = ExamReport(request_id=request_id, **body.model_dump())
    request.status = "reported"
    db.add(report)
    db.commit()
    db.refresh(report)
    return report


@router.get("/critical", response_model=list[ExamReportOut])
def list_critical_reports(db: Session = Depends(get_db)):
    """危急值清单：需立即通知申请机构处置。"""
    return (
        db.query(ExamReport)
        .filter(ExamReport.critical.is_(True))
        .order_by(ExamReport.id.desc())
        .limit(100)
        .all()
    )
