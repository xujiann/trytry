"""共享诊断中心（影像/心电/检验/病理）：基层检查、上级诊断、结果互认、危急值管理。"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..clock import now_naive
from ..concurrency import insert_or_conflict
from ..visibility import assert_org_writable, log_patient_access
from ..database import get_db
from ..deps import get_current_user, paginate, require_admin, require_roles, resolve_business_date
from ..models import CriticalAction, ExamReport, ExamRequest, Organization, Patient, RecognitionItem, User
from ..schemas import (
    CriticalActionOut,
    CriticalResolveBody,
    ExamReportCreate,
    ExamReportOut,
    ExamRequestCreate,
    ExamRequestOut,
    RecognitionItemCreate,
    RecognitionItemOut,
    RecognitionItemUpdate,
)
from ..notify import notify_patient, notify_staff
from ..ws import manager

router = APIRouter(prefix="/api/exams", tags=["共享诊断中心"], dependencies=[Depends(get_current_user)])

# 同一患者同一项目在此天数内已有报告的，提示可互认
RECOGNITION_WINDOW_DAYS = 30


# 县域层级集合：mutual_scope=county 的项目仅在这些层级的机构间互认
_COUNTY_LEVELS = {"county", "township", "village"}


def _directory_blocked_reason(
    db: Session,
    item_code: str,
    center_type: str | None = None,
    orgs: tuple[Organization | None, ...] = (),
) -> str | None:
    """互认目录管控：目录已配置时，仅目录内 active 项目可互认。

    返回 None=允许互认；返回字符串=阻断原因。目录为空视为未启用目录管控（兼容）。

    - L-5 整改：目录项 center_type 参与判定，申请中心类型须与目录一致；
    - L-4 整改：mutual_scope 参与判定——county（县域互认）要求相关机构
      均为县域层级（county/township/village）；city（市级互认）放开至市级机构。
    """
    if db.query(RecognitionItem.id).first() is None:
        return None
    item = db.query(RecognitionItem).filter(RecognitionItem.item_code == item_code).first()
    if item is None:
        return "该项目不在互认目录内"
    if not item.active:
        return "该项目已在互认目录中停用"
    if center_type and item.center_type and item.center_type != center_type:
        return f"项目所属中心类型不符（目录为 {item.center_type}）"
    if item.mutual_scope == "county":
        for org in orgs:
            if org is not None and org.level not in _COUNTY_LEVELS:
                return f"该项目互认范围为县域，机构「{org.name}」不在县域层级内"
    return None


# ---------- 互认项目目录维护（管理员） ----------


@router.post(
    "/recognition-items",
    response_model=RecognitionItemOut,
    status_code=201,
    dependencies=[Depends(require_admin)],
)
def create_recognition_item(body: RecognitionItemCreate, db: Session = Depends(get_db)):
    if db.query(RecognitionItem).filter(RecognitionItem.item_code == body.item_code).first():
        raise HTTPException(status_code=409, detail="该项目已在互认目录中")
    item = insert_or_conflict(db, RecognitionItem(**body.model_dump()), "该项目已在互认目录中")
    return item


@router.get("/recognition-items", response_model=list[RecognitionItemOut])
def list_recognition_items(
    center_type: str | None = None, active: bool | None = None, db: Session = Depends(get_db)
):
    query = db.query(RecognitionItem)
    if center_type:
        query = query.filter(RecognitionItem.center_type == center_type)
    if active is not None:
        query = query.filter(RecognitionItem.active.is_(active))
    return query.order_by(RecognitionItem.item_code).limit(500).all()


@router.patch(
    "/recognition-items/{item_id}",
    response_model=RecognitionItemOut,
    dependencies=[Depends(require_admin)],
)
def update_recognition_item(
    item_id: int, body: RecognitionItemUpdate, db: Session = Depends(get_db)
):
    item = db.get(RecognitionItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="目录项目不存在")
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(item, field, value)
    db.commit()
    db.refresh(item)
    return item


def _find_recognizable(db: Session, patient_id: int, item_code: str) -> ExamRequest | None:
    since = now_naive() - timedelta(days=RECOGNITION_WINDOW_DAYS)
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
def recognition_check(
    patient_id: int,
    item_code: str,
    center_type: str | None = None,
    from_org_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """开单前互认检查：项目须在互认目录内（目录已配置时），且近期已有同项目报告。

    可选传 center_type / from_org_id，与建单侧判定口径一致（L-4/L-5）。
    """
    # 互认查询按设计要跨机构工作（见 visibility.log_patient_access）：
    # 患者初次到院、本机构一条记录都没有，正是最该问"别处做过没有"的时刻。
    log_patient_access(db, user, patient_id, "exam_recognition", "recognition")
    org = db.get(Organization, from_org_id) if from_org_id is not None else None
    blocked = _directory_blocked_reason(db, item_code, center_type, (org,))
    if blocked is not None:
        return {"recognizable": False, "reason": blocked}
    existing = _find_recognizable(db, patient_id, item_code)
    if existing is None:
        return {"recognizable": False}
    return {
        "recognizable": True,
        "request_id": existing.id,
        "item_name": existing.item_name,
        "conclusion": existing.report.conclusion if existing.report else "",
    }


@router.post(
    "",
    response_model=ExamRequestOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "operator"))],  # H2: 检查申请=医疗岗
)
def create_request(
    body: ExamRequestCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    from_org = db.get(Organization, body.from_org_id)
    if from_org is None:
        raise HTTPException(status_code=404, detail="申请机构不存在")

    data = body.model_dump(exclude={"accept_recognition_of"})
    request = ExamRequest(**data, created_by=user.id)

    if body.accept_recognition_of is not None:
        source = db.get(ExamRequest, body.accept_recognition_of)
        if source is None or source.status != "reported":
            raise HTTPException(status_code=422, detail="被互认的申请单不存在或尚无报告")
        if source.patient_id != body.patient_id or source.item_code != body.item_code:
            raise HTTPException(status_code=422, detail="互认必须是同一患者的同一检查项目")
        # L-5 整改：建单侧校验中心类型一致（源申请与新申请须同为影像/心电/检验/病理）
        if source.center_type != body.center_type:
            raise HTTPException(status_code=422, detail="互认必须是同一中心类型的检查项目")
        source_org = db.get(Organization, source.from_org_id)
        # L-4/L-5 整改：目录判定纳入 center_type 与 mutual_scope（源/新申请机构层级）
        blocked = _directory_blocked_reason(
            db, body.item_code, body.center_type, (from_org, source_org)
        )
        if blocked is not None:
            raise HTTPException(status_code=422, detail=f"不可互认：{blocked}")
        # L1 整改：建单侧同样复核 30 天互认窗口，与 recognition-check 预检口径一致
        window_start = now_naive() - timedelta(days=RECOGNITION_WINDOW_DAYS)
        if source.report is None or source.report.reported_at < window_start.replace(tzinfo=None):
            raise HTTPException(
                status_code=422,
                detail=f"被互认报告已超出 {RECOGNITION_WINDOW_DAYS} 天互认窗口，不可互认",
            )
        request.status = "recognized"
        request.recognized_from_id = source.id
    db.add(request)
    db.commit()
    db.refresh(request)
    return request


@router.get("", response_model=list[ExamRequestOut])
def list_requests(
    response: Response,
    center_type: str | None = None,
    status: str | None = None,
    offset: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
):
    """检查申请列表（L-3 分页：offset/limit，总数见 X-Total-Count 响应头）。"""
    query = db.query(ExamRequest)
    if center_type:
        query = query.filter(ExamRequest.center_type == center_type)
    if status:
        query = query.filter(ExamRequest.status == status)
    return paginate(query.order_by(ExamRequest.id.desc()), response, offset, limit)


@router.post(
    "/{request_id}/claim",
    response_model=ExamRequestOut,
    dependencies=[Depends(require_roles("doctor"))],
)
def claim_request(
    request_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    request = db.get(ExamRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="申请单不存在")
    # L-6 整改：条件 UPDATE 原子领取（并发双领仅一人成功）并记录领取人
    claimed = (
        db.query(ExamRequest)
        .filter(ExamRequest.id == request_id, ExamRequest.status == "pending")
        .update(
            {
                ExamRequest.status: "diagnosing",
                ExamRequest.claimed_by: user.full_name or user.username,
                ExamRequest.center_org_id: user.org_id,  # 承接诊断中心=领取人机构
            },
            synchronize_session=False,
        )
    )
    if not claimed:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"当前状态 {request.status} 不可领取")
    db.commit()
    db.refresh(request)
    return request


@router.post(
    "/{request_id}/report",
    response_model=ExamReportOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor"))],
)
def submit_report(
    request_id: int, body: ExamReportCreate, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    request = db.get(ExamRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="申请单不存在")
    if request.status not in ("pending", "diagnosing"):
        raise HTTPException(status_code=409, detail=f"当前状态 {request.status} 不可出报告")
    # 承接归属：已被某中心领取的，只能由该中心本机构出报告（堵他院医师出别家已领报告）；
    # 直接从 pending 出报告的（未经 claim），出报告方即承接中心。
    if request.center_org_id is not None:
        assert_org_writable(db, user, request.center_org_id)
    else:
        request.center_org_id = user.org_id
    report = ExamReport(request_id=request_id, **body.model_dump())
    request.status = "reported"
    if report.critical:
        # 危急值闭环起点：自动置为"已通知"并留痕
        report.critical_status = "notified"
    db.add(report)
    db.flush()
    if report.critical:
        db.add(
            CriticalAction(
                report_id=report.id,
                action=(
                    f"危急值报告发布，已通知申请机构(org={request.from_org_id})"
                    f"在线用户与监管角色（定向推送）：{report.conclusion}"
                ),
                actor=report.reported_by or "system",
            )
        )
    # 站内消息：广播只送在线连接，离线即丢；这里补一条可留存可追查的记录
    if report.critical:
        notify_staff(
            db,
            category="critical_value",
            title=f"危急值：{request.item_name}",
            body=report.conclusion,
            org_id=request.from_org_id,
            roles=("doctor",),
            link_type="exam_report",
            link_id=report.id,
        )
    else:
        notify_patient(
            db,
            request.patient_id,
            category="exam_report",
            title=f"{request.item_name} 报告已出具",
            body="请在「我的档案」查看结论，如有疑问请咨询开单医师。",
            link_type="exam_report",
            link_id=report.id,
        )
    try:
        db.commit()
    except IntegrityError:
        # request.status 那道判断挡不住并发：两个请求都读到 pending 就都会走到这里。
        # 一张申请单只能有一份报告，撞了就是"已出具"，不是 500。
        db.rollback()
        raise HTTPException(status_code=409, detail="该申请单报告已出具") from None
    db.refresh(report)
    if report.critical:
        # M-2 整改：危急值定向广播——仅申请机构在线用户与 admin/director 收到
        manager.broadcast(
            {
                "type": "critical_report",
                "org_id": request.from_org_id,
                "request_id": request_id,
                "patient_id": request.patient_id,
                "item_name": request.item_name,
                "conclusion": report.conclusion,
            },
            target_org_id=request.from_org_id,
        )
    return report


_SAMPLE_FLOW = {"": "collected", "collected": "in_transit", "in_transit": "received"}


@router.post(
    "/{request_id}/sample/advance",
    response_model=ExamRequestOut,
    dependencies=[Depends(require_roles("doctor", "operator"))],  # H2: 样本物流
)
def advance_sample(request_id: int, db: Session = Depends(get_db)):
    """检验样本物流：采样→冷链转运→中心核收（仅检验类申请）。"""
    request = db.get(ExamRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="申请单不存在")
    if request.center_type != "lab":
        raise HTTPException(status_code=422, detail="仅检验申请有样本物流环节")
    if request.status not in ("pending", "diagnosing"):
        raise HTTPException(status_code=409, detail="申请单已出报告或已互认")
    next_status = _SAMPLE_FLOW.get(request.sample_status)
    if next_status is None:
        raise HTTPException(status_code=409, detail="样本已核收")
    request.sample_status = next_status
    db.commit()
    db.refresh(request)
    return request


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


# ---------- 危急值闭环：通知 → 确认接收 → 处置反馈 ----------


@router.post(
    "/reports/{report_id}/acknowledge",
    response_model=ExamReportOut,
    dependencies=[Depends(require_roles("doctor"))],
)
def acknowledge_critical(
    report_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """医师确认接收危急值通知（notified → acknowledged）。"""
    report = db.get(ExamReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    if not report.critical:
        raise HTTPException(status_code=422, detail="非危急值报告，无需确认")
    # M-1 整改：存量危急报告（迁移前 critical_status=''）等同"已通知"，可正常进入闭环
    if report.critical_status not in ("notified", ""):
        raise HTTPException(status_code=409, detail=f"当前状态 {report.critical_status} 不可确认接收")
    report.critical_status = "acknowledged"
    db.add(
        CriticalAction(
            report_id=report.id,
            action="医师确认接收危急值通知",
            actor=user.full_name or user.username,
        )
    )
    db.commit()
    db.refresh(report)
    return report


@router.post(
    "/reports/{report_id}/resolve",
    response_model=ExamReportOut,
    dependencies=[Depends(require_roles("doctor"))],
)
def resolve_critical(
    report_id: int,
    body: CriticalResolveBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """处置反馈（acknowledged → resolved）：须先确认接收后方可反馈处置结果。"""
    report = db.get(ExamReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    if not report.critical:
        raise HTTPException(status_code=422, detail="非危急值报告，无需处置反馈")
    if report.critical_status != "acknowledged":
        raise HTTPException(status_code=409, detail="须先确认接收后方可处置反馈")
    report.critical_status = "resolved"
    db.add(
        CriticalAction(
            report_id=report.id,
            action=f"处置反馈：{body.note}" if body.note else "处置反馈完成",
            actor=user.full_name or user.username,
        )
    )
    db.commit()
    db.refresh(report)
    return report


@router.get("/reports/{report_id}/critical-actions", response_model=list[CriticalActionOut])
def list_critical_actions(report_id: int, db: Session = Depends(get_db)):
    """危急值处置留痕轨迹。"""
    if db.get(ExamReport, report_id) is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    return (
        db.query(CriticalAction)
        .filter(CriticalAction.report_id == report_id)
        .order_by(CriticalAction.id)
        .all()
    )


@router.get("/critical/unacknowledged")
def list_unacknowledged_critical(
    today: str | None = None, timeout_minutes: int = 30, db: Session = Depends(get_db)
):
    """超时未确认危急值清单：报告发布后医师超时未确认接收的（催办用）。

    - 缺省按服务端当前时间与 timeout_minutes 比对（L-2：服务端注入当前日期）；
    - today 覆盖参数（YYYY-MM-DD）仅限测试/管理排查用途：报告日期早于 today
      的未确认危急值均计入（粗略按天），格式非法返回 422。
    """
    # M-1 整改：存量危急报告（critical_status=''）同样计入催办清单
    query = db.query(ExamReport).filter(
        ExamReport.critical.is_(True), ExamReport.critical_status.in_(["notified", ""])
    )
    if today is not None:
        deadline = datetime.combine(resolve_business_date(today), datetime.min.time())
        rows = [r for r in query.all() if r.reported_at < deadline]
    else:
        cutoff = now_naive() - timedelta(minutes=timeout_minutes)
        rows = [r for r in query.all() if r.reported_at < cutoff]
    return [
        {
            "report_id": r.id,
            "request_id": r.request_id,
            "conclusion": r.conclusion,
            "reported_by": r.reported_by,
            "reported_at": r.reported_at.isoformat(),
            "critical_status": r.critical_status,
        }
        for r in sorted(rows, key=lambda r: r.reported_at)
    ]


# ---------- 互认统计 ----------


@router.get("/recognition-stats")
def recognition_stats(db: Session = Depends(get_db)):
    """互认统计：互认率、按项目统计、节约检查次数。"""
    reported = (
        db.query(func.count(ExamRequest.id)).filter(ExamRequest.status == "reported").scalar() or 0
    )
    recognized = (
        db.query(func.count(ExamRequest.id)).filter(ExamRequest.status == "recognized").scalar()
        or 0
    )
    total = reported + recognized
    by_item = (
        db.query(
            ExamRequest.item_code,
            ExamRequest.item_name,
            func.count(ExamRequest.id).label("recognized_count"),
        )
        .filter(ExamRequest.status == "recognized")
        .group_by(ExamRequest.item_code, ExamRequest.item_name)
        .order_by(func.count(ExamRequest.id).desc())
        .all()
    )
    return {
        "recognized_total": recognized,
        "reported_total": reported,
        # 互认率 = 互认单数 / (已报告 + 互认)
        "recognition_ratio_pct": round(recognized / total * 100, 1) if total else 0.0,
        # 每一次互认即节约一次重复检查
        "saved_exams": recognized,
        "by_item": [
            {
                "item_code": r.item_code,
                "item_name": r.item_name,
                "recognized_count": r.recognized_count,
            }
            for r in by_item
        ],
    }
