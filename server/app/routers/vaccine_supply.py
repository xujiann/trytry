"""㉕疫苗批次、冷链监测与 AEFI 上报。

第七轮子功能级重审把第 25 条从 ✅ 降到 ⚠️：`vaccination_records` 只有 6 列，
"疫苗信息查询""冷链信息查询""疑似预防接种异常反应查询""统计分析"四项
全部无处落地。这个模块补的就是这四项。

三条口径，都写进接口返回：

1. **效期与超温都不自动改状态**。过期按日期现算，超温不自动封存批次——
   封存整批是有成本的决定，平台把异常标出来、把该批次点出来，由人决定。
   与"超支不自动扣减"同一条原则。
2. **接种时批次三查**：过期、封存、库存不足各自给出明确原因，不合并成
   一句"批次不可用"——接种台前的人需要知道是该换批次还是该补货。
3. **AEFI 关联到剂次**而不只是患者：同一人打过多种疫苗，不落到剂次上
   就归不了因。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..clock import now_naive
from ..visibility import assert_obj_org_writable, assert_org_writable, scope_org_list, scope_patient_list
from ..database import get_db
from ..datetypes import DateStr, OptionalDateStr
from ..deps import get_current_user, require_roles, resolve_business_date, resolve_org_scope
from ..models import (
    AefiReport,
    ColdChainRecord,
    Organization,
    Patient,
    User,
    VaccinationRecord,
    VaccineBatch,
)

router = APIRouter(
    prefix="/api/vaccine-supply",
    tags=["疫苗批次与冷链"],
    dependencies=[Depends(get_current_user)],
)

REACTION_TYPES = {
    "general": "一般反应",
    "severe": "严重反应",
    "psychogenic": "心因性反应",
    "coincidental": "偶合症",
}
AEFI_OUTCOMES = {
    "recovered": "痊愈",
    "improving": "好转中",
    "sequelae": "留有后遗症",
    "death": "死亡",
    "unknown": "未知",
}


# ============================================================ 响应契约
#
# 模型集中放在**所有端点之前**：`response_model=` 是装饰器参数，导入时就要求值。
# 字段与顺序**精确镜像**各 `_xxx_out` 的当前出参——治理不得改响应字节
# （CLAUDE.md 第 7 条），逐字节取证见 tests/test_vaccine_supply_contract.py
# 与套件级捕获（tests/capture_plugin.py）。


class VaccineBatchOut(BaseModel):
    """批次。`expired`/`usable` 按 today 现算（模块口径 1）；quantity 一族是
    Integer 列，`remaining` 是它们的差，全是 int。"""

    id: int
    vaccine_code: str
    vaccine_name: str
    batch_no: str
    manufacturer: str
    expire_date: str
    org_id: int
    quantity: int
    used_quantity: int
    remaining: int
    status: str
    frozen_reason: str
    expired: bool
    usable: bool
    unusable_reason: str


class BatchRecipientOut(BaseModel):
    record_id: int
    patient_id: int
    patient_name: str
    dose_no: int
    vaccinated_date: str
    org_id: int


class BatchRecipientsOut(BaseModel):
    batch_no: str
    vaccine_name: str
    total: int
    recipients: list[BatchRecipientOut]


class ColdChainOut(BaseModel):
    """冷链记录。`temperature` 是 **Float 列**（整数入参读回来是 8.0），
    声明 float 才是原样；`range` 是 handler 拼好的 "2.0~8.0℃" 字符串。"""

    id: int
    org_id: int
    device_name: str
    temperature: float
    range: str
    exceeded: bool
    recorded_at: str
    handled: bool
    handle_note: str


class ColdChainCreatedOut(ColdChainOut):
    """录温回执 = 冷链记录 + **条件键** `hint`：只在超温时出现，未超温时
    键**整个不出现**（不是 null），靠 exclude_unset 钉住。"""

    hint: str | None = None


class AefiOut(BaseModel):
    id: int
    patient_id: int
    # 未关联接种记录时为 null（键仍在）
    record_id: int | None
    vaccine_code: str
    batch_no: str
    reaction_type: str
    reaction_type_name: str
    symptom: str
    onset_date: str
    outcome: str
    outcome_name: str
    org_id: int


class VaccinePeriodOut(BaseModel):
    start: str
    end: str


class AefiReactionCountOut(BaseModel):
    count: int
    name: str


class AefiStatsOut(BaseModel):
    total: int
    severe: int
    # 键是反应分级取值（general/severe/…），随数据出现而定
    by_reaction: dict[str, AefiReactionCountOut]
    # 分母为 0 时明说无接种（null），不报 0——两个比率恒 float | None
    rate_per_100k_doses: float | None
    severe_rate_per_100k_doses: float | None


class VaccineBatchStatsOut(BaseModel):
    total: int
    expired: int
    frozen: int
    expiring_soon: int


class ColdChainStatsOut(BaseModel):
    exceeded: int
    exceeded_unhandled: int


class VaccineCaliberOut(BaseModel):
    aefi_rate: str
    batch_status: str


class VaccineStatsOut(BaseModel):
    period: VaccinePeriodOut
    group_id: int | None
    doses: int
    aefi: AefiStatsOut
    batches: VaccineBatchStatsOut
    cold_chain: ColdChainStatsOut
    caliber: VaccineCaliberOut


class ExpiringBatchesOut(BaseModel):
    today: str
    within_days: int
    batches: list[VaccineBatchOut]
    generated_at: str


# ============================================================ 批次


class BatchIn(BaseModel):
    vaccine_code: str = Field(min_length=1, max_length=64)
    vaccine_name: str = Field(min_length=1, max_length=128)
    batch_no: str = Field(min_length=1, max_length=64)
    manufacturer: str = Field(default="", max_length=128)
    expire_date: DateStr
    org_id: int
    quantity: int = Field(default=0, ge=0)


class BatchFreeze(BaseModel):
    frozen_reason: str = Field(min_length=1, max_length=256)


def _batch_out(batch: VaccineBatch, today: str) -> dict:
    expired = batch.expire_date < today
    remaining = batch.quantity - batch.used_quantity
    return {
        "id": batch.id,
        "vaccine_code": batch.vaccine_code,
        "vaccine_name": batch.vaccine_name,
        "batch_no": batch.batch_no,
        "manufacturer": batch.manufacturer,
        "expire_date": batch.expire_date,
        "org_id": batch.org_id,
        "quantity": batch.quantity,
        "used_quantity": batch.used_quantity,
        "remaining": remaining,
        "status": batch.status,
        "frozen_reason": batch.frozen_reason,
        "expired": expired,
        # 是否可用于接种。三个条件分开报，见模块口径 2
        "usable": (not expired) and batch.status == "normal" and remaining > 0,
        "unusable_reason": (
            "已过效期" if expired
            else "已封存" if batch.status != "normal"
            else "库存已用完" if remaining <= 0
            else ""
        ),
    }


@router.post("/batches", response_model=VaccineBatchOut, status_code=201,
             dependencies=[Depends(require_roles("public_health", "operator"))])
def create_batch(body: BatchIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    batch = VaccineBatch(**body.model_dump())
    db.add(batch)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该疫苗的此批号已登记") from None
    db.refresh(batch)
    return _batch_out(batch, resolve_business_date(None).isoformat())


@router.get("/batches", response_model=list[VaccineBatchOut])
def list_batches(
    vaccine_code: str | None = None,
    org_id: int | None = None,
    usable_only: bool = False,
    today: str | None = None,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    today_str = resolve_business_date(today).isoformat()
    query = db.query(VaccineBatch)
    if vaccine_code:
        query = query.filter(VaccineBatch.vaccine_code == vaccine_code)
    query = scope_org_list(db, user, query, VaccineBatch, org_id)
    rows = [_batch_out(b, today_str) for b in query.order_by(VaccineBatch.id.desc()).limit(500).all()]
    return [r for r in rows if r["usable"]] if usable_only else rows


@router.post(
    "/batches/{batch_id}/freeze",
    response_model=VaccineBatchOut,
    dependencies=[Depends(require_roles("public_health", "director"))],
)
def freeze_batch(batch_id: int, body: BatchFreeze, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """封存批次（超温、召回）。封存不删行——这批打给了谁仍要查得到。"""
    batch = db.get(VaccineBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    assert_obj_org_writable(db, user, batch)
    if batch.status == "frozen":
        raise HTTPException(status_code=409, detail="该批次已封存")
    batch.status = "frozen"
    batch.frozen_reason = body.frozen_reason
    db.commit()
    db.refresh(batch)
    return _batch_out(batch, resolve_business_date(None).isoformat())


@router.post(
    "/batches/{batch_id}/unfreeze",
    response_model=VaccineBatchOut,
    dependencies=[Depends(require_roles("public_health", "director"))],
)
def unfreeze_batch(batch_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """解除封存。凡是拦得住的，都要放得开（D-1/D-2/D-4 教训）。"""
    batch = db.get(VaccineBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    assert_obj_org_writable(db, user, batch)
    if batch.status != "frozen":
        raise HTTPException(status_code=409, detail="该批次未封存")
    batch.status = "normal"
    batch.frozen_reason = ""
    db.commit()
    db.refresh(batch)
    return _batch_out(batch, resolve_business_date(None).isoformat())


@router.get("/batches/{batch_id}/recipients", response_model=BatchRecipientsOut)
def batch_recipients(batch_id: int, db: Session = Depends(get_db)):
    """按批号反查受种者——召回时唯一有用的那个查询。"""
    batch = db.get(VaccineBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    rows = (
        db.query(VaccinationRecord, Patient.name)
        .outerjoin(Patient, Patient.id == VaccinationRecord.patient_id)
        .filter(VaccinationRecord.batch_id == batch_id)
        .order_by(VaccinationRecord.id.desc())
        .limit(1000)
        .all()
    )
    # `total` 必须是**这一批实际接种了多少人次**，不是「这次取回了几行」。
    # 原实现是 `len(rows)`，而 rows 被 `.limit(1000)` 截断——一个批次发给 3000 人时，
    # 召回追溯页会显示「总计 1000」，**与真的只有 1000 人长得一模一样**。
    # 这个接口的用途是召回时反查受种者（见 docstring），少报人数意味着
    # 少通知的那 2000 人不会有任何一处提醒。列表本身仍受 1000 行上限约束
    # （放开它要先解决这个端点没有机构收口的问题，见 P1-49），
    # 但至少现在它会**如实说出还有多少人没列出来**。
    total = (
        db.query(func.count(VaccinationRecord.id))
        .filter(VaccinationRecord.batch_id == batch_id)
        .scalar()
        or 0
    )
    return {
        "batch_no": batch.batch_no,
        "vaccine_name": batch.vaccine_name,
        "total": total,
        "recipients": [
            {
                "record_id": r.id,
                "patient_id": r.patient_id,
                "patient_name": name or "",
                "dose_no": r.dose_no,
                "vaccinated_date": r.vaccinated_date,
                "org_id": r.org_id,
            }
            for r, name in rows
        ],
    }


# ============================================================ 冷链


class ColdChainIn(BaseModel):
    org_id: int
    device_name: str = Field(min_length=1, max_length=128)
    temperature: float
    min_allowed: float = 2.0
    max_allowed: float = 8.0
    recorded_at: str = Field(min_length=16, max_length=19)


class ColdChainHandle(BaseModel):
    handle_note: str = Field(min_length=1, max_length=512)


def _cold_out(r: ColdChainRecord) -> dict:
    return {
        "id": r.id,
        "org_id": r.org_id,
        "device_name": r.device_name,
        "temperature": r.temperature,
        "range": f"{r.min_allowed}~{r.max_allowed}℃",
        "exceeded": r.exceeded,
        "recorded_at": r.recorded_at,
        "handled": r.handled,
        "handle_note": r.handle_note,
    }


@router.post(
    "/cold-chain", response_model=ColdChainCreatedOut, response_model_exclude_unset=True,
    status_code=201, dependencies=[Depends(require_roles("public_health", "operator"))]
)
def record_temperature(body: ColdChainIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """录温。超温由平台判定并标记，**不自动封存批次**（见模块口径 1）。"""
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    if body.max_allowed <= body.min_allowed:
        raise HTTPException(status_code=422, detail="允许区间上限须大于下限")
    exceeded = body.temperature < body.min_allowed or body.temperature > body.max_allowed
    record = ColdChainRecord(exceeded=exceeded, **body.model_dump())
    db.add(record)
    db.commit()
    db.refresh(record)
    out = _cold_out(record)
    if exceeded:
        out["hint"] = "已超出允许区间，请核查该设备内疫苗批次并决定是否封存（平台不自动封存）"
    return out


@router.get("/cold-chain", response_model=list[ColdChainOut])
def list_temperatures(
    org_id: int | None = None,
    exceeded_only: bool = False,
    unhandled_only: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """冷链温度记录。按统计口径过滤（医共体内互见）：疫苗冷链是全县强监管
    事项，牵头单位要看得到成员机构的超温告警——只给本机构，片区监管就瞎了。"""
    query = db.query(ColdChainRecord)
    query = scope_org_list(db, user, query, ColdChainRecord, org_id, stats=True)
    if exceeded_only:
        query = query.filter(ColdChainRecord.exceeded.is_(True))
    if unhandled_only:
        query = query.filter(ColdChainRecord.exceeded.is_(True), ColdChainRecord.handled.is_(False))
    return [_cold_out(r) for r in query.order_by(ColdChainRecord.id.desc()).limit(500).all()]


@router.post(
    "/cold-chain/{record_id}/handle",
    response_model=ColdChainOut,
    dependencies=[Depends(require_roles("public_health", "operator"))],
)
def handle_exceedance(record_id: int, body: ColdChainHandle, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    record = db.get(ColdChainRecord, record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    assert_obj_org_writable(db, user, record)
    if not record.exceeded:
        raise HTTPException(status_code=422, detail="该记录未超温，无需处置")
    record.handled = True
    record.handle_note = body.handle_note
    db.commit()
    db.refresh(record)
    return _cold_out(record)


# ============================================================ AEFI


class AefiIn(BaseModel):
    patient_id: int
    record_id: int | None = None
    vaccine_code: str = Field(default="", max_length=64)
    reaction_type: str = Field(default="general", pattern="^(general|severe|psychogenic|coincidental)$")
    symptom: str = Field(min_length=1, max_length=512)
    onset_date: DateStr
    outcome: str = Field(default="unknown", pattern="^(recovered|improving|sequelae|death|unknown)$")
    org_id: int


class AefiOutcome(BaseModel):
    outcome: str = Field(pattern="^(recovered|improving|sequelae|death|unknown)$")


def _aefi_out(r: AefiReport) -> dict:
    return {
        "id": r.id,
        "patient_id": r.patient_id,
        "record_id": r.record_id,
        "vaccine_code": r.vaccine_code,
        "batch_no": r.batch_no,
        "reaction_type": r.reaction_type,
        "reaction_type_name": REACTION_TYPES.get(r.reaction_type, r.reaction_type),
        "symptom": r.symptom,
        "onset_date": r.onset_date,
        "outcome": r.outcome,
        "outcome_name": AEFI_OUTCOMES.get(r.outcome, r.outcome),
        "org_id": r.org_id,
    }


@router.post(
    "/aefi", response_model=AefiOut, status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))]
)
def report_aefi(
    body: AefiIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """AEFI 上报。给了接种记录 id 就从记录带出疫苗与批号，不让上报人自报——
    上报现场往往不知道打的是哪一批，让人凭记忆填只会填错。"""
    assert_org_writable(db, user, body.org_id)
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    data = body.model_dump()
    batch_no = ""
    if body.record_id is not None:
        record = db.get(VaccinationRecord, body.record_id)
        if record is None:
            raise HTTPException(status_code=404, detail="接种记录不存在")
        if record.patient_id != body.patient_id:
            raise HTTPException(status_code=422, detail="接种记录不属于该患者")
        data["vaccine_code"] = record.vaccine_code
        batch_no = record.batch_no
    elif not body.vaccine_code:
        raise HTTPException(status_code=422, detail="未关联接种记录时须填写疫苗编码")
    report = AefiReport(batch_no=batch_no, reported_by=user.id, **data)
    db.add(report)
    db.commit()
    db.refresh(report)
    return _aefi_out(report)


@router.get("/aefi", response_model=list[AefiOut])
def list_aefi(
    patient_id: int | None = None,
    vaccine_code: str | None = None,
    batch_no: str | None = None,
    severe_only: bool = False,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    query = db.query(AefiReport)
    query = scope_patient_list(db, user, query, AefiReport, patient_id, "aefi")
    if vaccine_code:
        query = query.filter(AefiReport.vaccine_code == vaccine_code)
    if batch_no:
        query = query.filter(AefiReport.batch_no == batch_no)
    if severe_only:
        query = query.filter(AefiReport.reaction_type == "severe")
    return [_aefi_out(r) for r in query.order_by(AefiReport.id.desc()).limit(500).all()]


@router.patch(
    "/aefi/{report_id}/outcome",
    response_model=AefiOut,
    dependencies=[Depends(require_roles("doctor", "public_health"))],
)
def update_outcome(report_id: int, body: AefiOutcome, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """转归随访：AEFI 上报时多为"未知"，好转与痊愈是后续随访才知道的。"""
    report = db.get(AefiReport, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    assert_obj_org_writable(db, user, report)
    report.outcome = body.outcome
    db.commit()
    db.refresh(report)
    return _aefi_out(report)


# ============================================================ 统计


@router.get("/stats", response_model=VaccineStatsOut)
def vaccination_stats(
    start_date: OptionalDateStr = Query(default=""),
    end_date: OptionalDateStr = Query(default=""),
    group_id: int | None = None,
    db: Session = Depends(get_db),
):
    """接种与 AEFI 统计。

    **AEFI 发生率的分母是同期接种剂次**，不是人数——同一人打三剂就是三次
    暴露机会。分母为 0 时不报 0（那会被读成"零发生率"），而是明说无接种。
    """
    scope = resolve_org_scope(db, group_id, None)

    def scoped(query, column):
        if scope is not None:
            query = query.filter(column.in_(scope))
        return query

    dose_q = scoped(db.query(func.count(VaccinationRecord.id)), VaccinationRecord.org_id)
    aefi_q = scoped(db.query(AefiReport.reaction_type, func.count(AefiReport.id)), AefiReport.org_id)
    if start_date:
        dose_q = dose_q.filter(VaccinationRecord.vaccinated_date >= start_date)
        aefi_q = aefi_q.filter(AefiReport.onset_date >= start_date)
    if end_date:
        dose_q = dose_q.filter(VaccinationRecord.vaccinated_date <= end_date)
        aefi_q = aefi_q.filter(AefiReport.onset_date <= end_date)

    doses = dose_q.scalar() or 0
    by_reaction = dict(aefi_q.group_by(AefiReport.reaction_type).all())
    aefi_total = sum(by_reaction.values())
    severe = by_reaction.get("severe", 0)

    today = resolve_business_date(None).isoformat()
    batch_q = scoped(db.query(VaccineBatch), VaccineBatch.org_id)
    batches = batch_q.all()
    cold_q = scoped(
        db.query(func.count(ColdChainRecord.id)).filter(ColdChainRecord.exceeded.is_(True)),
        ColdChainRecord.org_id,
    )
    cold_unhandled = scoped(
        db.query(func.count(ColdChainRecord.id)).filter(
            ColdChainRecord.exceeded.is_(True), ColdChainRecord.handled.is_(False)
        ),
        ColdChainRecord.org_id,
    )

    return {
        "period": {"start": start_date or "不限", "end": end_date or "不限"},
        "group_id": group_id,
        "doses": doses,
        "aefi": {
            "total": aefi_total,
            "severe": severe,
            "by_reaction": {
                k: {"count": v, "name": REACTION_TYPES.get(k, k)} for k, v in by_reaction.items()
            },
            # 十万剂次发生率，AEFI 监测的通行口径
            "rate_per_100k_doses": (
                round(aefi_total * 100000 / doses, 2) if doses else None
            ),
            "severe_rate_per_100k_doses": (
                round(severe * 100000 / doses, 2) if doses else None
            ),
        },
        "batches": {
            "total": len(batches),
            "expired": len([b for b in batches if b.expire_date < today]),
            "frozen": len([b for b in batches if b.status == "frozen"]),
            # 30 天内到期的，提前给出来——过期了才发现就只能报废
            "expiring_soon": len(
                [b for b in batches if today <= b.expire_date <= _plus_days(today, 30)]
            ),
        },
        "cold_chain": {
            "exceeded": cold_q.scalar() or 0,
            "exceeded_unhandled": cold_unhandled.scalar() or 0,
        },
        "caliber": {
            "aefi_rate": "分母为同期接种剂次（非人数）；无接种时返回 null 而非 0，"
                         "避免被读成零发生率",
            "batch_status": "过期与封存均按当下现算，不设定时任务改状态",
        },
    }


def _plus_days(date_str: str, days: int) -> str:
    from datetime import date, timedelta

    return (date.fromisoformat(date_str) + timedelta(days=days)).isoformat()


@router.get("/expiring", response_model=ExpiringBatchesOut)
def expiring_batches(
    days: int = Query(default=30, ge=1, le=365),
    today: str | None = None,
    db: Session = Depends(get_db),
):
    """临期与过期批次清单。过期的也一并列出并标注——不是列出来催人用掉，
    而是提示尽快按报废流程处理，别让它躺在冰箱里被误用。"""
    today_str = resolve_business_date(today).isoformat()
    limit = _plus_days(today_str, days)
    # 「仍有余量」必须在 SQL 里筛，不能取完 500 行再用 Python 丢行。
    # 原实现两件事凑在一起，让这个预警实际上早就不预警了：
    # 过滤**只有上界没有下界**（含已过期，见 docstring），排序又是按到期日**升序**,
    # 于是 `.limit(500)` 砍掉的恰好是「即将到期」那一端，留下的是「早就过期、
    # 也早就发完」那一端；而发完的批次不会删行（只累加 used_quantity），
    # 真实库里这 500 行大半是零余量——**最该预警的批次一条都出不来**，
    # 页面上和「没有临期批次」长得一模一样。同一形状已在 pharmacy 侧修过一次。
    # 下界**不能加**：本端点的口径就是要把已过期的一并列出并标注，
    # 提示按报废流程处理（见 docstring），加了下界等于把它们从预警里删掉。
    rows = (
        db.query(VaccineBatch)
        .filter(
            VaccineBatch.expire_date <= limit,
            VaccineBatch.quantity - VaccineBatch.used_quantity > 0,
        )
        # expire_date 只有 index、不唯一，补 id 尾键才有确定次序
        .order_by(VaccineBatch.expire_date, VaccineBatch.id)
        .limit(500)
        .all()
    )
    return {
        "today": today_str,
        "within_days": days,
        "batches": [_batch_out(r, today_str) for r in rows],
        "generated_at": now_naive().isoformat(),
    }
