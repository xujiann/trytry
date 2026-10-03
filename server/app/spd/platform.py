"""慢专病子系统 → 医共体平台的**唯一**依赖入口。

子系统的其余模块一律经由本文件访问平台，不直接 `from app.models import ...`。
`tests/test_spd_boundary.py` 把这条约束钉住。

## 为什么要收口

慢专病共用平台的患者主索引、机构树、账号与就诊数据（招标文件平台管理端 #5/#13、
#21 明确要求"共用底座"），这是对的——复制一份主数据等于复制一份永久对账工作。
但"共用"必须是**一条可数的依赖**，而不是散落在九个路由文件里的十几处 import：

- 平台哪天调整 `Encounter` 的诊断字段，改这里一处，而不是去十几个地方找；
- 采购方问"这个子系统到底用了平台什么"，答案就是本文件的清单，不用翻代码；
- 将来若要把子系统装到别的平台上（同一套业务、不同的底座），要重写的
  只有这一层。

## 允许依赖的平台能力（白名单）

| 类别 | 内容 | 用途 |
|---|---|---|
| 主数据 | `Patient` / `Organization` / `User` | 患者归属、三级机构树、责任人 |
| 诊疗数据 | `Encounter` / `Admission` | 纳入规则取诊断、随访方案取出院信息 |
| 居民端 | `ResidentAccount` / `current_resident` / `accessible_patient` | 患者移动端身份与"能看谁的档案" |
| 消息 | `Notification` / `broadcast` / `notify_resident` / `send_sms` | 定向投递、实时广播、居民触达、短信通道 |
| 告警 | `send_alert`（经 `broadcast` 用） | 广播无人在线时转发运维告警 webhook，与平台 `jobs._alert` 同一句（P2-273） |
| 附件 | `Attachment` / `store_attachment` / `register_attachment_owner` / `quarantined_copy` | 任务佐证材料走平台附件服务（白名单/限额/去重同一份）；被病毒扫描隔离的不算佐证，判据与下载侧 410 同一份（P2-1251） |
| 公卫数据 | `FollowUp`（慢病随访） | publichealth 采集器的数据源 |
| 列类型 | `Money` / `utcnow` | 与平台其余表同一套金额与时间口径 |
| PII 检索 | `pii_filter` | 加密态证件号等值检索：开态密文列 contains 恒空，必须走索引列（P1-25） |
| 证件号写法 | `id_card_variants` | 末位校验码 X 大小写两种写法都认（P1-114）：主索引按录入原样存，检索不能只认一种 |
| 角色 | `ROLE_NAMES` / `GLOBAL_ROLES` | 挑责任人时认内置角色：角色办不了这项工作的不派（第二十二批 X3-1）、全域角色不按机构拦 |

清单之外的平台模块（处方、医保、库存……）**不在依赖范围内**。确有需要时，
先在这里加一行并说明理由，让依赖面始终是可数的。
"""
from sqlalchemy.orm import Session

# ruff: noqa: F401  —— 本模块的职责就是再导出，未被本文件使用是正常的
from ..models import (
    Admission,
    Attachment,
    Encounter,
    FollowUp,
    Money,
    Notification,
    Organization,
    Patient,
    ResidentAccount,
    SystemParam,
    User,
    utcnow,
)
from ..notify import notify_patient as _notify_patient
from ..notify import patient_recipients as _patient_recipients
from ..notify import BODY_MAX as _NOTIFY_BODY_MAX, TITLE_MAX as _NOTIFY_TITLE_MAX
# PII 加密态等值检索（P1-25）：开态下证件号密文列 contains 恒空，spd 的证件号
# 筛选必须与平台 patients.py 走同一条索引列等值路径。只再导出 pii_filter 这一个
# 名字——加解密原语（encrypt/decrypt）不在依赖面里，子系统不该碰密文本身。
from ..pii import pii_filter
# 证件号末位 X 大小写（P1-114）：同一个号两种写法，平台主索引一处定义，spd 的证件号检索照用，
# 不在子系统里另写一份「怎么算同一个号」。
from ..routers.patients import id_card_variants
from ..routers.organizations import ORG_LEVEL_NAMES  # 机构层级文案（纳管网络树显示，P2-74）
from ..routers.encounters import ENCOUNTER_TYPE_NAMES  # 就诊类型文案：平台驾驶舱与慢专病随访前置资料同一份（P2-646）
# 二维码 SVG：实现在平台侧 qrsvg（ADR-0015 打印件验真也要用），spd 经这里取。
# 方向由此变顺：原实现长在 spd 内部时，平台侧想复用只能违反单向依赖。
from ..qrsvg import qr_svg
from ..wechat import get_wechat_provider as _get_wechat_provider
from ..routers.attachments import register_owner as _register_attachment_owner
from ..routers.attachments import quarantined_copy as _quarantined_copy
from ..routers.attachments import store_upload as _store_upload
from ..routers.portal import accessible_patient, current_resident
from ..routers.portal import REFERRAL_FEED_LIMIT as _REFERRAL_FEED_LIMIT
from ..routers.portal import _org_names as _platform_org_names
from ..routers.portal import ENROLLMENT_FEED_LIMIT as _ENROLLMENT_FEED_LIMIT
from ..routers.portal import enrollment_feed_item as _enrollment_feed_item
from ..routers.portal import referral_feed_item as _referral_feed_item
from ..routers.portal import register_enrollment_source as _register_enrollment_source
from ..routers.portal import register_referral_source as _register_referral_source
from ..sms import get_sms_provider as _get_sms_provider
from ..ws import manager as _ws_manager
from ..alerting import send_alert as _send_alert
from ..deps import ROLE_NAMES as _ROLE_NAMES
from ..visibility import GLOBAL_ROLES as _GLOBAL_ROLES

def patient_of(db: Session, patient_id: int) -> Patient | None:
    return db.get(Patient, patient_id)


def unusable_user(db: Session, user_id: int) -> str:
    """这个账号能不能承接新业务：能就返回空串，不能返回「不存在」或「已停用」，供调用处拼进报错。

    停用（`status = disabled`）即时生效——`deps.get_current_user` 每请求校验，停用的人登录不了。
    把新任务、随访、纳管患者、团队职务挂到这样的账号名下，就没有人办了（P1-106）。已经挂在它
    名下的存量不归这里管：停用可能是暂时的，存量怎么转交由业务决定。

    返回原因而不是布尔：「不存在」沿用各处原有的报错文案（P1-90），「已停用」是新增的一种。
    """
    user = db.get(User, user_id)
    if user is None:
        return "不存在"
    return "已停用" if user.status == "disabled" else ""


def assignee_outside_org(db: Session, assignee_id: int, org_id: int | None) -> bool:
    """显式指定的责任人办不了挂在这家机构的业务：不是全域角色、又不在该机构（第十六批 T2-1 / 第十九批 K1-2）。

    办事的写接口都要求「能以记录所属机构的名义写入」（`assert_org_writable`）——指给别家机构的人，他打开是 403；
    本机构的人认领又是 409（已有责任人），这条记录谁都办不了。与 `assert_org_writable` 同一判据：记录不挂机构的不拦，
    全域角色（县级中心）不拦。调用前先经 `unusable_user` 查过存在与停用。慢专病任务（P1-210）与目标池分发（P2-725）共用；
    系统替人挑的责任人（档案上的主管医生在别家机构时派生的任务）另行裁定（P1-211）。
    """
    if org_id is None:
        return False
    assignee = db.get(User, assignee_id)
    return assignee is not None and assignee.role not in _GLOBAL_ROLES and assignee.org_id != org_id


def role_unfit(db: Session, user_id: int, roles: tuple[str, ...]) -> str:
    """这个账号的角色办不了这项工作：返回角色的中文名（「经办人员」「药师」……），办得了返回空串（第二十二批 X3-1）。

    各办理端点按角色把门（`require_roles`）：慢专病任务、目标患者、复诊、干预是医师 / 公卫 / 管理层，在线咨询的回复是
    医师 / 管理层。主管医生后来被改成经办、药师（改角色只改 `role`，名下的档案一概不动），系统照旧把新派生的工作挂给他——
    他打开 403，别人接收又 409（已有责任人），这件事就没人办了；显式指派给这样的人也一样。后果与停用（`unusable_user`）
    相同，分开判是因为报错不同：人在、角色不对，是 422（与 `assignee_outside_org` 同一类），不是 404。

    只认六个内置角色：平台管理员过一切角色门，不拦；自定义角色按权限点放行（`deps.require_roles`），哪几个权限点算
    「办得了」要看具体动作，这里不猜，照旧当办得了。`roles` 为空不筛。不存在的返回空串——调用前先经 `unusable_user`
    查过存在与停用。
    """
    user = db.get(User, user_id)
    if user is None or not roles or user.role == "admin" or user.role in roles or user.role not in _ROLE_NAMES:
        return ""
    return _ROLE_NAMES[user.role]


def usable_or_none(db: Session, user_id: int | None, *, roles: tuple[str, ...]) -> int | None:
    """系统替人挑责任人（取档案上的主管医生、转诊发起人）时用：能承接这项工作就原样返回，停用、已不存在、角色办不了
    的落成 None。

    落成 None 的工作是「待接收 / 未分配」：中心端待办里数得到、别人认领得了。原先照挂停用账号——认领要么是空着要么
    是本人，别人认领 409，中心端「未分配」也不数它，新派生的处置任务进了一个没人登得上的待办箱（第十五批 S1-1）。
    角色办不了的同一个后果（第二十二批 X3-1）：主管医生改成经办之后，新派生的任务照挂给他，他办理 403、别人接收 409。
    `roles` 是这项工作的办理角色（各办理端点 `require_roles` 的那一组），必传：挑人的地方各自说清这活谁办得了；只是
    知会（发站内信）的传空元组，不按角色筛。
    显式指定的人仍由各写接口经 `unusable_user` / `role_unfit` 拒掉（P1-106）；已经挂在停用账号名下的存量怎么转交另行裁定。
    """
    if user_id is None or unusable_user(db, user_id) or role_unfit(db, user_id, roles):
        return None
    return user_id


def diagnosis_codes(db: Session, patient_id: int) -> list[str]:
    """患者**全部历史就诊**的诊断编码（含 ICD-10 父目）。

    取全部历史而不是最近一次：慢病的纳入依据是"曾被确诊"，
    按最近一次判定会让一个来看感冒的高血压患者掉出目标池。
    父目一并给出（`I10.x` → 也产出 `I10`），否则病种规则要把亚目列全。
    四位亚目同样给出（P2-1077）：国临版的扩展码 `E11.201` → 也产出 `E11.2` 与 `E11`——原先只补三位类目，按亚目写的规则
    （如糖尿病肾病 `E11.2`）碰到国临版编码永不命中；小数点后是占位符 `x`（`I10.x00`）的没有亚目，只产出类目。
    """
    codes: list[str] = []
    for enc in db.query(Encounter).filter(Encounter.patient_id == patient_id).all():
        code = getattr(enc, "diagnosis_code", "")
        if code:
            code = str(code)
            head, _, tail = code.partition(".")
            codes.append(code)
            if tail[:1].isascii() and tail[:1].isdigit():   # 只认 ASCII 数字（P1-97 的口径）
                codes.append(f"{head}.{tail[0]}")
            codes.append(head)
    return list(dict.fromkeys(codes))


def diagnosis_names(db: Session, patient_id: int) -> list[str]:
    return [
        enc.diagnosis_name
        for enc in db.query(Encounter).filter(Encounter.patient_id == patient_id).all()
        if getattr(enc, "diagnosis_name", "")
    ]


def org_level(db: Session, org_id: int | None) -> str:
    """机构层级 → 转诊链路层级。村卫生室=village，乡镇=township，其余按县级处理。"""
    if org_id is None:
        return "village"
    org = db.get(Organization, org_id)
    if org is None:
        return "village"
    return {"village": "village", "township": "township", "county": "county",
            "city": "county"}.get(org.level, "township")


def notify_user(
    db: Session, user_id: int, *, category: str, title: str, body: str,
    link_type: str = "", link_id: int = 0,
) -> None:
    """给**某一个人**投递站内消息。

    平台的 `notify.notify_staff` 是按机构 + 角色群发的，发不到具体某个人；
    任务催办要发给任务的责任人，所以这里直接落 `Notification`。
    与 `notify.py` 同一契约：只 `add` 不 `commit`，提交时机由业务事务决定；标题 / 正文超列宽同样截断（P1-164）。
    """
    db.add(
        Notification(
            user_id=user_id, category=category, title=title[:_NOTIFY_TITLE_MAX], body=body[:_NOTIFY_BODY_MAX],
            link_type=link_type, link_id=link_id,
        )
    )


def broadcast(kind: str, title: str, count: int) -> None:
    """把扫描结果推给在线的管理端；确定无人收到时转发运维告警 webhook 摘要兜底。

    与平台 `jobs.py::_alert` 同一形状——同一件事在两处长得一样，
    看日志的人不必分辨"这条是哪个模块推的"。兜底原先只在平台那一份里（P2-273）：夜间 / 节假日无人在线时，
    慢专病的任务超期与数据源同步结果广播即丢，平台的同类预警却会转发 webhook（未配置 webhook 时仍是空操作）。
    """
    if not count:
        return
    delivered = _ws_manager.broadcast({"type": kind, "title": title, "count": count})
    if not delivered:
        _send_alert(f"unattended:{kind}", f"{title}：{count} 条（无在线管理端，广播未送达）")


def send_sms(phone: str, content: str) -> bool:
    """经平台短信通道外发；成功返回 True。通道实现不抛异常，失败一律 False。"""
    if not phone:
        return False
    return _get_sms_provider().send(phone, content)


def notify_resident(
    db: Session, patient_id: int, *, category: str, title: str, body: str,
    link_type: str = "", link_id: int = 0,
) -> int:
    """给患者绑定的居民账户投递站内消息；返回收件人数（0=尚无人绑定该档案）。"""
    return _notify_patient(
        db, patient_id, category=category, title=title, body=body,
        link_type=link_type, link_id=link_id,
    )


#: 慢专病宣教的微信模板参数键。与平台站内信的模板旁路同一套约定
#: （`notify.WECHAT_TEMPLATE_PARAM_PREFIX + 类目`），由管理端 /api/mgmt/params 维护，
#: 不进 config——各县用哪个模板 id 是运营配置，不是部署配置。
WECHAT_EDU_TEMPLATE_KEY = "wechat_template_spd_edu"


def send_wechat_edu(db: Session, patient_id: int, *, title: str, body: str) -> tuple[int, str]:
    """给患者绑定的微信推一条宣教模板消息；返回 (送达人数, 说明)。

    走平台既有的公众号通道（`app/wechat.py` 的 provider，mock/official 两实现），
    模板 id 从系统参数取——与站内信的模板旁路完全同一套配置，不另起一套。

    三种"发不出去"给出**不同的原因**，而不是笼统一句失败：没配模板（运营没配）、
    没人绑定微信（患者侧）、接口未受理（通道侧）。这三件事的处置完全不同，
    合成一句"发送失败"等于让实施期去猜。
    """
    param = db.query(SystemParam).filter(SystemParam.key == WECHAT_EDU_TEMPLATE_KEY).first()
    if param is None or not param.value:
        return 0, f"未配置微信宣教模板（系统参数 {WECHAT_EDU_TEMPLATE_KEY}）"
    # 发给谁与站内信同一个口径（P2-502）：本人账户 + 代管家属账户。原先只认本人绑定的——儿童、失能老人的宣教由代管的
    # 家属收，本人名下往往根本没有账户，推送记「尚无绑定微信」失败，家属那头一条也收不到
    openids = [
        a.wechat_openid
        for a in db.query(ResidentAccount)
        .filter(
            ResidentAccount.id.in_(_patient_recipients(db, patient_id)),
            ResidentAccount.status == "active",
            ResidentAccount.wechat_openid.isnot(None),
        )
        .order_by(ResidentAccount.id)
        .all()
        if a.wechat_openid
    ]
    if not openids:
        return 0, "该患者及代管家属尚无绑定微信的居民账号"
    send = getattr(_get_wechat_provider(), "send_template_message", None)
    if send is None:  # 测试注入的旧桩件可能没实现模板消息
        return 0, "当前微信通道不支持模板消息"
    delivered = sum(1 for openid in openids if send(openid, param.value, {"title": title, "body": body}, ""))
    if not delivered:
        return 0, "微信模板消息接口未受理（通道侧失败）"
    return delivered, f"已推送 {delivered} 个微信账号"


def register_attachment_owner(
    owner_type: str, model: type, roles: tuple[str, ...], scope: str, **kwargs
) -> None:
    """把子系统的业务对象登记为附件挂接域（装载时调用一次）。

    `scope` 必填——附件的可见性口径由**登记方**声明，平台不替子系统猜
    （见 `routers/attachments.OwnerSpec`）。
    """
    _register_attachment_owner(owner_type, model, roles, scope, **kwargs)


def register_referral_source(name: str, loader) -> None:
    """把子系统的转诊单登记进居民端**读侧聚合**（ADR-0003 方案 B）。

    平台不能 import 子系统（依赖方向），所以由子系统在装载时把自己的读取函数
    递过去；子系统关掉，聚合接口就只剩平台那一个源。
    """
    _register_referral_source(name, loader)


def referral_feed_item(**kwargs) -> dict:
    """聚合列表的统一条目形状（由平台定义，子系统照此产出）。"""
    return _referral_feed_item(**kwargs)


#: 聚合列表的条数上限，由平台统一定义——子系统各写各的字面量，
#: 改一处就会静默少报另一处的数据。
REFERRAL_FEED_LIMIT = _REFERRAL_FEED_LIMIT


def register_enrollment_source(name: str, loader) -> None:
    """把子系统的入组档案登记进居民端**读侧聚合**（ADR-0003 方案 B）。"""
    _register_enrollment_source(name, loader)


def enrollment_feed_item(**kwargs) -> dict:
    """入组聚合列表的统一条目形状（由平台定义，子系统照此产出）。"""
    return _enrollment_feed_item(**kwargs)


#: 入组聚合的条数上限，由平台统一定义。
ENROLLMENT_FEED_LIMIT = _ENROLLMENT_FEED_LIMIT


def org_names(db, ids) -> dict:
    """按 id 批量取机构名（与平台源共用同一实现）。"""
    return _platform_org_names(db, ids)


def store_attachment(
    db: Session, *, data: bytes, filename: str, content_type: str,
    owner_type: str, owner_id: int, uploaded_by: int | None,
) -> Attachment:
    """存一份附件（校验白名单/限额/去重与平台上传完全同一份代码）。不 commit。"""
    return _store_upload(
        db, data=data, filename=filename, content_type=content_type,
        owner_type=owner_type, owner_id=owner_id, uploaded_by=uploaded_by,
    )


def _quarantined(db: Session, attachment: Attachment) -> bool:
    """这份附件已被病毒扫描隔离：本行 infected，或同一份内容（sha256）的任意一行 infected——与下载侧
    （`attachments.download_attachment` 的 410，P2-395）同一个判据（P2-1251）。"""
    return attachment.scan_status == "infected" or _quarantined_copy(db, attachment.sha256) is not None


def valid_task_evidence(db: Session, task_id: int, evidence: list) -> list[str]:
    """校验佐证清单里的每一项都是挂在该任务上的真实附件，返回问题列表（空=通过）。

    佐证从"任意字符串"收紧为附件 id：`require_evidence` 是节点配置里勾选过的
    硬要求，能用随便一串字符糊弄过去，配置就形同虚设。被病毒扫描隔离的附件同样不收（P2-1251）：
    下载侧对它一律 410，审核人打不开，原先照样拿来提交、办结。
    """
    problems: list[str] = []
    for item in evidence or []:
        try:
            attachment_id = int(item)
        except (TypeError, ValueError):
            problems.append(f"佐证「{item}」不是附件编号")
            continue
        attachment = db.get(Attachment, attachment_id)
        if attachment is None:
            problems.append(f"附件 #{attachment_id} 不存在")
        elif attachment.owner_type != "spd_task" or attachment.owner_id != task_id:
            problems.append(f"附件 #{attachment_id} 不属于该任务")
        elif _quarantined(db, attachment):
            problems.append(f"附件 #{attachment_id} 已被病毒扫描隔离")
    return problems


def usable_task_evidence(db: Session, evidence: list) -> list:
    """佐证清单里还算数的条目：去掉已被病毒扫描隔离的附件（P2-1251）。

    要佐证的任务在医护端提交 / 办结、居民端提交时数的是它，而不是「清单非空」：扫描是异步的，常常先记进佐证、补扫之后
    才判出病毒，清单里只剩一张隔离件时审核人一张也打不开（下载 410），任务却照样办结。只按此刻的扫描结论剔隔离件，
    其余条目照旧计数——编号不对、不属于该任务的，记进清单时已由 `valid_task_evidence` 拦下。
    """
    usable = []
    for item in evidence or []:
        try:
            attachment = db.get(Attachment, int(item))
        except (TypeError, ValueError):
            attachment = None
        if attachment is None or not _quarantined(db, attachment):
            usable.append(item)
    return usable


def evidence_urls(evidence: list) -> list[dict]:
    """佐证附件的下载地址（登录鉴权后可取）。"""
    out = []
    for item in evidence or []:
        try:
            attachment_id = int(item)
        except (TypeError, ValueError):
            continue
        out.append({"attachment_id": attachment_id,
                    "url": f"/api/attachments/{attachment_id}"})
    return out


def iter_recent_chronic_followups(db: Session, since, limit: int = 500, after_id: int = 0):
    """公卫慢病随访记录（含 patient_id），供 publichealth 采集器同步体征。

    联表在这里做——FollowUp 挂在 chronic_patients 下，没有 patient_id 列，
    这是平台数据的形状，不该让采集器知道。按 id 升序一页 `limit` 条，`after_id` 翻下一页
    （调用方要把窗口取完——只取第一页，窗口里多于一页的随访永远轮不到，P2-94）。
    """
    from ..models import ChronicPatient

    rows = (
        db.query(FollowUp, ChronicPatient.patient_id)
        .join(ChronicPatient, ChronicPatient.id == FollowUp.chronic_id)
        .filter(FollowUp.created_at >= since, FollowUp.id > after_id)
        .order_by(FollowUp.id)
        .limit(limit)
        .all()
    )
    return rows
