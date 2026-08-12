"""站内消息投递（与 WebSocket 广播互补）。

广播解决"在线的人马上看到"，站内消息解决"不在线的人回来也能看到"。
两者并行：危急值既广播也落消息，前者求快，后者求不丢。

投递函数只 `db.add()` **不 commit**——它们被业务流程内联调用，提交时机
必须由业务事务决定。否则报告出具失败回滚了，通知却已经发出去，
用户收到一条指向不存在记录的消息。
"""
import logging

from sqlalchemy.orm import Session

from .models import Notification, ResidentAccount, ResidentFamilyMember, User

logger = logging.getLogger("medplat.notify")

# 单次投递的收件人上限：一家机构的同角色人数再多也不该无限展开
MAX_RECIPIENTS = 200


def notify_staff(
    db: Session,
    *,
    category: str,
    title: str,
    body: str = "",
    org_id: int | None = None,
    roles: tuple[str, ...] = (),
    link_type: str = "",
    link_id: int = 0,
) -> int:
    """给工作人员投递；返回收件人数。

    `org_id` 限定机构（None 表示全平台），`roles` 限定角色（空表示不限）。
    admin/director 是否收到由调用方通过 roles 显式决定，这里不做隐式扩散——
    否则每条危急值都惊动全院管理层。
    """
    query = db.query(User)
    if org_id is not None:
        query = query.filter(User.org_id == org_id)
    if roles:
        query = query.filter(User.role.in_(roles))
    recipients = query.limit(MAX_RECIPIENTS).all()
    for user in recipients:
        db.add(
            Notification(
                user_id=user.id,
                category=category,
                title=title,
                body=body,
                link_type=link_type,
                link_id=link_id,
            )
        )
    return len(recipients)


def notify_patient(
    db: Session,
    patient_id: int,
    *,
    category: str,
    title: str,
    body: str = "",
    link_type: str = "",
    link_id: int = 0,
) -> int:
    """给某位患者的居民账户投递；返回收件人数（0 表示还没人绑定该档案）。

    本人绑定的账户与**代管该档案的家属账户**都会收到——儿童与失能老人的
    消息本来就该发给代管人，只发给"本人账户"等于发进黑洞。
    """
    account_ids = {
        a.id
        for a in db.query(ResidentAccount)
        .filter(ResidentAccount.patient_id == patient_id, ResidentAccount.status == "active")
        .all()
    }
    account_ids |= {
        m.account_id
        for m in db.query(ResidentFamilyMember)
        .filter(ResidentFamilyMember.patient_id == patient_id)
        .all()
    }
    for account_id in sorted(account_ids)[:MAX_RECIPIENTS]:
        db.add(
            Notification(
                resident_account_id=account_id,
                category=category,
                title=title,
                body=body,
                link_type=link_type,
                link_id=link_id,
            )
        )
    return len(account_ids)
