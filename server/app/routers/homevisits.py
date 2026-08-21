"""上门服务调度（送医送护上门）：申请 → 派单 → 完成，关联家医签约。

ADR-0006：原先住在 `gapfill.py` 这个倾倒场里，按前缀搬回自己的模块。
"""


from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..clock import now_naive
from ..visibility import assert_obj_org_writable, assert_org_writable, scope_patient_list
from ..database import get_db
from ..deps import get_current_user, paginate, require_roles, row_dict
from ..models import (
    FamilyDoctorContract,
    HomeVisitOrder,
    Organization,
    Patient,
    User,
)

router = APIRouter(
    prefix="/api/homevisits", tags=["上门服务调度"],
    dependencies=[Depends(get_current_user)]
)


# ===========================================================================
# ⑨ 上门服务调度（送医送护上门）
# ===========================================================================


VISIT_SERVICES = {
    "nursing": "上门护理",
    "doctor": "上门诊疗",
    "rehab": "康复指导",
    "sampling": "上门采样",
}


class VisitCreate(BaseModel):
    patient_id: int
    org_id: int
    service_type: str = Field(pattern="^(nursing|doctor|rehab|sampling)$")
    demand: str = ""
    address: str = ""
    expect_date: str = ""
    # 不传则自动关联该患者在该机构的履约中家医签约
    contract_id: int | None = None


def _visit_out(o: HomeVisitOrder) -> dict:
    return {
        "id": o.id,
        "patient_id": o.patient_id,
        "contract_id": o.contract_id,
        "org_id": o.org_id,
        "service_type": o.service_type,
        "service_type_name": VISIT_SERVICES.get(o.service_type, o.service_type),
        "demand": o.demand,
        "address": o.address,
        "expect_date": o.expect_date,
        "status": o.status,
        "assignee_name": o.assignee_name,
        "dispatched_at": o.dispatched_at.isoformat() if o.dispatched_at else None,
        "service_note": o.service_note,
        "completed_at": o.completed_at.isoformat() if o.completed_at else None,
    }


@router.post(
    "", status_code=201, dependencies=[Depends(require_roles("operator", "doctor", "public_health"))]
)
def create_visit(body: VisitCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    """上门服务申请：显式指定签约或按患者+机构自动关联履约中的家医签约。"""
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="服务机构不存在")
    contract_id = body.contract_id
    if contract_id is not None:
        contract = db.get(FamilyDoctorContract, contract_id)
        if contract is None:
            raise HTTPException(status_code=404, detail="家医签约不存在")
        if contract.patient_id != body.patient_id:
            raise HTTPException(status_code=422, detail="签约与申请患者不一致")
        if contract.status != "active":
            raise HTTPException(status_code=409, detail="家医签约已解约，不可派生上门服务")
    else:
        contract = (
            db.query(FamilyDoctorContract)
            .filter(
                FamilyDoctorContract.patient_id == body.patient_id,
                FamilyDoctorContract.org_id == body.org_id,
                FamilyDoctorContract.status == "active",
            )
            .first()
        )
        contract_id = contract.id if contract else None
    order = HomeVisitOrder(
        **body.model_dump(exclude={"contract_id"}), contract_id=contract_id, created_by=user.id
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return _visit_out(order)


@router.get("")
def list_visits(
    response: Response,
    status: str | None = None,
    patient_id: int | None = None,
    org_id: int | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(HomeVisitOrder)
    if status:
        query = query.filter(HomeVisitOrder.status == status)
    query = scope_patient_list(db, user, query, HomeVisitOrder, patient_id, "home_visit")
    if org_id is not None:
        query = query.filter(HomeVisitOrder.org_id == org_id)
    return [
        _visit_out(o)
        for o in paginate(query.order_by(HomeVisitOrder.id.desc()), response, offset, limit)
    ]


class VisitDispatch(BaseModel):
    assignee_name: str = Field(min_length=1)


@router.post(
    "/{order_id}/dispatch", dependencies=[Depends(require_roles("operator", "doctor"))]
)
def dispatch_visit(order_id: int, body: VisitDispatch, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """派单：指派上门人员（仅待派单工单可派）。"""
    order = db.get(HomeVisitOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="上门工单不存在")
    assert_obj_org_writable(db, user, order)
    if order.status != "applied":
        raise HTTPException(status_code=409, detail=f"工单当前状态 {order.status} 不可派单")
    order.status = "dispatched"
    order.assignee_name = body.assignee_name
    order.dispatched_at = now_naive()
    db.commit()
    db.refresh(order)
    return _visit_out(order)


class VisitComplete(BaseModel):
    service_note: str = Field(min_length=1)


@router.post(
    "/{order_id}/complete", dependencies=[Depends(require_roles("operator", "doctor", "public_health"))]
)
def complete_visit(order_id: int, body: VisitComplete, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """完成上门服务：登记服务记录（仅已派单工单可完成）。"""
    order = db.get(HomeVisitOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="上门工单不存在")
    assert_obj_org_writable(db, user, order)
    if order.status != "dispatched":
        raise HTTPException(status_code=409, detail=f"工单当前状态 {order.status} 不可完成")
    order.status = "completed"
    order.service_note = body.service_note
    order.completed_at = now_naive()
    db.commit()
    db.refresh(order)
    return _visit_out(order)


@router.post(
    "/{order_id}/cancel", dependencies=[Depends(require_roles("operator", "doctor"))]
)
def cancel_visit(order_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    order = db.get(HomeVisitOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="上门工单不存在")
    assert_obj_org_writable(db, user, order)
    if order.status == "completed":
        raise HTTPException(status_code=409, detail="已完成工单不可取消")
    order.status = "cancelled"
    db.commit()
    db.refresh(order)
    return _visit_out(order)


@router.get("/stats")
def visit_stats(db: Session = Depends(get_db)):
    """上门服务统计：状态分布、签约关联率（体现家医签约履约）。"""
    by_status = row_dict(
        db.query(HomeVisitOrder.status, func.count(HomeVisitOrder.id))
        .group_by(HomeVisitOrder.status)
        .all()
    )
    total = sum(by_status.values())
    linked = (
        db.query(func.count(HomeVisitOrder.id))
        .filter(HomeVisitOrder.contract_id.isnot(None))
        .scalar()
        or 0
    )
    return {
        "total": total,
        "by_status": by_status,
        "contract_linked": linked,
        "contract_linked_ratio_pct": round(linked * 100.0 / total, 2) if total else 0.0,
    }
