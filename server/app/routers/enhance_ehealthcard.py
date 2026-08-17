"""S15 电子健康卡一卡通：发卡、展码核身、医保凭证绑定、冻结挂失。

设计要点：

- **一人一卡**：发卡按 `patient_id` 唯一，并发重复发卡由 `insert_or_conflict`
  收成一句 409，而非 500。
- **确定性派生**：`card_no` / `qr_token` 由 `f"{patient.id}:{patient.ehc_no}:ehcard"`
  经国密 SM3 杂凑得到，不用随机源。患者 id 唯一，派生值对不同患者天然不撞；
  同一患者重发也得同一张卡号（幂等于身份）。
- **患者可见性一律走 `assert_patient_visible`**：`Patient` 无 org_id，凡带
  patient_id 的读写都过它（判定 + 留痕绑死）。唯独 `/verify` 是"刷卡核身"入口——
  按 token 反查，token 本身即凭证（等同读卡器读到的卡），故不在路径里带
  patient_id，也就没有可先行判定的患者；由 operator 角色守住入口。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles
from ..gmcrypto import sm3_hash
from ..concurrency import insert_or_conflict
from ..visibility import assert_patient_visible
from ..models import EHealthCard, Patient, User

router = APIRouter(
    prefix="/api/ehealth-cards",
    tags=["电子健康卡"],
    dependencies=[Depends(get_current_user)],
)


# ---------------------------------------------------------------- 派生
def _digest(patient: Patient) -> str:
    """患者稳定标识 → SM3 十六进制摘要（64 位）。

    以 `id:ehc_no:ehcard` 为输入：id 全局唯一保证不同患者摘要不同，
    ehc_no 一并纳入让摘要与对外主索引绑定。纯确定性，无随机源。
    """
    base = f"{patient.id}:{patient.ehc_no}:ehcard"
    return sm3_hash(base.encode("utf-8")).hex()


def _card_no(patient: Patient) -> str:
    return "EHC" + _digest(patient)[:12].upper()


def _qr_token(patient: Patient) -> str:
    return _digest(patient)


# ---------------------------------------------------------------- schemas
class IssueIn(BaseModel):
    patient_id: int


class VerifyIn(BaseModel):
    qr_token: str = Field(min_length=1, max_length=64)


class BindVoucherIn(BaseModel):
    medical_voucher: str = Field(min_length=1, max_length=64)


def _card_out(card: EHealthCard) -> dict:
    return {
        "id": card.id,
        "patient_id": card.patient_id,
        "card_no": card.card_no,
        "qr_token": card.qr_token,
        "medical_voucher": card.medical_voucher,
        "status": card.status,
        "issued_by": card.issued_by,
    }


# ---------------------------------------------------------------- 发卡
@router.post("", status_code=201, dependencies=[Depends(require_roles("operator"))])
def issue_card(body: IssueIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """发卡：一患者一卡。重复发卡撞唯一约束 → 409。"""
    patient = db.get(Patient, body.patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    assert_patient_visible(db, user, patient.id, resource="ehealth_card")
    card = EHealthCard(
        patient_id=patient.id,
        card_no=_card_no(patient),
        qr_token=_qr_token(patient),
        issued_by=user.id,
    )
    insert_or_conflict(db, card, "该患者已有电子健康卡")
    return _card_out(card)


# ---------------------------------------------------------------- 查卡
@router.get("/{patient_id}")
def get_card(patient_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """查看患者的卡。`assert_patient_visible` 已把可见性把住，故一并返回 qr_token。"""
    assert_patient_visible(db, user, patient_id, resource="ehealth_card")
    card = db.query(EHealthCard).filter(EHealthCard.patient_id == patient_id).first()
    if card is None:
        raise HTTPException(status_code=404, detail="该患者尚未发卡")
    return _card_out(card)


# ---------------------------------------------------------------- 展码
@router.post("/{patient_id}/show-qr")
def show_qr(patient_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """展码：返回 qr_token 供窗口/读卡端核身。"""
    assert_patient_visible(db, user, patient_id, resource="ehealth_card_qr")
    card = db.query(EHealthCard).filter(EHealthCard.patient_id == patient_id).first()
    if card is None:
        raise HTTPException(status_code=404, detail="该患者尚未发卡")
    return {"qr_token": card.qr_token, "patient_id": patient_id}


# ---------------------------------------------------------------- 核身
@router.post("/verify", dependencies=[Depends(require_roles("operator"))])
def verify_card(body: VerifyIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """一卡通就诊核身入口：按 qr_token 反查患者（等同刷卡读卡）。

    token 本身即凭证，无 patient_id 路径参数，故不走 assert_patient_visible——
    由 operator 角色守住入口，语义与实体读卡器一致。
    """
    card = db.query(EHealthCard).filter(EHealthCard.qr_token == body.qr_token).first()
    if card is None:
        raise HTTPException(status_code=404, detail="无效的健康卡凭证")
    if card.status != "active":
        raise HTTPException(status_code=409, detail="卡已冻结")
    patient = db.get(Patient, card.patient_id)
    return {
        "patient_id": card.patient_id,
        "card_no": card.card_no,
        "patient": {
            "name": patient.name if patient else "",
            "ehc_no": patient.ehc_no if patient else "",
        },
    }


# ---------------------------------------------------------------- 绑定医保凭证
@router.patch("/{patient_id}/bind-voucher", dependencies=[Depends(require_roles("operator"))])
def bind_voucher(
    patient_id: int,
    body: BindVoucherIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """绑定医保电子凭证号。"""
    assert_patient_visible(db, user, patient_id, resource="ehealth_card")
    card = db.query(EHealthCard).filter(EHealthCard.patient_id == patient_id).first()
    if card is None:
        raise HTTPException(status_code=404, detail="该患者尚未发卡")
    card.medical_voucher = body.medical_voucher
    db.commit()
    db.refresh(card)
    return _card_out(card)


# ---------------------------------------------------------------- 冻结/解冻
@router.patch("/{patient_id}/freeze", dependencies=[Depends(require_roles("operator", "admin"))])
def freeze_card(patient_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """冻结（挂失/停用）：核身入口据此拒绝。"""
    assert_patient_visible(db, user, patient_id, resource="ehealth_card")
    card = db.query(EHealthCard).filter(EHealthCard.patient_id == patient_id).first()
    if card is None:
        raise HTTPException(status_code=404, detail="该患者尚未发卡")
    card.status = "frozen"
    db.commit()
    db.refresh(card)
    return _card_out(card)


@router.patch("/{patient_id}/unfreeze", dependencies=[Depends(require_roles("operator", "admin"))])
def unfreeze_card(patient_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """解冻：恢复正常。"""
    assert_patient_visible(db, user, patient_id, resource="ehealth_card")
    card = db.query(EHealthCard).filter(EHealthCard.patient_id == patient_id).first()
    if card is None:
        raise HTTPException(status_code=404, detail="该患者尚未发卡")
    card.status = "active"
    db.commit()
    db.refresh(card)
    return _card_out(card)
