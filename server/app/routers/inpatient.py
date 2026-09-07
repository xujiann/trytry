"""住院与床位（浙江省指南 M7）：病区/床位资源库、入出转（ADT）、住院医嘱、病案首页。

- 床位占用采用条件 UPDATE 原子分配（WHERE status='free'），并发抢占仅一人成功；
- 状态机：入院登记(admitted) → 转科/转床 → 出院(discharged)；
- 出院前置：病案首页已填写（M8 计费上线后另加"费用已结清"校验）；
- 病案首页含出院诊断/手术/费用汇总/转归（WS 445 最小集），为 DRGs（M12）数据底座。
"""
from datetime import datetime
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import case, func, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ..concurrency import insert_or_conflict
from ..visibility import assert_obj_org_writable, assert_org_writable, scope_org_list, scope_patient_list
from .. import events
from ..database import get_db
from ..deps import (
    get_current_user,
    paginate,
    require_admin,
    require_roles,
    resolve_org_scope,
    row_dict,
)
from ..models import (
    Admission,
    Bed,
    CaseSummary,
    Encounter,
    InpatientOrder,
    NursingRecord,
    OrderExecution,
    Organization,
    Patient,
    User,
    Ward,
    utcnow,
)

router = APIRouter(prefix="/api/inpatient", tags=["住院与床位"], dependencies=[Depends(get_current_user)])


# ---------- 病区/床位资源库 ----------
#
# 响应契约（特征化网见 tests/test_inpatient_contract.py）：金额是 Money 列，
# 一律 `int | float`（整数读回 int，声明 float 会把「6000 元」印成「6000.0 元」）；
# `drg_weight`/`occupancy_pct` 是 Float 列或真除法产地，恒 float。


class WardCreate(BaseModel):
    org_id: int
    name: str = Field(min_length=1, max_length=64)


class WardOut(BaseModel):
    id: int
    org_id: int
    name: str


@router.post("/wards", response_model=WardOut, status_code=201, dependencies=[Depends(require_admin)])
def create_ward(body: WardCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    if db.query(Ward).filter(Ward.org_id == body.org_id, Ward.name == body.name).first():
        raise HTTPException(status_code=409, detail="该机构下病区已存在")
    ward = insert_or_conflict(db, Ward(**body.model_dump()), "该机构下病区已存在")
    return {"id": ward.id, "org_id": ward.org_id, "name": ward.name}


@router.get("/wards", response_model=list[WardOut])
def list_wards(
    response: Response,
    org_id: int | None = None,
    offset: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(Ward)
    q = scope_org_list(db, user, q, Ward, org_id)
    return [
        {"id": w.id, "org_id": w.org_id, "name": w.name}
        for w in paginate(q.order_by(Ward.id), response, offset, limit)
    ]


class BedCreate(BaseModel):
    ward_id: int
    bed_no: str = Field(min_length=1, max_length=16)


class BedOut(BaseModel):
    id: int
    ward_id: int
    bed_no: str
    status: str


@router.post("/beds", response_model=BedOut, status_code=201, dependencies=[Depends(require_admin)])
def create_bed(body: BedCreate, db: Session = Depends(get_db)):
    if db.get(Ward, body.ward_id) is None:
        raise HTTPException(status_code=404, detail="病区不存在")
    if db.query(Bed).filter(Bed.ward_id == body.ward_id, Bed.bed_no == body.bed_no).first():
        raise HTTPException(status_code=409, detail="该病区下床号已存在")
    bed = insert_or_conflict(db, Bed(**body.model_dump()), "该病区下床号已存在")
    return {"id": bed.id, "ward_id": bed.ward_id, "bed_no": bed.bed_no, "status": bed.status}


@router.get("/beds", response_model=list[BedOut])
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


def _mark_discharged(db: Session, admission_id: int, now: datetime) -> bool:
    """出院状态迁移与出院时间压在**同一条带状态条件的 UPDATE** 里，返回本次是否由这一路迁移。

    旧写法是"读 status → 判 admitted → `admission.status = 'discharged'` → commit"：
    两路出院（平台端点与 HL7 A03 镜像也算两路）同时到达都读到 admitted，
    PG 的 READ COMMITTED 下八路全过——床被释放八次、出院随访与通知各派生一份、
    ADMISSION_DISCHARGED 发布八次、discharged_at 以最后提交的为准。
    `WHERE status = 'admitted'` 让后到的那几路 rowcount 为 0，与顺序请求一样拿 409。

    两条使用约定，都不是可选项：

    - `synchronize_session=False`：ORM 版 UPDATE 默认会把 SET 值评估到会话内对象上
      （连 rowcount=0 的抢输方也会被翻成 discharged，因为它手上那份**旧属性**恰好
      满足 WHERE），这里关掉，对象保持读到时的样子；
    - rowcount=0 一路**先 `db.rollback()` 再读任何东西**（这条 UPDATE 已经开了写事务，
      不回滚就一路攥着 SQLite 写锁，随后审计中间件/交换日志另开会话即
      `database is locked`，见 `concurrency.insert_if_absent`）；rowcount=1 一路
      **先 `db.refresh()` 再用 `bed_id`**——中途有转床提交时，拿到的才是新床，
      否则释放旧床、把新床漏成永久占用。
    """
    discharged = cast(CursorResult, db.execute(
        update(Admission)
        .where(Admission.id == admission_id, Admission.status == "admitted")
        .values(status="discharged", discharged_at=now)
        .execution_options(synchronize_session=False)
    ))
    return bool(discharged.rowcount)


# ---------- 入出转（ADT） ----------


class AdmissionCreate(BaseModel):
    patient_id: int
    ward_id: int
    bed_id: int
    doctor_name: str = ""
    diagnosis_name: str = ""


class AdmissionOut(BaseModel):
    id: int
    patient_id: int
    org_id: int
    ward_id: int
    bed_id: int
    doctor_name: str
    diagnosis_name: str
    status: str
    admitted_at: str
    discharged_at: str | None


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
    response_model=AdmissionOut,
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
    # 住院就诊记录入档（Encounter inpatient 类型），进入 360 视图。
    # 先挂起、与 admission 同一次 commit 落库：抢输的那一路整体回滚，
    # 不会留下"有就诊记录没有住院记录"的半截档案，占的床也一并退回。
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
    admission = Admission(**body.model_dump(), org_id=ward.org_id, created_by=user.id)
    # 上面那句"已在院"判定是 check-then-act：并发下两路都查不到在院记录都会建单。
    # uq_admission_patient_admitted（部分唯一索引）是兜底，抢输者拿到的
    # 409 文案与顺序请求完全一致——对调用方来说两种情形没有区别。
    insert_or_conflict(db, admission, "该患者已在院，不可重复入院登记")
    return _admission_out(admission)


@router.get("/admissions", response_model=list[AdmissionOut])
def list_admissions(
    response: Response,
    status: str | None = None,
    patient_id: int | None = None,
    offset: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    q = db.query(Admission)
    if status:
        q = q.filter(Admission.status == status)
    q = scope_patient_list(db, user, q, Admission, patient_id, "admission")
    return [
        _admission_out(a)
        for a in paginate(q.order_by(Admission.id.desc()), response, offset, limit)
    ]


class TransferBody(BaseModel):
    ward_id: int
    bed_id: int


@router.post(
    "/admissions/{admission_id}/transfer",
    response_model=AdmissionOut,
    dependencies=[Depends(require_roles("doctor"))],  # 转科/转床=医师
)
def transfer_admission(admission_id: int, body: TransferBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    admission = db.get(Admission, admission_id)
    if admission is None:
        raise HTTPException(status_code=404, detail="住院记录不存在")
    assert_obj_org_writable(db, user, admission)
    if admission.status != "admitted":
        raise HTTPException(status_code=409, detail="仅在院患者可转科/转床")
    if body.bed_id == admission.bed_id:
        raise HTTPException(status_code=422, detail="目标床位与当前床位相同")
    ward = db.get(Ward, body.ward_id)
    if ward is None:
        raise HTTPException(status_code=404, detail="目标病区不存在")
    if ward.org_id != admission.org_id:
        raise HTTPException(status_code=422, detail="转科仅限同机构病区（跨机构请走双向转诊）")
    # 目标床位校验前移：原先它长在 `_occupy_bed` 里，而下面的比较交换要写
    # `admissions.bed_id`——那是 PG 上一条**真的、不可延迟的外键**（SQLite 不校验）。
    # 不先查一下，转到不存在的床在 PG 上会在比较交换处抛 IntegrityError，
    # 由 404 变成没人接的 500。位置紧跟病区校验之后，顺序请求的报错次序不变。
    bed = db.get(Bed, body.bed_id)
    if bed is None or bed.ward_id != body.ward_id:
        raise HTTPException(status_code=404, detail="床位不存在或不属于该病区")
    # 比较交换排在占床/释床**之前**：出院已改为先锁 admission 行，转床若仍先锁床，
    # 转床与出院并发就会形成锁环（PG 上一方被判 DeadlockDetected → 500）。
    # 但"先 admission 后床"只覆盖出院与转床两条迁移，**不是全局不变式**：入院登记
    # （`create_admission`）至今仍是先占床、后插 admission 行（既有顺序，本轮未动）。
    # 要用它构造出环得让"同一患者的重复入院登记"恰好撞上"把这位患者转进那张登记
    # 目标床"，概率极低，单列技术债；别照着这里以为三条路径的加锁顺序已经统一。
    # 上面那句 status 预检同样是 check-then-act（两路并发转床都读到 admitted、
    # 都读到同一张旧床），把"从我读到的那张床上移走"压进 WHERE 才拦得住：
    # 抢输的一路 rowcount=0，占的目标床随回滚退回，不会留下没有住院记录的占用床。
    old_bed_id = admission.bed_id
    moved = cast(CursorResult, db.execute(
        update(Admission)
        .where(
            Admission.id == admission.id,
            Admission.status == "admitted",
            Admission.bed_id == old_bed_id,
        )
        .values(ward_id=body.ward_id, bed_id=body.bed_id)
        .execution_options(synchronize_session=False)
    ))
    if not moved.rowcount:
        # 先 rollback 再 db.get：回滚把会话内的旧状态一并过期，重查才读得到已提交的真相
        # （也释放这条 UPDATE 已经开出的写事务）。不要在 rollback 前读 admission 的任何属性。
        db.rollback()
        current = db.get(Admission, admission_id)
        if current is None or current.status != "admitted":
            raise HTTPException(status_code=409, detail="仅在院患者可转科/转床")
        raise HTTPException(status_code=409, detail="床位信息刚被其他操作变更，请刷新后重试")
    _occupy_bed(db, body.bed_id, body.ward_id)
    _release_bed(db, old_bed_id)
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


class CaseSummaryOut(BaseModel):
    id: int
    admission_id: int
    discharge_diagnosis: str
    operation: str
    total_cost: int | float
    drug_cost: int | float
    outcome: str
    note: str
    drg_code: str
    drg_weight: float  # Float 列（非 Money）：整数权重读回来就是 x.0
    created_by_name: str


class DrgAssignOut(BaseModel):
    """`assign_drg_group` 的入组结果（routers/drgs.py 唯一产地，恒六键）。"""

    drg_code: str
    drg_name: str
    mdc: str
    mdc_name: str
    weight: float
    fallback: bool


class CaseSummaryCreateOut(CaseSummaryOut):
    """结案回执。`drg` 是**条件键**：M12（drgs 模块）在位时恒出现（含兜底组
    QY；兜底组种子缺失时为 null），模块摘除（ImportError 分支）时键整个不
    出现——故端点带 `response_model_exclude_unset=True`，镜像该分支而不是
    把它声明成恒在。"""

    drg: DrgAssignOut | None = None


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
    response_model=CaseSummaryCreateOut,
    response_model_exclude_unset=True,  # drg 键仅 M12 在位时出现（见模型注释）
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
    assert_obj_org_writable(db, user, admission)
    if db.query(CaseSummary).filter(CaseSummary.admission_id == admission_id).first():
        raise HTTPException(status_code=409, detail="病案首页已填写")
    if body.drug_cost > body.total_cost:
        raise HTTPException(status_code=422, detail="药费不得超过总费用")
    payload = body.model_dump()
    # T2.3：首页手术栏未填时，从本次住院已完成的术中记录带出实际术式。
    # DRG 外科组按主手术关键词入组，靠医生手敲这一栏最容易漏，带出来准确得多。
    if not payload.get("operation"):
        payload["operation"] = _operations_of_admission(db, admission_id)
    summary = insert_or_conflict(db, CaseSummary(
            admission_id=admission_id,
            **payload,
            created_by_name=user.full_name or user.username,
        ), "病案首页已填写")
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


@router.get("/admissions/{admission_id}/case-summary", response_model=CaseSummaryOut)
def get_case_summary(admission_id: int, db: Session = Depends(get_db)):
    summary = db.query(CaseSummary).filter(CaseSummary.admission_id == admission_id).first()
    if summary is None:
        raise HTTPException(status_code=404, detail="病案首页未填写")
    return _case_summary_out(summary)


# ---------- 出院 ----------


@router.post(
    "/admissions/{admission_id}/discharge",
    response_model=AdmissionOut,
    dependencies=[Depends(require_roles("doctor"))],  # 出院=医师
)
def discharge_admission(admission_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    admission = db.get(Admission, admission_id)
    if admission is None:
        raise HTTPException(status_code=404, detail="住院记录不存在")
    assert_obj_org_writable(db, user, admission)
    if admission.status != "admitted":
        raise HTTPException(status_code=409, detail="该患者已出院")
    summary = db.query(CaseSummary).filter(CaseSummary.admission_id == admission_id).first()
    if summary is None:
        raise HTTPException(status_code=409, detail="病案首页未填写，不可出院")
    _assert_billing_settled(db, admission)
    # 上面那句"是否仍在院"是 check-then-act，只是快路径；真正的闸门是这条带状态条件的
    # UPDATE，抢输的一路拿到的 409 与顺序重复出院完全一致——对调用方没有区别。
    # 闸门必须是本次事务的**第一条写语句**：停医嘱、释床、派随访、发通知、发事件
    # 全部只在命中后执行，抢输的一路一行都不动。
    now = utcnow()
    if not _mark_discharged(db, admission.id, now):
        db.rollback()
        raise HTTPException(status_code=409, detail="该患者已出院")
    db.refresh(admission)  # 行锁已到手：bed_id 以库内为准（中途有转床提交时拿到的是新床）
    # 停止全部执行中医嘱
    db.query(InpatientOrder).filter(
        InpatientOrder.admission_id == admission_id, InpatientOrder.status == "active"
    ).update(
        {InpatientOrder.status: "stopped", InpatientOrder.stopped_at: now},
        synchronize_session=False,
    )
    _release_bed(db, admission.bed_id)
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
    # 领域事件：订阅方（如慢专病子系统）据此派生自己的随访计划。
    # 同事务、只 add 不 commit，订阅者异常由总线兜住，不影响出院办理本身。
    events.publish(db, events.ADMISSION_DISCHARGED, {
        "admission_id": admission.id,
        "patient_id": admission.patient_id,
        "org_id": admission.org_id,
        "diagnosis_name": admission.diagnosis_name or "",
        "discharged_on": now.date().isoformat(),
    })
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


#: 预检与库兜底共用同一句文案：两处分头写，早晚会分叉，
#: 调用方就能从措辞上分辨"并发抢输"与"本来就重复"，那正是要避免的。
_DUPLICATE_LONG_ORDER_DETAIL = "该住院已有内容相同的执行中长期医嘱，请先停止原医嘱再开立"


class OrderOut(BaseModel):
    id: int
    admission_id: int
    order_type: str
    content: str
    status: str
    created_by_name: str
    # 出院批量停止不回填姓名（bulk UPDATE），手工停止才有——保持空串语义
    stopped_by_name: str
    created_at: str
    stopped_at: str | None


@router.post(
    "/orders",
    response_model=OrderOut,
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
    # 长期医嘱按"一条一直执行"开立，同一次住院里内容一模一样的在执行长期医嘱只该有一条：
    # 两条就是两行医嘱单、两笔执行登记，最后要主管医师回头人工仲裁停掉一条。
    # 临时医嘱按次开立（同内容多条合法）、停用后重开也合法，故只查 long+active。
    if body.order_type == "long":
        duplicate = (
            db.query(InpatientOrder.id)
            .filter(
                InpatientOrder.admission_id == body.admission_id,
                InpatientOrder.order_type == "long",
                InpatientOrder.status == "active",
                InpatientOrder.content == body.content,
            )
            .first()
        )
        if duplicate:
            raise HTTPException(status_code=409, detail=_DUPLICATE_LONG_ORDER_DETAIL)
    order = InpatientOrder(
        **body.model_dump(), created_by_name=user.full_name or user.username
    )
    # 上面那句查重是 check-then-act：并发下（含双击）两路都查不到就都开立。
    # uq_inpatient_order_active_long（部分唯一索引）是兜底，抢输者拿到的
    # 409 文案与顺序重复完全一致——对调用方来说两种情形没有区别。
    insert_or_conflict(db, order, _DUPLICATE_LONG_ORDER_DETAIL)
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


@router.get("/orders", response_model=list[OrderOut])
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
    response_model=OrderOut,
    dependencies=[Depends(require_roles("doctor"))],  # 医嘱停止=医师
)
def stop_order(
    order_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    order = db.get(InpatientOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="医嘱不存在")
    # 归属校验（上线前审计）：`InpatientOrder` 自己不带 org_id，归属隔一跳在
    # `admissions` 上。原先只有 `require_roles("doctor")`——那是**认证**不是授权，
    # 于是任一成员单位的医师顺序遍历 order_id 就能停掉别家医院任何一条在用医嘱。
    # 归属判定排在状态机之前：先 403，免得用 409/200 的差别探出别家医嘱的状态。
    admission = db.get(Admission, order.admission_id)
    assert_obj_org_writable(db, user, admission)
    if order.status != "active":
        raise HTTPException(status_code=409, detail="医嘱已停止")
    order.status = "stopped"
    order.stopped_at = utcnow()
    order.stopped_by_name = user.full_name or user.username
    db.commit()
    return _order_out(order)


# ---------- 医嘱执行记录（工程包 B1） ----------
#
# 停用医嘱不可再登记执行（409）；皮试结果可空——空与"阴性"是两回事。
# 护理记录联动（P1-24a）：护理记录挂在医嘱上，执行视图按医嘱附护理记录计数。


class ExecutionCreate(BaseModel):
    note: str = Field(default="", max_length=512)
    # negative=阴性, positive=阳性；不需要皮试的医嘱不传
    skin_test_result: str | None = Field(default=None, pattern="^(negative|positive)$")


class ExecutionOut(BaseModel):
    id: int
    inpatient_order_id: int
    executed_by: int
    executed_by_name: str
    executed_at: str
    note: str
    skin_test_result: str | None
    # 护理执行联动（P1-24a）：该医嘱名下的关联护理记录数。护理记录经
    # nursing_records.inpatient_order_id 挂在**医嘱**上（不是单次执行上），
    # 所以这是医嘱级计数，同一响应内各条相同——契约兼容扩展，旧客户端可忽略。
    nursing_record_count: int = 0


def _execution_out(
    e: OrderExecution, executed_by_name: str, nursing_record_count: int = 0
) -> dict:
    return {
        "id": e.id,
        "inpatient_order_id": e.inpatient_order_id,
        "executed_by": e.executed_by,
        "executed_by_name": executed_by_name,
        "executed_at": e.executed_at.isoformat(),
        "note": e.note,
        "skin_test_result": e.skin_test_result,
        "nursing_record_count": nursing_record_count,
    }


def _order_nursing_count(db: Session, order_id: int) -> int:
    """该医嘱关联的护理记录数（P1-24a）。"""
    return (
        db.query(NursingRecord).filter(NursingRecord.inpatient_order_id == order_id).count()
    )


@router.post(
    "/orders/{order_id}/executions",
    response_model=ExecutionOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "operator"))],  # 执行登记=医疗岗/经办
)
def record_order_execution(
    order_id: int,
    body: ExecutionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    order = db.get(InpatientOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="医嘱不存在")
    admission = db.get(Admission, order.admission_id)
    if admission is not None:
        assert_obj_org_writable(db, user, admission)
    if order.status != "active":
        raise HTTPException(status_code=409, detail="医嘱已停止，不可再登记执行")
    execution = OrderExecution(
        inpatient_order_id=order_id,
        executed_by=user.id,
        note=body.note,
        skin_test_result=body.skin_test_result,
    )
    db.add(execution)
    db.commit()
    db.refresh(execution)
    return _execution_out(
        execution, user.full_name or user.username, _order_nursing_count(db, order_id)
    )


@router.get("/orders/{order_id}/executions", response_model=list[ExecutionOut])
def list_order_executions(order_id: int, db: Session = Depends(get_db)):
    if db.get(InpatientOrder, order_id) is None:
        raise HTTPException(status_code=404, detail="医嘱不存在")
    rows = (
        db.query(OrderExecution, User.full_name, User.username)
        .outerjoin(User, User.id == OrderExecution.executed_by)
        .filter(OrderExecution.inpatient_order_id == order_id)
        .order_by(OrderExecution.id.desc())
        .limit(200)
        .all()
    )
    nursing_count = _order_nursing_count(db, order_id)
    return [
        _execution_out(e, full_name or username or "", nursing_count)
        for e, full_name, username in rows
    ]


# ---------- 床位效率统计（#15 运行效率数据源） ----------


class InpatientStatOut(BaseModel):
    org_id: int
    org_name: str
    beds_total: int
    beds_occupied: int
    occupancy_pct: float  # *100.0 真除法或兜底 0.0：恒 float
    in_hospital: int
    discharged_total: int


@router.get("/stats", response_model=list[InpatientStatOut])
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
    admitted = row_dict(
        db.query(Admission.org_id, func.count(Admission.id))
        .filter(Admission.status == "admitted")
        .group_by(Admission.org_id)
        .all()
    )
    discharged = row_dict(
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
