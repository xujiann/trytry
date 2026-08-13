"""⑳远程医学教育（含㉑适宜技术培训考核）：课程、学习/考核记录。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..clock import now_naive
from ..concurrency import insert_if_absent, upsert_unique
from ..database import get_db
from ..deps import get_current_user, require_admin, require_roles
from ..models import Course, LiveFeedback, LiveSession, TrainingRecord, User

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
        # 取最高分是累加型语义，用不了覆盖式的 upsert_unique
        insert_if_absent(
            db, TrainingRecord(course_id=course_id, user_id=user.id, score=0, passed=False)
        )
        record = (
            db.query(TrainingRecord)
            .filter(TrainingRecord.course_id == course_id, TrainingRecord.user_id == user.id)
            .first()
        )
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


# ---------- 终审轮：直播申请/审核/结束（⑳，音视频通道为对接项） ----------


class LiveCreate(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    speaker: str = ""
    planned_at: str = ""
    course_id: int | None = None


@router.post(
    "/live-sessions",
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "operator", "public_health"))],  # 直播申请
)
def request_live(
    body: LiveCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    if body.course_id is not None and db.get(Course, body.course_id) is None:
        raise HTTPException(status_code=404, detail="关联课程不存在")
    session = LiveSession(requested_by=user.id, **body.model_dump())
    db.add(session)
    db.commit()
    return {"id": session.id, "title": session.title, "status": session.status}


@router.post(
    "/live-sessions/{session_id}/review",
    dependencies=[Depends(require_roles("director"))],  # 直播审核=管理层
)
def review_live(session_id: int, approve: bool, comment: str = "", db: Session = Depends(get_db)):
    session = db.get(LiveSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="直播申请不存在")
    if session.status != "pending":
        raise HTTPException(status_code=409, detail="该申请已审核")
    session.status = "approved" if approve else "rejected"
    session.review_comment = comment
    db.commit()
    return {"id": session.id, "status": session.status}


@router.post(
    "/live-sessions/{session_id}/finish",
    dependencies=[Depends(require_roles("director", "operator"))],
)
def finish_live(session_id: int, db: Session = Depends(get_db)):
    session = db.get(LiveSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="直播申请不存在")
    if session.status != "approved":
        raise HTTPException(status_code=409, detail="仅已排期直播可结束")
    session.status = "finished"
    db.commit()
    return {"id": session.id, "status": "finished"}


class LiveRecording(BaseModel):
    recording_url: str = Field(min_length=1, max_length=512)


@router.post(
    "/live-sessions/{session_id}/recording",
    dependencies=[Depends(require_roles("director", "operator"))],
)
def upload_recording(session_id: int, body: LiveRecording, db: Session = Depends(get_db)):
    """课程录制回放（指引⑳"课程录制"）。

    只有已结束的直播能挂回放——排期中的直播挂上回放，学员点进去是空的，
    比没有回放更糟。
    """
    session = db.get(LiveSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="直播申请不存在")
    if session.status != "finished":
        raise HTTPException(status_code=409, detail="仅已结束的直播可上传回放")
    session.recording_url = body.recording_url
    session.recorded_at = now_naive()
    db.commit()
    return {"id": session.id, "recording_url": session.recording_url}


class LiveFeedbackIn(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=512)


@router.post("/live-sessions/{session_id}/feedback", status_code=201)
def submit_live_feedback(
    session_id: int,
    body: LiveFeedbackIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """直播反馈（指引⑳"直播反馈"）。一人一场一条，重复提交按覆盖——
    改主意是正常的，累计多条会让均分被反复提交的人带偏。"""
    session = db.get(LiveSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="直播申请不存在")
    if session.status != "finished":
        raise HTTPException(status_code=409, detail="直播结束后方可反馈")
    feedback, updated = upsert_unique(
        db,
        LiveFeedback,
        keys={"session_id": session_id, "user_id": user.id},
        values=body.model_dump(),
    )
    return {"id": feedback.id, "updated": updated}


@router.get("/live-sessions/{session_id}/feedback")
def list_live_feedback(session_id: int, db: Session = Depends(get_db)):
    rows = (
        db.query(LiveFeedback)
        .filter(LiveFeedback.session_id == session_id)
        .order_by(LiveFeedback.id.desc())
        .all()
    )
    ratings = [r.rating for r in rows]
    return {
        "session_id": session_id,
        "count": len(rows),
        "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else None,
        "feedbacks": [
            {"id": r.id, "user_id": r.user_id, "rating": r.rating, "comment": r.comment}
            for r in rows
        ],
    }


@router.get("/live-sessions")
def list_live_sessions(status: str | None = None, db: Session = Depends(get_db)):
    q = db.query(LiveSession)
    if status:
        q = q.filter(LiveSession.status == status)
    return [
        {
            "id": s.id,
            "title": s.title,
            "speaker": s.speaker,
            "planned_at": s.planned_at,
            "status": s.status,
            "review_comment": s.review_comment,
            "recording_url": s.recording_url,
        }
        for s in q.order_by(LiveSession.id.desc()).limit(200).all()
    ]
