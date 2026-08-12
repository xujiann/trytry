"""站内消息中心（工作人员侧）。

投递逻辑在 `app/notify.py`，由业务流程内联调用；这里只负责读取与标记已读。
居民侧的读取接口在 `routers/portal.py` 的 `/me/notifications`——两类身份的
鉴权路径不同，接口也分开，避免在一个端点里做身份分支。
"""
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, paginate
from ..models import Notification, User, utcnow

router = APIRouter(
    prefix="/api/notifications", tags=["站内消息"], dependencies=[Depends(get_current_user)]
)


def notification_out(n: Notification) -> dict:
    return {
        "id": n.id,
        "category": n.category,
        "title": n.title,
        "body": n.body,
        "link_type": n.link_type,
        "link_id": n.link_id,
        "read": n.read_at is not None,
        "created_at": n.created_at.isoformat(),
    }


@router.get("")
def list_notifications(
    response: Response,
    unread_only: bool = False,
    category: str | None = None,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """我的消息（分页，未读在前、同状态按时间倒序）。"""
    query = db.query(Notification).filter(Notification.user_id == user.id)
    if unread_only:
        query = query.filter(Notification.read_at.is_(None))
    if category:
        query = query.filter(Notification.category == category)
    # 未读优先：read_at 为空排前面，再按时间倒序
    rows = paginate(
        query.order_by(Notification.read_at.isnot(None), Notification.id.desc()),
        response,
        offset,
        limit,
    )
    return [notification_out(n) for n in rows]


@router.get("/unread-count")
def unread_count(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """未读数：供角标轮询，单独一个轻查询，不必拉整页列表。"""
    count = (
        db.query(Notification)
        .filter(Notification.user_id == user.id, Notification.read_at.is_(None))
        .count()
    )
    return {"unread": count}


@router.post("/{notification_id}/read")
def mark_read(
    notification_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """标记已读。别人的消息按 404 处理，不暴露其存在。"""
    notification = db.get(Notification, notification_id)
    if notification is None or notification.user_id != user.id:
        raise HTTPException(status_code=404, detail="消息不存在")
    if notification.read_at is None:
        notification.read_at = utcnow()
        db.commit()
    return {"id": notification.id, "read": True}


@router.post("/read-all")
def mark_all_read(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """全部标记已读；返回本次标记数（已读的不重复计入）。"""
    updated = (
        db.query(Notification)
        .filter(Notification.user_id == user.id, Notification.read_at.is_(None))
        .update({Notification.read_at: utcnow()}, synchronize_session=False)
    )
    db.commit()
    return {"marked": updated}
