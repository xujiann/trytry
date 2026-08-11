"""统一知识库（⑯药物政策/临床指南查询、⑫转诊知识库、浙#38质管资料管理、⑬养生知识库）。

- 分类条目维护（管理层/公卫可发布，质管制度含有效期管理）
- 检索：分类 + 标题关键字；过期条目默认过滤、可显式包含并标记
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles, resolve_business_date
from ..models import KnowledgeEntry, User

router = APIRouter(prefix="/api/knowledge", tags=["统一知识库"], dependencies=[Depends(get_current_user)])

CATEGORIES = {
    "drug_policy": "药物政策",
    "clinical_guideline": "临床指南",
    "referral": "转诊知识",
    "regulation": "质量制度规范",
    "tcm_health": "中医养生",
}


class EntryCreate(BaseModel):
    category: str = Field(pattern="^(drug_policy|clinical_guideline|referral|regulation|tcm_health)$")
    title: str = Field(min_length=1)
    body: str = ""
    expire_date: str = ""


@router.post(
    "",
    status_code=201,
    dependencies=[Depends(require_roles("director", "public_health"))],  # 知识条目发布
)
def create_entry(
    body: EntryCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    entry = KnowledgeEntry(created_by=user.id, **body.model_dump())
    db.add(entry)
    db.commit()
    return {"id": entry.id, "category": entry.category, "title": entry.title}


class EntryUpdate(BaseModel):
    body: str | None = None
    expire_date: str | None = None
    active: bool | None = None


@router.patch(
    "/{entry_id}",
    dependencies=[Depends(require_roles("director", "public_health"))],  # 修订/停用/续期
)
def update_entry(entry_id: int, body: EntryUpdate, db: Session = Depends(get_db)):
    entry = db.get(KnowledgeEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="知识条目不存在")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(entry, field, value)
    db.commit()
    return {"id": entry.id, "active": entry.active, "expire_date": entry.expire_date}


@router.get("")
def search_entries(
    category: str | None = None,
    q: str | None = None,
    include_expired: bool = False,
    today: str | None = None,
    db: Session = Depends(get_db),
):
    """知识检索：过期条目（有效期管理）默认不返回，include_expired=true 时返回并标记。"""
    current = resolve_business_date(today).isoformat()
    query = db.query(KnowledgeEntry).filter(KnowledgeEntry.active.is_(True))
    if category:
        query = query.filter(KnowledgeEntry.category == category)
    if q:
        query = query.filter(KnowledgeEntry.title.contains(q))
    results = []
    for e in query.order_by(KnowledgeEntry.id.desc()).limit(200).all():
        expired = bool(e.expire_date) and e.expire_date < current
        if expired and not include_expired:
            continue
        results.append(
            {
                "id": e.id,
                "category": e.category,
                "category_name": CATEGORIES.get(e.category, e.category),
                "title": e.title,
                "body": e.body,
                "expire_date": e.expire_date,
                "expired": expired,
            }
        )
    return results


@router.get("/expiring")
def expiring_entries(
    days: int = 30, today: str | None = None, db: Session = Depends(get_db)
):
    """临近过期资料提醒（浙#38 有效期管理）：expire_date 距今 ≤days 的在用条目。"""
    from datetime import timedelta

    current = resolve_business_date(today)
    deadline = (current + timedelta(days=days)).isoformat()
    return [
        {"id": e.id, "category": e.category, "title": e.title, "expire_date": e.expire_date}
        for e in db.query(KnowledgeEntry)
        .filter(
            KnowledgeEntry.active.is_(True),
            KnowledgeEntry.expire_date != "",
            KnowledgeEntry.expire_date <= deadline,
            KnowledgeEntry.expire_date >= current.isoformat(),
        )
        .order_by(KnowledgeEntry.expire_date)
        .all()
    ]
