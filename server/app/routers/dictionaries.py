"""统一编码字典：诊断、药品、耗材、收费"四统一"，结果互认与业务联动的数据基础。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_admin
from ..models import CodeEntry, CodeSystem
from ..schemas import CodeEntryCreate, CodeEntryOut

router = APIRouter(prefix="/api/dictionaries", tags=["统一编码字典"])

SYSTEM_CODES = {"diagnosis": "诊断(ICD-10)", "drug": "药品", "consumable": "耗材", "charge": "收费"}


def _get_system(db: Session, system_code: str) -> CodeSystem:
    if system_code not in SYSTEM_CODES:
        raise HTTPException(status_code=404, detail=f"未知字典类型: {system_code}")
    system = db.query(CodeSystem).filter(CodeSystem.code == system_code).first()
    if system is None:
        system = CodeSystem(code=system_code, name=SYSTEM_CODES[system_code])
        db.add(system)
        db.commit()
        db.refresh(system)
    return system


@router.post(
    "/{system_code}/entries",
    response_model=CodeEntryOut,
    status_code=201,
    dependencies=[Depends(require_admin)],
)
def create_entry(system_code: str, body: CodeEntryCreate, db: Session = Depends(get_db)):
    system = _get_system(db, system_code)
    if (
        db.query(CodeEntry)
        .filter(CodeEntry.system_id == system.id, CodeEntry.code == body.code)
        .first()
    ):
        raise HTTPException(status_code=409, detail="编码已存在")
    entry = CodeEntry(system_id=system.id, **body.model_dump())
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


@router.get(
    "/{system_code}/entries",
    response_model=list[CodeEntryOut],
    dependencies=[Depends(get_current_user)],
)
def list_entries(system_code: str, keyword: str = "", db: Session = Depends(get_db)):
    system = _get_system(db, system_code)
    query = db.query(CodeEntry).filter(CodeEntry.system_id == system.id)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter((CodeEntry.code.like(like)) | (CodeEntry.name.like(like)))
    return query.order_by(CodeEntry.code).limit(200).all()
