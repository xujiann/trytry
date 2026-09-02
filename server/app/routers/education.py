"""⑳远程医学教育（含㉑适宜技术培训考核）：课程、学习/考核记录。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session
from ..clock import now_naive
from ..concurrency import (
    add_amount,
    claim_quota,
    ensure_present,
    insert_if_absent,
    take_amount,
    upsert_unique,
)
from ..database import get_db
from ..deps import get_current_user, require_admin, require_roles, row_dict
from ..models import (
    Attachment,
    Course,
    CourseMaterial,
    HealthArticle,
    LiveFeedback,
    LiveSession,
    Organization,
    TcmTechnique,
    TrainingAssessment,
    TrainingEnrollment,
    TrainingPlan,
    TrainingRecord,
    User,
)
from sqlalchemy.exc import IntegrityError
from ..datetypes import DateStr
from ..visibility import assert_obj_org_writable, assert_org_writable

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


class ExamResultOut(BaseModel):
    """考核回执。score 是 Float 列（training_records.score）：整数成绩读回来
    就是 85.0，声明 `float` 才是原样——与 Money 列相反，判据是列类型
    （见 docs/接口标准与治理.md 陷阱一）；入参侧 Pydantic float 也已把
    `{"score": 85}` 转成 85.0，不存在 int 分支。"""

    course_id: int
    score: float
    passed: bool


@router.post("/courses/{course_id}/exam", response_model=ExamResultOut)
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
    record = ensure_present(record, "培训记录")
    record.score = max(record.score, body.score)
    record.passed = record.score >= PASS_SCORE
    db.commit()
    return {"course_id": course_id, "score": record.score, "passed": record.passed}


class CourseStatsOut(BaseModel):
    course_id: int
    trainees: int
    passed: int
    #: 两条分支（round 出的浮点 / 兜底字面量 0.0）都是 float，不存在 int 取值
    pass_rate_pct: float


@router.get("/courses/{course_id}/stats", response_model=CourseStatsOut)
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


class MyRecordOut(BaseModel):
    """我的培训记录行：score 同为 Float 列，声明 float 才是原样。"""

    course_id: int
    title: str
    score: float
    passed: bool


@router.get("/my-records", response_model=list[MyRecordOut])
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


class LiveRequestedOut(BaseModel):
    """申请回执（id/title/status 三键）；审核/结束只回两键，是另一个模型。"""

    id: int
    title: str
    status: str


class LiveStatusOut(BaseModel):
    """审核与结束两个端点同形（都只回 id + status），共用一个模型。
    结束那个的 status 是字面量 "finished"，不是读回来的列——照实建模。"""

    id: int
    status: str


@router.post(
    "/live-sessions",
    response_model=LiveRequestedOut,
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
    response_model=LiveStatusOut,
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
    response_model=LiveStatusOut,
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


class LiveRecordingOut(BaseModel):
    id: int
    recording_url: str


@router.post(
    "/live-sessions/{session_id}/recording",
    response_model=LiveRecordingOut,
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


class LiveFeedbackReceiptOut(BaseModel):
    """一人一场一条：updated=false 为新建、true 为覆盖，同一形状。"""

    id: int
    updated: bool


@router.post(
    "/live-sessions/{session_id}/feedback", response_model=LiveFeedbackReceiptOut, status_code=201
)
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


class LiveFeedbackItemOut(BaseModel):
    id: int
    user_id: int
    rating: int
    comment: str


class LiveFeedbackListOut(BaseModel):
    session_id: int
    count: int
    #: 键恒在、无反馈时值为 null（不是条件键）——`float | None` 即原样，
    #: 有数据分支是 `round(sum/len, 2)` 的浮点
    avg_rating: float | None
    feedbacks: list[LiveFeedbackItemOut]


@router.get("/live-sessions/{session_id}/feedback", response_model=LiveFeedbackListOut)
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


class LiveSessionRowOut(BaseModel):
    """直播列表行：申请/审核/回放各环节字段的合并视图（列全非空，空值为空串）。"""

    id: int
    title: str
    speaker: str
    planned_at: str
    status: str
    review_comment: str
    recording_url: str


@router.get("/live-sessions", response_model=list[LiveSessionRowOut])
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


# ===========================================================================
# ⑳ 课件资源管理 + ㉑ 适宜技术实训管理
# ===========================================================================


MATERIAL_TYPES = {"slide": "课件", "video": "视频", "doc": "文档", "link": "外链"}


class MaterialCreate(BaseModel):
    title: str = Field(min_length=1)
    material_type: str = Field(default="slide", pattern="^(slide|video|doc|link)$")
    url: str = ""


def _material_out(m: CourseMaterial, attachments: int = 0) -> dict:
    return {
        "id": m.id,
        "course_id": m.course_id,
        "title": m.title,
        "material_type": m.material_type,
        "material_type_name": MATERIAL_TYPES.get(m.material_type, m.material_type),
        "url": m.url,
        "play_count": m.play_count,
        "attachments": attachments,
    }


class MaterialOut(BaseModel):
    """字段与顺序精确镜像 `_material_out`（新建/点播回执与列表行同形，
    attachments 在不查附件数的端点恒为 0——照实建模，不是条件键）。"""

    id: int
    course_id: int
    title: str
    material_type: str
    material_type_name: str
    url: str
    play_count: int
    attachments: int


@router.post(
    "/courses/{course_id}/materials",
    response_model=MaterialOut,
    status_code=201,
    dependencies=[Depends(require_roles("director", "public_health", "operator", "doctor"))],
)
def create_material(
    course_id: int,
    body: MaterialCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """课程下挂课件资源（附件另经 /api/attachments 以 owner_type=course_material 上传）。"""
    if db.get(Course, course_id) is None:
        raise HTTPException(status_code=404, detail="课程不存在")
    material = CourseMaterial(course_id=course_id, created_by=user.id, **body.model_dump())
    db.add(material)
    db.commit()
    db.refresh(material)
    return _material_out(material)


@router.get("/courses/{course_id}/materials", response_model=list[MaterialOut])
def list_materials(course_id: int, db: Session = Depends(get_db)):
    if db.get(Course, course_id) is None:
        raise HTTPException(status_code=404, detail="课程不存在")
    materials = (
        db.query(CourseMaterial)
        .filter(CourseMaterial.course_id == course_id)
        .order_by(CourseMaterial.id.desc())
        .all()
    )
    counts = row_dict(
        db.query(Attachment.owner_id, func.count(Attachment.id))
        .filter(
            Attachment.owner_type == "course_material",
            Attachment.owner_id.in_([m.id for m in materials] or [0]),
        )
        .group_by(Attachment.owner_id)
        .all()
    )
    return [_material_out(m, counts.get(m.id, 0)) for m in materials]


@router.post("/materials/{material_id}/play", response_model=MaterialOut)
def play_material(material_id: int, db: Session = Depends(get_db)):
    """点播计数：每次调阅 +1（点播量用于课件资源热度统计）。"""
    material = db.get(CourseMaterial, material_id)
    if material is None:
        raise HTTPException(status_code=404, detail="课件不存在")
    add_amount(db, CourseMaterial, material.id, "play_count", 1)
    db.commit()
    db.refresh(material)
    return _material_out(material)


class MaterialStatsOut(BaseModel):
    total_materials: int
    total_plays: int
    top: list[MaterialOut]


@router.get("/material-stats", response_model=MaterialStatsOut)
def material_stats(db: Session = Depends(get_db)):
    """课件点播排行（前 20）与总点播量。"""
    materials = (
        db.query(CourseMaterial).order_by(CourseMaterial.play_count.desc()).limit(20).all()
    )
    total_plays = db.query(func.coalesce(func.sum(CourseMaterial.play_count), 0)).scalar() or 0
    return {
        "total_materials": db.query(func.count(CourseMaterial.id)).scalar() or 0,
        "total_plays": int(total_plays),
        "top": [_material_out(m) for m in materials],
    }


class PlanCreate(BaseModel):
    title: str = Field(min_length=1)
    org_id: int
    technique_id: int | None = None
    plan_date: DateStr
    capacity: int = Field(default=30, ge=1, le=1000)
    trainer: str = ""


def _plan_out(p: TrainingPlan, enrolled: int = 0) -> dict:
    return {
        "id": p.id,
        "title": p.title,
        "technique_id": p.technique_id,
        "org_id": p.org_id,
        "plan_date": p.plan_date,
        "capacity": p.capacity,
        "trainer": p.trainer,
        "status": p.status,
        "enrolled": enrolled,
        "remaining": max(p.capacity - enrolled, 0),
    }


class TrainingPlanOut(BaseModel):
    """字段与顺序精确镜像 `_plan_out`（technique_id 是可空外键，值可为 null；
    新建回执 enrolled 恒 0，列表行带实时报名数，同一形状）。"""

    id: int
    title: str
    technique_id: int | None
    org_id: int
    plan_date: str
    capacity: int
    trainer: str
    status: str
    enrolled: int
    remaining: int


@router.post(
    "/training-plans",
    response_model=TrainingPlanOut,
    status_code=201,
    dependencies=[Depends(require_roles("director", "doctor", "public_health"))],
)
def create_plan(body: PlanCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """发布适宜技术实训计划。"""
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="承办机构不存在")
    if body.technique_id is not None and db.get(TcmTechnique, body.technique_id) is None:
        raise HTTPException(status_code=404, detail="适宜技术不存在")
    plan = TrainingPlan(created_by=user.id, **body.model_dump())
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return _plan_out(plan)


@router.get("/training-plans", response_model=list[TrainingPlanOut])
def list_plans(status: str | None = None, db: Session = Depends(get_db)):
    query = db.query(TrainingPlan)
    if status:
        query = query.filter(TrainingPlan.status == status)
    plans = query.order_by(TrainingPlan.id.desc()).limit(200).all()
    return [_plan_out(p, p.enrolled_count) for p in plans]


class EnrollmentReceiptOut(BaseModel):
    """报名回执（四键）；退报名回执**没有 id**（三键），是另一个模型，不许互相注入。"""

    id: int
    plan_id: int
    user_id: int
    status: str


@router.post("/training-plans/{plan_id}/enroll", response_model=EnrollmentReceiptOut, status_code=201)
def enroll_plan(plan_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """学员报名（登录用户本人报名，名额满则 409）。"""
    plan = db.get(TrainingPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="实训计划不存在")
    assert_obj_org_writable(db, user, plan)
    if plan.status != "open":
        raise HTTPException(status_code=409, detail=f"计划当前状态 {plan.status} 不接受报名")
    existing = (
        db.query(TrainingEnrollment)
        .filter(TrainingEnrollment.plan_id == plan_id, TrainingEnrollment.user_id == user.id)
        .first()
    )
    if existing and existing.status == "enrolled":
        raise HTTPException(status_code=409, detail="已报名该实训计划")
    # 原子占额：判满与占位同一条 SQL。原先 COUNT>=capacity 再插是 check-then-act，
    # 并发下多人同数到"还差一个"一起挤进来（实测容量 2 报上 3 人）。
    # 占额与写报名行同一个事务提交——commit 失败则一起回滚，名额不泄。
    if not claim_quota(db, TrainingPlan, plan_id, "enrolled_count", "capacity"):
        db.rollback()
        raise HTTPException(status_code=409, detail="实训名额已满")
    if existing:
        existing.status = "enrolled"
        enrollment = existing
    else:
        enrollment = TrainingEnrollment(plan_id=plan_id, user_id=user.id)
        db.add(enrollment)
    try:
        db.commit()
    except IntegrityError:
        # 同一个人重复点报名，两个请求都查不到既有记录就都去插；后插的撞唯一
        # 约束回滚——占额也在同一事务里，一并回退，不会白占一个名额。
        db.rollback()
        raise HTTPException(status_code=409, detail="已报名该实训计划") from None
    db.refresh(enrollment)
    return {
        "id": enrollment.id,
        "plan_id": plan_id,
        "user_id": user.id,
        "status": enrollment.status,
    }


class CancelEnrollOut(BaseModel):
    """退报名回执：status 是字面量 "cancelled"，照实建模。"""

    plan_id: int
    user_id: int
    status: str


@router.post("/training-plans/{plan_id}/cancel-enroll", response_model=CancelEnrollOut)
def cancel_enroll(plan_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    enrollment = (
        db.query(TrainingEnrollment)
        .filter(TrainingEnrollment.plan_id == plan_id, TrainingEnrollment.user_id == user.id)
        .first()
    )
    if enrollment is None or enrollment.status != "enrolled":
        raise HTTPException(status_code=404, detail="未报名该实训计划")
    enrollment.status = "cancelled"
    # 退报名释放一个名额，与占额对称；take_amount 的 WHERE 挡住减成负数
    take_amount(db, TrainingPlan, plan_id, "enrolled_count", 1)
    db.commit()
    return {"plan_id": plan_id, "user_id": user.id, "status": "cancelled"}


class EnrollmentRowOut(BaseModel):
    """报名名册行（带学员账号与姓名，full_name 列非空默认空串）。"""

    id: int
    user_id: int
    username: str
    full_name: str
    status: str


@router.get("/training-plans/{plan_id}/enrollments", response_model=list[EnrollmentRowOut])
def list_enrollments(plan_id: int, db: Session = Depends(get_db)):
    if db.get(TrainingPlan, plan_id) is None:
        raise HTTPException(status_code=404, detail="实训计划不存在")
    rows = (
        db.query(TrainingEnrollment, User.username, User.full_name)
        .join(User, TrainingEnrollment.user_id == User.id)
        .filter(TrainingEnrollment.plan_id == plan_id)
        .order_by(TrainingEnrollment.id)
        .all()
    )
    return [
        {
            "id": e.id,
            "user_id": e.user_id,
            "username": username,
            "full_name": full_name,
            "status": e.status,
        }
        for e, username, full_name in rows
    ]


class AssessmentCreate(BaseModel):
    user_id: int
    score: float = Field(ge=0, le=100)
    comment: str = ""


class AssessmentReceiptOut(BaseModel):
    """考核录入回执。score 是 Float 列（training_assessments.score），
    声明 float 才是原样（同 ExamResultOut 的判断）。"""

    id: int
    plan_id: int
    user_id: int
    score: float
    passed: bool
    assessor: str


@router.post(
    "/training-plans/{plan_id}/assessments",
    response_model=AssessmentReceiptOut,
    status_code=201,
    dependencies=[Depends(require_roles("director", "doctor"))],
)
def create_assessment(
    plan_id: int,
    body: AssessmentCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """实训考核录入：须已报名，60 分及格，同一计划同一学员唯一（重录更新成绩）。"""
    plan = db.get(TrainingPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="实训计划不存在")
    # 考核由办实训的机构录入（plan.org_id 是主办方）
    assert_obj_org_writable(db, user, plan)
    enrolled = (
        db.query(TrainingEnrollment)
        .filter(
            TrainingEnrollment.plan_id == plan_id,
            TrainingEnrollment.user_id == body.user_id,
            TrainingEnrollment.status == "enrolled",
        )
        .first()
    )
    if enrolled is None:
        raise HTTPException(status_code=409, detail="该学员未报名本次实训，不可录入考核")
    record, _ = upsert_unique(
        db,
        TrainingAssessment,
        keys={"plan_id": plan_id, "user_id": body.user_id},
        values={
            "score": body.score,
            "passed": body.score >= 60,
            "comment": body.comment,
            "assessor": user.full_name or user.username,
        },
    )
    return {
        "id": record.id,
        "plan_id": plan_id,
        "user_id": record.user_id,
        "score": record.score,
        "passed": record.passed,
        "assessor": record.assessor,
    }


class AssessmentItemOut(BaseModel):
    id: int
    user_id: int
    score: float
    passed: bool
    comment: str
    assessor: str


class AssessmentBoardOut(BaseModel):
    total: int
    passed: int
    #: 两条分支（round 出的浮点 / 兜底字面量 0.0）都是 float
    pass_rate_pct: float
    items: list[AssessmentItemOut]


@router.get("/training-plans/{plan_id}/assessments", response_model=AssessmentBoardOut)
def list_assessments(plan_id: int, db: Session = Depends(get_db)):
    rows = (
        db.query(TrainingAssessment)
        .filter(TrainingAssessment.plan_id == plan_id)
        .order_by(TrainingAssessment.score.desc())
        .all()
    )
    passed = sum(1 for r in rows if r.passed)
    return {
        "total": len(rows),
        "passed": passed,
        "pass_rate_pct": round(passed * 100.0 / len(rows), 2) if rows else 0.0,
        "items": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "score": r.score,
                "passed": r.passed,
                "comment": r.comment,
                "assessor": r.assessor,
            }
            for r in rows
        ],
    }

# ---------------------------------------------------------------- ADR-0006 搬家
#
# 以下自 `service_extras.py`（倾倒场）搬入：健康宣教文章。
# 路径一字未改（`/api/education...` 原样），两边 router 的鉴权本就一致
# （都是 `dependencies=[Depends(get_current_user)]`），故可直接并入本模块的
# router——不像 ADR-0006 第一批的 `/api/performance` 那样存在鉴权分裂。


class HealthArticleOut(BaseModel):
    """建稿与发布两个端点同形（都只回 id + status），共用一个模型。
    发布那个的 status 是字面量 "published"，不是读回来的列——照实建模。"""

    id: int
    status: str


# ---- ⑨⑩ 健康宣教 ----


class ArticleCreate(BaseModel):
    title: str = Field(min_length=1)
    category: str = "general"
    content: str = ""


@router.post(
    "/articles",
    response_model=HealthArticleOut,
    status_code=201,
    dependencies=[Depends(require_roles("public_health", "operator"))],  # H2: 宣教编制
)
def create_article(body: ArticleCreate, db: Session = Depends(get_db)):
    a = HealthArticle(**body.model_dump())
    db.add(a)
    db.commit()
    return {"id": a.id, "status": a.status}


@router.post(
    "/articles/{article_id}/publish",
    response_model=HealthArticleOut,
    dependencies=[Depends(require_roles("public_health", "operator"))],  # H2: 宣教发布
)
def publish_article(article_id: int, db: Session = Depends(get_db)):
    a = db.get(HealthArticle, article_id)
    if a is None:
        raise HTTPException(status_code=404, detail="文章不存在")
    a.status = "published"
    db.commit()
    return {"id": a.id, "status": "published"}