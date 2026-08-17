"""E8 居民端可办 + 续方审核（医方侧）。

- 居民端（scope=portal 令牌，复用 portal.current_resident_patient）：
  居家监测自报、检查检验报告查看、在线复诊/图文咨询发起、账单在线自助支付、续方申请。
- 医方侧（业务令牌）：续方申请的审核与配送。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..clock import now_naive
from ..deps import get_current_user, require_roles
from ..models import (
    ExamReport,
    ExamRequest,
    HomeMonitorReading,
    OnlineConsult,
    Organization,
    Patient,
    Prescription,
    RefillRequest,
    Settlement,
    User,
)
from ..routers.billing import _gateway, _payment_out
from ..routers.portal import current_resident_patient
from ..models import PaymentOrder

# 居民端路由（portal 令牌 + current_resident_patient 定身份；医方侧续方审核见 enhance_refill.py）
me_router = APIRouter(prefix="/api/portal/me", tags=["居民端-可办"])


# ---------------------------------------------------------------- schemas
class HomeReadingIn(BaseModel):
    reading_type: str = Field(pattern="^(bp|glucose|weight)$")
    value1: float = Field(gt=0)
    value2: float | None = None
    unit: str = ""
    measured_at: str = ""
    note: str = ""


class ConsultIn(BaseModel):
    org_id: int
    question: str = Field(min_length=1, max_length=1024)
    consult_type: str = Field(default="consult", pattern="^(consult|refollow)$")


class PayIn(BaseModel):
    channel: str = Field(default="online", pattern="^(online|card)$")


class RefillIn(BaseModel):
    org_id: int
    prescription_id: int | None = None
    drug_summary: str = Field(min_length=1, max_length=256)
    drug_code: str = Field(default="", max_length=64)
    qty: int = Field(default=0, ge=0)
    note: str = ""


# ---------------------------------------------------------------- 居家监测自报
def _is_abnormal(t: str, v1: float, v2: float | None) -> bool:
    if t == "bp":
        return v1 >= 140 or (v2 is not None and v2 >= 90)
    if t == "glucose":
        return v1 >= 7.0
    return False  # 体重不单点判异常


@me_router.post("/home-readings", status_code=201)
def upload_reading(body: HomeReadingIn, db: Session = Depends(get_db),
                   patient: Patient = Depends(current_resident_patient)):
    if body.reading_type == "bp" and body.value2 is None:
        raise HTTPException(status_code=422, detail="血压需同时上传收缩压与舒张压")
    abnormal = _is_abnormal(body.reading_type, body.value1, body.value2)
    row = HomeMonitorReading(
        patient_id=patient.id, reading_type=body.reading_type, value1=body.value1,
        value2=body.value2, unit=body.unit, measured_at=body.measured_at, note=body.note,
        abnormal=abnormal)
    db.add(row)
    db.flush()
    # S6：异常值并入慢病随访——派生一条随访任务到本人签约机构，并提醒签约医生。
    if abnormal:
        _spawn_followup_from_reading(db, patient, row)
    db.commit()
    db.refresh(row)
    return _reading_out(row)


def _spawn_followup_from_reading(db: Session, patient: Patient, row: HomeMonitorReading) -> None:
    """把异常居家自报并入慢病随访：定位患者当前有效签约机构，派生随访任务并通知医生。

    无有效签约时只留异常读数（不凭空造任务），家医签约后可回看历史异常。
    """
    from ..models import FamilyDoctorContract
    from ..notify import notify_staff
    from ..routers.followups import create_task

    contract = (
        db.query(FamilyDoctorContract)
        .filter(FamilyDoctorContract.patient_id == patient.id,
                FamilyDoctorContract.status == "active")
        .order_by(FamilyDoctorContract.id.desc())
        .first()
    )
    if contract is None:
        return
    label = {"bp": "血压", "glucose": "血糖", "weight": "体重"}.get(row.reading_type, row.reading_type)
    val = f"{row.value1}/{row.value2}" if row.value2 is not None else f"{row.value1}"
    title = f"居家{label}异常（{val}），需随访核实"
    create_task(db, patient_id=patient.id, org_id=contract.org_id, category="chronic",
                source_id=row.id, title=title, due_days=3)
    notify_staff(db, category="home_monitor", title="居民居家监测异常",
                 body=f"{patient.name}：{title}", org_id=contract.org_id,
                 roles=("doctor", "public_health"), link_type="patient", link_id=patient.id)


@me_router.get("/home-readings")
def my_readings(reading_type: str | None = None, db: Session = Depends(get_db),
                patient: Patient = Depends(current_resident_patient)):
    q = db.query(HomeMonitorReading).filter(HomeMonitorReading.patient_id == patient.id)
    if reading_type:
        q = q.filter(HomeMonitorReading.reading_type == reading_type)
    return [_reading_out(r) for r in q.order_by(HomeMonitorReading.id.desc()).limit(200).all()]


def _reading_out(r: HomeMonitorReading) -> dict:
    return {"id": r.id, "reading_type": r.reading_type, "value1": r.value1, "value2": r.value2,
            "unit": r.unit, "measured_at": r.measured_at, "abnormal": r.abnormal, "note": r.note,
            "created_at": r.created_at.isoformat()}


# ---------------------------------------------------------------- 报告查看
@me_router.get("/exam-reports")
def my_exam_reports(db: Session = Depends(get_db),
                    patient: Patient = Depends(current_resident_patient)):
    rows = (
        db.query(ExamReport, ExamRequest)
        .join(ExamRequest, ExamRequest.id == ExamReport.request_id)
        .filter(ExamRequest.patient_id == patient.id)
        .order_by(ExamReport.id.desc())
        .limit(200)
        .all()
    )
    return [
        {"report_id": rep.id, "request_id": req.id, "center_type": req.center_type,
         "item_name": req.item_name, "conclusion": rep.conclusion, "critical": rep.critical,
         "reported_at": rep.reported_at.isoformat() if rep.reported_at else None}
        for rep, req in rows
    ]


# ---------------------------------------------------------------- 在线复诊/图文咨询发起
@me_router.post("/online-consults", status_code=201)
def start_consult(body: ConsultIn, db: Session = Depends(get_db),
                  patient: Patient = Depends(current_resident_patient)):
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    row = OnlineConsult(patient_id=patient.id, org_id=body.org_id,
                        consult_type=body.consult_type, question=body.question, status="open")
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "org_id": row.org_id, "consult_type": row.consult_type,
            "question": row.question, "status": row.status}


@me_router.get("/online-consults")
def my_consults(db: Session = Depends(get_db),
                patient: Patient = Depends(current_resident_patient)):
    rows = db.query(OnlineConsult).filter(OnlineConsult.patient_id == patient.id).order_by(
        OnlineConsult.id.desc()).limit(100).all()
    return [{"id": c.id, "org_id": c.org_id, "consult_type": c.consult_type,
             "question": c.question, "reply": c.reply, "doctor_name": c.doctor_name,
             "status": c.status} for c in rows]


# ---------------------------------------------------------------- 账单在线自助支付
@me_router.post("/bills/{settlement_id}/pay", status_code=201)
def pay_bill(settlement_id: int, body: PayIn, db: Session = Depends(get_db),
             patient: Patient = Depends(current_resident_patient)):
    """居民自助支付本人结算单个人自付部分（复用 billing 网关与已付额校验口径）。"""
    settlement = db.get(Settlement, settlement_id)
    if settlement is None:
        raise HTTPException(status_code=404, detail="结算单不存在")
    if settlement.patient_id != patient.id:
        raise HTTPException(status_code=403, detail="只能支付本人的结算单")
    amount = round(settlement.self_pay, 2)
    if amount <= 0:
        raise HTTPException(status_code=422, detail="该结算单无个人自付金额")
    # 居民端只承担个人自付段，封顶必须是 self_pay 而非 total_amount——否则
    # self_pay≤insurance_pay 时可重复自付到 total_amount（把医保段的钱也让居民付了）。
    # 只统计非医保渠道的已付净额（医保段由结算侧另走 insurance 渠道，不占自付额度）。
    paid_self = round(
        sum(round(o.amount - (o.refunded_amount or 0), 2)
            for o in db.query(PaymentOrder).filter(
                PaymentOrder.settlement_id == settlement.id,
                PaymentOrder.channel != "insurance",
                PaymentOrder.status.in_(["paid", "refunded"])).all()), 2)
    if paid_self + amount > round(settlement.self_pay, 2) + 1e-6:  # 超出个人自付总额
        raise HTTPException(status_code=409, detail="该结算单个人自付部分已支付，无未付余额")
    order = PaymentOrder(settlement_id=settlement.id, channel=body.channel, amount=amount,
                         created_by=settlement.created_by)
    db.add(order)
    db.flush()
    result = _gateway(body.channel).pay(order.id, amount, body.channel)
    if result.get("success"):
        order.status = "paid"
        order.trade_no = result.get("trade_no", "")
        order.paid_at = now_naive()
    else:
        order.status = "failed"
        order.fail_reason = result.get("message", "通道未返回原因")[:256]
    db.commit()
    db.refresh(order)
    return _payment_out(order)


# ---------------------------------------------------------------- 续方申请（居民发起）
@me_router.post("/refill-requests", status_code=201)
def request_refill(body: RefillIn, db: Session = Depends(get_db),
                   patient: Patient = Depends(current_resident_patient)):
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    if body.prescription_id is not None:
        pres = db.get(Prescription, body.prescription_id)
        if pres is None or pres.patient_id != patient.id:
            raise HTTPException(status_code=422, detail="原处方不存在或不属于本人")
    row = RefillRequest(patient_id=patient.id, org_id=body.org_id,
                        prescription_id=body.prescription_id, drug_summary=body.drug_summary,
                        drug_code=body.drug_code, qty=body.qty, note=body.note, status="pending")
    db.add(row)
    db.commit()
    db.refresh(row)
    return _refill_out(row)


@me_router.get("/refill-requests")
def my_refills(db: Session = Depends(get_db),
               patient: Patient = Depends(current_resident_patient)):
    rows = db.query(RefillRequest).filter(RefillRequest.patient_id == patient.id).order_by(
        RefillRequest.id.desc()).limit(100).all()
    return [_refill_out(r) for r in rows]


def _refill_out(r: RefillRequest) -> dict:
    return {"id": r.id, "patient_id": r.patient_id, "org_id": r.org_id,
            "prescription_id": r.prescription_id, "drug_summary": r.drug_summary,
            "status": r.status, "reviewed_by": r.reviewed_by, "note": r.note,
            "created_at": r.created_at.isoformat()}
