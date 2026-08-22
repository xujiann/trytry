"""站内消息投递（与 WebSocket 广播互补）。

广播解决"在线的人马上看到"，站内消息解决"不在线的人回来也能看到"。
两者并行：危急值既广播也落消息，前者求快，后者求不丢。

投递函数只 `db.add()` **不 commit**——它们被业务流程内联调用，提交时机
必须由业务事务决定。否则报告出具失败回滚了，通知却已经发出去，
用户收到一条指向不存在记录的消息。

工程包 I2：居民侧站内信之上加一条**微信模板消息旁路**——账户绑定了
openid 且该类目配置了模板 id（SystemParam，key = ``wechat_template_<类目>``）
时顺带推一条模板消息。旁路是尽力而为：失败只 log，绝不影响站内信落库，
更不反过来拖垮业务事务。
"""
import logging

from sqlalchemy.orm import Session

from .models import Notification, ResidentAccount, ResidentFamilyMember, SystemParam, User
from .wechat import get_wechat_provider

logger = logging.getLogger("medplat.notify")

# 单次投递的收件人上限：一家机构的同角色人数再多也不该无限展开
MAX_RECIPIENTS = 200

#: 模板消息的系统参数前缀：wechat_template_exam_report / wechat_template_followup …
#: 参数经管理端 /api/mgmt/params 维护（与 I1 的 FHIR 水位同一张表），不进 config。
WECHAT_TEMPLATE_PARAM_PREFIX = "wechat_template_"


def _wechat_template_bypass(
    db: Session, account_ids: list[int], *, category: str, title: str, body: str
) -> None:
    """微信模板消息旁路：未配置模板/未绑 openid 即整体跳过，是缺省状态。

    任何异常都吞掉只 log——触达通道抖动不该让"出报告/办出院"失败；
    站内信在此之前已 db.add()，本函数不碰事务。
    """
    if not account_ids:
        return
    try:
        param = (
            db.query(SystemParam)
            .filter(SystemParam.key == WECHAT_TEMPLATE_PARAM_PREFIX + category)
            .first()
        )
        if param is None or not param.value:
            return
        provider = get_wechat_provider()
        send = getattr(provider, "send_template_message", None)
        if send is None:  # 测试注入的旧桩件可能没实现该方法
            return
        accounts = (
            db.query(ResidentAccount)
            .filter(ResidentAccount.id.in_(account_ids), ResidentAccount.wechat_openid.isnot(None))
            .all()
        )
        for account in accounts:
            if not send(account.wechat_openid, param.value, {"title": title, "body": body}, ""):
                logger.warning(
                    "[NOTIFY-WECHAT] 模板消息发送失败 account=%s category=%s", account.id, category
                )
    except Exception:
        logger.exception("[NOTIFY-WECHAT] 模板消息旁路异常，忽略（站内信不受影响）")


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
    recipients = sorted(account_ids)[:MAX_RECIPIENTS]
    for account_id in recipients:
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
    # I2：绑定了 openid 且配置了该类目模板时，旁路推一条微信模板消息（尽力而为）
    _wechat_template_bypass(db, recipients, category=category, title=title, body=body)
    return len(account_ids)
