"""满意度调查：代录、明细清单与按对象类型的分布统计。

自 `service_extras.py`（倾倒场）搬出（ADR-0006）。**新建模块而不是并入别处**：
满意度不隶属于任何一个既有业务域——它评价的对象横跨签约、就诊、会诊三类，
挂到其中任何一个下面都会让另外两类看起来像附属品。

路径一字未改：`POST /api/surveys`、`GET /api/surveys`、`GET /api/surveys/stats`。
鉴权沿用倾倒场原样：router 级 `get_current_user`，代录再叠 `require_roles("operator")`
（居民本人评价走 portal，不走这里）。
"""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, paginate, require_roles, row_dict
from ..models import Patient, SatisfactionSurvey
from ..numtypes import INT4_MAX, INT4_MIN

router = APIRouter(
    prefix="/api/surveys", tags=["满意度调查"], dependencies=[Depends(get_current_user)]
)


_SURVEY_TARGETS = {"contract", "encounter", "consultation"}


class SurveyCreatedOut(BaseModel):
    id: int


class SurveyStatsOut(BaseModel):
    """按评价对象类型的分布统计。

    **字段顺序照 handler 的实际出键顺序排，不是照读起来顺眼的顺序**——
    handler 先建 `{target_type, count, total, distribution, negative}`，再
    `pop("count")`/`pop("total")` 然后重新赋 `count`，于是 `count` 被挪到了
    `distribution`/`negative` 之后。序列化按模型声明顺序走，排错即改字节。

    `distribution` 的键是 "1".."5" 五档分数（字符串，JSON 对象键只能是字符串），
    恒定五个不缺档——统计要看的是分布形状，缺档补 0 才画得出直方图。
    """

    target_type: str
    distribution: dict[str, int]
    # 差评（≤2 分）单列：均分会把个别差评稀释掉，而那正是管理层要盯的
    negative: int
    count: int
    avg_score: float
    negative_rate_pct: float


class SurveyOut(BaseModel):
    id: int
    target_type: str
    target_id: int
    patient_id: int
    patient_name: str
    score: int
    comment: str
    date: str


class SurveyCreate(BaseModel):
    target_type: str = Field(max_length=16)
    target_id: int = Field(ge=INT4_MIN, le=INT4_MAX)
    patient_id: int
    score: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=512)


@router.post(
    "",
    response_model=SurveyCreatedOut,
    status_code=201,
    dependencies=[Depends(require_roles("operator"))],  # H2: 满意度代录=经办（居民本人走 portal）
)
def submit_survey(body: SurveyCreate, db: Session = Depends(get_db)):
    if body.target_type not in _SURVEY_TARGETS:
        raise HTTPException(status_code=422, detail="未知评价对象类型")
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    s = SatisfactionSurvey(**body.model_dump())
    db.add(s)
    db.commit()
    return {"id": s.id}


# 差评阈值：≤2 分算差评，是管理层真正要盯的部分（均分会把个别差评稀释掉）
NEGATIVE_SCORE = 2


@router.get("/stats", response_model=list[SurveyStatsOut])
def survey_stats(target_type: str | None = None, db: Session = Depends(get_db)):
    """满意度统计：均分 + 分值分布 + 差评数。

    只看均分会把个别差评稀释掉——4.6 分和"20 条里有 3 条 1 分"是两回事，
    所以这里一并给出分布与差评数。

    在库里按评价对象、分数 GROUP BY 取计数（P2-1670）：原先 `q.all()` 把满意度表整表载入 ORM、在内存里逐条分组——满意度页
    每次渲染都调，居民端每交一次评价就多一行（P2-810），1 万行 0.35 s、13.5 MB，5 万行 3 s、68 MB（扫描实测）。同形的随访
    中心统计已按 P2-1151 改成库内分组。分数只有 1–5 五档，计数行最多「对象数 × 5」；均分的分子是「分数 × 条数」之和，
    分布、差评数照旧由同样的计数累加，取整（P2-1001 记过的口径）与出参一字不改。
    """
    q = db.query(SatisfactionSurvey.target_type, SatisfactionSurvey.score, func.count(SatisfactionSurvey.id))
    if target_type:
        q = q.filter(SatisfactionSurvey.target_type == target_type)
    rows = (
        q.group_by(SatisfactionSurvey.target_type, SatisfactionSurvey.score)
        .order_by(SatisfactionSurvey.target_type, SatisfactionSurvey.score)
        .all()
    )
    grouped: dict[str, dict] = {}
    for kind, score, n in rows:
        entry = grouped.setdefault(
            kind,
            {"target_type": kind, "count": 0, "total": 0,
             "distribution": {str(i): 0 for i in range(1, 6)}, "negative": 0},
        )
        entry["count"] += n
        entry["total"] += score * n
        entry["distribution"][str(score)] += n
        if score <= NEGATIVE_SCORE:
            entry["negative"] += n
    result = []
    for entry in grouped.values():
        count = entry.pop("count")
        total = entry.pop("total")
        entry["count"] = count
        entry["avg_score"] = round(total / count, 2) if count else 0.0
        entry["negative_rate_pct"] = round(entry["negative"] * 100 / count, 2) if count else 0.0
        result.append(entry)
    return sorted(result, key=lambda x: x["target_type"])


@router.get("", response_model=list[SurveyOut])
def list_surveys(
    response: Response,
    target_type: str | None = None,
    max_score: int | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """满意度明细（分页）。`max_score=2` 即差评清单——带评语的那些才有改进价值。"""
    query = db.query(SatisfactionSurvey)
    if target_type:
        query = query.filter(SatisfactionSurvey.target_type == target_type)
    if max_score is not None:
        query = query.filter(SatisfactionSurvey.score <= max_score)
    rows = paginate(query.order_by(SatisfactionSurvey.id.desc()), response, offset, limit)
    # 姓名只按本页的患者号取，本页为空就不查（P2-1153）：原先为了给一页配姓名把整张患者表的（id, 姓名）读进内存。
    # 照随访中心 `followups._name_maps` 的写法；姓名不是加密列，PII 加密开态下照旧直读
    patient_ids = {s.patient_id for s in rows}
    names = (
        row_dict(db.query(Patient.id, Patient.name).filter(Patient.id.in_(patient_ids)).all())
        if patient_ids
        else {}
    )
    return [
        {
            "id": s.id,
            "target_type": s.target_type,
            "target_id": s.target_id,
            "patient_id": s.patient_id,
            "patient_name": names.get(s.patient_id, ""),
            "score": s.score,
            "comment": s.comment,
            "date": s.created_at.date().isoformat(),
        }
        for s in rows
    ]
