"""传染病病例报告与多点触发监测预警。"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import InfectiousCase, Organization
from ..schemas import InfectiousCaseCreate, InfectiousCaseOut

router = APIRouter(prefix="/api/infectious", tags=["传染病监测"], dependencies=[Depends(get_current_user)])

DEFAULT_WINDOW_DAYS = 7
DEFAULT_THRESHOLD = 5


@router.post("/cases", response_model=InfectiousCaseOut, status_code=201)
def report_case(body: InfectiousCaseCreate, db: Session = Depends(get_db)):
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="报告机构不存在")
    case = InfectiousCase(**body.model_dump())
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
