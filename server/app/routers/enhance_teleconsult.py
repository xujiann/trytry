"""S7 在线复诊/图文咨询——医方应答端。

E8 居民端 `POST /api/portal/me/online-consults` 建单后 `status="open"`，
但一直没有医方接口去应答，状态永远卡在 open。这里补上医方（业务令牌）侧：
列表、回复（open/replied → replied）、结束（→ closed）。

状态口径沿用 telemedicine.py 已有的 open → replied → closed，
不新增词汇；OnlineConsult 复用不建表。

A 案隔离：OnlineConsult 属机构（org_id 为居民发起时指定的应答机构），
- 列表走 `scope_org_list`（按调用者可见机构收口）；
- 按 id 的回复/结束走 `assert_obj_org_writable`（防"按 id 绕过清单"跨机构写他院单）。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import OnlineConsult, User
from ..visibility import assert_obj_org_writable, scope_org_list

router = APIRouter(
    prefix="/api/online-consults",
    tags=["互联网复诊-医方"],
    dependencies=[Depends(get_current_user)],
)


# ---------------------------------------------------------------- schemas
class ConsultOut(BaseModel):
    id: int
    patient_id: int
    org_id: int
    consult_type: str
    question: str
    reply: str
    doctor_name: str
    status: str
    prescription_id: int | None

    model_config = {"from_attributes": True}


class ReplyBody(BaseModel):
    reply: str = Field(min_length=1)
    doctor_name: str | None = None


# ---------------------------------------------------------------- 列表
@router.get("", response_model=list[ConsultOut])
def list_consults(
    status: str | None = None,
    org_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """医方侧咨询清单：任何登录业务账号可查，按可见机构范围收口。"""
    q = db.query(OnlineConsult)
    if status:
        q = q.filter(OnlineConsult.status == status)
    q = scope_org_list(db, user, q, OnlineConsult, org_id)
    return q.order_by(OnlineConsult.id.desc()).limit(200).all()


# ---------------------------------------------------------------- 回复
@router.patch(
    "/{consult_id}/reply",
    response_model=ConsultOut,
    dependencies=[Depends(require_roles("doctor"))],
)
def reply_consult(
    consult_id: int,
    body: ReplyBody,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    consult = db.get(OnlineConsult, consult_id)
    if consult is None:
        raise HTTPException(status_code=404, detail="咨询不存在")
    assert_obj_org_writable(db, user, consult)
    if consult.status not in ("open", "replied"):
        raise HTTPException(status_code=409, detail=f"当前状态 {consult.status} 不可回复")
    consult.reply = body.reply
    consult.doctor_name = body.doctor_name or user.full_name or user.username
    consult.status = "replied"
    db.commit()
    db.refresh(consult)
    return consult


# ---------------------------------------------------------------- 结束
@router.patch(
    "/{consult_id}/close",
    response_model=ConsultOut,
    dependencies=[Depends(require_roles("doctor"))],
)
def close_consult(
    consult_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    consult = db.get(OnlineConsult, consult_id)
    if consult is None:
        raise HTTPException(status_code=404, detail="咨询不存在")
    assert_obj_org_writable(db, user, consult)
    if consult.status == "closed":
        raise HTTPException(status_code=409, detail="咨询已结束")
    consult.status = "closed"
    db.commit()
    db.refresh(consult)
    return consult
