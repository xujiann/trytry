"""传染病病例报告与多点触发监测预警。"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles, resolve_business_date
from ..models import InfectiousCase, InfectiousDisease, Organization
from ..schemas import InfectiousCaseCreate, InfectiousCaseOut, InfectiousDiseaseOut
from .reports import _csv_response

router = APIRouter(prefix="/api/infectious", tags=["传染病监测"], dependencies=[Depends(get_current_user)])


# 响应契约：字段与原手拼 dict 一一对应，保持向后兼容。
class AlertOut(BaseModel):
    disease_code: str
    disease_name: str
    case_count: int
    org_count: int
    window_days: int
    severity: str


class LateReportOut(BaseModel):
    case_id: int
    org_id: int
    disease_code: str
    disease_name: str
    category: str
    report_hours: int
    onset_date: str
    reported_at: str
    days_late: int

DEFAULT_WINDOW_DAYS = 7
DEFAULT_THRESHOLD = 5

# 法定传染病目录种子（启动时写入 infectious_diseases 表）：
# 甲类（A）2小时报告；乙类（B）/丙类（C）24小时报告
SEED_DISEASES = [
    {"code": "A20", "name": "鼠疫", "category": "A", "report_hours": 2},
    {"code": "A00", "name": "霍乱", "category": "A", "report_hours": 2},
    {"code": "U071", "name": "新型冠状病毒感染", "category": "B", "report_hours": 24},
    {"code": "A15", "name": "肺结核", "category": "B", "report_hours": 24},
    {"code": "B15", "name": "病毒性肝炎", "category": "B", "report_hours": 24},
    {"code": "B20", "name": "艾滋病", "category": "B", "report_hours": 24},
    {"code": "A82", "name": "狂犬病", "category": "B", "report_hours": 24},
    {"code": "A38", "name": "猩红热", "category": "B", "report_hours": 24},
    {"code": "J11", "name": "流行性感冒", "category": "C", "report_hours": 24},
    {"code": "B084", "name": "手足口病", "category": "C", "report_hours": 24},
    {"code": "A09", "name": "感染性腹泻病", "category": "C", "report_hours": 24},
]


@router.get("/diseases", response_model=list[InfectiousDiseaseOut])
def list_diseases(category: str | None = None, db: Session = Depends(get_db)):
    """法定传染病目录（含分类与报告时限）。"""
    query = db.query(InfectiousDisease)
    if category:
        query = query.filter(InfectiousDisease.category == category)
    return query.order_by(InfectiousDisease.category, InfectiousDisease.code).all()


@router.post(
    "/cases",
    response_model=InfectiousCaseOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # H2: 传染病报告
)
def report_case(body: InfectiousCaseCreate, db: Session = Depends(get_db)):
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="报告机构不存在")
    case = InfectiousCase(**body.model_dump())
    # 目录内病种自动回填甲/乙/丙分类
    disease = (
        db.query(InfectiousDisease).filter(InfectiousDisease.code == body.disease_code).first()
    )
    if disease is not None:
        case.category = disease.category
    db.add(case)
    db.commit()
    db.refresh(case)
    return case


@router.get("/cases", response_model=list[InfectiousCaseOut])
def list_cases(disease_code: str | None = None, db: Session = Depends(get_db)):
    query = db.query(InfectiousCase)
    if disease_code:
        query = query.filter(InfectiousCase.disease_code == disease_code)
    return query.order_by(InfectiousCase.id.desc()).limit(500).all()


@router.get("/alerts", response_model=list[AlertOut])
def multi_point_alerts(
    window_days: int = DEFAULT_WINDOW_DAYS,
    threshold: int = DEFAULT_THRESHOLD,
    today: str | None = None,
    db: Session = Depends(get_db),
):
    """多点触发预警：滑动窗口内同病种病例数≥阈值，且涉及机构数≥2 时升级预警。

    L-2：默认取服务端当前日期；today 覆盖参数仅限测试/管理排查用途（YYYY-MM-DD）。
    """
    end = resolve_business_date(today)
    start = (end - timedelta(days=window_days)).isoformat()
    rows = (
        db.query(
            InfectiousCase.disease_code,
            InfectiousCase.disease_name,
            func.count(InfectiousCase.id).label("case_count"),
            func.count(func.distinct(InfectiousCase.org_id)).label("org_count"),
        )
        .filter(InfectiousCase.onset_date >= start, InfectiousCase.onset_date <= end.isoformat())
        .group_by(InfectiousCase.disease_code, InfectiousCase.disease_name)
        .having(func.count(InfectiousCase.id) >= threshold)
        .all()
    )
    return [
        {
            "disease_code": r.disease_code,
            "disease_name": r.disease_name,
            "case_count": r.case_count,
            "org_count": r.org_count,
            "window_days": window_days,
            # 多机构同时报告，聚集性风险升级
            "severity": "high" if r.org_count >= 2 else "medium",
        }
        for r in rows
    ]


# ---------- 工程包 I1：法定上报导出（传染病报告卡） ----------

_CATEGORY_NAMES = {"A": "甲类", "B": "乙类", "C": "丙类"}


class CaseReportCardOut(BaseModel):
    """传染病报告卡导出契约（平台留存字段集 + 报告及时性判定）。

    及时性口径与 GET /late-reports 完全一致（报告日−发病日折算小时数超
    目录时限即迟报）；目录外病种/发病日期非法时三个及时性字段为 null。
    """

    case_id: int
    org_id: int
    org_name: str
    disease_code: str
    disease_name: str
    category: str
    category_name: str
    onset_date: str
    reported_at: str
    report_hours: int | None
    days_late: int | None
    late: bool | None


def _timeliness(
    case: InfectiousCase, meta: tuple[str, str, int] | None
) -> tuple[int | None, int | None, bool | None]:
    """(法定时限小时, 迟报天数, 是否迟报)；口径与 late_reports 一致，判不了返回 None。"""
    if meta is None:
        return None, None, None
    _, _, report_hours = meta
    try:
        onset = date.fromisoformat(case.onset_date)
    except ValueError:
        return report_hours, None, None
    days_late = (case.reported_at.date() - onset).days
    return report_hours, days_late, days_late * 24 > report_hours


def _case_card(case: InfectiousCase, org_names: dict, meta_by_code: dict) -> dict:
    meta = meta_by_code.get(case.disease_code)
    report_hours, days_late, late = _timeliness(case, meta)
    return {
        "case_id": case.id,
        "org_id": case.org_id,
        "org_name": org_names.get(case.org_id, ""),
        "disease_code": case.disease_code,
        "disease_name": case.disease_name,
        "category": case.category,
        "category_name": _CATEGORY_NAMES.get(case.category, "目录外"),
        "onset_date": case.onset_date,
        "reported_at": case.reported_at.isoformat(),
        "report_hours": report_hours,
        "days_late": days_late,
        "late": late,
    }


def _disease_meta(db: Session) -> dict:
    return {
        d.code: (d.name, d.category, d.report_hours) for d in db.query(InfectiousDisease).all()
    }


@router.get(
    "/cases/export.csv",
    response_model=str,
    dependencies=[Depends(require_roles("director"))],  # 法定上报导出=管理层
)
def export_case_report_cards_csv(
    disease_code: str | None = None,
    late_only: bool = False,
    db: Session = Depends(get_db),
):
    """传染病报告卡批量导出（CSV，Excel 可直接打开）。

    **报送方式说明**：平台与县疾控无网络直报专线，本导出供**手工网报**
    （录入中国疾病预防控制信息系统/大疫情网）或交换前置机对接使用；
    及时性列与"未及时上报清单"（GET /late-reports）同口径联动，
    `late_only=true` 即只导迟报清单。
    """
    meta_by_code = _disease_meta(db)
    org_names = {o.id: o.name for o in db.query(Organization).all()}
    query = db.query(InfectiousCase)
    if disease_code:
        query = query.filter(InfectiousCase.disease_code == disease_code)
    cards = [
        _case_card(c, org_names, meta_by_code)
        for c in query.order_by(InfectiousCase.id).limit(2000).all()
    ]
    if late_only:
        cards = [c for c in cards if c["late"]]
    rows = [
        [
            c["case_id"], c["org_name"], c["disease_code"], c["disease_name"],
            c["category_name"], c["onset_date"], c["reported_at"],
            c["report_hours"] if c["report_hours"] is not None else "",
            c["days_late"] if c["days_late"] is not None else "",
            "迟报" if c["late"] else ("" if c["late"] is None else "及时"),
        ]
        for c in cards
    ]
    return _csv_response(
        "infectious_report_cards.csv",
        ["卡片编号", "报告机构", "病种编码", "病种名称", "分类", "发病日期", "报告时间",
         "法定时限(小时)", "迟报天数", "及时性"],
        rows,
    )


@router.get(
    "/cases/{case_id}/report-card",
    response_model=CaseReportCardOut,
    dependencies=[Depends(require_roles("director"))],  # 法定上报导出=管理层
)
def case_report_card(case_id: int, db: Session = Depends(get_db)):
    """单张传染病报告卡（JSON，平台留存的法定字段集 + 及时性判定）。

    **报送方式说明**：导出供手工网报（大疫情网）或县疾控前置机对接，
    平台不直连国家传染病网络直报系统。病例登记未含患者个体标识
    （infectious_cases 仅记报告机构/病种/发病日期），卡片即按此字段集导出，
    不虚构未存储的字段。
    """
    case = db.get(InfectiousCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="病例不存在")
    org = db.get(Organization, case.org_id)
    return _case_card(case, {case.org_id: org.name if org else ""}, _disease_meta(db))


@router.get("/late-reports", response_model=list[LateReportOut])
def late_reports(db: Session = Depends(get_db)):
    """迟报清单：reported_at 与 onset_date 间隔超过目录报告时限的病例（粗略按天折算）。

    判定口径：报告日与发病日相差天数 × 24 小时 > report_hours 即视为迟报，
    即甲类（2h）跨日报告即迟报，乙/丙类（24h）相隔≥2天迟报。
    """
    hours_by_code = {d.code: (d.name, d.category, d.report_hours) for d in db.query(InfectiousDisease).all()}
    rows = []
    for case in db.query(InfectiousCase).order_by(InfectiousCase.id).all():
        meta = hours_by_code.get(case.disease_code)
        if meta is None:
            continue  # 目录外病种无法定时限，不判迟报
        _, category, report_hours = meta
        try:
            onset = date.fromisoformat(case.onset_date)
        except ValueError:
            continue
        days_late = (case.reported_at.date() - onset).days
        if days_late * 24 > report_hours:
            rows.append(
                {
                    "case_id": case.id,
                    "org_id": case.org_id,
                    "disease_code": case.disease_code,
                    "disease_name": case.disease_name,
                    "category": category,
                    "report_hours": report_hours,
                    "onset_date": case.onset_date,
                    "reported_at": case.reported_at.isoformat(),
                    "days_late": days_late,
                }
            )
    return rows
