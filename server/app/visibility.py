"""横向数据隔离：谁能看哪个机构、谁能看哪个患者。

第九轮的主线。此前平台只防了**纵向越权**（医生不能干管理员的事），
越权矩阵测试一直绿着、还报"覆盖率 100%"——但**横向越权一条没测**。
实测：乙镇卫生院的医生按 ehc_no 就能调阅甲县医院患者的档案（HTTP 200），
也能拉到全部患者清单。涉及 `org_id` 的 203 个函数里只有 20 个做了调用者
机构校验。

与第八轮那道"只关了 11% 的闸门"是同一个形状：`deps.resolve_org_scope`
有 26 个文件在用，看起来像作用域控制，但它是**筛选器不是授权器**——
它解析"调用方想看哪个机构"，从不校验"调用方能不能看这个机构"。

## 口径（A 案）

默认只能看本机构；跨机构要有一条**明确的业务理由**。医共体的存在意义就是
跨机构协同（结果互认、双向转诊、签约随访），一刀切禁止等于把平台关掉，
所以这里建的是可见范围模型，不是墙。

## 患者可见性一律走业务关系

`Patient` 表**没有 org_id**——它是全县主索引，一个人一份档案，这在医共体里
是对的。所以不存在"本机构的患者"这种东西，可见性只能由业务关系推出来：

| 依据 | 含义 |
|---|---|
| `global` | admin / director |
| `self` | 居民端账号看自己的档案 |
| `encounter` | 该患者在本机构就诊过 |
| `service` | 本机构存有该患者的业务记录（就诊之外：体检、慢病、接种、住院、开单…）|
| `contract` | 该患者与本机构签了家庭医生 |
| `referral` | 有转入或转出本机构的转诊单 |
| `authorization` | 患者授权本机构调阅（浙#59 模型，且未过期未撤销） |

另有一类**按业务设计就要跨机构**的接口（结果互认查询、授权办理），
用 `log_patient_access` 只留痕不阻断，依据记为 `recognition` / `consent_admin`——
详见该函数。

**判定与留痕是同一个动作**。`assert_patient_visible` 校验通过后自己写
`AccessLog`——不做成"先校验、再记得去记一笔"，因为今天已经反复看到：
需要人记得的事就会被忘掉（D-5～D-7 三次复发、D-8 两个 GET 忘了换出参）。
"能看"与"看了什么、凭什么"绑死在一次调用里，漏不掉。
"""
from datetime import date

import sqlalchemy as sa
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .clock import now_naive
from .database import SessionLocal
from .models import (
    AccessLog,
    ArchiveAuthorization,
    Base,
    Encounter,
    FamilyDoctorContract,
    OrgGroupMember,
    Referral,
    User,
)

# 全域可见的角色。director（业务院长/管委会）要看全县运行情况，
# admin 是系统管理员——两者都留痕，全域不等于不记账。
GLOBAL_ROLES = {"admin", "director"}

__all__ = [
    "visible_org_ids",
    "assert_org_visible",
    "assert_org_writable",
    "stats_org_ids",
    "patient_basis",
    "assert_patient_visible",
    "log_patient_access",
    "visible_patient_ids",
    "scope_patient_list",
    "scope_org_list",
]


# 推导服务关系表时要排除的：它们带 patient_id 与机构外键，但不构成"本机构在
# 为这个患者服务"。漏排一个的后果是把可见性放宽，所以这份清单要写理由。
_NOT_A_RELATION = {
    # 留痕表本身。用它当依据会自我循环：查过一次就永远有权再查。
    "access_logs",
    # 授权表：grantee_org_id 是被授权方，另有专门分支按有效期判定，
    # 直接当服务关系会绕过"过期/撤销"这两条。
    "archive_authorizations",
}


def _relation_tables():
    """从模型元数据推导"患者↔机构"关系表：(模型, 机构列名列表)。

    不手工维护清单——手工清单必漏，而这里漏一条的后果是医生在诊室里
    打不开本该看的档案。新模块只要按惯例带上 patient_id 与机构外键，
    自动就被算进服务关系。
    """
    out = []
    for cls in Base.registry._class_registry.values():
        if not hasattr(cls, "__tablename__"):
            continue
        if cls.__tablename__ in _NOT_A_RELATION:
            continue
        cols = cls.__table__.columns
        if "patient_id" not in cols:
            continue
        org_cols = [c.name for c in cols if c.name.endswith("org_id")]
        if org_cols:
            out.append((cls, org_cols))
    return out


# ---------------------------------------------------------------- 机构维度


def visible_org_ids(db: Session, user: User) -> list[int] | None:
    """该用户能看到**明细数据**的机构 id；`None` 表示全域。

    返回 None 而不是"全部机构 id"，与 `deps.resolve_org_scope` 保持同一约定：
    拿到 None 就不加过滤，既省一次全表查询，也让"没限制"和"限制到 0 家"
    在代码里不会长得一样。
    """
    if user.role in GLOBAL_ROLES:
        return None
    if user.org_id is None:
        # 没挂机构的业务账号：看不到任何机构明细。宁可什么都看不到，
        # 也不默认放行——默认放行的账号最后总会有人拿去当通用账号使。
        return []
    return [user.org_id]


def assert_org_visible(db: Session, user: User, org_id: int | None) -> None:
    """校验该用户可以看这个机构的明细数据，不行就 403。"""
    if org_id is None:
        return
    allowed = visible_org_ids(db, user)
    if allowed is None or org_id in allowed:
        return
    raise HTTPException(status_code=403, detail="无权查看该机构的数据")


def assert_org_writable(db: Session, user: User, org_id: int | None) -> None:
    """校验调用者可以**以这家机构的名义写入**，不行就 403。

    读侧的洞是看见别家，写侧的洞重得多：实测乙卫生院的 director 能给
    甲县医院记一笔 88888 的支出、建一个假职工——**这是在替别家做账**。
    集中核算的数字要是能被任何成员机构写进别家账本，汇总就没有意义了。

    规则与读侧明细同一档：非全域角色只能写本机构（org_id 为 None 的记录
    不在此列，由各接口自己定语义）。刻意不用统计那档——统计是"看得宽"，
    写永远不能宽。

    与 `assert_org_visible` 分开成两个函数，虽然此刻实现几乎一样：
    读与写的口径将来多半会分化（例如允许授权代录），合在一起到时就得
    在每个调用点回忆当初是读是写。
    """
    if org_id is None:
        return
    if user.role in GLOBAL_ROLES:
        return
    if user.org_id == org_id:
        return
    raise HTTPException(status_code=403, detail="无权以该机构名义写入数据")


def stats_org_ids(db: Session, user: User) -> list[int] | None:
    """**汇总统计**可见范围：本机构 + 同医共体（分组）内的成员机构。

    比明细宽一档，这是刻意的：牵头医院要看得到片区运行情况，否则医共体的
    管理职能无从谈起。但统计只到聚合数，不含患者明细——两者用不同的函数，
    免得哪天有人图省事拿统计范围去查明细。
    """
    if user.role in GLOBAL_ROLES:
        return None
    if user.org_id is None:
        return []
    groups = [
        g for (g,) in db.query(OrgGroupMember.group_id)
        .filter(OrgGroupMember.org_id == user.org_id)
        .all()
    ]
    if not groups:
        return [user.org_id]
    peers = {
        o for (o,) in db.query(OrgGroupMember.org_id)
        .filter(OrgGroupMember.group_id.in_(groups))
        .all()
    }
    peers.add(user.org_id)
    return sorted(peers)


# ---------------------------------------------------------------- 患者维度


def patient_basis(db: Session, user: User, patient_id: int) -> str | None:
    """该用户看这个患者的**依据**；看不了返回 None。

    依据按"最不需要解释"的顺序判：本机构就诊过 > 签约 > 转诊 > 患者授权。
    先判哪个不影响能不能看，只影响留痕里记下的那个词，所以取最直接的那条。
    """
    if user.role in GLOBAL_ROLES:
        return "global"
    if user.org_id is None:
        return None
    org_id = user.org_id

    if (
        db.query(Encounter.id)
        .filter(Encounter.patient_id == patient_id, Encounter.org_id == org_id)
        .first()
        is not None
    ):
        return "encounter"

    if (
        db.query(FamilyDoctorContract.id)
        .filter(
            FamilyDoctorContract.patient_id == patient_id,
            FamilyDoctorContract.org_id == org_id,
            FamilyDoctorContract.status == "active",
        )
        .first()
        is not None
    ):
        return "contract"

    # 服务关系：本机构存有这个患者的任何一条业务记录。
    #
    # **第一版只写了"就诊过"**，是临床视角，漏掉一整片。县域里卫生院与村卫生室
    # 干的大量是公卫：建档体检、管高血压糖尿病、打疫苗，这些居民常常
    # 一条 `Encounter` 都没有——真按"就诊过"判，管了三年高血压的村医
    # 反而看不到自己管的人。补了体检、慢病、接种、住院之后，检查开单又露出来：
    # 医生给患者开了检查单，同样是服务关系。
    #
    # 于是不再手工枚举，改为**从模型元数据推导**（`_relation_tables`）：
    # 凡同时带 `patient_id` 与机构外键的表，都算一种服务关系。理由同第八轮那条
    # 只覆盖 11% 的扫描——手工清单永远漏，而这里漏一条的后果是
    # 医生在诊室里打不开该看的档案。
    #
    # 代价是这条判据偏宽：本机构存过这个患者的任何一条记录就算有关系。
    # 这是**刻意选的方向**——风险不对等：错拦一次是诊疗事故，
    # 而"同县机构存有该患者记录"本就是一条真实关系，且每次调阅都留痕可查。
    for model, org_cols in _relation_tables():
        cond = None
        for col in org_cols:
            c = getattr(model, col) == org_id
            cond = c if cond is None else (cond | c)
        if (
            db.query(model.id).filter(model.patient_id == patient_id, cond).first()
            is not None
        ):
            # 转诊单独成一个依据词：双向转诊是医共体最核心的协同场景，
            # 事后审计要一眼分得出"这次调阅是因为转诊"。
            return "referral" if model is Referral else "service"

    # 患者授权：过期与撤销都当作无效。**按日期现算不看状态字段**，
    # 理由与疫苗禁忌一致——定时任务改状态会让"何时失效"取决于任务跑没跑，
    # 而这条判定直接决定档案给不给看。
    today = date.today().isoformat()
    grants = (
        db.query(ArchiveAuthorization)
        .filter(
            ArchiveAuthorization.patient_id == patient_id,
            ArchiveAuthorization.grantee_org_id == org_id,
            ArchiveAuthorization.status == "active",
        )
        .all()
    )
    if any(not g.expire_date or g.expire_date >= today for g in grants):
        return "authorization"

    return None


def _write_access_log(user: User, patient_id: int, resource: str, basis: str) -> None:
    """留痕落库，走独立会话。

    与写审计同一条理由：**与业务事务解耦**。读接口本身不提交事务，
    在业务会话里写这一笔会把每一次患者档案调阅变成一次写事务——
    SQLite 上就是每读一次拿一次写锁（第八轮实测过 `database is locked`）。

    留痕失败不能挡住看病。记不下来是运维问题，看不了病是医疗事故，
    两者不对等，所以这里吞掉异常——但**吞掉的是落库失败，不是校验失败**，
    校验不过在上面就已经 403 了。
    """
    db = SessionLocal()
    try:
        db.add(
            AccessLog(
                user_id=user.id,
                username=user.username,
                org_id=user.org_id,
                patient_id=patient_id,
                resource=resource,
                basis=basis,
                created_at=now_naive(),
            )
        )
        db.commit()
    except Exception:  # pragma: no cover - 留痕失败不阻断诊疗
        db.rollback()
    finally:
        db.close()


def visible_patient_ids(db: Session, user: User):
    """本机构服务过的患者 id 子查询；全域角色返回 None（不加过滤）。

    给那些**自己不带机构列**的表用：老年人能力评估、妇女保健记录、就诊凭据、
    预约、费用明细——它们只有 `patient_id`，没有"这条记录是哪家机构做的"。
    没有机构列就没法直接按机构筛，只能反过来问：这个患者是不是本机构服务过的。

    这本身暴露一个**数据模型缺口**（登记在案，见 `docs`）：这几张表连
    "哪家机构做的"都答不出来，于是机构工作量统计也做不了。补 `org_id` 是根治，
    但要迁移与回填，不在本批；这里先用可见患者集合把越权堵上。

    实现上用 `UNION ALL` 拼成一个子查询交给数据库，不在 Python 里物化——
    县医院服务过的患者接近全县人口，取回内存再过滤等于把整张表搬一遍。
    """
    if user.role in GLOBAL_ROLES:
        return None
    if user.org_id is None:
        return select(Encounter.patient_id).where(sa.false())
    parts = []
    for model, org_cols in _relation_tables():
        cond = None
        for col in org_cols:
            c = getattr(model, col) == user.org_id
            cond = c if cond is None else (cond | c)
        parts.append(select(model.patient_id).where(cond))
    return parts[0].union_all(*parts[1:]) if len(parts) > 1 else parts[0]


def scope_patient_list(
    db: Session,
    user: User,
    query,
    model,
    patient_id: int | None,
    resource: str,
):
    """患者维度**清单接口**的统一收口。

    抽出来而不是在 20 个接口里各写一遍：第八轮的结论是"没抽出来的正确做法
    等于没有"——`claim_quota` 早就存在却没抽出来，于是疫苗批次那里又手写了
    一遍错的。这里同理，20 处手写必然有几处写漏或写歪。

    - 给了 `patient_id`：走可见性判定并留痕，再按患者过滤；
    - 没给：按可见机构过滤；模型没有机构列的，退回按"本机构服务过的患者"过滤。
    """
    if patient_id is not None:
        assert_patient_visible(db, user, patient_id, resource=resource)
        return query.filter(model.patient_id == patient_id)

    orgs = visible_org_ids(db, user)
    if orgs is None:
        return query
    for col in ("org_id", "from_org_id", "managed_by_org_id"):
        if hasattr(model, col):
            return query.filter(getattr(model, col).in_(orgs))
    patients = visible_patient_ids(db, user)
    return query.filter(model.patient_id.in_(patients))


def scope_org_list(
    db: Session,
    user: User,
    query,
    model,
    org_id: int | None,
    org_col: str = "org_id",
    stats: bool = False,
):
    """机构维度清单接口的统一收口，与 `scope_patient_list` 对称。

    - 给了 `org_id`：校验能不能看这家机构，不能就 403（**不是悄悄返回空**——
      悄悄返回空会让人以为那家机构没数据，进而去翻别的入口；明说无权更省事）；
    - 没给：按可见范围过滤。

    `stats=True` 用医共体范围（`stats_org_ids`）而不是本机构：牵头医院要看得到
    片区的运行汇总，否则医共体的管理职能无从谈起。**统计与明细分两个范围**，
    正是为了不让人图省事拿统计范围去查明细。

    实测过的洞：乙卫生院的账号带上 `?org_id=甲` 就能拉到甲县医院的职工名册、
    资产清单、药品库存——`resolve_org_scope` 看着像作用域控制，
    但它只解析"想看哪个机构"，从不校验"能不能看"。
    """
    allowed = stats_org_ids(db, user) if stats else visible_org_ids(db, user)
    column = getattr(model, org_col)
    if org_id is not None:
        if allowed is not None and org_id not in allowed:
            raise HTTPException(status_code=403, detail="无权查看该机构的数据")
        return query.filter(column == org_id)
    if allowed is None:
        return query
    return query.filter(column.in_(allowed))


def log_patient_access(
    db: Session, user: User, patient_id: int, resource: str, basis: str
) -> str:
    """**按业务设计开放**的患者数据访问：不拦，但必须留痕。

    有几类接口天然要跨机构工作，加关系判定等于把功能关掉：

    - 结果互认查询（`exams.recognition-check`）：医共体搞互认，就是为了让
      任一成员机构在开单前查得到"这个人最近在别处做过没有"。患者初次到院、
      本机构一条记录都没有，正是最该问这一句的时刻——要求先有关系，
      互认就不存在了；
    - 授权管理（`patients.authorizations`）：给窗口办理知情同意用的。
      患者本人就在柜台前，而此刻本机构往往还没有他的任何记录。

    所以这里的口径是**可问责而非可阻断**：谁问的、问了谁、以什么名义，
    一条不落地记下来。留痕本身就是约束——查得到，就有人负责。

    `basis` 由调用方给一个说明性的词（`recognition` / `consent_admin`），
    与关系型依据区分开，事后审计能一眼看出这次不是靠业务关系进来的。
    """
    _write_access_log(user, patient_id, resource, basis)
    return basis


def assert_patient_visible(
    db: Session, user: User, patient_id: int, resource: str = "archive"
) -> str:
    """校验并留痕，返回依据；无依据则 403。

    校验与留痕绑在一起：能看的每一次都记下来，记不下来的就说明没走这条路。
    分成两个函数意味着总有人只调其中一个。
    """
    basis = patient_basis(db, user, patient_id)
    if basis is None:
        raise HTTPException(
            status_code=403,
            detail="无权调阅该患者档案：本机构与该患者无就诊、签约或转诊关系，也未获授权",
        )
    _write_access_log(user, patient_id, resource, basis)
    return basis
