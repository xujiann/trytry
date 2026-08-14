"""医疗废弃物：点位、收集、暂存、交接全过程监管与追溯码。

第七轮子功能级重审把第 36 条从 ✅ 降到 ⚠️：指引点名的"医废点位管理""转运
人员管理""医废追溯"三项都没有落地——原表只有 `org_id`，答得出"哪家机构"，
答不出"哪个科室产生的、存在哪间暂存间、谁转运的"。

三条口径：

1. **追溯码由平台生成，不让人填**。让人填码，迟早会有两包医废共用一个码，
   而追溯的全部价值就在码的唯一性上。
2. **转运人员挂员工档案**，不是自由文本。靠手打名字既统计不出人次也追不了责。
3. **点位停用不影响历史记录**。点位会撤并，但"这包医废当年是从哪个科室出来的"
   必须永远查得到，故引用不做级联清理。
"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..clock import now_naive
from ..concurrency import insert_with_retry
from ..visibility import assert_org_writable, scope_org_list
from ..database import get_db
from ..deps import get_current_user, require_roles, resolve_business_date
from ..models import Employee, MedicalWaste, Organization, User, WasteLocation
from ..schemas import WasteCreate, WasteHandover

router = APIRouter(prefix="/api/medwaste", tags=["医废追溯"], dependencies=[Depends(get_current_user)])

# 收集后超过该天数未交接即预警（《医疗废物管理条例》暂存不得超过2天）
STORAGE_LIMIT_DAYS = 2

WASTE_TYPES = {
    "infectious": "感染性",
    "sharp": "损伤性",
    "pathological": "病理性",
    "pharmaceutical": "药物性",
    "chemical": "化学性",
}


# ---------------------------------------------------------------- 点位


class LocationIn(BaseModel):
    org_id: int
    name: str = Field(min_length=1, max_length=128)
    location_type: str = Field(default="source", pattern="^(source|storage)$")
    manager_name: str = Field(default="", max_length=64)


def _location_out(loc: WasteLocation) -> dict:
    return {
        "id": loc.id,
        "org_id": loc.org_id,
        "name": loc.name,
        "location_type": loc.location_type,
        "location_type_name": "产生点" if loc.location_type == "source" else "暂存间",
        "manager_name": loc.manager_name,
        "active": loc.active,
    }


@router.post("/locations", status_code=201, dependencies=[Depends(require_roles("operator", "director"))])
def create_location(body: LocationIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    loc = WasteLocation(**body.model_dump())
    db.add(loc)
    db.commit()
    db.refresh(loc)
    return _location_out(loc)


@router.get("/locations")
def list_locations(
    org_id: int | None = None,
    location_type: str | None = None,
    include_inactive: bool = False,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    query = db.query(WasteLocation)
    query = scope_org_list(db, user, query, WasteLocation, org_id)
    if location_type:
        query = query.filter(WasteLocation.location_type == location_type)
    if not include_inactive:
        query = query.filter(WasteLocation.active.is_(True))
    return [_location_out(r) for r in query.order_by(WasteLocation.id).limit(500).all()]


@router.delete("/locations/{location_id}", dependencies=[Depends(require_roles("operator", "director"))])
def deactivate_location(location_id: int, db: Session = Depends(get_db)):
    """停用点位（不删行）。科室撤并很常见，但历史医废的来源必须永远查得到。"""
    loc = db.get(WasteLocation, location_id)
    if loc is None:
        raise HTTPException(status_code=404, detail="点位不存在")
    loc.active = False
    db.commit()
    return {"id": location_id, "active": False}


@router.post("/locations/{location_id}/reactivate", dependencies=[Depends(require_roles("operator", "director"))])
def reactivate_location(location_id: int, db: Session = Depends(get_db)):
    loc = db.get(WasteLocation, location_id)
    if loc is None:
        raise HTTPException(status_code=404, detail="点位不存在")
    loc.active = True
    db.commit()
    return {"id": location_id, "active": True}


# ---------------------------------------------------------------- 收集与追溯


def _next_trace_code(db: Session, collected_date: str) -> str:
    """追溯码：MW-YYYYMMDD-序号。日期段让人肉眼可读，序号保证唯一。

    并发下两个请求可能算出同一个序号，落库时被唯一约束挡住——调用方重试即可。
    这里不上锁：医废收集不是高并发场景，为它上锁得不偿失。
    """
    prefix = f"MW-{collected_date.replace('-', '')}-"
    used = (
        db.query(func.count(MedicalWaste.id))
        .filter(MedicalWaste.trace_code.like(f"{prefix}%"))
        .scalar()
        or 0
    )
    return f"{prefix}{used + 1:04d}"


class WasteCollect(WasteCreate):
    source_location_id: int | None = None


def _waste_out(w: MedicalWaste) -> dict:
    return {
        "id": w.id,
        "org_id": w.org_id,
        "trace_code": w.trace_code,
        "waste_type": w.waste_type,
        "waste_type_name": WASTE_TYPES.get(w.waste_type, w.waste_type),
        "weight_kg": w.weight_kg,
        "status": w.status,
        "collected_date": w.collected_date,
        "source_location_id": w.source_location_id,
        "storage_location_id": w.storage_location_id,
        "handler_name": w.handler_name,
        "handler_employee_id": w.handler_employee_id,
        "stored_at": w.stored_at,
        "handed_over_at": w.handed_over_at,
    }


@router.post(
    "",
    status_code=201,
    dependencies=[Depends(require_roles("operator"))],  # H2: 医废收集=经办
)
def collect(body: WasteCollect, db: Session = Depends(get_db)):
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    if body.source_location_id is not None:
        loc = db.get(WasteLocation, body.source_location_id)
        if loc is None:
            raise HTTPException(status_code=404, detail="产生点位不存在")
        if loc.location_type != "source":
            raise HTTPException(status_code=422, detail="该点位不是产生点")
        if loc.org_id != body.org_id:
            raise HTTPException(status_code=422, detail="点位不属于该机构")
    # 追溯码是服务端算出来的顺序号，并发下两个请求会算出同一个。
    # **重试放在服务端**：调用方重试的是整个收集动作，而且压力下它会撞进下一次
    # 冲突；真正该重试的只是"取下一个号"这一步。
    # 实测：8 线程并发收集，原先只落库 5 条、3 条抛未捕获 IntegrityError 成 500。
    waste = insert_with_retry(
        db,
        lambda: MedicalWaste(
            trace_code=_next_trace_code(db, body.collected_date), **body.model_dump()
        ),
    )
    return _waste_out(waste)


@router.get("")
def list_wastes(org_id: int | None = None, status: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user),):
    """医废清单。

    D-8：这里原先套 `WasteOut`，而那个 schema 是阶段五写的，字段停在
    org_id/waste_type/weight_kg/collected_date/status/handler_name——
    **追溯码不在里面**。于是这个模块最核心的那一列，清单页一列都看不到，
    只能靠 `/trace/{code}` 反查；可码是从清单上抄的，抄不到就查不了。
    改回 `_waste_out`（点位、暂存与交接时间也一并带出）。
    """
    query = db.query(MedicalWaste)
    query = scope_org_list(db, user, query, MedicalWaste, org_id)
    if status:
        query = query.filter(MedicalWaste.status == status)
    return [_waste_out(w) for w in query.order_by(MedicalWaste.id.desc()).limit(500).all()]


class WasteStore(BaseModel):
    storage_location_id: int


@router.post("/{waste_id}/store", dependencies=[Depends(require_roles("operator"))])
def store(waste_id: int, body: WasteStore, db: Session = Depends(get_db)):
    """入暂存间。原先 `stored` 这个状态在表里定义了却没有接口能走到它，
    收集之后只能直接交接——暂存环节整段是空的。"""
    waste = db.get(MedicalWaste, waste_id)
    if waste is None:
        raise HTTPException(status_code=404, detail="医废记录不存在")
    if waste.status != "collected":
        raise HTTPException(status_code=409, detail=f"当前状态 {waste.status} 不可入暂存")
    loc = db.get(WasteLocation, body.storage_location_id)
    if loc is None:
        raise HTTPException(status_code=404, detail="暂存点位不存在")
    if loc.location_type != "storage":
        raise HTTPException(status_code=422, detail="该点位不是暂存间")
    waste.status = "stored"
    waste.storage_location_id = loc.id
    waste.stored_at = now_naive()
    db.commit()
    db.refresh(waste)
    return _waste_out(waste)


class WasteHandoverIn(WasteHandover):
    handler_employee_id: int | None = None


@router.post(
    "/{waste_id}/handover",
    dependencies=[Depends(require_roles("operator"))],  # H2: 医废交接=经办
)
def handover(waste_id: int, body: WasteHandoverIn, db: Session = Depends(get_db)):
    waste = db.get(MedicalWaste, waste_id)
    if waste is None:
        raise HTTPException(status_code=404, detail="医废记录不存在")
    if waste.status == "handed_over":
        raise HTTPException(status_code=409, detail="该批医废已交接")
    if body.handler_employee_id is not None:
        employee = db.get(Employee, body.handler_employee_id)
        if employee is None:
            raise HTTPException(status_code=404, detail="转运人员不存在")
        waste.handler_employee_id = employee.id
        waste.handler_name = employee.name
    else:
        waste.handler_name = body.handler_name
    waste.status = "handed_over"
    waste.handed_over_at = now_naive()
    db.commit()
    db.refresh(waste)
    return _waste_out(waste)


@router.get("/trace/{trace_code}")
def trace(trace_code: str, db: Session = Depends(get_db)):
    """扫码追溯：一包医废从哪来、经过哪里、谁交接的、现在到哪一步。"""
    waste = db.query(MedicalWaste).filter(MedicalWaste.trace_code == trace_code).first()
    if waste is None:
        raise HTTPException(status_code=404, detail="追溯码不存在")
    locations = {
        loc.id: _location_out(loc)
        for loc in db.query(WasteLocation)
        .filter(
            WasteLocation.id.in_(
                [i for i in (waste.source_location_id, waste.storage_location_id) if i]
            )
        )
        .all()
    }
    timeline = [{"step": "收集", "at": waste.collected_date,
                 "location": locations.get(waste.source_location_id, {}).get("name", "")}]
    if waste.stored_at:
        timeline.append({"step": "暂存", "at": waste.stored_at.isoformat(),
                         "location": locations.get(waste.storage_location_id, {}).get("name", "")})
    if waste.handed_over_at:
        timeline.append({"step": "交接", "at": waste.handed_over_at.isoformat(),
                         "location": waste.handler_name})
    return {**_waste_out(waste), "timeline": timeline}


@router.get("/handler-stats")
def handler_stats(
    start_date: str | None = None, end_date: str | None = None, db: Session = Depends(get_db)
):
    """转运人员工作量。挂了员工档案的按人汇总；只填了名字的历史记录单列报出，
    不硬凑进某个人头上——名字重合就会张冠李戴。"""
    query = db.query(MedicalWaste).filter(MedicalWaste.status == "handed_over")
    if start_date:
        query = query.filter(MedicalWaste.collected_date >= start_date)
    if end_date:
        query = query.filter(MedicalWaste.collected_date <= end_date)
    rows = query.all()
    by_employee: dict[int, dict] = {}
    unlinked = {"count": 0, "weight_kg": 0.0}
    for w in rows:
        if w.handler_employee_id is None:
            unlinked["count"] += 1
            unlinked["weight_kg"] += w.weight_kg
            continue
        entry = by_employee.setdefault(
            w.handler_employee_id,
            {"employee_id": w.handler_employee_id, "name": w.handler_name,
             "count": 0, "weight_kg": 0.0},
        )
        entry["count"] += 1
        entry["weight_kg"] += w.weight_kg
    for entry in by_employee.values():
        entry["weight_kg"] = round(entry["weight_kg"], 2)
    unlinked["weight_kg"] = round(unlinked["weight_kg"], 2)
    return {
        "handlers": sorted(by_employee.values(), key=lambda x: -x["count"]),
        # 未挂员工档案的交接单列：与 DDD 未维护、职称等级未填同一条原则
        "unlinked_records": unlinked,
    }


@router.get("/alerts")
def overdue_alerts(today: str | None = None, db: Session = Depends(get_db)):
    """滞留预警：收集超过 2 天仍未交接的医废。

    L-2：默认取服务端当前日期；today 覆盖参数仅限测试/管理排查用途（YYYY-MM-DD）。

    D-8：同样带上追溯码与暂存点位。预警的下一步动作是去把那几包找出来，
    只报"某机构有 3 包超期"而不报是哪几包、在哪间暂存间，等于没报。
    """
    end = resolve_business_date(today)
    cutoff = (end - timedelta(days=STORAGE_LIMIT_DAYS)).isoformat()
    rows = (
        db.query(MedicalWaste)
        .filter(MedicalWaste.status != "handed_over", MedicalWaste.collected_date <= cutoff)
        .order_by(MedicalWaste.collected_date)
        .all()
    )
    overdue_from = (end - timedelta(days=STORAGE_LIMIT_DAYS)).isoformat()
    return [
        {
            **_waste_out(w),
            # 超期几天直接算好：预警页要按严重程度排序，别让前端自己减日期
            "overdue_days": (end - date.fromisoformat(w.collected_date)).days
            - STORAGE_LIMIT_DAYS,
            "limit_date": overdue_from,
        }
        for w in rows
    ]
