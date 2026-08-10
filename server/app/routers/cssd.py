"""消毒供应中心：复用器械批次灭菌→发放→回收全流程追溯。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import Organization, SterilizationBatch
from ..schemas import BatchCreate, BatchOut

router = APIRouter(prefix="/api/cssd", tags=["消毒供应"], dependencies=[Depends(get_current_user)])

_FLOW = {"sterilizing": "sterile", "sterile": "dispatched", "dispatched": "recycled"}


@router.post(
    "/batches",
    response_model=BatchOut,
    status_code=201,
    dependencies=[Depends(require_roles("operator"))],  # H2: 消毒批次=经办
)
def create_batch(body: BatchCreate, db: Session = Depends(get_db)):
    if db.get(Organization, body.center_org_id) is None:
        raise HTTPException(status_code=404, detail="消毒供应中心机构不存在")
    if db.query(SterilizationBatch).filter(SterilizationBatch.batch_no == body.batch_no).first():
        raise HTTPException(status_code=409, detail="批次号已存在")
    batch = SterilizationBatch(**body.model_dump())
    db.add(batch)
    db.commit()
    db.refresh(batch)
    return batch


@router.get("/batches", response_model=list[BatchOut])
def list_batches(status: str | None = None, batch_no: str | None = None, db: Session = Depends(get_db)):
    query = db.query(SterilizationBatch)
    if status:
        query = query.filter(SterilizationBatch.status == status)
    if batch_no:
        query = query.filter(SterilizationBatch.batch_no == batch_no)
    return query.order_by(SterilizationBatch.id.desc()).limit(200).all()


@router.post(
    "/batches/{batch_id}/advance",
    response_model=BatchOut,
    dependencies=[Depends(require_roles("operator"))],  # H2: 批次流转=经办
)
def advance(batch_id: int, dispatched_to_org_id: int | None = None, db: Session = Depends(get_db)):
    """流转到下一状态：灭菌中→已灭菌→已发放（需指定接收机构）→已回收。"""
    batch = db.get(SterilizationBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    next_status = _FLOW.get(batch.status)
    if next_status is None:
        raise HTTPException(status_code=409, detail=f"状态 {batch.status} 已是终态")
    if next_status == "dispatched":
        if dispatched_to_org_id is None:
            raise HTTPException(status_code=422, detail="发放需指定接收机构")
        if db.get(Organization, dispatched_to_org_id) is None:
            raise HTTPException(status_code=404, detail="接收机构不存在")
        batch.dispatched_to_org_id = dispatched_to_org_id
    batch.status = next_status
    db.commit()
    db.refresh(batch)
    return batch
