"""综合管理补齐：㉚人力资源、㉛财务、㉜物资、㉞行政公文，及①-④排班/质控。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_admin, require_roles
from ..models import (
    Asset,
    DutyRoster,
    Employee,
    FinanceEntry,
    OfficialDoc,
    Organization,
    QcRecord,
    Secondment,
)

router = APIRouter(prefix="/api/mgmt", tags=["综合管理"], dependencies=[Depends(get_current_user)])

# ---------- ㉚ 人力资源 ----------


class EmployeeCreate(BaseModel):
    org_id: int
    name: str = Field(min_length=1)
    title: str = ""
    position: str = ""


class EmployeeOut(EmployeeCreate):
    id: int
    status: str

    model_config = {"from_attributes": True}


@router.post(
    "/employees",
    response_model=EmployeeOut,
    status_code=201,
    dependencies=[Depends(require_roles("director", "operator"))],  # H2: 人事管理
)
def create_employee(body: EmployeeCreate, db: Session = Depends(get_db)):
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    employee = Employee(**body.model_dump())
    db.add(employee)
    db.commit()
    db.refresh(employee)
    return employee


@router.get("/employees", response_model=list[EmployeeOut])
def list_employees(org_id: int | None = None, db: Session = Depends(get_db)):
    query = db.query(Employee)
    if org_id is not None:
        query = query.filter(Employee.org_id == org_id)
    return query.order_by(Employee.id).limit(500).all()


class SecondmentCreate(BaseModel):
    employee_id: int
    to_org_id: int
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


@router.post(
    "/secondments",
    status_code=201,
    dependencies=[Depends(require_roles("director", "operator"))],  # H2: 人员下派
)
def second_employee(body: SecondmentCreate, db: Session = Depends(get_db)):
    """人员派驻下沉：状态改为派驻中，支撑监测指标4（医师派驻人数）。"""
    employee = db.get(Employee, body.employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="员工不存在")
    if employee.status == "seconded":
        raise HTTPException(status_code=409, detail="该员工已在派驻中")
    if db.get(Organization, body.to_org_id) is None:
        raise HTTPException(status_code=404, detail="派驻机构不存在")
    record = Secondment(
        employee_id=body.employee_id,
        from_org_id=employee.org_id,
        to_org_id=body.to_org_id,
        start_date=body.start_date,
    )
    employee.status = "seconded"
    db.add(record)
    db.commit()
    return {"id": record.id, "employee_id": body.employee_id, "status": "seconded"}


@router.post(
    "/secondments/{secondment_id}/end",
    dependencies=[Depends(require_roles("director", "operator"))],  # H2
)
def end_secondment(secondment_id: int, end_date: str, db: Session = Depends(get_db)):
    record = db.get(Secondment, secondment_id)
    if record is None:
        raise HTTPException(status_code=404, detail="派驻记录不存在")
    if record.end_date:
        raise HTTPException(status_code=409, detail="派驻已结束")
    record.end_date = end_date
    employee = db.get(Employee, record.employee_id)
    if employee:
        employee.status = "active"
    db.commit()
    return {"id": secondment_id, "end_date": end_date}


@router.get("/secondments/stats")
def secondment_stats(db: Session = Depends(get_db)):
    """在派人数（监测指标4的过程数据）。"""
    active = db.query(func.count(Secondment.id)).filter(Secondment.end_date == "").scalar() or 0
    total = db.query(func.count(Secondment.id)).scalar() or 0
    return {"active_secondments": active, "total_secondments": total}


# ---------- ㉛ 财务 ----------


class FinanceCreate(BaseModel):
    org_id: int
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    category: str = Field(pattern="^(income|expense)$")
    item: str = ""
    amount: float = Field(gt=0)


class FinanceOut(FinanceCreate):
    id: int

    model_config = {"from_attributes": True}


@router.post("/finance", response_model=FinanceOut, status_code=201, dependencies=[Depends(require_roles("director", "operator"))])
def add_finance_entry(body: FinanceCreate, db: Session = Depends(get_db)):
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    entry = FinanceEntry(**body.model_dump())
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


@router.get("/finance/summary")
def finance_summary(period: str | None = None, db: Session = Depends(get_db)):
    """集中核算：各成员单位收支结余叠加汇总。"""
    query = db.query(
        FinanceEntry.org_id,
        FinanceEntry.category,
        func.sum(FinanceEntry.amount).label("total"),
    )
    if period:
        query = query.filter(FinanceEntry.period == period)
    rows = query.group_by(FinanceEntry.org_id, FinanceEntry.category).all()
    orgs: dict[int, dict] = {}
    for r in rows:
        entry = orgs.setdefault(r.org_id, {"org_id": r.org_id, "income": 0.0, "expense": 0.0})
        entry[r.category] = round(r.total, 2)
    for entry in orgs.values():
        entry["balance"] = round(entry["income"] - entry["expense"], 2)
    total_income = round(sum(o["income"] for o in orgs.values()), 2)
    total_expense = round(sum(o["expense"] for o in orgs.values()), 2)
    return {
        "period": period or "全部",
        "orgs": sorted(orgs.values(), key=lambda o: o["org_id"]),
        "consolidated": {"income": total_income, "expense": total_expense, "balance": round(total_income - total_expense, 2)},
    }


# ---------- ㉜ 物资 ----------


class AssetCreate(BaseModel):
    org_id: int
    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    category: str = Field(default="office", pattern="^(equipment|office)$")
    quantity: int = Field(default=1, ge=1)


class AssetOut(AssetCreate):
    id: int
    status: str

    model_config = {"from_attributes": True}


@router.post(
    "/assets",
    response_model=AssetOut,
    status_code=201,
    dependencies=[Depends(require_roles("director", "operator"))],  # H2: 资产管理
)
def create_asset(body: AssetCreate, db: Session = Depends(get_db)):
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    if db.query(Asset).filter(Asset.code == body.code).first():
        raise HTTPException(status_code=409, detail="物资编码已存在")
    asset = Asset(**body.model_dump())
    db.add(asset)
    db.commit()
    db.refresh(asset)
    return asset


@router.get("/assets", response_model=list[AssetOut])
def list_assets(org_id: int | None = None, db: Session = Depends(get_db)):
    query = db.query(Asset)
    if org_id is not None:
        query = query.filter(Asset.org_id == org_id)
    return query.order_by(Asset.id).limit(500).all()


@router.post(
    "/assets/{asset_id}/transfer",
    response_model=AssetOut,
    dependencies=[Depends(require_roles("director", "operator"))],  # H2
)
def transfer_asset(asset_id: int, to_org_id: int, db: Session = Depends(get_db)):
    """物资调拨划拨。"""
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="物资不存在")
    if asset.status == "scrapped":
        raise HTTPException(status_code=409, detail="已报废物资不可调拨")
    if db.get(Organization, to_org_id) is None:
        raise HTTPException(status_code=404, detail="调入机构不存在")
    asset.org_id = to_org_id
    db.commit()
    db.refresh(asset)
    return asset


@router.post(
    "/assets/{asset_id}/scrap",
    response_model=AssetOut,
    dependencies=[Depends(require_roles("director", "operator"))],  # H2
)
def scrap_asset(asset_id: int, db: Session = Depends(get_db)):
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="物资不存在")
    asset.status = "scrapped"
    db.commit()
    db.refresh(asset)
    return asset


# ---------- ㉞ 行政公文 ----------


class DocCreate(BaseModel):
    title: str = Field(min_length=1)
    doc_type: str = Field(default="notice", pattern="^(notice|policy|minutes)$")
    body: str = ""
    issuer: str = ""


class DocOut(DocCreate):
    id: int
    status: str

    model_config = {"from_attributes": True}


@router.post("/docs", response_model=DocOut, status_code=201, dependencies=[Depends(require_roles("director", "operator"))])
def create_doc(body: DocCreate, db: Session = Depends(get_db)):
    doc = OfficialDoc(**body.model_dump())
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


@router.post("/docs/{doc_id}/publish", response_model=DocOut, dependencies=[Depends(require_roles("director"))])
def publish_doc(doc_id: int, db: Session = Depends(get_db)):
    doc = db.get(OfficialDoc, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="公文不存在")
    if doc.status != "draft":
        raise HTTPException(status_code=409, detail="仅草稿可发布")
    doc.status = "published"
    db.commit()
    db.refresh(doc)
    return doc


@router.get("/docs", response_model=list[DocOut])
def list_docs(status: str | None = None, db: Session = Depends(get_db)):
    query = db.query(OfficialDoc)
    if status:
        query = query.filter(OfficialDoc.status == status)
    return query.order_by(OfficialDoc.id.desc()).limit(200).all()


# ---------- ①-④ 共享中心排班与质控 ----------

_CENTERS = {"imaging", "ecg", "lab", "pathology"}


class RosterCreate(BaseModel):
    center_type: str
    duty_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    shift: str = "全天"
    doctor_name: str = Field(min_length=1)


class RosterOut(RosterCreate):
    id: int

    model_config = {"from_attributes": True}


@router.post("/rosters", response_model=RosterOut, status_code=201, dependencies=[Depends(require_admin)])
def create_roster(body: RosterCreate, db: Session = Depends(get_db)):
    if body.center_type not in _CENTERS:
        raise HTTPException(status_code=422, detail="未知中心类型")
    roster = DutyRoster(**body.model_dump())
    db.add(roster)
    db.commit()
    db.refresh(roster)
    return roster


@router.get("/rosters", response_model=list[RosterOut])
def list_rosters(center_type: str | None = None, duty_date: str | None = None, db: Session = Depends(get_db)):
    query = db.query(DutyRoster)
    if center_type:
        query = query.filter(DutyRoster.center_type == center_type)
    if duty_date:
        query = query.filter(DutyRoster.duty_date == duty_date)
    return query.order_by(DutyRoster.duty_date, DutyRoster.id).limit(200).all()


class QcCreate(BaseModel):
    center_type: str
    item: str = Field(min_length=1)
    result: str = Field(pattern="^(pass|fail)$")
    note: str = ""
    record_date: str = ""


class QcOut(QcCreate):
    id: int

    model_config = {"from_attributes": True}


@router.post(
    "/qc",
    response_model=QcOut,
    status_code=201,
    dependencies=[Depends(require_roles("director", "doctor"))],  # H2: 质控记录
)
def add_qc(body: QcCreate, db: Session = Depends(get_db)):
    if body.center_type not in _CENTERS:
        raise HTTPException(status_code=422, detail="未知中心类型")
    record = QcRecord(**body.model_dump())
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@router.get("/qc", response_model=list[QcOut])
def list_qc(center_type: str | None = None, result: str | None = None, db: Session = Depends(get_db)):
    query = db.query(QcRecord)
    if center_type:
        query = query.filter(QcRecord.center_type == center_type)
    if result:
        query = query.filter(QcRecord.result == result)
    return query.order_by(QcRecord.id.desc()).limit(200).all()
