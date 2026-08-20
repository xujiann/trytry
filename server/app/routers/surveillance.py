"""㉖多点触发监测与应急资源保障：症候群、病原、应急物资队伍。

第七轮把第 26 条降到 ⚠️，缺的正是这三项。指引要求的是"智慧化多点触发传染病
监测预警体系"，而平台此前只有法定传染病病例上报——那是**确诊之后**的事。
多点触发的意义恰恰在确诊之前：症候群异常聚集与病原检出率抬头，都早于确诊。

三条口径：

1. **阈值存在记录上，不是全局常量**。县医院发热门诊日均 50 人不算异常，
   村卫生室 5 人就该看一眼。写死一个数只会让大机构天天报警、小机构从不报警。
2. **同机构同症候群同日只有一条**，重复上报按覆盖。日报是要反复核对的，
   累加会算出虚高的病例数（与基金预结同一条教训）。
3. **预警只提示不定性**。超阈值给出的是"值得看一眼"，不是"发生疫情"——
   平台不替疾控下判断。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..concurrency import upsert_unique
from ..visibility import assert_obj_org_writable, assert_org_writable, scope_org_list
from ..database import get_db
from ..datetypes import DateStr, OptionalDateStr
from ..deps import get_current_user, require_roles, resolve_business_date, resolve_org_scope, row_dict
from ..models import EmergencyResource, Organization, PathogenMonitor, SyndromeMonitor, User

router = APIRouter(
    prefix="/api/surveillance",
    tags=["多点触发监测与应急资源"],
    dependencies=[Depends(get_current_user)],
)

SYNDROMES = {
    "fever": "发热",
    "respiratory": "呼吸道",
    "diarrhea": "腹泻",
    "rash": "皮疹",
    "jaundice": "黄疸",
    "neuro": "脑炎脑膜炎",
}
RESOURCE_TYPES = {"material": "应急物资", "team": "应急队伍", "equipment": "应急装备"}


# ============================================================ 症候群监测


class SyndromeIn(BaseModel):
    org_id: int
    syndrome: str = Field(pattern="^(fever|respiratory|diarrhea|rash|jaundice|neuro)$")
    case_count: int = Field(ge=0)
    # 0 表示该机构该症候群不设阈值（不参与预警，但仍进趋势）
    threshold: int = Field(default=0, ge=0)
    record_date: DateStr
    note: str = Field(default="", max_length=256)


def _syndrome_out(r: SyndromeMonitor) -> dict:
    return {
        "id": r.id,
        "org_id": r.org_id,
        "syndrome": r.syndrome,
        "syndrome_name": SYNDROMES.get(r.syndrome, r.syndrome),
        "case_count": r.case_count,
        "threshold": r.threshold,
        "record_date": r.record_date,
        # 阈值为 0 即不参与预警，而不是"任何数都超阈值"
        "alert": bool(r.threshold) and r.case_count >= r.threshold,
        "note": r.note,
    }


@router.post(
    "/syndromes", status_code=201, dependencies=[Depends(require_roles("public_health", "doctor"))]
)
def report_syndrome(body: SyndromeIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    """症候群日报。同机构同症候群同日重复上报按**覆盖**处理（见模块口径 2）。"""
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    # 先查有没有、没有就插，是 check-then-act：并发下两个请求都查不到就都去插，
    # 唯一约束挡住其中一个，抛出未捕获的 IntegrityError——**实测 8 并发出一个 500**。
    # 改为先试插、撞了再取回来更新，覆盖语义不变，并发下也不会 500。
    record, overwritten = upsert_unique(
        db,
        SyndromeMonitor,
        keys={
            "org_id": body.org_id,
            "syndrome": body.syndrome,
            "record_date": body.record_date,
        },
        values={
            "case_count": body.case_count,
            "threshold": body.threshold,
            "note": body.note,
        },
    )
    return {**_syndrome_out(record), "overwritten": overwritten}


@router.get("/syndromes")
def list_syndromes(
    org_id: int | None = None,
    syndrome: str | None = None,
    start_date: OptionalDateStr = Query(default=""),
    end_date: OptionalDateStr = Query(default=""),
    group_id: int | None = None,
    db: Session = Depends(get_db),
):
    query = db.query(SyndromeMonitor)
    scope = resolve_org_scope(db, group_id, org_id)
    if scope is not None:
        query = query.filter(SyndromeMonitor.org_id.in_(scope))
    if syndrome:
        query = query.filter(SyndromeMonitor.syndrome == syndrome)
    if start_date:
        query = query.filter(SyndromeMonitor.record_date >= start_date)
    if end_date:
        query = query.filter(SyndromeMonitor.record_date <= end_date)
    rows = query.order_by(SyndromeMonitor.record_date.desc(), SyndromeMonitor.id.desc())
    return [_syndrome_out(r) for r in rows.limit(1000).all()]


# ============================================================ 病原监测


class PathogenIn(BaseModel):
    org_id: int
    pathogen_name: str = Field(min_length=1, max_length=128)
    specimen_type: str = Field(default="", max_length=64)
    tested_count: int = Field(ge=0)
    positive_count: int = Field(ge=0)
    record_date: DateStr
    note: str = Field(default="", max_length=256)


def _pathogen_out(r: PathogenMonitor) -> dict:
    return {
        "id": r.id,
        "org_id": r.org_id,
        "pathogen_name": r.pathogen_name,
        "specimen_type": r.specimen_type,
        "tested_count": r.tested_count,
        "positive_count": r.positive_count,
        # 送检为 0 时不报 0 阳性率——那会被读成"检了没检出"，实际是"没检"
        "positive_rate_pct": (
            round(r.positive_count * 100 / r.tested_count, 2) if r.tested_count else None
        ),
        "record_date": r.record_date,
        "note": r.note,
    }


@router.post(
    "/pathogens", status_code=201, dependencies=[Depends(require_roles("public_health", "doctor"))]
)
def report_pathogen(body: PathogenIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    if body.positive_count > body.tested_count:
        raise HTTPException(status_code=422, detail="阳性数不能大于送检数")
    record = PathogenMonitor(**body.model_dump())
    db.add(record)
    db.commit()
    db.refresh(record)
    return _pathogen_out(record)


@router.get("/pathogens")
def list_pathogens(
    org_id: int | None = None,
    pathogen_name: str | None = None,
    start_date: OptionalDateStr = Query(default=""),
    end_date: OptionalDateStr = Query(default=""),
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    query = db.query(PathogenMonitor)
    query = scope_org_list(db, user, query, PathogenMonitor, org_id)
    if pathogen_name:
        query = query.filter(PathogenMonitor.pathogen_name.like(f"%{pathogen_name}%"))
    if start_date:
        query = query.filter(PathogenMonitor.record_date >= start_date)
    if end_date:
        query = query.filter(PathogenMonitor.record_date <= end_date)
    rows = query.order_by(PathogenMonitor.record_date.desc(), PathogenMonitor.id.desc())
    return [_pathogen_out(r) for r in rows.limit(1000).all()]


# ============================================================ 多点触发汇总


@router.get("/alerts")
def multi_point_alerts(
    today: str | None = None,
    days: int = Query(default=7, ge=1, le=90),
    group_id: int | None = None,
    db: Session = Depends(get_db),
):
    """多点触发预警：症候群超阈值 + 病原阳性率抬头，一屏给出。

    这里刻意**不做综合评分**。把两路信号压成一个分数，看的人就再也说不清
    是哪一路在响——而处置动作恰恰取决于此：症候群异常查的是接诊，
    病原阳性抬头查的是实验室。
    """
    from datetime import timedelta

    end = resolve_business_date(today)
    start = (end - timedelta(days=days - 1)).isoformat()
    end_str = end.isoformat()
    scope = resolve_org_scope(db, group_id, None)

    syn_q = db.query(SyndromeMonitor).filter(
        SyndromeMonitor.record_date >= start, SyndromeMonitor.record_date <= end_str
    )
    pat_q = db.query(PathogenMonitor).filter(
        PathogenMonitor.record_date >= start, PathogenMonitor.record_date <= end_str
    )
    if scope is not None:
        syn_q = syn_q.filter(SyndromeMonitor.org_id.in_(scope))
        pat_q = pat_q.filter(PathogenMonitor.org_id.in_(scope))

    syndrome_alerts = [
        _syndrome_out(r) for r in syn_q.all() if r.threshold and r.case_count >= r.threshold
    ]
    pathogen_rows = pat_q.all()
    pathogen_alerts = [
        _pathogen_out(r)
        for r in pathogen_rows
        if r.tested_count >= 10 and r.positive_count * 100 / r.tested_count >= 10
    ]
    return {
        "window": {"start": start, "end": end_str, "days": days},
        "group_id": group_id,
        "syndrome_alerts": sorted(syndrome_alerts, key=lambda x: -x["case_count"]),
        "pathogen_alerts": sorted(
            pathogen_alerts, key=lambda x: -(x["positive_rate_pct"] or 0)
        ),
        "caliber": {
            "syndrome": "上报值达到该机构自设阈值即列出；阈值为 0 的不参与预警",
            "pathogen": "送检数 ≥10 且阳性率 ≥10% 才列出——小样本的高阳性率没有意义",
            "no_score": "两路信号分列，不做综合评分：处置动作取决于是哪一路在响",
        },
    }


# ============================================================ 应急资源保障


class ResourceIn(BaseModel):
    org_id: int
    resource_type: str = Field(default="material", pattern="^(material|team|equipment)$")
    name: str = Field(min_length=1, max_length=128)
    quantity: int = Field(default=0, ge=0)
    unit: str = Field(default="", max_length=16)
    min_quantity: int = Field(default=0, ge=0)
    expire_date: OptionalDateStr = ""
    contact: str = Field(default="", max_length=64)
    location: str = Field(default="", max_length=256)


class ResourceUpdate(BaseModel):
    quantity: int | None = Field(default=None, ge=0)
    min_quantity: int | None = Field(default=None, ge=0)
    expire_date: OptionalDateStr | None = None
    contact: str | None = None
    location: str | None = None


def _resource_out(r: EmergencyResource, today: str) -> dict:
    expired = bool(r.expire_date) and r.expire_date < today
    return {
        "id": r.id,
        "org_id": r.org_id,
        "resource_type": r.resource_type,
        "resource_type_name": RESOURCE_TYPES.get(r.resource_type, r.resource_type),
        "name": r.name,
        "quantity": r.quantity,
        "unit": r.unit,
        "min_quantity": r.min_quantity,
        "expire_date": r.expire_date,
        "expired": expired,
        "below_min": bool(r.min_quantity) and r.quantity < r.min_quantity,
        "contact": r.contact,
        "location": r.location,
    }


@router.post(
    "/resources", status_code=201, dependencies=[Depends(require_roles("public_health", "operator"))]
)
def create_resource(body: ResourceIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    if body.resource_type == "team" and body.expire_date:
        raise HTTPException(status_code=422, detail="应急队伍无效期，请勿填写")
    resource = EmergencyResource(**body.model_dump())
    db.add(resource)
    db.commit()
    db.refresh(resource)
    return _resource_out(resource, resolve_business_date(None).isoformat())


@router.get("/resources")
def list_resources(
    org_id: int | None = None,
    resource_type: str | None = None,
    shortage_only: bool = False,
    today: str | None = None,
    group_id: int | None = None,
    db: Session = Depends(get_db),
):
    today_str = resolve_business_date(today).isoformat()
    query = db.query(EmergencyResource)
    scope = resolve_org_scope(db, group_id, org_id)
    if scope is not None:
        query = query.filter(EmergencyResource.org_id.in_(scope))
    if resource_type:
        query = query.filter(EmergencyResource.resource_type == resource_type)
    rows = [
        _resource_out(r, today_str)
        for r in query.order_by(EmergencyResource.id.desc()).limit(500).all()
    ]
    if shortage_only:
        return [r for r in rows if r["below_min"] or r["expired"]]
    return rows


@router.patch(
    "/resources/{resource_id}", dependencies=[Depends(require_roles("public_health", "operator"))]
)
def update_resource(resource_id: int, body: ResourceUpdate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    resource = db.get(EmergencyResource, resource_id)
    if resource is None:
        raise HTTPException(status_code=404, detail="资源不存在")
    assert_obj_org_writable(db, user, resource)
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(resource, field, value)
    db.commit()
    db.refresh(resource)
    return _resource_out(resource, resolve_business_date(None).isoformat())


@router.get("/resources/readiness")
def readiness(today: str | None = None, group_id: int | None = None, db: Session = Depends(get_db)):
    """应急资源保障情况：按机构给出缺口与过期。

    **缺口与过期分开报**：缺口是补货问题，过期是报废问题，处置的人不同。
    """
    today_str = resolve_business_date(today).isoformat()
    query = db.query(EmergencyResource)
    scope = resolve_org_scope(db, group_id, None)
    if scope is not None:
        query = query.filter(EmergencyResource.org_id.in_(scope))
    rows = query.all()
    org_ids = {r.org_id for r in rows}
    names = {
        o.id: o.name
        for o in db.query(Organization).filter(Organization.id.in_(org_ids)).all()
    } if org_ids else {}

    by_org: dict[int, dict] = {}
    for r in rows:
        entry = by_org.setdefault(
            r.org_id,
            {"org_id": r.org_id, "org_name": names.get(r.org_id, ""),
             "total": 0, "below_min": [], "expired": []},
        )
        entry["total"] += 1
        out = _resource_out(r, today_str)
        if out["below_min"]:
            entry["below_min"].append(
                {"name": r.name, "quantity": r.quantity, "min_quantity": r.min_quantity}
            )
        if out["expired"]:
            entry["expired"].append({"name": r.name, "expire_date": r.expire_date})
    by_type = row_dict(
        db.query(EmergencyResource.resource_type, func.count(EmergencyResource.id))
        .group_by(EmergencyResource.resource_type)
        .all()
    )
    return {
        "today": today_str,
        "group_id": group_id,
        "orgs": sorted(by_org.values(), key=lambda x: -(len(x["below_min"]) + len(x["expired"]))),
        "by_type": {
            k: {"count": v, "name": RESOURCE_TYPES.get(k, k)} for k, v in by_type.items()
        },
        "caliber": "缺口（低于储备下限）与过期分列：前者是补货问题，后者是报废问题",
    }
