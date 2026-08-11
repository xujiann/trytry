"""居民端服务门户：电子健康档案向本人开放（121号文第五条）。

身份核验：电子健康卡号 + 身份证号双因子匹配，仅返回本人档案。

M-3 整改：
- 未认证核验接口接入速率限制（复用 state_store.LoginFailureTracker，
  按证件号维度计数，连续核验失败达阈值锁定10分钟，防已知证件号撞库）；
- my-archive 支持 POST body 传参（推荐），避免身份证号经 GET query
  进入代理日志/浏览器历史；GET 方式仅为兼容保留。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (
    ChronicPatient,
    Encounter,
    ExamReport,
    ExamRequest,
    HealthArticle,
    Patient,
    SatisfactionSurvey,
)
from ..state_store import LoginFailureTracker
from .chronic import guidance_for

router = APIRouter(prefix="/api/portal", tags=["居民端"])

# 双因子核验防撞库：同一证件号连续5次核验失败锁定10分钟
_verify_failures = LoginFailureTracker(fail_limit=5, lock_seconds=600)


def _reset_portal_failures() -> None:
    """测试辅助：清空核验锁定状态。"""
    _verify_failures.clear_all()


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


@router.get("/my-archive")
def my_archive(ehc_no: str, id_card: str, db: Session = Depends(get_db)):
    """兼容保留的 GET 方式（身份证号入 query，有日志泄露面），推荐使用 POST。"""
    patient = _verify_patient(db, ehc_no, id_card)
    return _build_archive(db, patient)


@router.post("/my-archive")
def my_archive_post(body: ArchiveQuery, db: Session = Depends(get_db)):
    """推荐方式：POST body 传参，避免敏感标识经 URL 泄露。"""
    patient = _verify_patient(db, body.ehc_no, body.id_card)
    return _build_archive(db, patient)


class PortalSurveyCreate(BaseModel):
    ehc_no: str
    id_card: str
    target_type: str = Field(pattern="^(contract|encounter|consultation)$")
    target_id: int = 0
    score: int = Field(ge=1, le=5)
    comment: str = ""


@router.post("/surveys", status_code=201)
def portal_submit_survey(body: PortalSurveyCreate, db: Session = Depends(get_db)):
    """居民端满意度提交：电子健康卡号+身份证号双因子核验后，以本人身份评价。"""
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
