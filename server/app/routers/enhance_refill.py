"""E8 续方审核（医方侧，业务令牌）。

从 enhance_portal.py 分出：居民端（portal 令牌）与医方侧（业务令牌）分属两套鉴权，
拆开后医方侧接口继续受横向隔离门禁扫描（by-id 写须 assert_obj_org_writable），
居民端文件按 portal 令牌豁免。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import RefillRequest, User
from ..visibility import assert_obj_org_writable, scope_org_list

router = APIRouter(prefix="/api/refill-requests", tags=["续方审核"],
                   dependencies=[Depends(get_current_user)])


class RefillReview(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$")
    note: str = ""


def _refill_out(r: RefillRequest) -> dict:
    return {"id": r.id, "patient_id": r.patient_id, "org_id": r.org_id,
            "prescription_id": r.prescription_id, "drug_summary": r.drug_summary,
            "status": r.status, "reviewed_by": r.reviewed_by, "note": r.note,
            "created_at": r.created_at.isoformat()}


@router.get("")
def list_refills(status: str | None = None, org_id: int | None = None,
                 db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    q = db.query(RefillRequest)
    if status:
        q = q.filter(RefillRequest.status == status)
    q = scope_org_list(db, user, q, RefillRequest, org_id)
    return [_refill_out(r) for r in q.order_by(RefillRequest.id.desc()).limit(300).all()]


@router.patch("/{refill_id}/review", dependencies=[Depends(require_roles("doctor"))])
def review_refill(refill_id: int, body: RefillReview, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    r = db.get(RefillRequest, refill_id)
    if r is None:
        raise HTTPException(status_code=404, detail="续方申请不存在")
    assert_obj_org_writable(db, user, r)
    if r.status != "pending":
        raise HTTPException(status_code=409, detail=f"申请当前状态为 {r.status}，不可再审")
    r.status = body.decision
    r.reviewed_by = user.full_name or user.username
    if body.note:
        r.note = body.note
    db.commit()
    return _refill_out(r)


@router.patch("/{refill_id}/dispense",
              dependencies=[Depends(require_roles("pharmacist", "operator"))])
def dispense_refill(refill_id: int, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user)):
    r = db.get(RefillRequest, refill_id)
    if r is None:
        raise HTTPException(status_code=404, detail="续方申请不存在")
    assert_obj_org_writable(db, user, r)
    if r.status != "approved":
        raise HTTPException(status_code=409, detail="仅已通过的申请可配送")
    r.status = "dispensed"
    db.commit()
    return _refill_out(r)
