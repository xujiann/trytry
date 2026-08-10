"""⑳远程医学教育（含㉑适宜技术培训考核）：课程、学习/考核记录。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_admin
from ..models import Course, TrainingRecord, User

router = APIRouter(prefix="/api/education", tags=["远程医学教育"], dependencies=[Depends(get_current_user)])

PASS_SCORE = 60


class CourseCreate(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    course_type: str = Field(default="vod", pattern="^(live|vod)$")
    category: str = Field(default="clinical", pattern="^(clinical|tcm|public_health)$")
    speaker: str = ""


class CourseOut(CourseCreate):
    id: int

    model_config = {"from_attributes": True}


@router.post("/courses", response_model=CourseOut, status_code=201, dependencies=[Depends(require_admin)])
def create_course(body: CourseCreate, db: Session = Depends(get_db)):
    course = Course(**body.model_dump())
    db.add(course)
    db.commit()
    db.refresh(course)
    return course


@router.get("/courses", response_model=list[CourseOut])
def list_courses(category: str | None = None, db: Session = Depends(get_db)):
    query = db.query(Course)
    if category:
        query = query.filter(Course.category == category)
    return query.order_by(Course.id.desc()).limit(200).all()


class ExamSubmit(BaseModel):
    score: float = Field(ge=0, le=100)


@router.post("/courses/{course_id}/exam")
def submit_exam(course_id: int, body: ExamSubmit, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """培训考核：每人每课一条记录，重考取最高分。"""
    if db.get(Course, course_id) is None:
        raise HTTPException(status_code=404, detail="课程不存在")
    record = (
        db.query(TrainingRecord)
        .filter(TrainingRecord.course_id == course_id, TrainingRecord.user_id == user.id)
        .first()
    )
    if record is None:
        record = TrainingRecord(course_id=course_id, user_id=user.id, score=0, passed=False)
        db.add(record)
    record.score = max(record.score, body.score)
    record.passed = record.score >= PASS_SCORE
    db.commit()
    return {"course_id": course_id, "score": record.score, "passed": record.passed}


@router.get("/courses/{course_id}/stats")
def course_stats(course_id: int, db: Session = Depends(get_db)):
    if db.get(Course, course_id) is None:
        raise HTTPException(status_code=404, detail="课程不存在")
    total = db.query(func.count(TrainingRecord.id)).filter(TrainingRecord.course_id == course_id).scalar() or 0
    passed = (
        db.query(func.count(TrainingRecord.id))
        .filter(TrainingRecord.course_id == course_id, TrainingRecord.passed.is_(True))
        .scalar()
        or 0
    )
    return {
        "course_id": course_id,
        "trainees": total,
        "passed": passed,
        "pass_rate_pct": round(passed * 100.0 / total, 2) if total else 0.0,
    }


@router.get("/my-records")
def my_records(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (
        db.query(TrainingRecord, Course.title)
        .join(Course, TrainingRecord.course_id == Course.id)
        .filter(TrainingRecord.user_id == user.id)
        .order_by(TrainingRecord.id.desc())
        .all()
    )
    return [
        {"course_id": r.TrainingRecord.course_id, "title": r.title, "score": r.TrainingRecord.score, "passed": r.TrainingRecord.passed}
        for r in rows
    ]
