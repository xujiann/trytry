"""统一编码字典：诊断、药品、耗材、收费"四统一"，结果互认与业务联动的数据基础。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..concurrency import insert_if_absent
from ..database import get_db
from ..deps import get_current_user, require_admin
from ..models import CodeEntry, CodeSystem
from ..schemas import CodeEntryCreate, CodeEntryOut

router = APIRouter(prefix="/api/dictionaries", tags=["统一编码字典"])

SYSTEM_CODES = {"diagnosis": "诊断(ICD-10)", "drug": "药品", "consumable": "耗材", "charge": "收费"}


def _get_system(db: Session, system_code: str) -> CodeSystem:
    """M5 整改：字典类型于应用启动时种子化（main.lifespan），读路径不再写库。

    兜底：若历史库缺少种子（如未经启动初始化的存量库），仅在写路径按需补建，
    并捕获并发唯一约束冲突后重查，保证幂等。
    """
    if system_code not in SYSTEM_CODES:
        raise HTTPException(status_code=404, detail=f"未知字典类型: {system_code}")
    system = db.query(CodeSystem).filter(CodeSystem.code == system_code).first()
    if system is None:
        system = CodeSystem(code=system_code, name=SYSTEM_CODES[system_code])
        db.add(system)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            system = db.query(CodeSystem).filter(CodeSystem.code == system_code).first()
            if system is None:  # pragma: no cover
                raise
            return system
        db.refresh(system)
    return system


def _get_system_readonly(db: Session, system_code: str) -> CodeSystem | None:
    """GET 只读路径：不产生写副作用（M5）。种子由启动初始化保证。"""
    if system_code not in SYSTEM_CODES:
        raise HTTPException(status_code=404, detail=f"未知字典类型: {system_code}")
    return db.query(CodeSystem).filter(CodeSystem.code == system_code).first()


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


@router.post(
    "/{system_code}/import",
    dependencies=[Depends(require_admin)],
)
def bulk_import(system_code: str, entries: list[CodeEntryCreate], db: Session = Depends(get_db)):
    """标准字典全量/增量导入：已存在的编码跳过，返回导入统计。"""
    system = _get_system(db, system_code)
    existing = {
        code for (code,) in db.query(CodeEntry.code).filter(CodeEntry.system_id == system.id).all()
    }
    imported = 0
    for entry in entries:
        if entry.code in existing:
            continue
        # 预读的 existing 只是省一次 SAVEPOINT 的快路径，不是判据：
        # 两个导入并发跑会读到同一份 existing，都判定"不存在"就都去插。
        # 而这里是一次 commit 提交整批，一条撞车会让**整批回滚**——
        # 500 加上一条都没导进去。落库成不成以 insert_if_absent 为准。
        if not insert_if_absent(db, CodeEntry(system_id=system.id, **entry.model_dump())):
            continue
        existing.add(entry.code)
        imported += 1
    db.commit()
    return {"imported": imported, "skipped": len(entries) - imported}


@router.get(
    "/{system_code}/entries",
    response_model=list[CodeEntryOut],
    dependencies=[Depends(get_current_user)],
)
def list_entries(system_code: str, keyword: str = "", db: Session = Depends(get_db)):
    system = _get_system_readonly(db, system_code)
    if system is None:
        # 种子缺失（存量库未经启动初始化）：读路径保持只读，返回空清单
        return []
    query = db.query(CodeEntry).filter(CodeEntry.system_id == system.id)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter((CodeEntry.code.like(like)) | (CodeEntry.name.like(like)))
    return query.order_by(CodeEntry.code).limit(200).all()
