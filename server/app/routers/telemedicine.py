"""⑨互联网+诊疗：在线咨询、复诊续方（医师回复，续方联动集中审方）。"""
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..concurrency import move_row
from ..visibility import assert_org_writable
from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import OnlineConsult, Organization, Patient, Prescription, User
from ..schemas import PrescriptionOut  # noqa: F401
from ..texttypes import NON_BLANK
from .dispense import prescription_not_reversed   # 已退药与用量统计、医生 360 同一判据（P2-1476）
from .prescriptions import PRESCRIPTION_STATUS_NAMES

router = APIRouter(prefix="/api/telemedicine", tags=["互联网+诊疗"], dependencies=[Depends(get_current_user)])

# 状态文案（措辞照抄模型列注释；报错文案用它，别把英文码直接拼给窗口人员看——P2-74）
CONSULT_STATUS_NAMES = {"open": "待回复", "replied": "已回复", "closed": "已结束"}


class ConsultCreate(BaseModel):
    patient_id: int
    org_id: int
    consult_type: str = Field(default="consult", pattern="^(consult|repeat_rx)$")
    question: str = Field(min_length=1, max_length=1024, pattern=NON_BLANK)


class ConsultOut(ConsultCreate):
    id: int
    reply: str
    doctor_name: str
    status: str
    prescription_id: int | None
    # 出参不带「不能只填空格」（P1-109）：修之前存进去的纯空白行要原样读出来，而不是让整个清单 500
    question: str = Field(min_length=1, max_length=1024)
    #: 以下两个键是 P2-1476 加的（只在末尾加键，原有键与次序不动）：关联处方的审方状态中文名、是否已退药冲销，与医生 360
    #: 处方段的 `status_name` / `dispense_reversed` 同义（P2-1198）。回复时挡得住已冲销的处方，挡不住回复之后才退药的那张；
    #: 页面「关联处方」一列原先只印编号，看不出这张方还发不发得出。没关联处方的为 null / false；中文名表外的码原样回显
    prescription_status_name: str | None
    prescription_dispense_reversed: bool

    model_config = {"from_attributes": True}


class ReplyBody(BaseModel):
    reply: str = Field(min_length=1, max_length=2048, pattern=NON_BLANK)
    doctor_name: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    # 复诊续方：医师回复时可关联已开具处方（须先经集中审方）
    prescription_id: int | None = None


def _reversed_prescriptions(db: Session, prescription_ids: set[int]) -> set[int]:
    """其中已退药冲销的处方（P2-1476）：判据就是用量统计、医生 360 那一句（`dispense.prescription_not_reversed`，P2-624 /
    P2-1198），一条 SQL 取回，不另写一份。"""
    if not prescription_ids:
        return set()
    return {
        rx_id
        for (rx_id,) in db.query(Prescription.id)
        .filter(Prescription.id.in_(prescription_ids), ~prescription_not_reversed())
    }


def _with_prescription_state(db: Session, rows: list[OnlineConsult]) -> list[OnlineConsult]:
    """挂上关联处方此刻的状态供响应模型取用（不入库，P2-1476）：审方状态中文名、是否已退药冲销。

    一页的关联处方合起来取（审方状态一条、冲销一条），不逐行查；没关联处方的一条也不查。
    """
    linked = {c.prescription_id for c in rows if c.prescription_id is not None}
    statuses: dict[int, str] = {}
    if linked:
        statuses = {
            rx_id: status
            for rx_id, status in db.query(Prescription.id, Prescription.status).filter(Prescription.id.in_(linked))
        }
    reversed_ids = _reversed_prescriptions(db, linked)
    for consult in rows:
        status = statuses.get(consult.prescription_id) if consult.prescription_id is not None else None
        setattr(consult, "prescription_status_name",
                None if status is None else PRESCRIPTION_STATUS_NAMES.get(status, status))
        setattr(consult, "prescription_dispense_reversed", consult.prescription_id in reversed_ids)
    return rows


@router.post(
    "/consults",
    response_model=ConsultOut,
    status_code=201,
    dependencies=[Depends(require_roles("operator", "doctor"))],  # H2/L5: 咨询建立
)
def create_consult(body: ConsultCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    consult = OnlineConsult(**body.model_dump())
    db.add(consult)
    db.commit()
    db.refresh(consult)
    return _with_prescription_state(db, [consult])[0]


@router.get("/consults", response_model=list[ConsultOut])
def list_consults(status: str | None = None, db: Session = Depends(get_db)):
    query = db.query(OnlineConsult)
    if status:
        query = query.filter(OnlineConsult.status == status)
    return _with_prescription_state(db, query.order_by(OnlineConsult.id.desc()).limit(200).all())


def _conflict(db: Session, consult: OnlineConsult, action: str) -> NoReturn:
    """条件翻转抢输：回滚、按库里此刻的状态报与顺序请求同一句 409（P2-404）。"""
    db.rollback()
    db.refresh(consult)
    raise HTTPException(
        status_code=409, detail=f"当前状态 {CONSULT_STATUS_NAMES.get(consult.status, consult.status)} {action}"
    )


@router.post("/consults/{consult_id}/reply", response_model=ConsultOut, dependencies=[Depends(require_roles("doctor"))])
def reply(consult_id: int, body: ReplyBody, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    consult = db.get(OnlineConsult, consult_id)
    if consult is None:
        raise HTTPException(status_code=404, detail="咨询不存在")
    if consult.status != "open":
        raise HTTPException(status_code=409, detail=f"当前状态 {CONSULT_STATUS_NAMES.get(consult.status, consult.status)} 不可回复")
    if body.prescription_id is not None:
        prescription = db.get(Prescription, body.prescription_id)
        if prescription is None:
            raise HTTPException(status_code=404, detail="关联处方不存在")
        if prescription.patient_id != consult.patient_id:
            raise HTTPException(status_code=422, detail="处方与咨询患者不一致")
        if prescription.status not in ("auto_passed", "approved"):
            raise HTTPException(status_code=409, detail="处方未通过审方，不可用于续方")
        # 退药冲销过的不能挂（P2-1476）：冲销只把发药记录置 reversed、处方表不动（它的状态是审方结论），上面那句照样放行——
        # 回复显示「已回复」、写着同意续方，患者拿去药房必 409（冲销后不可再发、确需再发的走新处方，`dispense.reverse_dispense`
        # 写明的规矩）。咨询之前就已发过药的旧处方、普通咨询挂不挂处方是业务口径（随 P2-900 待裁定），这里不拦
        if _reversed_prescriptions(db, {prescription.id}):
            raise HTTPException(status_code=409, detail="该处方已退药冲销，续方请开新处方")
    # 回复与「还待回复」压进同一条 UPDATE（P2-404）：上面的预检是锁外读的——两位医生同时回复，先回的答复被后回的整段
    # 盖掉、两路都 200；已回复并结束的咨询被迟到的回复翻回「已回复」
    values: dict = {"reply": body.reply, "doctor_name": body.doctor_name, "status": "replied"}
    if body.prescription_id is not None:
        values["prescription_id"] = body.prescription_id
    if not move_row(db, OnlineConsult, consult.id, OnlineConsult.status == "open", **values):
        _conflict(db, consult, "不可回复")
    db.commit()
    db.refresh(consult)
    return _with_prescription_state(db, [consult])[0]


@router.post(
    "/consults/{consult_id}/close",
    response_model=ConsultOut,
    dependencies=[Depends(require_roles("operator", "doctor"))],  # H2: 咨询结束
)
def close(consult_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    consult = db.get(OnlineConsult, consult_id)
    if consult is None:
        raise HTTPException(status_code=404, detail="咨询不存在")
    if consult.status != "replied":
        raise HTTPException(status_code=409, detail=f"当前状态 {CONSULT_STATUS_NAMES.get(consult.status, consult.status)} 不可结束")
    if not move_row(db, OnlineConsult, consult.id, OnlineConsult.status == "replied", status="closed"):
        _conflict(db, consult, "不可结束")
    db.commit()
    db.refresh(consult)
    return _with_prescription_state(db, [consult])[0]
