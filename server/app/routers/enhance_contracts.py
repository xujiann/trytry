"""E1 家医签约内容化：服务包目录、履约率、签约费按人头分配。

在 `routers/contracts.py`（签约/解约/履约流水）之上补齐"内容化 + 考核 + 分配"：
- 服务包目录（管理员）：ServicePackage + ServicePackageItem，签约挂 package_id。
- 履约闭环：由包条目派生"应履约次数"，ContractService.item_id 核销到条目，算履约率。
- 签约费池（管理层）：按有效签约人头计提，年终按 人头×履约率×重点人群系数 分配，
  复用 allocation.hamilton_amounts 精确到分（与 fund.py 结余分配同口径）。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..allocation import hamilton_amounts
from ..concurrency import insert_or_conflict
from ..database import get_db
from ..deps import get_current_user, paginate, require_roles
from ..models import (
    ContractService,
    FamilyDoctorContract,
    Organization,
    OrgGroup,
    OrgGroupMember,
    ServicePackage,
    ServicePackageItem,
    SignFeeDistribution,
    SignFeePool,
    User,
)
from ..visibility import (
    assert_obj_org_visible,
    assert_obj_org_writable,
    assert_org_writable,
    visible_org_ids,
)

router = APIRouter(prefix="/api", tags=["家医签约内容化"], dependencies=[Depends(get_current_user)])


# ---------------------------------------------------------------- schemas
class PackageIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    annual_fee: float = Field(default=0, ge=0)
    target_population: str = ""
    description: str = ""


class PackageItemIn(BaseModel):
    service_type: str = Field(pattern="^(visit|consult|followup|referral|exam|other)$")
    name: str = Field(min_length=1, max_length=64)
    annual_times: int = Field(default=0, ge=0)
    billable: bool = False


class PackageBindIn(BaseModel):
    package_id: int | None = None
    key_population: str = Field(default="", max_length=32)
    expire_date: str = ""


class FulfillIn(BaseModel):
    item_id: int
    note: str = ""


class PoolIn(BaseModel):
    year: str = Field(pattern=r"^\d{4}$")
    org_group_id: int | None = None
    per_capita: float = Field(default=0, ge=0)
    # 直接核定总额；给 0 则按 per_capita×有效人头自动计提
    total_amount: float = Field(default=0, ge=0)
    note: str = ""


# ---------------------------------------------------------------- 服务包目录（管理员）
@router.post("/service-packages", status_code=201, dependencies=[Depends(require_roles("admin"))])
def create_package(body: PackageIn, db: Session = Depends(get_db)):
    pkg = insert_or_conflict(db, ServicePackage(**body.model_dump()), "服务包编码已存在")
    return _package_out(pkg)


@router.get("/service-packages")
def list_packages(active: bool | None = None, db: Session = Depends(get_db)):
    q = db.query(ServicePackage)
    if active is not None:
        q = q.filter(ServicePackage.active.is_(active))
    return [_package_out(p) for p in q.order_by(ServicePackage.id).all()]


@router.patch("/service-packages/{package_id}", dependencies=[Depends(require_roles("admin"))])
def update_package(package_id: int, active: bool | None = None, annual_fee: float | None = None,
                   db: Session = Depends(get_db)):
    pkg = db.get(ServicePackage, package_id)
    if pkg is None:
        raise HTTPException(status_code=404, detail="服务包不存在")
    if active is not None:
        pkg.active = active
    if annual_fee is not None:
        if annual_fee < 0:
            raise HTTPException(status_code=422, detail="年签约费不能为负")
        pkg.annual_fee = annual_fee
    db.commit()
    return _package_out(pkg)


@router.post("/service-packages/{package_id}/items", status_code=201,
             dependencies=[Depends(require_roles("admin"))])
def add_item(package_id: int, body: PackageItemIn, db: Session = Depends(get_db)):
    if db.get(ServicePackage, package_id) is None:
        raise HTTPException(status_code=404, detail="服务包不存在")
    item = ServicePackageItem(package_id=package_id, **body.model_dump())
    db.add(item)
    db.commit()
    db.refresh(item)
    return _item_out(item)


def _item_out(i: ServicePackageItem) -> dict:
    return {"id": i.id, "package_id": i.package_id, "service_type": i.service_type,
            "name": i.name, "annual_times": i.annual_times, "billable": i.billable}


def _package_out(p: ServicePackage) -> dict:
    return {"id": p.id, "code": p.code, "name": p.name, "annual_fee": p.annual_fee,
            "target_population": p.target_population, "description": p.description,
            "active": p.active, "items": [_item_out(i) for i in p.items]}


# ---------------------------------------------------------------- 签约绑定包 + 履约
@router.patch("/contracts/{contract_id}/package",
              dependencies=[Depends(require_roles("doctor", "public_health"))])
def bind_package(contract_id: int, body: PackageBindIn, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """把签约挂到内容化服务包，并可设重点人群标签与到期日。"""
    c = db.get(FamilyDoctorContract, contract_id)
    if c is None:
        raise HTTPException(status_code=404, detail="签约不存在")
    assert_obj_org_writable(db, user, c)
    if body.package_id is not None:
        pkg = db.get(ServicePackage, body.package_id)
        if pkg is None or not pkg.active:
            raise HTTPException(status_code=422, detail="服务包不存在或已停用")
        c.package_id = body.package_id
    c.key_population = body.key_population
    c.expire_date = body.expire_date
    db.commit()
    return {"id": c.id, "package_id": c.package_id, "key_population": c.key_population,
            "expire_date": c.expire_date}


@router.post("/contracts/{contract_id}/fulfill", status_code=201,
             dependencies=[Depends(require_roles("doctor", "public_health"))])
def fulfill_item(contract_id: int, body: FulfillIn, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """按服务包条目核销一次履约（写入 ContractService.item_id）。"""
    c = db.get(FamilyDoctorContract, contract_id)
    if c is None:
        raise HTTPException(status_code=404, detail="签约不存在")
    assert_obj_org_writable(db, user, c)
    item = db.get(ServicePackageItem, body.item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="服务包条目不存在")
    if c.package_id is not None and item.package_id != c.package_id:
        raise HTTPException(status_code=422, detail="该条目不属于本签约所挂服务包")
    svc = ContractService(contract_id=contract_id, service_type=item.service_type,
                          item_id=item.id, note=body.note)
    db.add(svc)
    db.commit()
    db.refresh(svc)
    return {"id": svc.id, "contract_id": contract_id, "item_id": item.id,
            "service_type": svc.service_type}


@router.get("/contracts/{contract_id}/fulfillment")
def contract_fulfillment(contract_id: int, db: Session = Depends(get_db),
                         user: User = Depends(get_current_user)):
    """应履约 vs 已履约 → 履约率（按服务包条目逐项对比）。"""
    c = db.get(FamilyDoctorContract, contract_id)
    if c is None:
        raise HTTPException(status_code=404, detail="签约不存在")
    assert_obj_org_visible(db, user, c)
    return _fulfillment(db, c)


def _fulfillment(db: Session, c: FamilyDoctorContract) -> dict:
    """计算单份签约的履约明细与总履约率。

    履约率 = Σ min(已履约, 应履约) / Σ 应履约（封顶到应履约，超额不虚增分子）。
    未挂服务包（package_id 为空）无应履约基准，履约率记 None。
    """
    done_by_item: dict[int, int] = {}
    for s in db.query(ContractService).filter(ContractService.contract_id == c.id).all():
        if s.item_id is not None:
            done_by_item[s.item_id] = done_by_item.get(s.item_id, 0) + 1
    items = []
    total_expected = 0
    total_done_capped = 0
    if c.package_id is not None:
        pkg_items = db.query(ServicePackageItem).filter(
            ServicePackageItem.package_id == c.package_id
        ).all()
        for it in pkg_items:
            done = done_by_item.get(it.id, 0)
            expected = it.annual_times
            capped = min(done, expected) if expected > 0 else 0
            total_expected += expected
            total_done_capped += capped
            items.append({"item_id": it.id, "name": it.name, "service_type": it.service_type,
                          "expected": expected, "done": done})
    rate = round(total_done_capped / total_expected, 4) if total_expected > 0 else None
    return {"contract_id": c.id, "package_id": c.package_id, "org_id": c.org_id,
            "expected_total": total_expected, "done_total": sum(done_by_item.values()),
            "fulfillment_rate": rate, "items": items}


@router.get("/contracts/expiring")
def expiring_contracts(before: str, db: Session = Depends(get_db),
                       user: User = Depends(get_current_user), response=None):
    """到期（expire_date ≤ before 且仍 active）签约清单，用于续签提醒。"""
    q = db.query(FamilyDoctorContract).filter(
        FamilyDoctorContract.status == "active",
        FamilyDoctorContract.expire_date != "",
        FamilyDoctorContract.expire_date <= before,
    )
    vis = visible_org_ids(db, user)
    if vis is not None:
        q = q.filter(FamilyDoctorContract.org_id.in_(vis or [-1]))
    rows = q.order_by(FamilyDoctorContract.expire_date).all()
    return [{"id": c.id, "patient_id": c.patient_id, "org_id": c.org_id,
             "doctor_name": c.doctor_name, "expire_date": c.expire_date,
             "key_population": c.key_population} for c in rows]


# ---------------------------------------------------------------- 签约费池（管理层）
def _group_org_ids(db: Session, group_id: int | None) -> list[int] | None:
    if group_id is None:
        return None
    if db.get(OrgGroup, group_id) is None:
        raise HTTPException(status_code=404, detail="机构分组不存在")
    return [m.org_id for m in db.query(OrgGroupMember).filter(
        OrgGroupMember.group_id == group_id).all()]


@router.post("/sign-fee/pools", status_code=201, dependencies=[Depends(require_roles("director"))])
def create_pool(body: PoolIn, db: Session = Depends(get_db)):
    # 全域池（org_group_id 为空）：DB 唯一约束把 NULL 视作互异不去重，补代码级预检拦重复
    if body.org_group_id is None and db.query(SignFeePool.id).filter(
        SignFeePool.year == body.year, SignFeePool.org_group_id.is_(None)
    ).first():
        raise HTTPException(status_code=409, detail="该年度的全域签约费池已存在")
    total = body.total_amount
    if total <= 0:
        # 按 per_capita × 范围内有效签约人头自动计提
        head_q = db.query(FamilyDoctorContract).filter(FamilyDoctorContract.status == "active")
        gids = _group_org_ids(db, body.org_group_id)
        if gids is not None:
            head_q = head_q.filter(FamilyDoctorContract.org_id.in_(gids or [-1]))
        total = round(body.per_capita * head_q.count(), 2)
    pool = SignFeePool(year=body.year, org_group_id=body.org_group_id,
                       per_capita=body.per_capita, total_amount=total, note=body.note)
    return _pool_created(db, pool)


def _pool_created(db: Session, pool: SignFeePool) -> dict:
    saved = insert_or_conflict(db, pool, "该年度/分组的签约费池已存在")
    return _pool_out(db, saved)


@router.get("/sign-fee/pools", dependencies=[Depends(require_roles("director"))])
def list_pools(year: str | None = None, db: Session = Depends(get_db)):
    q = db.query(SignFeePool)
    if year:
        q = q.filter(SignFeePool.year == year)
    return [_pool_out(db, p) for p in q.order_by(SignFeePool.id.desc()).all()]


@router.post("/sign-fee/pools/{pool_id}/distribute",
             dependencies=[Depends(require_roles("director"))])
def distribute_pool(pool_id: int, db: Session = Depends(get_db)):
    """按 人头×履约率×重点人群系数 加权，把池子总额精确分配到各机构。

    重点人群系数 = 1 + 0.5×(重点人群签约占比)，鼓励覆盖重点人群但不喧宾夺主。
    履约率为 None（未挂包）的签约按 1.0 计（不因缺基准而受罚）。
    """
    pool = db.get(SignFeePool, pool_id)
    if pool is None:
        raise HTTPException(status_code=404, detail="签约费池不存在")
    if pool.total_amount <= 0:
        raise HTTPException(status_code=409, detail="池子总额为 0，无可分配金额")

    gids = _group_org_ids(db, pool.org_group_id)
    contract_q = db.query(FamilyDoctorContract).filter(FamilyDoctorContract.status == "active")
    if gids is not None:
        contract_q = contract_q.filter(FamilyDoctorContract.org_id.in_(gids or [-1]))
    contracts = contract_q.all()
    if not contracts:
        raise HTTPException(status_code=409, detail="范围内没有有效签约，无法分配")

    # 按机构聚合：人头、平均履约率、重点人群占比
    agg: dict[int, dict] = {}
    for c in contracts:
        a = agg.setdefault(c.org_id, {"head": 0, "rate_sum": 0.0, "key": 0})
        a["head"] += 1
        r = _fulfillment(db, c)["fulfillment_rate"]
        a["rate_sum"] += 1.0 if r is None else r
        if c.key_population:
            a["key"] += 1

    org_ids = sorted(agg)
    rows = []
    for oid in org_ids:
        a = agg[oid]
        avg_rate = a["rate_sum"] / a["head"] if a["head"] else 0.0
        key_factor = 1.0 + 0.5 * (a["key"] / a["head"] if a["head"] else 0.0)
        weight = a["head"] * avg_rate * key_factor
        rows.append({"org_id": oid, "head": a["head"], "rate": round(avg_rate, 4),
                     "key_factor": round(key_factor, 4), "weight": weight})

    wsum = sum(r["weight"] for r in rows)
    if wsum <= 0:
        # 全域权重为 0（常见于"已挂服务包但尚无履约记录"，avg_rate 全 0）。
        # 此时 hamilton 会把每户分到 0、总额凭空蒸发——与 fund.py 同口径显式拒绝，
        # 不静默吞钱。让操作方先录履约，或对这些签约暂不挂包（履约率按 1.0 计入人头）。
        raise HTTPException(
            status_code=409,
            detail="范围内所有机构的分配权重为 0（通常是已挂服务包但尚无履约记录），"
                   "无法按履约加权分配；请先录入履约，或对这些签约暂不挂服务包（按人头计）",
        )
    amounts = hamilton_amounts(pool.total_amount, [r["weight"] for r in rows])

    # 覆盖式重算：可重分配
    db.query(SignFeeDistribution).filter(
        SignFeeDistribution.pool_id == pool.id).delete(synchronize_session=False)
    out = []
    for r, amt in zip(rows, amounts):
        dist = SignFeeDistribution(
            pool_id=pool.id, org_id=r["org_id"], headcount=r["head"],
            fulfillment_rate=r["rate"], key_pop_factor=r["key_factor"],
            weight=round(r["weight"], 6), share_pct=round(r["weight"] / wsum * 100, 4),
            amount=amt)
        db.add(dist)
        out.append(dist)
    pool.status = "distributed"
    db.commit()
    return _pool_out(db, pool)


@router.get("/sign-fee/pools/{pool_id}/distributions")
def list_distributions(pool_id: int, db: Session = Depends(get_db)):
    if db.get(SignFeePool, pool_id) is None:
        raise HTTPException(status_code=404, detail="签约费池不存在")
    rows = db.query(SignFeeDistribution).filter(
        SignFeeDistribution.pool_id == pool_id).order_by(SignFeeDistribution.org_id).all()
    return [_dist_out(d) for d in rows]


def _dist_out(d: SignFeeDistribution) -> dict:
    return {"id": d.id, "pool_id": d.pool_id, "org_id": d.org_id, "headcount": d.headcount,
            "fulfillment_rate": d.fulfillment_rate, "key_pop_factor": d.key_pop_factor,
            "weight": d.weight, "share_pct": d.share_pct, "amount": d.amount}


def _pool_out(db: Session, p: SignFeePool) -> dict:
    dist = db.query(SignFeeDistribution).filter(SignFeeDistribution.pool_id == p.id).count()
    return {"id": p.id, "year": p.year, "org_group_id": p.org_group_id,
            "per_capita": p.per_capita, "total_amount": p.total_amount, "status": p.status,
            "note": p.note, "distribution_count": dist}
