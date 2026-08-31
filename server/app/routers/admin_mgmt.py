"""综合管理补齐：㉚人力资源、㉛财务、㉜物资、㉞行政公文，及①-④排班/质控。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..concurrency import add_amount, insert_or_conflict, take_amount, upsert_unique
from ..database import get_db
from ..datetypes import DateStr
from ..visibility import (
    assert_obj_org_writable,
    assert_org_visible,
    assert_org_writable,
    scope_org_list,
    stats_org_ids,
)
from ..deps import get_current_user, require_admin, require_roles, resolve_business_date
from ..models import (
    Asset,
    AssetMovement,
    Budget,
    Department,
    DutyRoster,
    Employee,
    EmployeeChange,
    FinanceEntry,
    OfficialDoc,
    Organization,
    PayrollRecord,
    QcRecord,
    Secondment,
    StaffContract,
    SystemParam,
    User,
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
    dept_id: int | None = None

    model_config = {"from_attributes": True}


@router.post(
    "/employees",
    response_model=EmployeeOut,
    status_code=201,
    dependencies=[Depends(require_roles("director", "operator"))],  # H2: 人事管理
)
def create_employee(body: EmployeeCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    employee = Employee(**body.model_dump())
    db.add(employee)
    db.commit()
    db.refresh(employee)
    return employee


@router.get("/employees", response_model=list[EmployeeOut])
def list_employees(org_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user),):
    query = db.query(Employee)
    query = scope_org_list(db, user, query, Employee, org_id)
    return query.order_by(Employee.id).limit(500).all()


class SecondmentCreate(BaseModel):
    employee_id: int
    to_org_id: int
    start_date: DateStr


class SecondmentReceiptOut(BaseModel):
    """派驻回执。status 恒为字面量 seconded（发起即在派）。"""

    id: int
    employee_id: int
    status: str


class SecondmentEndOut(BaseModel):
    id: int
    end_date: str


class SecondmentStatsOut(BaseModel):
    active_secondments: int
    total_secondments: int


@router.post(
    "/secondments",
    response_model=SecondmentReceiptOut,
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
    response_model=SecondmentEndOut,
    dependencies=[Depends(require_roles("director", "operator"))],  # H2
)
def end_secondment(
    secondment_id: int,
    end_date: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    record = db.get(Secondment, secondment_id)
    if record is None:
        raise HTTPException(status_code=404, detail="派驻记录不存在")
    # 派驻由原单位发起，也由原单位结束（from_org 是派出方）
    assert_obj_org_writable(db, user, record, org_attr="from_org_id")
    if record.end_date:
        raise HTTPException(status_code=409, detail="派驻已结束")
    record.end_date = end_date
    employee = db.get(Employee, record.employee_id)
    if employee:
        employee.status = "active"
    db.commit()
    return {"id": secondment_id, "end_date": end_date}


@router.get("/secondments/stats", response_model=SecondmentStatsOut)
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
def add_finance_entry(body: FinanceCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    entry = FinanceEntry(**body.model_dump())
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


class FinanceOrgSummaryOut(BaseModel):
    """集中核算单机构行。Money 列 SUM 后 round：整数金额是 int、带小数才是
    float——`int | float` 原样透传（docs/接口标准与治理.md 陷阱一）；
    未记账的一侧保持初始字面量 0.0。"""

    org_id: int
    income: int | float
    expense: int | float
    balance: int | float


class FinanceConsolidatedOut(BaseModel):
    """全区叠加汇总。全空分支是 round(sum([]), 2) = 0（int）。"""

    income: int | float
    expense: int | float
    balance: int | float


class FinanceSummaryOut(BaseModel):
    period: str
    orgs: list[FinanceOrgSummaryOut]
    consolidated: FinanceConsolidatedOut


@router.get("/finance/summary", response_model=FinanceSummaryOut)
def finance_summary(
    period: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """集中核算：各成员单位收支结余叠加汇总。

    第九轮：按**统计口径**过滤（本机构 + 同医共体成员）。财务是横向隔离里
    最敏感的一类——乙院的院长没有理由看到甲院的收支明细；但牵头医院要看得到
    片区汇总，这正是"集中核算"的业务含义，所以用 stats 范围而不是本机构。
    """
    query = db.query(
        FinanceEntry.org_id,
        FinanceEntry.category,
        func.sum(FinanceEntry.amount).label("total"),
    )
    allowed = stats_org_ids(db, user)
    if allowed is not None:
        query = query.filter(FinanceEntry.org_id.in_(allowed))
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
    """D-10：出参不能直接继承入参的校验。

    `AssetCreate.quantity` 是 `ge=1`——建档时不该建一条 0 件的物资，这没错。
    但出参继承了这条约束，于是物资一旦全部出库（现存量 0），整张物资清单
    **连带 500**：一件东西领完了，别的物资也跟着看不见了。
    出参放宽到 `ge=0`：现存量为 0 是完全正常的业务状态。

    与 D-8（医废清单套着阶段五的旧出参、看不到追溯码）同因——
    出参与入参形状相近就顺手继承，而两者的约束本来就不是一回事。
    """

    id: int
    status: str
    quantity: int = Field(ge=0)

    model_config = {"from_attributes": True}


@router.post(
    "/assets",
    response_model=AssetOut,
    status_code=201,
    dependencies=[Depends(require_roles("director", "operator"))],  # H2: 资产管理
)
def create_asset(body: AssetCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    if db.query(Asset).filter(Asset.code == body.code).first():
        raise HTTPException(status_code=409, detail="物资编码已存在")
    asset = insert_or_conflict(db, Asset(**body.model_dump()), "物资编码已存在")
    return asset


@router.get("/assets", response_model=list[AssetOut])
def list_assets(org_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user),):
    query = db.query(Asset)
    query = scope_org_list(db, user, query, Asset, org_id)
    return query.order_by(Asset.id).limit(500).all()


@router.post(
    "/assets/{asset_id}/transfer",
    response_model=AssetOut,
    dependencies=[Depends(require_roles("director", "operator"))],  # H2
)
def transfer_asset(
    asset_id: int,
    to_org_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """物资调拨划拨。"""
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="物资不存在")
    # 调出方必须是本机构：不能把别家的东西划走
    assert_obj_org_writable(db, user, asset)
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
def scrap_asset(
    asset_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="物资不存在")
    assert_obj_org_writable(db, user, asset)
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
    duty_date: DateStr
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


# ---------- 终审轮：科室信息基础库（浙#9） ----------


class DeptCreate(BaseModel):
    org_id: int
    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    category: str = Field(default="clinical", pattern="^(clinical|medtech|admin)$")


class DeptReceiptOut(BaseModel):
    id: int
    org_id: int
    code: str
    name: str


class DeptRowOut(DeptReceiptOut):
    """清单行 = 建档回执四键 + category（前四键键序一致，故可继承）。"""

    category: str


class DeptAssignOut(BaseModel):
    employee_id: int
    dept_id: int


@router.post(
    "/departments",
    response_model=DeptReceiptOut,
    status_code=201,
    dependencies=[Depends(require_roles("director", "operator"))],  # 科室建档
)
def create_department(body: DeptCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    if (
        db.query(Department)
        .filter(Department.org_id == body.org_id, Department.code == body.code)
        .first()
    ):
        raise HTTPException(status_code=409, detail="该机构下科室编码已存在")
    dept = insert_or_conflict(db, Department(**body.model_dump()), "该机构下科室编码已存在")
    return {"id": dept.id, "org_id": dept.org_id, "code": dept.code, "name": dept.name}


@router.get("/departments", response_model=list[DeptRowOut])
def list_departments(org_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user),):
    q = db.query(Department).filter(Department.active.is_(True))
    q = scope_org_list(db, user, q, Department, org_id)
    return [
        {"id": d.id, "org_id": d.org_id, "code": d.code, "name": d.name, "category": d.category}
        for d in q.order_by(Department.org_id, Department.code).all()
    ]


@router.post(
    "/employees/{employee_id}/department",
    response_model=DeptAssignOut,
    dependencies=[Depends(require_roles("director", "operator"))],  # 员工科室挂接
)
def assign_department(employee_id: int, dept_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="员工不存在")
    assert_obj_org_writable(db, user, employee)
    dept = db.get(Department, dept_id)
    if dept is None or not dept.active:
        raise HTTPException(status_code=404, detail="科室不存在或已停用")
    if dept.org_id != employee.org_id:
        raise HTTPException(status_code=422, detail="科室与员工不属于同一机构")
    employee.dept_id = dept_id
    db.commit()
    return {"employee_id": employee_id, "dept_id": dept_id}


# ---------- 终审轮：人员变动管理（㉚） ----------


class ChangeCreate(BaseModel):
    change_type: str = Field(pattern="^(hire|regularize|transfer|leave)$")
    to_org_id: int | None = None
    detail: str = ""
    effective_date: str = ""


class EmployeeChangeReceiptOut(BaseModel):
    """变动回执：回显员工联动后的状态与所属机构。"""

    id: int
    employee_id: int
    change_type: str
    employee_status: str
    employee_org_id: int


class EmployeeChangeRowOut(BaseModel):
    id: int
    change_type: str
    to_org_id: int | None
    detail: str
    effective_date: str


@router.post(
    "/employees/{employee_id}/changes",
    response_model=EmployeeChangeReceiptOut,
    status_code=201,
    dependencies=[Depends(require_roles("director", "operator"))],  # 人员变动
)
def create_employee_change(
    employee_id: int,
    body: ChangeCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="员工不存在")
    assert_obj_org_writable(db, user, employee)
    if body.change_type == "transfer":
        if body.to_org_id is None:
            raise HTTPException(status_code=422, detail="调动须指定调入机构")
        if db.get(Organization, body.to_org_id) is None:
            raise HTTPException(status_code=404, detail="调入机构不存在")
        employee.org_id = body.to_org_id
        employee.dept_id = None  # 跨机构调动后科室待重新挂接
    elif body.change_type == "leave":
        employee.status = "left"
    elif body.change_type == "hire":
        employee.status = "active"
    change = EmployeeChange(employee_id=employee_id, created_by=user.id, **body.model_dump())
    db.add(change)
    db.commit()
    return {
        "id": change.id,
        "employee_id": employee_id,
        "change_type": change.change_type,
        "employee_status": employee.status,
        "employee_org_id": employee.org_id,
    }


@router.get("/employees/{employee_id}/changes", response_model=list[EmployeeChangeRowOut])
def list_employee_changes(employee_id: int, db: Session = Depends(get_db)):
    if db.get(Employee, employee_id) is None:
        raise HTTPException(status_code=404, detail="员工不存在")
    return [
        {
            "id": c.id,
            "change_type": c.change_type,
            "to_org_id": c.to_org_id,
            "detail": c.detail,
            "effective_date": c.effective_date,
        }
        for c in db.query(EmployeeChange)
        .filter(EmployeeChange.employee_id == employee_id)
        .order_by(EmployeeChange.id)
        .all()
    ]


# ---------- 终审轮：合同管理（㉚） ----------


class ContractCreate(BaseModel):
    employee_id: int
    contract_no: str = Field(min_length=1)
    start_date: DateStr
    end_date: DateStr


class ContractReceiptOut(BaseModel):
    id: int
    contract_no: str
    status: str


class ContractExpiringRowOut(BaseModel):
    """到期提醒行（4 键）——与 6 键清单行不同形，两个模型不许互相注入。"""

    id: int
    employee_id: int
    contract_no: str
    end_date: str


class ContractRowOut(BaseModel):
    id: int
    employee_id: int
    contract_no: str
    start_date: str
    end_date: str
    status: str


@router.post(
    "/staff-contracts",
    response_model=ContractReceiptOut,
    status_code=201,
    dependencies=[Depends(require_roles("director", "operator"))],  # 合同管理
)
def create_staff_contract(body: ContractCreate, db: Session = Depends(get_db)):
    if db.get(Employee, body.employee_id) is None:
        raise HTTPException(status_code=404, detail="员工不存在")
    if body.end_date <= body.start_date:
        raise HTTPException(status_code=422, detail="合同止期须晚于起期")
    if db.query(StaffContract).filter(StaffContract.contract_no == body.contract_no).first():
        raise HTTPException(status_code=409, detail="合同编号已存在")
    contract = insert_or_conflict(db, StaffContract(**body.model_dump()), "合同编号已存在")
    return {"id": contract.id, "contract_no": contract.contract_no, "status": contract.status}


@router.get("/staff-contracts/expiring", response_model=list[ContractExpiringRowOut])
def expiring_contracts(days: int = 60, today: str | None = None, db: Session = Depends(get_db)):
    """合同到期提醒：end_date 距今 ≤days 的履行中合同（续签管理）。"""
    from datetime import timedelta

    current = resolve_business_date(today)
    deadline = (current + timedelta(days=days)).isoformat()
    return [
        {
            "id": c.id,
            "employee_id": c.employee_id,
            "contract_no": c.contract_no,
            "end_date": c.end_date,
        }
        for c in db.query(StaffContract)
        .filter(StaffContract.status == "active", StaffContract.end_date <= deadline)
        .order_by(StaffContract.end_date)
        .all()
    ]


@router.get("/staff-contracts", response_model=list[ContractRowOut])
def list_staff_contracts(employee_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(StaffContract)
    if employee_id is not None:
        q = q.filter(StaffContract.employee_id == employee_id)
    return [
        {
            "id": c.id,
            "employee_id": c.employee_id,
            "contract_no": c.contract_no,
            "start_date": c.start_date,
            "end_date": c.end_date,
            "status": c.status,
        }
        for c in q.order_by(StaffContract.id.desc()).limit(200).all()
    ]


# ---------- 终审轮：薪酬福利管理（㉚）/ 绩效薪酬分配（㉟） ----------


class PayrollCreate(BaseModel):
    employee_id: int
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    base_salary: float = Field(ge=0)
    perf_bonus: float = Field(default=0, ge=0)
    perf_coefficient: float = Field(default=1.0, ge=0, le=2)


class PayrollReceiptOut(BaseModel):
    """薪酬回执。total 是 Money 列且经 db.refresh 读回：整数薪酬是 int
    （6000 不是 6000.0），带小数才是 float——`int | float` 原样透传。"""

    id: int
    period: str
    total: int | float


class PayrollRowOut(BaseModel):
    """perf_coefficient 是 Float 列：整数系数读回来就是 1.0，声明 `float`
    才是原样——与同一行的 Money 列相反，判据是列类型不是字段名。"""

    id: int
    employee_id: int
    period: str
    base_salary: int | float
    perf_bonus: int | float
    perf_coefficient: float
    total: int | float


class PayrollListOut(BaseModel):
    #: 空清单分支 round(sum([]), 2) 是 int 0
    total_amount: int | float
    records: list[PayrollRowOut]


@router.post(
    "/payroll",
    response_model=PayrollReceiptOut,
    status_code=201,
    dependencies=[Depends(require_roles("director"))],  # 薪酬发放=管理层
)
def create_payroll(body: PayrollCreate, db: Session = Depends(get_db)):
    if db.get(Employee, body.employee_id) is None:
        raise HTTPException(status_code=404, detail="员工不存在")
    if (
        db.query(PayrollRecord)
        .filter(
            PayrollRecord.employee_id == body.employee_id, PayrollRecord.period == body.period
        )
        .first()
    ):
        raise HTTPException(status_code=409, detail="该员工本期薪酬已录入")
    total = round(body.base_salary + body.perf_bonus * body.perf_coefficient, 2)
    record = insert_or_conflict(db, PayrollRecord(**body.model_dump(), total=total), "该员工本期薪酬已录入")
    return {"id": record.id, "period": record.period, "total": record.total}


@router.get("/payroll", response_model=PayrollListOut, dependencies=[Depends(require_roles("director"))])
def list_payroll(period: str | None = None, employee_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(PayrollRecord)
    if period:
        q = q.filter(PayrollRecord.period == period)
    if employee_id is not None:
        q = q.filter(PayrollRecord.employee_id == employee_id)
    records = q.order_by(PayrollRecord.id.desc()).limit(500).all()
    return {
        "total_amount": round(sum(r.total for r in records), 2),
        "records": [
            {
                "id": r.id,
                "employee_id": r.employee_id,
                "period": r.period,
                "base_salary": r.base_salary,
                "perf_bonus": r.perf_bonus,
                "perf_coefficient": r.perf_coefficient,
                "total": r.total,
            }
            for r in records
        ],
    }


# ---------- 终审轮：预算管理（㉛） ----------


class BudgetCreate(BaseModel):
    org_id: int
    year: str = Field(pattern=r"^\d{4}$")
    category: str = Field(pattern="^(income|expense)$")
    amount: float = Field(gt=0)


class BudgetReceiptOut(BaseModel):
    #: amount 是 Money 列且经 db.refresh 读回：整数预算是 int，带小数才是 float
    id: int
    amount: int | float
    adjusted: bool


class BudgetSideOut(BaseModel):
    """预算执行单侧。execution_pct 是**值可空**的恒在键：无预算时值为 null
    （不是键消失），声明 `float | None` 即可，无需 exclude_unset。"""

    budget: int | float
    actual: int | float
    execution_pct: float | None


class BudgetExecutionOut(BaseModel):
    org_id: int
    year: str
    income: BudgetSideOut
    expense: BudgetSideOut


@router.post(
    "/budgets",
    response_model=BudgetReceiptOut,
    status_code=201,
    dependencies=[Depends(require_roles("director"))],  # 预算编制=管理层
)
def create_budget(body: BudgetCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    # 预算调整：同机构同年同类别覆盖并留原记录时间
    budget, adjusted = upsert_unique(
        db,
        Budget,
        keys={"org_id": body.org_id, "year": body.year, "category": body.category},
        values={"amount": body.amount},
    )
    return {"id": budget.id, "amount": budget.amount, "adjusted": adjusted}


@router.get("/budgets/execution", response_model=BudgetExecutionOut)
def budget_execution(
    org_id: int,
    year: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """预算执行对比：预算数 vs 财务收支实际数（执行率）。"""
    assert_org_visible(db, user, org_id)
    budgets = {
        b.category: b.amount
        for b in db.query(Budget).filter(Budget.org_id == org_id, Budget.year == year).all()
    }
    result = {}
    for category in ("income", "expense"):
        actual = (
            db.query(func.coalesce(func.sum(FinanceEntry.amount), 0.0))
            .filter(
                FinanceEntry.org_id == org_id,
                FinanceEntry.category == category,
                FinanceEntry.period.like(f"{year}-%"),
            )
            .scalar()
        )
        budget_amount = budgets.get(category, 0.0)
        result[category] = {
            "budget": budget_amount,
            "actual": round(actual, 2),
            "execution_pct": round(actual * 100.0 / budget_amount, 2) if budget_amount else None,
        }
    return {"org_id": org_id, "year": year, **result}


# ---------- 终审轮：物资出入库管理（㉜） ----------


class MovementCreate(BaseModel):
    movement_type: str = Field(pattern="^(inbound|issue|return|scrap)$")
    quantity: int = Field(gt=0)
    note: str = ""


class MovementReceiptOut(BaseModel):
    """出入库回执：回显原子增减后的现存量与物资状态。"""

    id: int
    asset_id: int
    movement_type: str
    asset_quantity: int
    asset_status: str


class MovementRowOut(BaseModel):
    #: at 为 created_at.isoformat() 字符串
    id: int
    movement_type: str
    quantity: int
    note: str
    at: str


@router.post(
    "/assets/{asset_id}/movements",
    response_model=MovementReceiptOut,
    status_code=201,
    dependencies=[Depends(require_roles("director", "operator"))],  # 物资出入库
)
def create_asset_movement(
    asset_id: int,
    body: MovementCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    asset = db.get(Asset, asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="物资不存在")
    # 按 id 直取绕过了清单过滤：实测乙院经办据此领走了甲院 5 台 CT
    assert_obj_org_writable(db, user, asset)
    if asset.status == "scrapped":
        raise HTTPException(status_code=409, detail="已报废物资不可再出入库")
    # 判定与增减都放进同一条 UPDATE。原先"先判够不够、再 += / -="是读-改-写，
    # 并发下两个出库都判定够、各自算出同一个新值，最后只剩一笔——账实不符。
    if body.movement_type in ("inbound", "return"):
        add_amount(db, Asset, asset_id, "quantity", body.quantity)
    elif not take_amount(db, Asset, asset_id, "quantity", body.quantity):
        db.rollback()
        raise HTTPException(status_code=409, detail="出库数量超过现存量")
    movement = AssetMovement(asset_id=asset_id, created_by=user.id, **body.model_dump())
    db.add(movement)
    db.commit()
    db.refresh(asset)  # 上面走的是 Core UPDATE，会话里的 asset 还是旧值
    if body.movement_type == "scrap" and asset.quantity == 0:
        asset.status = "scrapped"
        db.commit()
    return {
        "id": movement.id,
        "asset_id": asset_id,
        "movement_type": movement.movement_type,
        "asset_quantity": asset.quantity,
        "asset_status": asset.status,
    }


@router.get("/assets/{asset_id}/movements", response_model=list[MovementRowOut])
def list_asset_movements(asset_id: int, db: Session = Depends(get_db)):
    if db.get(Asset, asset_id) is None:
        raise HTTPException(status_code=404, detail="物资不存在")
    return [
        {
            "id": m.id,
            "movement_type": m.movement_type,
            "quantity": m.quantity,
            "note": m.note,
            "at": m.created_at.isoformat(),
        }
        for m in db.query(AssetMovement)
        .filter(AssetMovement.asset_id == asset_id)
        .order_by(AssetMovement.id)
        .all()
    ]


# ---------- 终审轮：系统参数配置（浙#45） ----------


class ParamUpsert(BaseModel):
    key: str = Field(min_length=1, max_length=64)
    value: str = Field(max_length=256)
    description: str = ""


class ParamReceiptOut(BaseModel):
    key: str
    value: str


class ParamRowOut(BaseModel):
    key: str
    value: str
    description: str
    #: updated_at 为 isoformat() 字符串
    updated_at: str


@router.post("/params", response_model=ParamReceiptOut, dependencies=[Depends(require_admin)])
def upsert_param(body: ParamUpsert, db: Session = Depends(get_db)):
    values = {"value": body.value}
    if body.description:
        # 描述留空表示"不改"，不是"改成空"——参数说明是人写的，别被一次改值抹掉
        values["description"] = body.description
    param, _ = upsert_unique(db, SystemParam, {"key": body.key}, values)
    return {"key": param.key, "value": param.value}


@router.get("/params", response_model=list[ParamRowOut], dependencies=[Depends(require_admin)])
def list_params(db: Session = Depends(get_db)):
    return [
        {
            "key": p.key,
            "value": p.value,
            "description": p.description,
            "updated_at": p.updated_at.isoformat(),
        }
        for p in db.query(SystemParam).order_by(SystemParam.key).all()
    ]
