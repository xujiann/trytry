"""传染病病例报告与多点触发监测预警。"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import InfectiousCase, InfectiousDisease, Organization
from ..schemas import InfectiousCaseCreate, InfectiousCaseOut, InfectiousDiseaseOut

router = APIRouter(prefix="/api/infectious", tags=["传染病监测"], dependencies=[Depends(get_current_user)])

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


@router.get("/alerts")
def multi_point_alerts(
    window_days: int = DEFAULT_WINDOW_DAYS,
    threshold: int = DEFAULT_THRESHOLD,
    today: str | None = None,
    db: Session = Depends(get_db),
):
    """多点触发预警：滑动窗口内同病种病例数≥阈值，且涉及机构数≥2 时升级预警。"""
    end = date.fromisoformat(today) if today else date.today()
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


@router.get("/late-reports")
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
