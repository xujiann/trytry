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
from app.visibility import _NOT_A_RELATION, _relation_tables


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
