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

**模板消息等业务事务提交之后才发**（P2-348）。站内信「不 commit、随事务走」，模板消息原先却在投递函数里当场
外呼：业务事务随后回滚（手术排程撞约束 409、报告重复出具 409……），患者手机上已经收到那条指向不存在记录的消息——
正是上面那段要防的；外呼还发生在业务事务的行锁里（单次最多几秒、逐个收件人），SQLite 下连进程级的行锁一起占着。
现在投递函数只把要发的消息登记在会话上，最外层事务提交后再发，回滚或关会话即丢弃。
"""
import logging

from sqlalchemy import event
from sqlalchemy.orm import Session, SessionTransaction

from .models import Notification, ResidentAccount, ResidentFamilyMember, SystemParam, User
from .wechat import get_wechat_provider

logger = logging.getLogger("medplat.notify")

# 单次投递的收件人上限：一家机构的同角色人数再多也不该无限展开
MAX_RECIPIENTS = 200
#: 站内信标题 / 正文的列宽（`notifications.title` String(128) / `body` String(1024)，用例钉着两边同一个数）。
#: 标题多是「手术已安排：<手术名>」这种拼出来的，手术名本身就收 256 字——拼完超列宽，生产库整个业务事务 500（P1-164）
TITLE_MAX = 128
BODY_MAX = 1024

#: 模板消息的系统参数前缀：wechat_template_exam_report / wechat_template_followup …
#: 参数经管理端 /api/mgmt/params 维护（与 I1 的 FHIR 水位同一张表），不进 config。
WECHAT_TEMPLATE_PARAM_PREFIX = "wechat_template_"


#: 会话上登记的、等业务事务提交后再发的模板消息：[(账户 id, openid, 模板 id, 数据, 类目), ...]
_PENDING_WECHAT = "medplat_pending_wechat"


def _wechat_template_bypass(
    db: Session, account_ids: list[int], *, category: str, title: str, body: str
) -> None:
    """微信模板消息旁路：未配置模板/未绑 openid 即整体跳过，是缺省状态。

    这里只查模板与 openid、把要发的消息登记在会话上（`_PENDING_WECHAT`），外呼等最外层事务提交之后（P2-348）。
    任何异常都吞掉只 log——触达通道抖动不该让"出报告/办出院"失败；站内信在此之前已 db.add()，本函数不碰事务。
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
        accounts = (
            db.query(ResidentAccount)
            .filter(ResidentAccount.id.in_(account_ids), ResidentAccount.wechat_openid.isnot(None))
            .all()
        )
        db.info.setdefault(_PENDING_WECHAT, []).extend(
            (account.id, account.wechat_openid, param.value, {"title": title, "body": body}, category)
            for account in accounts
        )
    except Exception:
        logger.exception("[NOTIFY-WECHAT] 模板消息旁路异常，忽略（站内信不受影响）")


@event.listens_for(Session, "after_commit")
def _send_pending_wechat(session: Session) -> None:
    """最外层事务提交后发出登记的模板消息。保存点释放也会触发本事件——那时外层事务还可能回滚，不发。"""
    if session.in_nested_transaction():
        return
    pending = session.info.pop(_PENDING_WECHAT, None)
    if not pending:
        return
    try:
        send = getattr(get_wechat_provider(), "send_template_message", None)
        if send is None:  # 测试注入的旧桩件可能没实现该方法
            return
        for account_id, openid, template_id, data, category in pending:
            if not send(openid, template_id, data, ""):
                logger.warning(
                    "[NOTIFY-WECHAT] 模板消息发送失败 account=%s category=%s", account_id, category
                )
    except Exception:
        logger.exception("[NOTIFY-WECHAT] 模板消息旁路异常，忽略（站内信不受影响）")


@event.listens_for(Session, "after_transaction_end")
def _drop_pending_wechat(session: Session, transaction: SessionTransaction) -> None:
    """最外层事务结束（回滚、关会话；提交的已在上面发完取走）：没发出去的一律丢弃，不留给下一段事务。"""
    if transaction.parent is None:
        session.info.pop(_PENDING_WECHAT, None)


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
    title, body = title[:TITLE_MAX], body[:BODY_MAX]   # 拼出来的标题超列宽截断，不让业务事务 500（P1-164）
    # 只投在用的账号（P2-349）：停用的登录不了，消息投给它们等于没投——原先连它们一起按编号取前 200 个，
    # 一家机构停用的老账号多了，新来的在用医师收不到危急值
    query = db.query(User).filter(User.status == "active")
    if org_id is not None:
        query = query.filter(User.org_id == org_id)
    if roles:
        query = query.filter(User.role.in_(roles))
    recipients = query.order_by(User.id).limit(MAX_RECIPIENTS).all()
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


def patient_recipients(db: Session, patient_id: int) -> set[int]:
    """某位患者的消息该投给哪些居民账户：本人绑定的在用账户 + 代管该档案的家属里在用的账户（口径见 `notify_patient`）。

    站内信与慢专病微信宣教（`spd.platform.send_wechat_edu`，P2-502）共用这一处——原先宣教只认本人账户，
    代管的家属一条都收不到。家属一支与本人一支同样只认在用账户（P2-765）：原先不看账户状态，停用的家属账户（停用后
    令牌校验即失败、登不上）照收站内信，报告出具、宣教照往它的 openid 推微信模板消息（带检查项目名）。
    """
    account_ids = {
        a.id
        for a in db.query(ResidentAccount)
        .filter(ResidentAccount.patient_id == patient_id, ResidentAccount.status == "active")
        .all()
    }
    account_ids |= {
        account_id
        for (account_id,) in db.query(ResidentFamilyMember.account_id)
        .join(ResidentAccount, ResidentAccount.id == ResidentFamilyMember.account_id)
        .filter(ResidentFamilyMember.patient_id == patient_id, ResidentAccount.status == "active")
        .all()
    }
    return account_ids


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
    title, body = title[:TITLE_MAX], body[:BODY_MAX]   # 拼出来的标题超列宽截断，不让业务事务 500（P1-164）
    account_ids = patient_recipients(db, patient_id)
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
