"""功能指引查漏补缺：报告模板/报告修改、消毒申领、会诊专家、预约黑名单、健康宣教、满意度、智能导诊。"""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..concurrency import insert_or_conflict
from ..visibility import assert_obj_org_writable, assert_org_writable, scope_org_list
from ..database import get_db
from ..deps import get_current_user, paginate, require_admin, require_roles
from ..models import (
    ExamResource,
    ServiceBlacklist,
    ConsultExpert,
    CriticalAction,
    CssdRequest,
    ExamReport,
    HealthArticle,
    Organization,
    Patient,
    ReportRevision,
    ReportTemplate,
    SatisfactionSurvey,
    SterilizationBatch,
    User,
)

router = APIRouter(prefix="/api", tags=["指引补遗"], dependencies=[Depends(get_current_user)])

_CENTERS = {"imaging", "ecg", "lab", "pathology"}


# ---- ①-④ 报告模板管理 / 诊断报告修改 ----


class TemplateCreate(BaseModel):
    center_type: str
    name: str = Field(min_length=1)
    content: str = ""


@router.post("/exams/templates", status_code=201, dependencies=[Depends(require_admin)])
def create_template(body: TemplateCreate, db: Session = Depends(get_db)):
    if body.center_type not in _CENTERS:
        raise HTTPException(status_code=422, detail="未知中心类型")
    t = ReportTemplate(**body.model_dump())
    db.add(t)
    db.commit()
    return {"id": t.id}


@router.get("/exams/templates")
def list_templates(center_type: str | None = None, db: Session = Depends(get_db)):
    q = db.query(ReportTemplate)
    if center_type:
        q = q.filter(ReportTemplate.center_type == center_type)
    return [{"id": t.id, "center_type": t.center_type, "name": t.name, "content": t.content} for t in q.all()]


class ReportAmend(BaseModel):
    conclusion: str = Field(min_length=1)
    finding: str | None = None
    # 允许修订危急值标记：置 True/False 均联动闭环状态
    critical: bool | None = None
    reason: str = Field(default="", max_length=512)


# 守卫写在 dependencies=[] 而不是函数参数里：写成参数时它与请求体一起解析，
# 校验错误会先于鉴权返回——非授权角色会拿到一份 422，里面列着这个接口要哪些字段。
# 既是信息泄露，也让"越权一律 403"这条口径不成立。
@router.patch("/exams/reports/{report_id}", dependencies=[Depends(require_roles("doctor"))])
def amend_report(
    report_id: int,
    body: ReportAmend,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """诊断报告修改（限医师）。

    M-6 整改：
    - 修订前值写入 ReportRevision 历史表（前结论/前所见/前危急标记/修订人），可追溯；
    - 危急值联动：修订后仍为危急值 → 闭环状态复位为"已通知"须重新确认；
      解除危急标记 → 闭环状态清空；两种变化均写入 CriticalAction 留痕。
    """
    report = db.get(ExamReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    actor = user.full_name or user.username
    was_critical = report.critical
    db.add(
        ReportRevision(
            report_id=report.id,
            prev_conclusion=report.conclusion,
            prev_finding=report.finding,
            prev_critical=report.critical,
            revised_by=actor,
            reason=body.reason,
        )
    )
    report.conclusion = body.conclusion
    if body.finding is not None:
        report.finding = body.finding
    if body.critical is not None:
        report.critical = body.critical
    if report.critical:
        # 修订后（仍/新）为危急值：闭环状态复位，须重新确认接收
        report.critical_status = "notified"
        db.add(
            CriticalAction(
                report_id=report.id,
                action=f"报告修订，危急值闭环状态复位为已通知：{report.conclusion}",
                actor=actor,
            )
        )
    elif was_critical:
        # 修订解除危急标记：闭环状态清空并留痕
        report.critical_status = ""
        db.add(
            CriticalAction(
                report_id=report.id, action="报告修订解除危急值标记", actor=actor
            )
        )
    db.commit()
    return {
        "id": report.id,
        "conclusion": report.conclusion,
        "critical": report.critical,
        "critical_status": report.critical_status,
    }


@router.get("/exams/reports/{report_id}/revisions")
def list_report_revisions(report_id: int, db: Session = Depends(get_db)):
    """报告修订历史（前值留痕轨迹）。"""
    if db.get(ExamReport, report_id) is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    rows = (
        db.query(ReportRevision)
        .filter(ReportRevision.report_id == report_id)
        .order_by(ReportRevision.id)
        .all()
    )
    return [
        {
            "id": r.id,
            "prev_conclusion": r.prev_conclusion,
            "prev_finding": r.prev_finding,
            "prev_critical": r.prev_critical,
            "revised_by": r.revised_by,
            "reason": r.reason,
            "at": r.created_at.isoformat(),
        }
        for r in rows
    ]


# ---- ⑥ 消毒供应物品申领 ----


class CssdReqCreate(BaseModel):
    org_id: int
    item_name: str = Field(min_length=1)
    quantity: int = Field(default=1, ge=1)


@router.post(
    "/cssd/requests",
    status_code=201,
    dependencies=[Depends(require_roles("operator"))],  # H2: 物品申领=经办
)
def create_cssd_request(body: CssdReqCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="申领机构不存在")
    r = CssdRequest(**body.model_dump())
    db.add(r)
    db.commit()
    return {"id": r.id, "status": r.status}


@router.get("/cssd/requests")
def list_cssd_requests(status: str | None = None, db: Session = Depends(get_db)):
    q = db.query(CssdRequest)
    if status:
        q = q.filter(CssdRequest.status == status)
    return [
        {"id": r.id, "org_id": r.org_id, "item_name": r.item_name, "quantity": r.quantity, "status": r.status, "batch_id": r.batch_id}
        for r in q.order_by(CssdRequest.id.desc()).limit(200).all()
    ]


@router.post(
    "/cssd/requests/{request_id}/fulfill",
    dependencies=[Depends(require_roles("operator"))],  # H2: 申领响应=经办
)
def fulfill_cssd_request(request_id: int, batch_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """中心以已灭菌批次响应申领。"""
    r = db.get(CssdRequest, request_id)
    if r is None:
        raise HTTPException(status_code=404, detail="申领不存在")
    assert_obj_org_writable(db, user, r)
    if r.status != "requested":
        raise HTTPException(status_code=409, detail="申领已处理")
    batch = db.get(SterilizationBatch, batch_id)
    if batch is None or batch.status not in ("sterile", "dispatched"):
        raise HTTPException(status_code=409, detail="批次不存在或未完成灭菌")
    r.status = "fulfilled"
    r.batch_id = batch_id
    db.commit()
    return {"id": r.id, "status": "fulfilled", "batch_id": batch_id}


# ---- ⑤ 会诊专家管理 ----


class ExpertCreate(BaseModel):
    name: str = Field(min_length=1)
    org_id: int
    specialty: str = ""
    available: bool = True


@router.post("/consultations/experts", status_code=201, dependencies=[Depends(require_admin)])
def create_expert(body: ExpertCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    if db.query(ConsultExpert).filter(ConsultExpert.name == body.name).first():
        raise HTTPException(status_code=409, detail="专家已存在")
    e = insert_or_conflict(db, ConsultExpert(**body.model_dump()), "专家已存在")
    return {"id": e.id}


@router.get("/consultations/experts")
def list_experts(available: bool | None = None, db: Session = Depends(get_db)):
    q = db.query(ConsultExpert)
    if available is not None:
        q = q.filter(ConsultExpert.available.is_(available))
    return [{"id": e.id, "name": e.name, "org_id": e.org_id, "specialty": e.specialty, "available": e.available} for e in q.all()]


# ---- ⑫ 预约黑名单 ----


class BlacklistCreate(BaseModel):
    patient_id: int
    reason: str = ""
    domain: str = Field(default="appointment", pattern="^(appointment|shortage)$")


BLACKLIST_DOMAINS = {"appointment": "预约爽约", "shortage": "缺药登记后不取药"}


@router.post("/appointments/blacklist", status_code=201, dependencies=[Depends(require_admin)])
def add_blacklist(body: BlacklistCreate, db: Session = Depends(get_db)):
    """加入服务黑名单。

    路径保留 `/appointments/blacklist` 不改——已有对接方在用，为了内部模型
    泛化去破坏外部契约不值当。业务域由 `domain` 参数给出，默认预约。
    """
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if (
        db.query(ServiceBlacklist)
        .filter(
            ServiceBlacklist.domain == body.domain,
            ServiceBlacklist.patient_id == body.patient_id,
        )
        .first()
    ):
        raise HTTPException(status_code=409, detail=f"已在{BLACKLIST_DOMAINS[body.domain]}黑名单")
    entry = ServiceBlacklist(**body.model_dump())
    db.add(entry)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="已在黑名单") from None
    return {"id": entry.id, "domain": entry.domain}


@router.get("/appointments/blacklist", dependencies=[Depends(get_current_user)])
def list_blacklist(domain: str | None = None, db: Session = Depends(get_db)):
    query = db.query(ServiceBlacklist)
    if domain:
        query = query.filter(ServiceBlacklist.domain == domain)
    return [
        {"id": b.id, "domain": b.domain, "domain_name": BLACKLIST_DOMAINS.get(b.domain, b.domain),
         "patient_id": b.patient_id, "reason": b.reason}
        for b in query.order_by(ServiceBlacklist.id.desc()).limit(500).all()
    ]


@router.delete("/appointments/blacklist/{patient_id}", dependencies=[Depends(require_admin)])
def remove_blacklist(
    patient_id: int, domain: str = "appointment", db: Session = Depends(get_db)
):
    entry = (
        db.query(ServiceBlacklist)
        .filter(ServiceBlacklist.domain == domain, ServiceBlacklist.patient_id == patient_id)
        .first()
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="不在黑名单")
    db.delete(entry)
    db.commit()
    return {"removed": True, "domain": domain}


# ---- ⑨⑩ 健康宣教 ----


class ArticleCreate(BaseModel):
    title: str = Field(min_length=1)
    category: str = "general"
    content: str = ""


@router.post(
    "/education/articles",
    status_code=201,
    dependencies=[Depends(require_roles("public_health", "operator"))],  # H2: 宣教编制
)
def create_article(body: ArticleCreate, db: Session = Depends(get_db)):
    a = HealthArticle(**body.model_dump())
    db.add(a)
    db.commit()
    return {"id": a.id, "status": a.status}


@router.post(
    "/education/articles/{article_id}/publish",
    dependencies=[Depends(require_roles("public_health", "operator"))],  # H2: 宣教发布
)
def publish_article(article_id: int, db: Session = Depends(get_db)):
    a = db.get(HealthArticle, article_id)
    if a is None:
        raise HTTPException(status_code=404, detail="文章不存在")
    a.status = "published"
    db.commit()
    return {"id": a.id, "status": "published"}


# ---- ⑪㉟ 满意度调查 ----

_SURVEY_TARGETS = {"contract", "encounter", "consultation"}


class SurveyCreate(BaseModel):
    target_type: str
    target_id: int
    patient_id: int
    score: int = Field(ge=1, le=5)
    comment: str = ""


@router.post(
    "/surveys",
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


@router.get("/surveys/stats")
def survey_stats(target_type: str | None = None, db: Session = Depends(get_db)):
    """满意度统计：均分 + 分值分布 + 差评数。

    只看均分会把个别差评稀释掉——4.6 分和"20 条里有 3 条 1 分"是两回事，
    所以这里一并给出分布与差评数。
    """
    q = db.query(SatisfactionSurvey)
    if target_type:
        q = q.filter(SatisfactionSurvey.target_type == target_type)
    rows = q.all()
    grouped: dict[str, dict] = {}
    for s in rows:
        entry = grouped.setdefault(
            s.target_type,
            {"target_type": s.target_type, "count": 0, "total": 0,
             "distribution": {str(i): 0 for i in range(1, 6)}, "negative": 0},
        )
        entry["count"] += 1
        entry["total"] += s.score
        entry["distribution"][str(s.score)] += 1
        if s.score <= NEGATIVE_SCORE:
            entry["negative"] += 1
    result = []
    for entry in grouped.values():
        count = entry.pop("count")
        total = entry.pop("total")
        entry["count"] = count
        entry["avg_score"] = round(total / count, 2) if count else 0.0
        entry["negative_rate_pct"] = round(entry["negative"] * 100 / count, 2) if count else 0.0
        result.append(entry)
    return sorted(result, key=lambda x: x["target_type"])


@router.get("/surveys")
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
    names = dict(db.query(Patient.id, Patient.name).all())
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


# ---- ⑨ 智能导诊 ----

_TRIAGE_KB = [
    ({"胸痛", "胸闷", "心悸"}, "心血管内科", True),
    ({"咳嗽", "咳痰", "气喘", "发热"}, "呼吸内科", False),
    ({"腹痛", "腹泻", "呕吐", "反酸"}, "消化内科", False),
    ({"头晕", "头痛", "肢体麻木"}, "神经内科", True),
    ({"尿频", "尿急", "血尿"}, "泌尿外科", False),
    ({"皮疹", "瘙痒"}, "皮肤科", False),
]


@router.post("/triage/suggest")
def triage_suggest(symptoms: list[str], db: Session = Depends(get_db)):
    """智能导诊：症状匹配推荐科室，急症症状提示急诊。"""
    given = set(symptoms)
    ranked = sorted(
        (
            {"department": dept, "matched": sorted(kb & given), "urgent": urgent}
            for kb, dept, urgent in _TRIAGE_KB
            if kb & given
        ),
        key=lambda r: len(r["matched"]),
        reverse=True,
    )
    return {
        "recommendations": ranked[:3] or [{"department": "全科门诊", "matched": [], "urgent": False}],
        "emergency_hint": any(r["urgent"] for r in ranked[:1]),
    }


# ---------- 终审轮：检查资源要素档案（浙#18 设备/项目/价格/时长/注意事项） ----------


class ExamResourceCreate(BaseModel):
    org_id: int
    center_type: str = Field(pattern="^(imaging|ecg|lab|pathology)$")
    item_name: str = Field(min_length=1)
    device: str = ""
    price: float = Field(default=0, ge=0)
    duration_min: int = Field(default=15, gt=0)
    notes: str = ""


@router.post("/exams/resources", status_code=201, dependencies=[Depends(require_admin)])
def create_exam_resource(body: ExamResourceCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    resource = ExamResource(**body.model_dump())
    db.add(resource)
    db.commit()
    return {"id": resource.id, "center_type": resource.center_type, "item_name": resource.item_name}


@router.get("/exams/resources")
def list_exam_resources(
    center_type: str | None = None, org_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    q = db.query(ExamResource).filter(ExamResource.active.is_(True))
    if center_type:
        q = q.filter(ExamResource.center_type == center_type)
    q = scope_org_list(db, user, q, ExamResource, org_id)
    return [
        {
            "id": r.id,
            "org_id": r.org_id,
            "center_type": r.center_type,
            "item_name": r.item_name,
            "device": r.device,
            "price": r.price,
            "duration_min": r.duration_min,
            "notes": r.notes,
        }
        for r in q.order_by(ExamResource.id).all()
    ]
