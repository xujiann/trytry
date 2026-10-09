"""统一知识库（⑯药物政策/临床指南查询、⑫转诊知识库、浙#38质管资料管理、⑬养生知识库）。

- 分类条目维护（管理层/公卫可发布，质管制度含有效期管理）
- 检索：分类 + 标题关键字；过期条目默认过滤、可显式包含并标记
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import clock
from ..database import get_db
from ..datetypes import OptionalDateStr
from ..texttypes import NON_BLANK
from ..deps import get_current_user, require_roles, resolve_business_date, keyword_like
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
    title: str = Field(min_length=1, max_length=256, pattern=NON_BLANK)
    body: str = Field(default="", max_length=4096)
    # 是否过期按字符串比 `expire_date < 今天`：`2026/01/01`、`20260101` 在同一年份里比出来是反的，
    # 过期九个月的药品政策照样当"有效"检索出来（P1-61，实测）
    expire_date: OptionalDateStr = ""


# 响应契约：字段与原手拼 dict 一一对应，保持响应向后兼容。
class EntryCreateOut(BaseModel):
    id: int
    category: str
    title: str


class EntryUpdateOut(BaseModel):
    id: int
    active: bool
    expire_date: str


class EntrySearchOut(BaseModel):
    id: int
    category: str
    category_name: str
    title: str
    body: str
    expire_date: str
    expired: bool


class EntryExpiringOut(BaseModel):
    id: int
    category: str
    title: str
    expire_date: str


def _expire_date_problem(expire_date: str | None) -> str:
    """手填「有效期至」的下界（P2-1668），没问题（或留空 = 长期有效、PATCH 没带 = 不改）返回空串。

    原先只查格式（P1-61）：续期把 2027-10-20 敲成 2025-10-20，PATCH 照收 200；发布时填了过去的日期照收 201。默认检索滤掉
    过期的、临期提醒只列今天及以后到期的，这一条当场从两处同时消失，页面上只提示成功——只有勾上「含过期」才看得到。
    有效期不得早于今天（`clock.today()` 本地业务日，与临期提醒同一把尺子），当天照收；照慢病「下次随访日不得早于今天」
    （`chronic._next_due_problem`，P2-1545）的写法。只判请求里带了的值，存量已过期的行不动。
    """
    if expire_date and expire_date < clock.today().isoformat():
        return f"有效期至（{expire_date}）不得早于今天"
    return ""


@router.post(
    "",
    status_code=201,
    response_model=EntryCreateOut,
    dependencies=[Depends(require_roles("director", "public_health"))],  # 知识条目发布
)
def create_entry(
    body: EntryCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    problem = _expire_date_problem(body.expire_date)   # 新发布就已过期的条目哪儿都不显示（P2-1668）
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    entry = KnowledgeEntry(created_by=user.id, **body.model_dump())
    db.add(entry)
    db.commit()
    return {"id": entry.id, "category": entry.category, "title": entry.title}


class EntryUpdate(BaseModel):
    body: str | None = Field(default=None, max_length=4096)
    # None = 不改；空串 = 改为长期有效；非空走同一个日历校验（续期那个 prompt 是自由文本）
    expire_date: OptionalDateStr | None = None
    active: bool | None = None


@router.patch(
    "/{entry_id}",
    response_model=EntryUpdateOut,
    dependencies=[Depends(require_roles("director", "public_health"))],  # 修订/停用/续期
)
def update_entry(entry_id: int, body: EntryUpdate, db: Session = Depends(get_db)):
    entry = db.get(KnowledgeEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="知识条目不存在")
    # 续期到过去的日期当场从检索与临期提醒里消失（P2-1668）：只判带了 expire_date 的请求，停用、修订正文不受影响
    problem = _expire_date_problem(body.expire_date)
    if problem:
        raise HTTPException(status_code=422, detail=problem)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(entry, field, value)
    db.commit()
    return {"id": entry.id, "active": entry.active, "expire_date": entry.expire_date}


@router.get("", response_model=list[EntrySearchOut])
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
        query = query.filter(keyword_like(KnowledgeEntry.title, q))
    # 过期的在库里就滤掉，再取最新 200 条（P2-176）：原先先取最新 200 条、再在内存里滤——最新那批里过期的一多
    # （成批导入的旧政策文件），更早录入、仍在有效期内的条目整个检索不到
    if not include_expired:
        query = query.filter(or_(KnowledgeEntry.expire_date == "", KnowledgeEntry.expire_date >= current))
    results = []
    for e in query.order_by(KnowledgeEntry.id.desc()).limit(200).all():
        expired = bool(e.expire_date) and e.expire_date < current
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


@router.get("/expiring", response_model=list[EntryExpiringOut])
def expiring_entries(
    days: int = Query(default=30, ge=0, le=3650),  # 加天数的上界（P1-96）：原先无界，传个大数 date + timedelta 溢出，整个请求 500
    today: str | None = None, db: Session = Depends(get_db),
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
