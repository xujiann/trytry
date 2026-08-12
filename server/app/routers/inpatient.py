"""住院与床位（浙江省指南 M7）：病区/床位资源库、入出转（ADT）、住院医嘱、病案首页。

- 床位占用采用条件 UPDATE 原子分配（WHERE status='free'），并发抢占仅一人成功；
- 状态机：入院登记(admitted) → 转科/转床 → 出院(discharged)；
- 出院前置：病案首页已填写（M8 计费上线后另加"费用已结清"校验）；
- 病案首页含出院诊断/手术/费用汇总/转归（WS 445 最小集），为 DRGs（M12）数据底座。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_admin, require_roles, resolve_org_scope
from ..models import (
    Admission,
    Bed,
    CaseSummary,
    Encounter,
    InpatientOrder,
    Organization,
    Patient,
    User,
    Ward,
    utcnow,
)

router = APIRouter(prefix="/api/inpatient", tags=["住院与床位"], dependencies=[Depends(get_current_user)])


# ---------- 病区/床位资源库 ----------


class WardCreate(BaseModel):
    org_id: int
    name: str = Field(min_length=1, max_length=64)


@router.post("/wards", status_code=201, dependencies=[Depends(require_admin)])
def create_ward(body: WardCreate, db: Session = Depends(get_db)):
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    if db.query(Ward).filter(Ward.org_id == body.org_id, Ward.name == body.name).first():
        raise HTTPException(status_code=409, detail="该机构下病区已存在")
    ward = Ward(**body.model_dump())
    db.add(ward)
    db.commit()
    return {"id": ward.id, "org_id": ward.org_id, "name": ward.name}


@router.get("/wards")
def list_wards(org_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(Ward)
    if org_id is not None:
        q = q.filter(Ward.org_id == org_id)
    return [{"id": w.id, "org_id": w.org_id, "name": w.name} for w in q.order_by(Ward.id).limit(200).all()]


class BedCreate(BaseModel):
    ward_id: int
    bed_no: str = Field(min_length=1, max_length=16)


@router.post("/beds", status_code=201, dependencies=[Depends(require_admin)])
def create_bed(body: BedCreate, db: Session = Depends(get_db)):
    if db.get(Ward, body.ward_id) is None:
        raise HTTPException(status_code=404, detail="病区不存在")
    if db.query(Bed).filter(Bed.ward_id == body.ward_id, Bed.bed_no == body.bed_no).first():
        raise HTTPException(status_code=409, detail="该病区下床号已存在")
    bed = Bed(**body.model_dump())
    db.add(bed)
    db.commit()
    return {"id": bed.id, "ward_id": bed.ward_id, "bed_no": bed.bed_no, "status": bed.status}


@router.get("/beds")
def list_beds(ward_id: int | None = None, status: str | None = None, db: Session = Depends(get_db)):
    q = db.query(Bed)
    if ward_id is not None:
        q = q.filter(Bed.ward_id == ward_id)
    if status:
        q = q.filter(Bed.status == status)
    return [
        {"id": b.id, "ward_id": b.ward_id, "bed_no": b.bed_no, "status": b.status}
        for b in q.order_by(Bed.id).limit(500).all()
    ]


def _occupy_bed(db: Session, bed_id: int, ward_id: int) -> None:
    """原子占床：条件 UPDATE（WHERE status='free'），失败即 409。"""
    bed = db.get(Bed, bed_id)
    if bed is None or bed.ward_id != ward_id:
        db.rollback()
        raise HTTPException(status_code=404, detail="床位不存在或不属于该病区")
    occupied = (
        db.query(Bed)
        .filter(Bed.id == bed_id, Bed.status == "free")
        .update({Bed.status: "occupied"}, synchronize_session=False)
    )
    if not occupied:
        db.rollback()
        raise HTTPException(status_code=409, detail="床位已被占用")


def _release_bed(db: Session, bed_id: int) -> None:
    db.query(Bed).filter(Bed.id == bed_id).update(
        {Bed.status: "free"}, synchronize_session=False
    )


# ---------- 入出转（ADT） ----------


class AdmissionCreate(BaseModel):
    patient_id: int
    ward_id: int
    bed_id: int
    doctor_name: str = ""
    diagnosis_name: str = ""


def _admission_out(a: Admission) -> dict:
    return {
        "id": a.id,
        "patient_id": a.patient_id,
        "org_id": a.org_id,
        "ward_id": a.ward_id,
        "bed_id": a.bed_id,
        "doctor_name": a.doctor_name,
        "diagnosis_name": a.diagnosis_name,
        "status": a.status,
        "admitted_at": a.admitted_at.isoformat(),
        "discharged_at": a.discharged_at.isoformat() if a.discharged_at else None,
    }


@router.post(
    "/admissions",
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "operator"))],  # 入院登记=医疗岗/经办
)
def create_admission(
    body: AdmissionCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    ward = db.get(Ward, body.ward_id)
    if ward is None:
        raise HTTPException(status_code=404, detail="病区不存在")
    in_hospital = (
        db.query(Admission)
        .filter(Admission.patient_id == body.patient_id, Admission.status == "admitted")
        .first()
    )
    if in_hospital:
        raise HTTPException(status_code=409, detail="该患者已在院，不可重复入院登记")
    _occupy_bed(db, body.bed_id, body.ward_id)
    admission = Admission(**body.model_dump(), org_id=ward.org_id, created_by=user.id)
    db.add(admission)
    # 住院就诊记录入档（Encounter inpatient 类型），进入 360 视图
    db.add(
        Encounter(
            patient_id=body.patient_id,
            org_id=ward.org_id,
            doctor_name=body.doctor_name,
            encounter_type="inpatient",
            diagnosis_name=body.diagnosis_name,
            summary="住院入院登记",
        )
    )
    db.commit()
    db.refresh(admission)
    return _admission_out(admission)


@router.get("/admissions")
def list_admissions(
    status: str | None = None, patient_id: int | None = None, db: Session = Depends(get_db)
):
    q = db.query(Admission)
    if status:
        q = q.filter(Admission.status == status)
    if patient_id is not None:
        q = q.filter(Admission.patient_id == patient_id)
    return [_admission_out(a) for a in q.order_by(Admission.id.desc()).limit(200).all()]


class TransferBody(BaseModel):
    ward_id: int
    bed_id: int


@router.post(
    "/admissions/{admission_id}/transfer",
    dependencies=[Depends(require_roles("doctor"))],  # 转科/转床=医师
)
def transfer_admission(admission_id: int, body: TransferBody, db: Session = Depends(get_db)):
    admission = db.get(Admission, admission_id)
    if admission is None:
        raise HTTPException(status_code=404, detail="住院记录不存在")
    if admission.status != "admitted":
        raise HTTPException(status_code=409, detail="仅在院患者可转科/转床")
    if body.bed_id == admission.bed_id:
        raise HTTPException(status_code=422, detail="目标床位与当前床位相同")
    ward = db.get(Ward, body.ward_id)
    if ward is None:
        raise HTTPException(status_code=404, detail="目标病区不存在")
    if ward.org_id != admission.org_id:
        raise HTTPException(status_code=422, detail="转科仅限同机构病区（跨机构请走双向转诊）")
    _occupy_bed(db, body.bed_id, body.ward_id)
    _release_bed(db, admission.bed_id)
    admission.ward_id = body.ward_id
    admission.bed_id = body.bed_id
    db.commit()
    db.refresh(admission)
    return _admission_out(admission)


# ---------- 病案首页 ----------


class CaseSummaryCreate(BaseModel):
    discharge_diagnosis: str = Field(min_length=1, max_length=256)
    operation: str = ""
    total_cost: float = Field(default=0, ge=0)
    drug_cost: float = Field(default=0, ge=0)
    outcome: str = Field(default="好转", pattern="^(治愈|好转|未愈|死亡|其他)$")
    note: str = ""


def _case_summary_out(s: CaseSummary) -> dict:
    return {
        "id": s.id,
        "admission_id": s.admission_id,
        "discharge_diagnosis": s.discharge_diagnosis,
        "operation": s.operation,
        "total_cost": s.total_cost,
        "drug_cost": s.drug_cost,
        "outcome": s.outcome,
        "note": s.note,
        "drg_code": s.drg_code,
        "drg_weight": s.drg_weight,
        "created_by_name": s.created_by_name,
    }


def _operations_of_admission(db: Session, admission_id: int) -> str:
    """本次住院已完成手术的实际术式，多台以逗号连接。"""
    from ..models import SurgeryRecord, SurgeryRequest

    rows = (
        db.query(SurgeryRecord.actual_surgery_name)
        .join(SurgeryRequest, SurgeryRecord.request_id == SurgeryRequest.id)
        .filter(SurgeryRequest.admission_id == admission_id)
        .order_by(SurgeryRecord.id)
        .all()
    )
    return ",".join(name for (name,) in rows)[:256]


@router.post(
    "/admissions/{admission_id}/case-summary",
    status_code=201,
    dependencies=[Depends(require_roles("doctor"))],  # 病案首页=医师
)
def create_case_summary(
    admission_id: int,
    body: CaseSummaryCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    admission = db.get(Admission, admission_id)
    if admission is None:
        raise HTTPException(status_code=404, detail="住院记录不存在")
    if db.query(CaseSummary).filter(CaseSummary.admission_id == admission_id).first():
        raise HTTPException(status_code=409, detail="病案首页已填写")
    if body.drug_cost > body.total_cost:
        raise HTTPException(status_code=422, detail="药费不得超过总费用")
    payload = body.model_dump()
    # T2.3：首页手术栏未填时，从本次住院已完成的术中记录带出实际术式。
    # DRG 外科组按主手术关键词入组，靠医生手敲这一栏最容易漏，带出来准确得多。
    if not payload.get("operation"):
        payload["operation"] = _operations_of_admission(db, admission_id)
    summary = CaseSummary(
        admission_id=admission_id,
        **payload,
        created_by_name=user.full_name or user.username,
    )
    db.add(summary)
    db.commit()
    db.refresh(summary)
    out = _case_summary_out(summary)
    # M12：结案时按主诊断关键词自动 DRG 入组（模块可用时）
    try:
        from .drgs import assign_drg_group

        out["drg"] = assign_drg_group(db, summary)
        out["drg_code"] = summary.drg_code
        out["drg_weight"] = summary.drg_weight
    except ImportError:  # pragma: no cover - M12 上线前
        pass
    return out


@router.get("/admissions/{admission_id}/case-summary")
def get_case_summary(admission_id: int, db: Session = Depends(get_db)):
    summary = db.query(CaseSummary).filter(CaseSummary.admission_id == admission_id).first()
    if summary is None:
        raise HTTPException(status_code=404, detail="病案首页未填写")
    return _case_summary_out(summary)


# ---------- 出院 ----------


@router.post(
    "/admissions/{admission_id}/discharge",
    dependencies=[Depends(require_roles("doctor"))],  # 出院=医师
)
def discharge_admission(admission_id: int, db: Session = Depends(get_db)):
    admission = db.get(Admission, admission_id)
    if admission is None:
        raise HTTPException(status_code=404, detail="住院记录不存在")
    if admission.status != "admitted":
        raise HTTPException(status_code=409, detail="该患者已出院")
    summary = db.query(CaseSummary).filter(CaseSummary.admission_id == admission_id).first()
    if summary is None:
        raise HTTPException(status_code=409, detail="病案首页未填写，不可出院")
    _assert_billing_settled(db, admission)
    # 停止全部执行中医嘱
    db.query(InpatientOrder).filter(
        InpatientOrder.admission_id == admission_id, InpatientOrder.status == "active"
    ).update(
        {InpatientOrder.status: "stopped", InpatientOrder.stopped_at: utcnow()},
        synchronize_session=False,
    )
    _release_bed(db, admission.bed_id)
    admission.status = "discharged"
    admission.discharged_at = utcnow()
    # T2.4：出院即派生出院随访任务，交给统一随访中心跟踪
    from .followups import DISCHARGE_FOLLOWUP_DAYS, create_task

    create_task(
        db,
        patient_id=admission.patient_id,
        org_id=admission.org_id,
        category="discharge",
        source_id=admission.id,
        title=f"出院随访：{admission.diagnosis_name or '住院治疗'}",
        due_days=DISCHARGE_FOLLOWUP_DAYS,
    )
    from ..notify import notify_patient

    notify_patient(
        db,
        admission.patient_id,
        category="followup",
        title="出院随访安排",
        body=f"您已办理出院，我们将在 {DISCHARGE_FOLLOWUP_DAYS} 天内电话随访。"
             "费用清单可在「在线服务-住院」查看。",
        link_type="admission",
        link_id=admission.id,
    )
    db.commit()
    db.refresh(admission)
    return _admission_out(admission)


def _assert_billing_settled(db: Session, admission: Admission) -> None:
    """M8 联动：住院费用未结清不可出院（billing 模块上线前为空操作）。"""
    try:
        from .billing import unsettled_amount
    except ImportError:  # pragma: no cover - M8 上线前
        return
    amount = unsettled_amount(db, admission.id)
    if amount > 0:
        raise HTTPException(
            status_code=409, detail=f"存在未结清住院费用 {amount:.2f} 元，结算后方可出院"
        )


# ---------- 住院医嘱 ----------


class OrderCreate(BaseModel):
    admission_id: int
    order_type: str = Field(pattern="^(long|temp)$")
    content: str = Field(min_length=1, max_length=512)


@router.post(
    "/orders",
    status_code=201,
    dependencies=[Depends(require_roles("doctor"))],  # 医嘱开立=医师
)
def create_order(
    body: OrderCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    admission = db.get(Admission, body.admission_id)
    if admission is None:
        raise HTTPException(status_code=404, detail="住院记录不存在")
    if admission.status != "admitted":
        raise HTTPException(status_code=409, detail="患者已出院，不可开立医嘱")
    order = InpatientOrder(
        **body.model_dump(), created_by_name=user.full_name or user.username
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    return _order_out(order)


def _order_out(o: InpatientOrder) -> dict:
    return {
        "id": o.id,
        "admission_id": o.admission_id,
        "order_type": o.order_type,
        "content": o.content,
        "status": o.status,
        "created_by_name": o.created_by_name,
        "stopped_by_name": o.stopped_by_name,
        "created_at": o.created_at.isoformat(),
        "stopped_at": o.stopped_at.isoformat() if o.stopped_at else None,
    }


@router.get("/orders")
def list_orders(
    admission_id: int | None = None, status: str | None = None, db: Session = Depends(get_db)
):
    q = db.query(InpatientOrder)
    if admission_id is not None:
        q = q.filter(InpatientOrder.admission_id == admission_id)
    if status:
        q = q.filter(InpatientOrder.status == status)
    return [_order_out(o) for o in q.order_by(InpatientOrder.id.desc()).limit(200).all()]


@router.post(
    "/orders/{order_id}/stop",
    dependencies=[Depends(require_roles("doctor"))],  # 医嘱停止=医师
)
def stop_order(
    order_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    order = db.get(InpatientOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="医嘱不存在")
    if order.status != "active":
        raise HTTPException(status_code=409, detail="医嘱已停止")
    order.status = "stopped"
    order.stopped_at = utcnow()
    order.stopped_by_name = user.full_name or user.username
    db.commit()
    return _order_out(order)


# ---------- 床位效率统计（#15 运行效率数据源） ----------


@router.get("/stats")
def inpatient_stats(
    org_id: int | None = None,
    group_id: int | None = None,
    db: Session = Depends(get_db),
):
    """床位利用与在院情况：按机构统计总床位/占用/使用率、在院与累计出院人次。

    `group_id` 按机构协作分组筛选（片区/联盟/网格），与 `org_id` 同时给出时取交集。
    """
    scope = resolve_org_scope(db, group_id, org_id)
    rows_q = (
        db.query(
            Ward.org_id,
            Organization.name,
            func.count(Bed.id).label("beds"),
            func.sum(case((Bed.status == "occupied", 1), else_=0)).label("occupied"),
        )
        .join(Bed, Bed.ward_id == Ward.id)
        .join(Organization, Organization.id == Ward.org_id)
        .group_by(Ward.org_id, Organization.name)
    )
    if scope is not None:
        rows_q = rows_q.filter(Ward.org_id.in_(scope))
    rows = rows_q.all()
    admitted = dict(
        db.query(Admission.org_id, func.count(Admission.id))
        .filter(Admission.status == "admitted")
        .group_by(Admission.org_id)
        .all()
    )
    discharged = dict(
        db.query(Admission.org_id, func.count(Admission.id))
        .filter(Admission.status == "discharged")
        .group_by(Admission.org_id)
        .all()
    )
    return [
        {
            "org_id": r.org_id,
            "org_name": r.name,
            "beds_total": r.beds,
            "beds_occupied": int(r.occupied or 0),
            "occupancy_pct": round((r.occupied or 0) * 100.0 / r.beds, 2) if r.beds else 0.0,
            "in_hospital": admitted.get(r.org_id, 0),
            "discharged_total": discharged.get(r.org_id, 0),
        }
        for r in rows
    ]
