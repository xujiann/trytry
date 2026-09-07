"""闸门：每个 `paginate()` 站点的排序必须是**全序**（末位排序键唯一）。

**为什么这条规则和分页是同一件事。** `deps.paginate` 走的是 `OFFSET/LIMIT`：

    query.offset(20).limit(20).all()

数据库对**并列行**（排序键取值相同的那些行）**不承诺任何顺序**。所以只要末位
排序键不唯一，同一份数据两次查询就可能把并列行排成不同的次序——第二页于是
既可能重复吐第一页已经给过的行，也可能整条跳过某些行。**翻页翻不全，且没有
任何一处会报错**：调用方拿到的是一份看起来正常、实际上少了几行又多了几行的列表。

这比它替换掉的那个缺陷更隐蔽：`.limit(500)` 至少是"永远只给前 500 条"，
稳定且可预期；排序不全序是"每次给的都不太一样"。**切分页时不补尾键，
等于拿「静默少返回」换「静默重复 + 漏行」**，是往回走。

**这类缺陷 SQLite 测不出来。** 文件型 SQLite 对并列行按 rowid 稳定返回，
`make test-unit` 会全绿；PG 换个执行计划（seq scan / index scan、并行度变化）
就会给出不同的并列内顺序。CLAUDE.md §6 那句「别把『SQLite 绿了』当成
『PG 也对』」正是这一种。**所以这里用的是静态判据而不是跑一遍看看**——
能跑出来的那个环境恰好是照不出这个缺陷的环境。

**判据**：`paginate(q, ...)` 里 `q` 的 `order_by(...)` **最后一个**排序键，
必须是主键或带 `unique=True` 的列。理由是 SQL 的字典序比较——只要末位键唯一，
整个键组合就唯一，全序成立；前面的键是什么、有没有并列都不影响。

- `order_by(X.id.desc())` ✅ 主键
- `order_by(ChargeItem.category, ChargeItem.code)` ✅ `code` 有唯一约束
- `order_by(X.plan_date.desc())` ❌ `String(10)` 日期串，并列是常态
- `order_by(X.plan_date.desc(), X.id.desc())` ✅ 补了尾键

**零豁免、零基线。** 现有 102 个 `paginate` 站点全部通过（P2-8 第二批修掉了
最后 4 处：积分榜按余额排、体征按 measured_at 排、复诊按 plan_date 排、
随访按 planned_at 排）。这条规则不设名单也不设计数——**排序不全序没有"合理的
存量"**，一处都不该有，新增一处就该当场红。
"""
import ast
import os
import warnings

from app.database import Base

import app.models  # noqa: F401  确保平台侧模型全部注册进 Base.registry
import app.spd.models  # noqa: F401  spd 侧同理（两条链的模型分属两个包）

#: 排序表达式外面可能套的修饰，剥掉之后才是列本身
_MODIFIERS = {"desc", "asc", "nullsfirst", "nullslast", "collate"}

ROUTER_DIRS = (
    (os.path.join(os.path.dirname(__file__), "..", "app", "routers"), ""),
    (os.path.join(os.path.dirname(__file__), "..", "app", "spd", "routers"), "spd/"),
)


def _models() -> dict:
    return {m.class_.__name__: m.class_ for m in Base.registry.mappers}


def _router_files():
    """**必须递归**：`app/spd/routers/config/` 是子包，一层扫会整包看不见。"""
    files = []
    for directory, label in ROUTER_DIRS:
        root_dir = os.path.abspath(directory)
        for root, dirs, names in os.walk(root_dir):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            rel = os.path.relpath(root, root_dir)
            prefix = "" if rel == "." else rel.replace(os.sep, "/") + "/"
            for name in sorted(names):
                if name.endswith(".py"):
                    files.append((f"{label}{prefix}{name}", os.path.join(root, name)))
    return sorted(files)


def _unwrap(node: ast.AST) -> ast.AST:
    while (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _MODIFIERS
    ):
        node = node.func.value
    return node


def _find_order_by(expr: ast.AST, assigns: dict, seen: tuple = ()):
    """顺着调用链和局部变量找 `order_by(...)` 的实参。

    要顺着变量找，是因为仓库里两种写法都有：`paginate(query.order_by(...), ...)`
    直接写在调用点上，以及 `query = db.query(X)...order_by(...)` 之后
    `paginate(query, ...)`。只认前一种会把后一种误判成"根本没排序"。
    """
    if isinstance(expr, ast.Call):
        func = expr.func
        if isinstance(func, ast.Attribute):
            if func.attr == "order_by":
                return expr.args
            return _find_order_by(func.value, assigns, seen)
        return None
    if isinstance(expr, ast.Name):
        if expr.id in seen:  # 防自赋值成环（query = query.filter(...)）
            return None
        for rhs in assigns.get(expr.id, []):
            found = _find_order_by(rhs, assigns, seen + (expr.id,))
            if found:
                return found
    return None


def paginate_sites() -> list[tuple[str, str, str]]:
    """返回 (站点, 判定, 末位排序键)；判定为 "OK" 之外的都是问题。"""
    models = _models()
    sites: list[tuple[str, str, str]] = []
    for name, path in _router_files():
        tree = ast.parse(open(path, encoding="utf-8").read())
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            assigns: dict[str, list] = {}
            for node in ast.walk(fn):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            assigns.setdefault(target.id, []).append(node.value)
            for node in ast.walk(fn):
                is_paginate = (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "paginate"
                    and node.args
                )
                if not is_paginate:
                    continue
                where = f"{name}:{fn.name}"
                args = _find_order_by(node.args[0], assigns)
                if args is None:
                    sites.append((where, "没有 order_by", ""))
                    continue
                last = _unwrap(args[-1])
                if not (isinstance(last, ast.Attribute) and isinstance(last.value, ast.Name)):
                    sites.append((where, "末位排序键认不出是哪一列", ast.unparse(args[-1])))
                    continue
                model = models.get(last.value.id)
                if model is None:
                    sites.append((where, "末位排序键的模型认不出来", ast.unparse(last)))
                    continue
                column = model.__table__.columns.get(last.attr)
                if column is None:
                    sites.append((where, "末位排序键不是这张表的列", ast.unparse(last)))
                    continue
                verdict = "OK" if (column.primary_key or column.unique) else "末位排序键不唯一"
                sites.append((where, verdict, ast.unparse(last)))
    return sites


def test_扫描必须递归到子包():
    """防的是"路由拆进子包 → 扫描静默缩水"这一种失效（与另三条闸门同一自证）。"""
    scanned = {name for name, _ in _router_files()}
    assert any(name.startswith("spd/config/") for name in scanned), (
        "没扫到 app/spd/routers/config/ 子包——_router_files() 退回成不递归了"
    )


def test_变量里的order_by也要认出来():
    """自证：`query = db.query(X)...order_by(...)` 之后 `paginate(query, ...)` 这种写法
    必须被解析出来。只认调用点上直接写的那种，会把这类站点误报成"没有 order_by"，
    进而逼着人加豁免——判据的失效往往就是这么开始的。"""
    sites = dict((w, v) for w, v, _ in paginate_sites())
    assert sites.get("access_logs.py:my_access_logs") == "OK", sites.get(
        "access_logs.py:my_access_logs"
    )
    assert sites.get("outpatient_docs.py:list_treatments_by_patient") == "OK"


def test_每个分页站点的排序都必须是全序():
    """零豁免：末位排序键必须唯一，一处都不许有。

    **自证覆盖面**：扫了多少文件、认出多少个 `paginate` 站点，一并打印——
    一个不声张自己覆盖范围的绿灯，和假装看过全部的哨兵一样危险。
    """
    sites = paginate_sites()
    bad = [(w, v, k) for w, v, k in sites if v != "OK"]
    summary = (
        f"\n[分页排序全序闸门] 覆盖面自证\n"
        f"  扫描文件：{len(_router_files())} 个（app/routers + app/spd/routers，递归）\n"
        f"  认出的 paginate 站点：{len(sites)} 个    不合格：{len(bad)} 个\n"
        f"  判据：order_by 的末位键必须是主键或 unique 列，否则 OFFSET/LIMIT 翻页"
        f"会在并列行上重复+漏行（SQLite 照不出来，PG 才会犯）。"
    )
    print(summary)
    warnings.warn(summary, UserWarning, stacklevel=2)
    assert not bad, (
        "这些 paginate 站点的排序不是全序，翻页会重复或漏行——"
        "在 order_by 末尾补上该表的 id（例如 `.order_by(X.plan_date.desc(), X.id.desc())`）：\n  "
        + "\n  ".join(f"{w} —— {v}：{k}" for w, v, k in bad)
    )
