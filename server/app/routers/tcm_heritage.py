"""⑬名老中医经验数字化传承 + ㉑模拟诊疗。

两块都属"把人脑子里的东西留下来"，故合一个模块：前者留的是老中医的辨证思路，
后者留的是把思路练成能力的情境。

三条口径：

1. **医案分维度存，不压成一段文本**。四诊、辨证、治法、处方、按语各一列，
   否则此后再也检索不出"某某老师治痹证常用哪几味药"。
2. **平台不判医案疗效**。医案的价值在于传承思路，"这个方子有效"不是平台
   该下的结论。
3. **模拟诊疗取最高分**，作答全部留痕——"第几次才做对"本身就是教学反馈。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..datetypes import OptionalDateStr
from ..deps import get_current_user, require_roles
from ..models import SimulationAttempt, SimulationCase, TcmMasterCase, User

router = APIRouter(
    prefix="/api/tcm-heritage",
    tags=["名老中医传承与模拟诊疗"],
    dependencies=[Depends(get_current_user)],
)


# ============================================================ 名老中医医案


class MasterCaseIn(BaseModel):
    master_name: str = Field(min_length=1, max_length=64)
    successor_name: str = Field(default="", max_length=64)
    title: str = Field(min_length=1, max_length=256)
    disease: str = Field(default="", max_length=128)
    syndrome: str = Field(default="", max_length=128)
    four_exams: str = Field(default="", max_length=2048)
    treatment_method: str = Field(default="", max_length=512)
    prescription: str = Field(default="", max_length=1024)
    commentary: str = Field(default="", max_length=2048)
    visit_date: OptionalDateStr = ""


def _case_out(c: TcmMasterCase) -> dict:
    return {
        "id": c.id,
        "master_name": c.master_name,
        "successor_name": c.successor_name,
        "title": c.title,
        "disease": c.disease,
        "syndrome": c.syndrome,
        "four_exams": c.four_exams,
        "treatment_method": c.treatment_method,
        "prescription": c.prescription,
        "commentary": c.commentary,
        "visit_date": c.visit_date,
        "published": c.published,
    }


# ---------------------------------------------------------------- 响应契约
#
# 模型集中放在所有端点之前（`response_model=` 是装饰器参数，导入时求值）。


class MasterCaseOut(BaseModel):
    id: int
    master_name: str
    successor_name: str
    title: str
    disease: str
    syndrome: str
    four_exams: str
    treatment_method: str
    prescription: str
    commentary: str
    visit_date: str
    published: bool


class MasterCasePublishedOut(BaseModel):
    id: int
    published: bool


class MasterStatOut(BaseModel):
    master_name: str
    total: int
    published: int
    # 两个集合在响应里已排好序（set 的序不稳定，直出会让同一份数据每次不同）
    diseases: list[str]
    successors: list[str]


class MasterCaseStatsOut(BaseModel):
    total: int
    masters: list[MasterStatOut]


class DecisionPointOut(BaseModel):
    """情景模拟的决策点。`answer` 与 `explain` 是**条件键**——只有出题方
    （创建时的回执）看得到，学员拉题目时整个键不出现。声明成带默认值的可选
    字段会给学员那份也注入 `"answer": null`，等于把答案的存在公告出去；
    故端点带 `response_model_exclude_unset=True`。
    """

    key: str
    question: str
    options: list[str]
    score: int
    answer: str | None = None
    explain: str | None = None


class SimulationCaseOut(BaseModel):
    id: int
    title: str
    category: str
    scenario: str
    decision_points: list[DecisionPointOut]
    total_score: int
    pass_score: int
    active: bool


class AttemptDetailOut(BaseModel):
    key: str
    question: str
    # 未作答折成"未作答"，不是空串——空串会被读成"答了个空"
    chosen: str
    correct: bool
    answer: str
    # 答错才给解析：答对的人不需要，堆一屏解析反而没人看
    explain: str


class SimulationResultOut(BaseModel):
    attempt_id: int
    score: int
    pass_score: int
    passed: bool
    detail: list[AttemptDetailOut]


class AttemptOut(BaseModel):
    id: int
    user_id: int
    score: int
    passed: bool
    # 第几次作答：重复作答次数本身是教学反馈，不该被"只留最高分"抹掉
    attempt_no: int


class BestScoreOut(BaseModel):
    user_id: int
    best_score: int


class AttemptListOut(BaseModel):
    attempts: list[AttemptOut]
    best_by_user: list[BestScoreOut]
    caliber: str


@router.post(
    "/master-cases", response_model=MasterCaseOut, status_code=201,
    dependencies=[Depends(require_roles("doctor", "operator"))]
)
def create_master_case(
    body: MasterCaseIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    case = TcmMasterCase(created_by=user.id, **body.model_dump())
    db.add(case)
    db.commit()
    db.refresh(case)
    return _case_out(case)


@router.get("/master-cases", response_model=list[MasterCaseOut])
def list_master_cases(
    master_name: str | None = None,
    disease: str | None = None,
    syndrome: str | None = None,
    keyword: str | None = None,
    include_draft: bool = False,
    db: Session = Depends(get_db),
):
    """按老师、病、证、方药检索。

    `keyword` 同时搜处方与按语——学的时候常见的问法是"这味药老师什么时候用"，
    而它既可能出现在处方里，也可能只在按语里被点评。
    """
    query = db.query(TcmMasterCase)
    if not include_draft:
        query = query.filter(TcmMasterCase.published.is_(True))
    if master_name:
        query = query.filter(TcmMasterCase.master_name.like(f"%{master_name}%"))
    if disease:
        query = query.filter(TcmMasterCase.disease.like(f"%{disease}%"))
    if syndrome:
        query = query.filter(TcmMasterCase.syndrome.like(f"%{syndrome}%"))
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            (TcmMasterCase.prescription.like(like))
            | (TcmMasterCase.commentary.like(like))
            | (TcmMasterCase.title.like(like))
        )
    return [_case_out(c) for c in query.order_by(TcmMasterCase.id.desc()).limit(200).all()]


@router.post(
    "/master-cases/{case_id}/publish", response_model=MasterCasePublishedOut,
    dependencies=[Depends(require_roles("director", "doctor"))]
)
def publish_master_case(case_id: int, db: Session = Depends(get_db)):
    """发布。医案要经传承人或科室复核后才对外——未复核的医案传出去，
    错的不是平台而是那位老师的名声。"""
    case = db.get(TcmMasterCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="医案不存在")
    case.published = True
    db.commit()
    return {"id": case_id, "published": True}


@router.post(
    "/master-cases/{case_id}/unpublish",
    response_model=MasterCasePublishedOut,
    dependencies=[Depends(require_roles("director", "doctor"))],
)
def unpublish_master_case(case_id: int, db: Session = Depends(get_db)):
    case = db.get(TcmMasterCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="医案不存在")
    case.published = False
    db.commit()
    return {"id": case_id, "published": False}


@router.get("/master-cases/stats", response_model=MasterCaseStatsOut)
def master_case_stats(db: Session = Depends(get_db)):
    """传承概览：每位老师留下多少医案、覆盖多少病证。"""
    rows = db.query(TcmMasterCase).all()
    by_master: dict[str, dict] = {}
    for c in rows:
        entry = by_master.setdefault(
            c.master_name,
            {"master_name": c.master_name, "total": 0, "published": 0,
             "diseases": set(), "successors": set()},
        )
        entry["total"] += 1
        if c.published:
            entry["published"] += 1
        if c.disease:
            entry["diseases"].add(c.disease)
        if c.successor_name:
            entry["successors"].add(c.successor_name)
    return {
        "total": len(rows),
        "masters": sorted(
            (
                {**e, "diseases": sorted(e["diseases"]), "successors": sorted(e["successors"])}
                for e in by_master.values()
            ),
            key=lambda x: -x["total"],
        ),
    }


# ============================================================ 模拟诊疗


class DecisionPoint(BaseModel):
    key: str = Field(min_length=1, max_length=32)
    question: str = Field(min_length=1, max_length=512)
    options: list[str] = Field(min_length=2)
    answer: str = Field(min_length=1, max_length=256)
    score: int = Field(default=10, ge=1, le=100)
    explain: str = Field(default="", max_length=1024)


class SimulationIn(BaseModel):
    title: str = Field(min_length=1, max_length=256)
    category: str = Field(default="clinical", pattern="^(tcm|clinical|emergency)$")
    scenario: str = Field(default="", max_length=4096)
    decision_points: list[DecisionPoint] = Field(min_length=1)
    pass_score: int = Field(default=60, ge=1, le=100)


class SimulationSubmit(BaseModel):
    # {"决策点 key": "选项"}
    answers: dict[str, str]


def _sim_out(case: SimulationCase, with_answers: bool = False) -> dict:
    points = []
    for p in case.decision_points or []:
        item = {"key": p["key"], "question": p["question"], "options": p["options"],
                "score": p.get("score", 10)}
        if with_answers:
            item["answer"] = p.get("answer", "")
            item["explain"] = p.get("explain", "")
        points.append(item)
    return {
        "id": case.id,
        "title": case.title,
        "category": case.category,
        "scenario": case.scenario,
        "decision_points": points,
        "total_score": sum(p.get("score", 10) for p in (case.decision_points or [])),
        "pass_score": case.pass_score,
        "active": case.active,
    }


@router.post(
    "/simulations", response_model=SimulationCaseOut, response_model_exclude_unset=True,
    status_code=201, dependencies=[Depends(require_roles("director", "doctor"))]
)
def create_simulation(
    body: SimulationIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    keys = [p.key for p in body.decision_points]
    if len(set(keys)) != len(keys):
        raise HTTPException(status_code=422, detail="决策点 key 不可重复")
    for point in body.decision_points:
        if point.answer not in point.options:
            raise HTTPException(
                status_code=422, detail=f"决策点 {point.key} 的正确答案不在选项里"
            )
    case = SimulationCase(
        created_by=user.id,
        title=body.title,
        category=body.category,
        scenario=body.scenario,
        decision_points=[p.model_dump() for p in body.decision_points],
        pass_score=body.pass_score,
    )
    db.add(case)
    db.commit()
    db.refresh(case)
    return _sim_out(case, with_answers=True)


@router.get("/simulations", response_model=list[SimulationCaseOut],
            response_model_exclude_unset=True)
def list_simulations(category: str | None = None, db: Session = Depends(get_db)):
    """列表**不带答案**——带上答案，练习就没有意义了。"""
    query = db.query(SimulationCase).filter(SimulationCase.active.is_(True))
    if category:
        query = query.filter(SimulationCase.category == category)
    return [_sim_out(c) for c in query.order_by(SimulationCase.id.desc()).limit(200).all()]


@router.post("/simulations/{case_id}/attempts", response_model=SimulationResultOut,
             status_code=201)
def submit_simulation(
    case_id: int,
    body: SimulationSubmit,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """作答并即时评分。答错的给出解析——不给解析的练习只是筛人，不是教学。"""
    case = db.get(SimulationCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="模拟病例不存在")
    if not case.active:
        raise HTTPException(status_code=409, detail="该模拟病例已停用")
    points = case.decision_points or []
    total = sum(p.get("score", 10) for p in points)
    earned = 0
    detail = []
    for p in points:
        chosen = body.answers.get(p["key"], "")
        correct = chosen == p.get("answer")
        if correct:
            earned += p.get("score", 10)
        detail.append({
            "key": p["key"],
            "question": p["question"],
            "chosen": chosen or "未作答",
            "correct": correct,
            "answer": p.get("answer", ""),
            # 答错才给解析：答对的人不需要，堆一屏解析反而没人看
            "explain": "" if correct else p.get("explain", ""),
        })
    score = round(earned * 100 / total) if total else 0
    attempt = SimulationAttempt(
        case_id=case_id, user_id=user.id, answers=body.answers,
        score=score, passed=score >= case.pass_score,
    )
    db.add(attempt)
    db.commit()
    db.refresh(attempt)
    return {
        "attempt_id": attempt.id,
        "score": score,
        "pass_score": case.pass_score,
        "passed": attempt.passed,
        "detail": detail,
    }


@router.get("/simulations/{case_id}/attempts", response_model=AttemptListOut)
def list_attempts(case_id: int, user_id: int | None = None, db: Session = Depends(get_db)):
    """作答记录。取最高分参与考核，但全部留痕——"第几次才做对"本身
    就是教学反馈（与培训考核"重考取高分"一致）。"""
    query = db.query(SimulationAttempt).filter(SimulationAttempt.case_id == case_id)
    if user_id is not None:
        query = query.filter(SimulationAttempt.user_id == user_id)
    rows = query.order_by(SimulationAttempt.id.desc()).limit(500).all()
    best: dict[int, int] = {}
    for r in rows:
        best[r.user_id] = max(best.get(r.user_id, 0), r.score)
    return {
        "attempts": [
            {"id": r.id, "user_id": r.user_id, "score": r.score, "passed": r.passed,
             "attempt_no": len([x for x in rows if x.user_id == r.user_id and x.id <= r.id])}
            for r in rows
        ],
        "best_by_user": [{"user_id": uid, "best_score": s} for uid, s in best.items()],
        "caliber": "考核取每人最高分；全部作答留痕，重复作答次数本身是教学反馈",
    }
