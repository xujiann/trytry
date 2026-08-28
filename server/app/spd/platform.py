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
| 附件 | `Attachment` / `store_attachment` / `register_attachment_owner` | 任务佐证材料走平台附件服务（白名单/限额/去重同一份） |
| 公卫数据 | `FollowUp`（慢病随访） | publichealth 采集器的数据源 |
| 列类型 | `Money` / `utcnow` | 与平台其余表同一套金额与时间口径 |
| PII 检索 | `pii_filter` | 加密态证件号等值检索：开态密文列 contains 恒空，必须走索引列（P1-25） |

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
# PII 加密态等值检索（P1-25）：开态下证件号密文列 contains 恒空，spd 的证件号
# 筛选必须与平台 patients.py 走同一条索引列等值路径。只再导出 pii_filter 这一个
# 名字——加解密原语（encrypt/decrypt）不在依赖面里，子系统不该碰密文本身。
from ..pii import pii_filter
# 二维码 SVG：实现在平台侧 qrsvg（ADR-0015 打印件验真也要用），spd 经这里取。
# 方向由此变顺：原实现长在 spd 内部时，平台侧想复用只能违反单向依赖。
from ..qrsvg import qr_svg
from ..wechat import get_wechat_provider as _get_wechat_provider
from ..routers.attachments import register_owner as _register_attachment_owner
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

def patient_of(db: Session, patient_id: int) -> Patient | None:
    return db.get(Patient, patient_id)


def diagnosis_codes(db: Session, patient_id: int) -> list[str]:
    """患者**全部历史就诊**的诊断编码（含 ICD-10 父目）。

    取全部历史而不是最近一次：慢病的纳入依据是"曾被确诊"，
    按最近一次判定会让一个来看感冒的高血压患者掉出目标池。
    父目一并给出（`I10.x` → 也产出 `I10`），否则病种规则要把亚目列全。
    """
    codes: list[str] = []
    for enc in db.query(Encounter).filter(Encounter.patient_id == patient_id).all():
        code = getattr(enc, "diagnosis_code", "")
        if code:
            codes.append(code)
            codes.append(str(code).split(".")[0])
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
    与 `notify.py` 同一契约：只 `add` 不 `commit`，提交时机由业务事务决定。
    """
    db.add(
        Notification(
            user_id=user_id, category=category, title=title, body=body,
            link_type=link_type, link_id=link_id,
        )
    )


def broadcast(kind: str, title: str, count: int) -> None:
    """把扫描结果推给在线的管理端；无人在线时是空操作。

    与平台 `jobs.py::_alert` 同一形状——同一件事在两处长得一样，
    看日志的人不必分辨"这条是哪个模块推的"。
    """
    if count:
        _ws_manager.broadcast({"type": kind, "title": title, "count": count})


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
    openids = [
        a.wechat_openid
        for a in db.query(ResidentAccount)
        .filter(
            ResidentAccount.patient_id == patient_id,
            ResidentAccount.status == "active",
            ResidentAccount.wechat_openid.isnot(None),
        )
        .all()
        if a.wechat_openid
    ]
    if not openids:
        return 0, "该患者尚无绑定微信的居民账号"
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


def valid_task_evidence(db: Session, task_id: int, evidence: list) -> list[str]:
    """校验佐证清单里的每一项都是挂在该任务上的真实附件，返回问题列表（空=通过）。

    佐证从"任意字符串"收紧为附件 id：`require_evidence` 是节点配置里勾选过的
    硬要求，能用随便一串字符糊弄过去，配置就形同虚设。
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
    return problems


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


def iter_recent_chronic_followups(db: Session, since, limit: int = 500):
    """公卫慢病随访记录（含 patient_id），供 publichealth 采集器同步体征。

    联表在这里做——FollowUp 挂在 chronic_patients 下，没有 patient_id 列，
    这是平台数据的形状，不该让采集器知道。
    """
    from ..models import ChronicPatient

    rows = (
        db.query(FollowUp, ChronicPatient.patient_id)
        .join(ChronicPatient, ChronicPatient.id == FollowUp.chronic_id)
        .filter(FollowUp.created_at >= since)
        .order_by(FollowUp.id)
        .limit(limit)
        .all()
    )
    return rows
