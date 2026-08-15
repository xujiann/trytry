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
| 消息 | `Notification` / `broadcast` | 任务催办的定向投递、扫描结果的实时广播 |
| 列类型 | `Money` / `utcnow` | 与平台其余表同一套金额与时间口径 |

清单之外的平台模块（处方、医保、库存……）**不在依赖范围内**。确有需要时，
先在这里加一行并说明理由，让依赖面始终是可数的。
"""
from sqlalchemy.orm import Session

# ruff: noqa: F401  —— 本模块的职责就是再导出，未被本文件使用是正常的
from ..models import (
    Admission,
    Encounter,
    Money,
    Notification,
    Organization,
    Patient,
    ResidentAccount,
    User,
    utcnow,
)
from ..routers.portal import accessible_patient, current_resident
from ..ws import manager as _ws_manager

#: 子系统对平台的依赖清单，供边界用例与架构文档共同引用（改这里即改约束）。
PLATFORM_MODELS = (
    "Patient", "Organization", "User", "Encounter", "Admission",
    "ResidentAccount", "Notification",
)


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
