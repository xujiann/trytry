"""可见性关系表的**推导**守卫：正面样板也要自证覆盖面，排除清单也会腐烂。

`visibility._relation_tables()` 是本仓库把"手工清单"换成"从模型元数据推导"的
样板（见第 14 章）：凡是同时带 `patient_id` 与机构外键的表，自动成为
"患者↔机构"的服务关系依据，新模块按惯例建表就自动纳入，没人需要记得改清单。

但这个样板此前**一条测试都没有**。两件事因此是没人盯着的：

1. **它到底覆盖了多少表**——第 17 章例三：一个不声张自己覆盖范围的绿灯，
   和假装看过全部的哨兵一样危险。推导出 0 张表时，`patient_basis` 只会少给
   依据（医生打不开该看的档案），而任何既有用例都不会因此变红。
2. **`_NOT_A_RELATION` 这份排除清单会腐烂**——它按**表名字符串**排除两张表，
   表一旦改名，排除就静默失效：`access_logs` 会重新变成可见性依据，
   于是"查过一次就永远有权再查"的自我循环回来了，而且**没有任何报错**。
   这正是本轮要找的那种坏清单：忘记同步 → 静默出错。

这里把两件事都钉住：排除清单必须逐条对得上真实的表、且排除确实生效；
推导规模打印出来，缺口（有 `patient_id` 却没有机构列、因而永远当不了依据的表）
也一并计数——不让"没覆盖到的部分不出现在任何地方"再发生一次。
"""
from __future__ import annotations

import warnings

from app.main import app  # noqa: F401  触发全部模型 import（含 spd）
from app.database import Base
from app.visibility import (
    _NOT_A_SERVICE_NO_ORG,
    _NOT_A_RELATION,
    _ORG_COLUMN_MISSING,
    _ORG_VIA_PARENT,
    _PARENT_IS_THE_RELATION,
    _WRITER_ALREADY_NEEDED_BASIS,
    _org_via_parent_tables,
    _relation_tables,
)


def _mapped_classes() -> list[type]:
    return [
        cls for cls in list(Base.registry._class_registry.values())
        if hasattr(cls, "__table__") and hasattr(cls, "__tablename__")
    ]


def _org_columns(cls) -> list[str]:
    return [c.name for c in cls.__table__.columns if c.name.endswith("org_id")]


def _patient_tables() -> list[type]:
    return [cls for cls in _mapped_classes() if "patient_id" in cls.__table__.columns]


RELATIONS = _relation_tables()
RELATION_NAMES = {cls.__tablename__ for cls, _cols in RELATIONS}


def test_覆盖面自证():
    mapped = _mapped_classes()
    patient_tables = _patient_tables()
    with_org = [c for c in patient_tables if _org_columns(c)]
    no_org = sorted(c.__tablename__ for c in patient_tables if not _org_columns(c))
    summary = "\n".join([
        "",
        "[可见性关系表推导] 覆盖面自证",
        f"  扫描：已映射 ORM 类 {len(mapped)} 个（Base.registry 全量，无抽样、无跳过）",
        f"  带 patient_id 的表：{len(patient_tables)} 张"
        f" = 同时带机构列 {len(with_org)} + 只有 patient_id 无机构列 {len(no_org)}",
        f"  推导出的服务关系依据：{len(RELATIONS)} 张（= {len(with_org)} 减去显式排除"
        f" {len(_NOT_A_RELATION)} 张）",
        f"  永远当不了依据的表（无机构列，非缺陷但需可见）：{len(no_org)} 张 —— {no_org}",
    ])
    print(summary)
    warnings.warn(summary, UserWarning, stacklevel=2)
    assert RELATIONS, "一张关系表都没推导出来 = 跨机构可见性判定会把正当协同全拦死"


def test_排除清单里的表确实存在():
    """`_NOT_A_RELATION` 按表名排除，表改名/删表后条目会静默失效。

    失效的方向是**放松**（被排除的表重新变成可见性依据），不是变严——
    所以不会有任何用例因此变红，只会悄悄把留痕表变回"查过就有权再查"。
    """
    tables = {cls.__tablename__ for cls in _mapped_classes()}
    stale = sorted(_NOT_A_RELATION - tables)
    assert stale == [], (
        f"`visibility._NOT_A_RELATION` 里这些表已不存在（改名或删除）：{stale}。"
        " 排除按表名字符串匹配，条目对不上就等于没排除——改名时必须一起改。"
    )


def test_排除清单里的表本来就符合推导条件():
    """排除项必须是"本来会被推导进来"的表；否则这条排除就是死条目。

    死条目的坏处不在于多写了一行，而在于它让人**以为**这里已经排除过了：
    真有一张同名新表出现时，谁也不会再去确认排除是否还成立。
    """
    by_name = {cls.__tablename__: cls for cls in _mapped_classes()}
    pointless = sorted(
        name for name in _NOT_A_RELATION
        if name in by_name
        and not ("patient_id" in by_name[name].__table__.columns and _org_columns(by_name[name]))
    )
    assert pointless == [], (
        f"这些排除项本来就推导不进来（没有 patient_id 或没有机构列），是死条目：{pointless}。"
        " 排除清单只应登记「确实会被推导进来、但业务上不能当依据」的表。"
    )


def test_排除确实生效():
    """光有清单不算数——真的调一次推导，确认它们不在结果里（第 17 章的非空洞原则）。"""
    leaked = sorted(_NOT_A_RELATION & RELATION_NAMES)
    assert leaked == [], (
        f"以下表在排除清单里，却仍被推导为服务关系依据：{leaked}。"
        " 留痕表当依据会自我循环（查过一次就永远有权再查）；"
        " 授权表有过期与撤销，必须走专门判定。"
    )


def test_推导出的每张表都真的能用于判定():
    """每条 (模型, 机构列) 都要能被 `_patient_basis_uncached` 的查询用上。

    推导返回的机构列名必须真在表上——名字对不上会在运行期抛属性错误，
    而那是在医生调档案的请求里抛，不是在测试里。
    """
    broken = []
    for cls, org_cols in RELATIONS:
        columns = set(cls.__table__.columns.keys())
        missing = [c for c in org_cols if c not in columns]
        if missing or not org_cols:
            broken.append(f"{cls.__tablename__}: {missing or '机构列为空'}")
    assert broken == [], f"推导结果里这些表的机构列有问题：{broken}"


# ---------------------------------------------------------------------------
# P1-35：带 patient_id 却没有机构列的表，逐张说清是哪一种情况
# ---------------------------------------------------------------------------
#
# 这批表**永远**进不了 `_relation_tables()` 的推导面。此前它们只是被打印成一行
# 名字——数得清，却没人判断过。一片没人判断的盲区里如果真混着一张该当依据的表，
# 表现是医生在诊室里打不开本该看的档案，而**任何用例都不会因此变红**。
#
# 下面几条把四类分类逐条校验，并且**未分类即变红**（fail-closed）：新建一张
# 带 patient_id 而没有机构列的表，必须当场表态属于哪一类。

_JUDGED = {
    "不构成服务": _NOT_A_SERVICE_NO_ORG,
    "父行即依据": set(_PARENT_IS_THE_RELATION),
    "写入前已需依据": set(_WRITER_ALREADY_NEEDED_BASIS),
    "机构在父行(已接上)": set(_ORG_VIA_PARENT),
    "缺机构列(待补)": set(_ORG_COLUMN_MISSING),
}


def _no_org_tables() -> set[str]:
    return {c.__tablename__ for c in _patient_tables() if not _org_columns(c)}


def test_每张无机构列的表都已逐张判过():
    """未分类即变红——盲区不许再长回来。"""
    judged = set().union(*_JUDGED.values())
    unjudged = sorted(_no_org_tables() - judged)
    assert unjudged == [], (
        f"这些表带 patient_id 却没有机构列，且一类都没归：{unjudged}。"
        " 必须当场表态（见 `visibility` 里四份清单的说明）：它要么本就不构成服务关系、"
        " 要么父行已是依据、要么写入前就已需要依据、要么机构在父行上可一跳接进来、"
        " 要么就是真缺口（补机构列，登记进 `_ORG_COLUMN_MISSING`）。"
        " 不表态的后果不是报错，是医生打不开本该看的档案而没有任何提示。"
    )


def test_分类清单里没有陈旧条目():
    """表改名/删表后条目会静默失效——四份清单都按表名匹配。"""
    no_org = _no_org_tables()
    stale = {
        name: sorted(tables - no_org)
        for name, tables in _JUDGED.items()
        if tables - no_org
    }
    assert stale == {}, (
        f"这些条目已经对不上真实的表（改名、删表，或那张表现在已经有机构列了）：{stale}"
    )


def test_每张表只归一类():
    """归两类等于没归——两条理由里哪条失效了都看不出来。"""
    counts: dict[str, list[str]] = {}
    for label, tables in _JUDGED.items():
        for t in tables:
            counts.setdefault(t, []).append(label)
    dup = {t: labels for t, labels in counts.items() if len(labels) > 1}
    assert dup == {}, f"这些表归了不止一类：{dup}"


def test_父行即依据这一类的父表真在推导面里():
    """`bill_details` 说"父行已经是依据"——那父表就必须真的推导得进来。

    父表哪天被排除、改名或丢了机构列，这条理由当场不成立，而子表这边
    不会有任何迹象。所以理由要**可校验**，不能只写在注释里。
    """
    broken = {
        child: sorted(set(parents) - RELATION_NAMES)
        for child, parents in _PARENT_IS_THE_RELATION.items()
        if set(parents) - RELATION_NAMES
    }
    assert broken == {}, (
        f"这些子表的理由是「父行即依据」，但父表并不在推导面里：{broken}。"
        " 理由不成立了，这张子表要重新判。"
    )


def test_一跳机构的外键与父列都还在():
    """`_ORG_VIA_PARENT` 按表名与列名字符串写，改名会静默失效——失效方向是**收紧**
    （少一条依据），不会有任何用例变红，只会让医生打不开档案。"""
    resolved = {child.__tablename__ for child, *_ in _org_via_parent_tables()}
    assert resolved == set(_ORG_VIA_PARENT), (
        f"这些条目解析不出模型（表改名或已删除）：{sorted(set(_ORG_VIA_PARENT) - resolved)}"
    )
    broken = []
    for child, fk_col, parent, parent_org in _org_via_parent_tables():
        if fk_col not in child.__table__.columns:
            broken.append(f"{child.__tablename__}.{fk_col} 不存在")
        if parent_org not in parent.__table__.columns:
            broken.append(f"{parent.__tablename__}.{parent_org} 不存在")
        if "id" not in parent.__table__.columns:
            broken.append(f"{parent.__tablename__} 没有 id 主键，一跳接不上")
    assert broken == [], f"一跳配置对不上真实的列：{broken}"


def test_待补机构列的每条都写了理由():
    reasonless = sorted(t for t, why in _ORG_COLUMN_MISSING.items() if len(why.strip()) < 8)
    assert reasonless == [], f"这些缺口没写理由（或太短）：{reasonless}"


def test_逐表判断自证(capsys):
    """把四类的规模打印出来——闸门要自己说清楚这 23 张表各被判成了什么。"""
    with capsys.disabled():
        print(f"\n  [P1-35] 带 patient_id 而无机构列的表 {len(_no_org_tables())} 张，逐张判过：")
        for label, tables in _JUDGED.items():
            print(f"    {len(tables):3}  {label}")
        print(f"  其中 {len(_ORG_VIA_PARENT)} 张的机构在父行上，已接进判定；"
              f"{len(_ORG_COLUMN_MISSING)} 张是待补机构列的真缺口（只减不增）")
    assert _no_org_tables()
