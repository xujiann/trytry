"""请求体字符串没有长度上限就写进 `String(N)` 列：生产库上超长即 500（P1-91）。

写接口把请求体的字符串字段写进 `String(N)` 列，入参却不带 `max_length`（也不是锚定的枚举
`pattern`、日期类校验器或 `Literal`）。于是：

- **开发库（SQLite）不管 VARCHAR 长度**，照存——开发、测试一律绿；
- **生产库（PostgreSQL）超长即抛 `StringDataRightTruncation`**，没人接，整个请求 **500**。

最容易撞的是自由文本：就诊摘要（`String(1024)`）、转诊理由、各种备注与理由（`String(256/512)`）——
医生把一段长一点的病情描述贴进文本框，整条就诊登记就没了，页面只剩「Internal Server Error」。

**本文件**：①棘轮——这种「入参无上限 → 定长列」的字段只减不增（判据见 `unbounded_body_strings`）；
②逐批修过的端点的回归：超长 422、恰好到上限照常收。修法一律是给请求模型字段补
`max_length=列长`（出参模型若继承了请求模型会一并带上这条约束——PG 上存量不可能超过列长，
所以对读侧是空操作，见 P2-40）。

**生产库那一半要在真 PG 上跑才算数**：`tests/test_postgres_real.py` 末尾有一条 integration 用例，
用 `MEDPLAT_STRLEN_PG_URL` 把本文件换到 PG 上再跑一遍（接法照抄 `test_date_filter_pg_dialect.py`）。
"""
import ast
import importlib
import inspect
import os
import pathlib
import re
import typing

# 引擎是模块级的，`app.database` 一旦导入就定型——切库必须赶在导入之前。
_PG_URL = os.environ.get("MEDPLAT_STRLEN_PG_URL", "")
if _PG_URL:
    os.environ["MEDPLAT_DATABASE_URL"] = _PG_URL

import pytest  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app.database import engine  # noqa: E402

if _PG_URL:
    assert engine.dialect.name == "postgresql", (
        "MEDPLAT_STRLEN_PG_URL 已给出，引擎却不是 PostgreSQL——"
        f"实际 {engine.dialect.name}，多半是 app.database 在本模块之前就被导入了"
    )

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"

#: 基线：只许调小，已清零（`scripts/dump_gate_status.py` 把它列进闸门现状）。
#: 2026-09-24 实测 197 处（量法见 `unbounded_body_strings`）→ 182（第一批核心诊疗：就诊、入院、转诊、
#: 传染病报告、接种、满意度，15 个字段）→ 97（第二批诊疗与公卫：schemas 共用请求模型、孕产妇、临床文书、急救、
#: 处方、检查、医保、证明、慢病、上门、用血、手术、老年、短缺药、会诊、预约、公卫、质控，85 个字段）→ 0（第三批：
#: 管理侧 20 个文件与慢专病 3 个文件 90 个字段；最后一处是列本身太窄——角色变更留痕的两列 16 装不下
#: 32 位的自定义角色键，迁移 c3e4f5a6b7d9 扩到 32）。已清零，此后即零基线闸门。
#: 第二层（同日）：判据补上「请求体列表里的子项逐个写库」的形状（`for item in body.items:`），又量出 4 处——
#: 处方明细的药品编码 / 名称、批量号源模板的资源名 / 时段，真 PG 上超长即 500；同批补齐，仍为 0。
#: 第三层（同日）：再补「取出来的对象上显式赋值」`x.列 = body.字段`，又量出 10 处——检查报告修订的结论 / 所见、
#: 上门派单人与服务记录、医废交接人、整改措施 / 完成说明 / 验证意见、远程咨询回复与医师名；同批补齐，仍为 0。
#: 第四层（同日）：再补三处盲区（按业务键 `db.query(...).first()` 取出的对象、字典字面量构造、`body.x or 默认`
#: 一类原样取自入参的值），又量出 16 处——药品入库的编码 / 药名、绩效指标名、手术记录 8 个文本字段与手术申请的
#: 主刀名、护理 / 病程 / 体征记录人、儿童高危备注；同批补齐，仍为 0。
#: 第五层（同日）：再补「转一手再写」——`upsert_unique` 的两个字典、`add_amount`、`query(…).update({…})` /
#: `update(M).values(…)`、同模块取行 helper、局部变量与 `payload` 字典转手、推导式子项、传给同模块 helper 再写，
#: 又量出 37 处——登录用户名（写登录留痕，不登录就能打出 500）、体检总结 / 异常项 / 套餐名、检查申请的项目编码 /
#: 名称 / 临床信息 / 不互认理由、慢病与随访的下次到期日与随访指导、会诊专家名、凭证分录摘要、病案首页手术 / 备注、
#: 随访任务标题 / 责任人、采购验收说明、禁忌登记、各类改档（病种项目、分组、打印模板、行政项目、角色、资源、基金池）
#: 的名称与备注，以及慢专病任务审核意见（入参放到 512，列只有 256）；同批补齐，仍为 0。另把定长量词的 pattern
#: （`^[0-9]{4}$`）按宽度算，不再误报（`_pattern_width`）。
BASELINE = 0

_FINITE_PATTERN = re.compile(r"\^[^*+{]*\$")


def _pattern_width(pattern: str) -> int | None:
    """锚定 pattern 能匹配的最长长度；有无界量词（`*` / `+` / `{n,}`）或解析不了返回 None。

    `^[0-9]{4}$` 这种定长量词原先被 `_FINITE_PATTERN` 当成无界（它不认 `{`），预算年度一类字段因此误报；
    改用正则解析器算宽度，同时把「宽度不得超过列长」一并比了。解析器是内部模块，拿不到就退回原判据。"""
    try:
        from re import _parser  # type: ignore[attr-defined]
    except ImportError:  # pragma: no cover
        return 0 if _FINITE_PATTERN.fullmatch(pattern) else None
    if not (pattern.startswith("^") and pattern.endswith("$")):
        return None
    try:
        width = _parser.parse(pattern).getwidth()[1]
    except Exception:  # noqa: BLE001 — 解析不了就当无界，宁可点名
        return None
    return width if width < 10 ** 9 else None


def _column_lengths() -> dict[str, dict[str, int]]:
    import app.models  # noqa: F401
    import app.spd.models  # noqa: F401
    from sqlalchemy import String

    from app.database import Base
    return {m.class_.__name__: {c.name: c.type.length for c in m.columns
                                if isinstance(c.type, String) and getattr(c.type, "length", None)}
            for m in Base.registry.mappers}


def _bounded(field, limit: int) -> bool:
    """这个入参字段自己就挡得住超长：max_length ≤ 列长，或锚定且最长不超过列长的 pattern、日期类校验器、Literal。"""
    ann = field.annotation
    variants = (ann, *typing.get_args(ann))
    metas = list(field.metadata) + [m for a in variants for m in getattr(a, "__metadata__", ())]
    if any(type(m).__name__ in ("BeforeValidator", "AfterValidator") for m in metas):
        return True
    if any(typing.get_origin(a) is typing.Literal for a in variants):
        return True
    pattern = next((m.pattern for m in metas if getattr(m, "pattern", None)), None)
    width = _pattern_width(pattern) if pattern else None
    if width is not None and width <= limit:
        return True
    limit_in = next((m.max_length for m in metas if getattr(m, "max_length", None)), None)
    return limit_in is not None and limit_in <= limit


def _is_str(field) -> bool:
    ann = field.annotation
    return ann is str or str in typing.get_args(ann)


def _router_modules():
    for base in (APP_DIR / "routers", APP_DIR / "spd" / "routers"):
        for p in sorted(base.rglob("*.py")):
            if "__pycache__" in p.parts or p.name == "__init__.py":
                continue
            name = ".".join(p.relative_to(APP_DIR.parent).with_suffix("").parts)
            yield name, importlib.import_module(name), p.read_text(encoding="utf-8")


def _list_element_model(cls, field_name: str):
    """请求模型里 `list[子模型]` 字段的子模型；不是这种字段返回 None。"""
    info = cls.model_fields.get(field_name)
    if info is None:
        return None
    for tp in (info.annotation, *typing.get_args(info.annotation)):
        if typing.get_origin(tp) in (list, tuple, set):
            for arg in typing.get_args(tp):
                if inspect.isclass(arg) and issubclass(arg, BaseModel):
                    return arg
    return None


_SAME_OR_SHORTER = ("strip", "lstrip", "rstrip", "lower", "upper")
#: 第五层：数值量级不变的包装——`round(body.amount, 2)` 写进去的仍是入参那个数（长度一族碰不到它们）
_SAME_MAGNITUDE = ("round", "abs", "int", "float")


def _sources(value, items: dict, aliases: dict) -> list[tuple]:
    """`value` 可能原样就是哪些入参字段：`[(请求模型, 字段)]`。

    认的写法：`body.x`、`body.x or 默认`（`and` 同理）、`a if … else body.x`、`body.x.strip()` 这类不会变长的调用；
    第五层加 `round(body.x, 2)` / `abs` / `int` / `float`（数值量级不变），以及局部变量转手——`aliases` 记着
    `amount = round(body.amount …)` 这种赋值，之后写 `amount` 等于写那个入参字段。算术、拼接、函数加工过的不算：
    那写进去的已不是入参本身。"""
    if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
        cls = items.get(value.value.id)
        return [(cls, value.attr)] if cls is not None and value.attr in cls.model_fields else []
    if isinstance(value, ast.Name):
        return list(aliases.get(value.id, ()))
    if isinstance(value, ast.BoolOp):
        return [s for v in value.values for s in _sources(v, items, aliases)]
    if isinstance(value, ast.IfExp):
        return _sources(value.body, items, aliases) + _sources(value.orelse, items, aliases)
    if isinstance(value, ast.Call) and not value.keywords:
        if isinstance(value.func, ast.Attribute) and not value.args and value.func.attr in _SAME_OR_SHORTER:
            return _sources(value.func.value, items, aliases)
        if isinstance(value.func, ast.Name) and value.func.id in _SAME_MAGNITUDE and value.args:
            return _sources(value.args[0], items, aliases)
    return []


def _fetched_model(value, getters: dict, fetched: dict) -> str | None:
    """`x = …` 取出来的是哪张表的一行：`db.get(Model, …)`，或 `db.query(Model)….first()` / `.one()` /
    `.one_or_none()`（第四层：按业务键查出来再改的端点——定时任务、按编码改配置——原先整个看不见）；
    第五层加同模块的取行 helper（返回注解是 ORM 模型：`def _get(…) -> Consultation`，取行 + 归属校验一体的
    写法遍地都是）与 `ensure_present(<以上任一 / 已取出的变量>, …)`。"""
    if not isinstance(value, ast.Call):
        return None
    if isinstance(value.func, ast.Name):
        if value.func.id in getters:
            return getters[value.func.id]
        if value.func.id == "ensure_present" and value.args:
            inner = value.args[0]
            return fetched.get(inner.id) if isinstance(inner, ast.Name) else _fetched_model(inner, getters, fetched)
        return None
    if not isinstance(value.func, ast.Attribute):
        return None
    if value.func.attr == "get":
        return value.args[0].id if value.args and isinstance(value.args[0], ast.Name) else None
    if value.func.attr not in ("first", "one", "one_or_none"):
        return None
    cur = value.func.value
    while isinstance(cur, ast.Call) and isinstance(cur.func, ast.Attribute):
        if cur.func.attr == "query":
            return cur.args[0].id if len(cur.args) == 1 and isinstance(cur.args[0], ast.Name) else None
        cur = cur.func.value
    return None


def _call_name(node) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    return node.func.attr if isinstance(node.func, ast.Attribute) else None


def _core_target(node, models) -> str | None:
    """`update(Model)….values(…)` / `insert(Model)….values(…)`：顺着调用链往下找被写的那张表。"""
    cur = node
    while isinstance(cur, ast.Call) or isinstance(cur, ast.Attribute):
        if isinstance(cur, ast.Call) and isinstance(cur.func, ast.Name) and cur.func.id in ("update", "insert") \
                and cur.args and isinstance(cur.args[0], ast.Name) and cur.args[0].id in models:
            return cur.args[0].id
        cur = cur.func if isinstance(cur, ast.Call) else cur.value
    return None


def _function_writes(fn, items: dict, aliases: dict, fetched: dict, ctx: dict, depth: int = 0) -> list[tuple]:
    """一个函数体里入参字段写进了哪些列：`[(请求模型, 字段, ORM 模型, 列)]`。`items` 是请求体参数（及其列表子项的
    循环变量），`aliases` 是局部变量转手，`fetched` 是取出来的行；同模块 helper 顺着调用往下看一层（第五层）。"""
    models, getters, helpers = ctx["models"], ctx["getters"], ctx["helpers"]
    items = dict(items)
    aliases = {k: list(v) for k, v in aliases.items()}
    fetched = dict(fetched)
    # 列表子项：`for item in body.items:` 与推导式 `[M(**e.model_dump()) for e in body.entries]`（第五层）
    for node in ast.walk(fn):
        if isinstance(node, (ast.For, ast.comprehension)) and isinstance(node.target, ast.Name) \
                and isinstance(node.iter, ast.Attribute) and isinstance(node.iter.value, ast.Name) \
                and node.iter.value.id in items:
            elem = _list_element_model(items[node.iter.value.id], node.iter.attr)
            if elem is not None:
                items[node.target.id] = elem
    dumps: dict[str, tuple] = {}     # `payload = body.model_dump(...)`：{名: (请求模型, 排除的键)}
    overrides: dict[str, dict] = {}  # `payload["k"] = …`：{名: {键: 来源}}
    replaced: dict[str, set] = {}    # 函数体顶层（无条件）的 `payload["k"] = …`：入参原值不会再写进去
    top_level = {id(stmt) for stmt in fn.body}
    for _ in range(2):               # 两遍：`ensure_present(x)` 与转手的转手要等前一个名字先认出来
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
                continue
            target, value = node.targets[0], node.value
            if isinstance(target, ast.Name):
                model = _fetched_model(value, getters, fetched)
                if model is not None:
                    fetched[target.id] = model
                srcs = _sources(value, items, aliases)
                if srcs and not isinstance(value, ast.Name):
                    aliases[target.id] = list(dict.fromkeys(aliases.get(target.id, []) + srcs))
                if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) \
                        and value.func.attr == "model_dump" and isinstance(value.func.value, ast.Name) \
                        and value.func.value.id in items:
                    excluded = {c.value for k in value.keywords if k.arg == "exclude"
                                for c in ast.walk(k.value) if isinstance(c, ast.Constant)}
                    dumps[target.id] = (items[value.func.value.id], excluded)
            elif isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name) \
                    and isinstance(target.slice, ast.Constant) and isinstance(target.slice.value, str):
                overrides.setdefault(target.value.id, {})[target.slice.value] = _sources(value, items, aliases)
                if id(node) in top_level:
                    replaced.setdefault(target.value.id, set()).add(target.slice.value)
    writes = []

    def put(srcs, model, column):
        writes.extend((cls, f, model, column) for cls, f in srcs)

    def spread(value, model, overridden):
        """构造里的 `**…`：`body.model_dump(...)`、转手的 `payload`（第五层）。"""
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) \
                and value.func.attr == "model_dump" and isinstance(value.func.value, ast.Name) \
                and value.func.value.id in items:
            excluded = {c.value for k2 in value.keywords if k2.arg == "exclude"
                        for c in ast.walk(k2.value) if isinstance(c, ast.Constant)}
            cls = items[value.func.value.id]
            writes.extend((cls, f, model, f) for f in cls.model_fields if f not in excluded and f not in overridden)
        elif isinstance(value, ast.Name) and value.id in dumps:
            # `payload["k"] = …`：函数体顶层的无条件覆盖顶掉入参原值；包在分支里的（`if not payload.get("k"):
            # payload["k"] = 建议值`）只加不减——入参原值照样可能写进去，宁可多点名，不漏
            cls, excluded = dumps[value.id]
            gone = excluded | overridden | replaced.get(value.id, set())
            writes.extend((cls, f, model, f) for f in cls.model_fields if f not in gone)
            for key, srcs in overrides.get(value.id, {}).items():
                if key not in overridden:
                    put(srcs, model, key)

    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in models:
            for kw in node.keywords:
                v = kw.value
                # `Model(**{**body.model_dump(), "quantity": 0})`：字典字面量里展开，显式写出的键覆盖入参；
                # 覆盖的值若仍取自入参（`"surgeon_name": body.surgeon_name or …`），照样算那个字段写进这一列
                overridden: set = set()
                if kw.arg is None and isinstance(v, ast.Dict):
                    for k, val in zip(v.keys, v.values):
                        if isinstance(k, ast.Constant):
                            overridden.add(k.value)
                            put(_sources(val, items, aliases), node.func.id, k.value)
                    v = next((val for k, val in zip(v.keys, v.values) if k is None), None)
                if kw.arg is None:
                    spread(v, node.func.id, overridden)
                else:
                    put(_sources(v, items, aliases), node.func.id, kw.arg)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "setattr" \
                and node.args and isinstance(node.args[0], ast.Name) and node.args[0].id in fetched:
            writes.extend((cls, f, fetched[node.args[0].id], f)
                          for name, cls in items.items() if name in ctx["params"] or depth
                          for f in cls.model_fields)
        # 取出来的对象上显式赋值 `report.conclusion = body.conclusion`（改档、流转里最常见）
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Attribute) and isinstance(node.targets[0].value, ast.Name) \
                and node.targets[0].value.id in fetched:
            put(_sources(node.value, items, aliases), fetched[node.targets[0].value.id], node.targets[0].attr)
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        # 第五层：`upsert_unique(db, Model, {唯一键}, values={…})`——两个字典都会写进这张表
        if name == "upsert_unique" and len(node.args) >= 2 and isinstance(node.args[1], ast.Name) \
                and node.args[1].id in models:
            for d in [a for a in node.args[2:] if isinstance(a, ast.Dict)] + \
                     [k.value for k in node.keywords if isinstance(k.value, ast.Dict)]:
                for k, v in zip(d.keys, d.values):
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        put(_sources(v, items, aliases), node.args[1].id, k.value)
        # 第五层：原子累加 `add_amount(db, Model, id, "列", 增量)`——增量越过列容量、或是 null，累加即失败
        if name == "add_amount" and len(node.args) >= 5 and isinstance(node.args[1], ast.Name) \
                and node.args[1].id in models and isinstance(node.args[3], ast.Constant):
            put(_sources(node.args[4], items, aliases), node.args[1].id, node.args[3].value)
        # 第五层：`query(…).update({Model.列: 值})`、`update(Model).values(列=值)`；`Model.列 + 值` 是累加，同上
        if name == "update" and node.args and isinstance(node.args[0], ast.Dict):
            for k, v in zip(node.args[0].keys, node.args[0].values):
                if isinstance(k, ast.Attribute) and isinstance(k.value, ast.Name) and k.value.id in models:
                    if isinstance(v, ast.BinOp) and isinstance(v.op, ast.Add):
                        v = v.right
                    put(_sources(v, items, aliases), k.value.id, k.attr)
        if name == "values" and isinstance(node.func, ast.Attribute):
            model = _core_target(node.func.value, models)
            if model is not None:
                for kw in node.keywords:
                    v = kw.value
                    if isinstance(v, ast.BinOp) and isinstance(v.op, ast.Add):
                        v = v.right
                    if kw.arg:
                        put(_sources(v, items, aliases), model, kw.arg)
        # 第五层：同模块 helper 往下看一层——把实参的来源（入参字段 / 请求体本身 / 取出来的行）带进形参
        if depth == 0 and name in helpers and isinstance(node.func, ast.Name):
            helper = helpers[name]
            formal = [a.arg for a in helper.args.args + helper.args.kwonlyargs]
            bound = list(zip(formal, node.args)) + [(k.arg, k.value) for k in node.keywords if k.arg in formal]
            h_items, h_aliases, h_fetched = {}, {}, {}
            for param, arg in bound:
                if isinstance(arg, ast.Name) and arg.id in items:
                    h_items[param] = items[arg.id]
                elif isinstance(arg, ast.Name) and arg.id in fetched:
                    h_fetched[param] = fetched[arg.id]
                else:
                    srcs = _sources(arg, items, aliases)
                    if srcs:
                        h_aliases[param] = srcs
            if h_items or h_aliases:
                writes.extend(_function_writes(helper, h_items, h_aliases, h_fetched, ctx, depth + 1))
    return writes


def body_column_writes(modules=None) -> list[tuple]:
    """写接口把请求体的哪个字段写进了哪张表的哪一列：`[(模块, 请求模型, 字段, ORM 模型, 列)]`。

    写库形状五种：构造 `Model(**body.model_dump(...))`、构造里显式 `列=body.字段`、`x = db.get(Model, …)`
    之后 `setattr(x, …)` 的改档循环、**请求体里的列表字段逐项写库**——`for item in body.items:` 之后
    对 `item` 用前两种写法（P1-91 第二层：处方明细、批量号源曾因此整个漏在判据之外；setattr 那一种不认
    循环变量，改档循环写的是取出来的对象），以及**取出来的对象上显式赋值** `x.列 = body.字段`（第三层：
    检查报告修订、上门派单、远程咨询回复这类流转端点都是这么写的）。
    第四层补了三处盲区：取对象除 `db.get` 外也认 `db.query(Model)….first()` / `.one()` / `.one_or_none()`
    （按业务键查出来再改：入库累加、按指标键改名、按任务名改周期）；构造里的字典字面量
    `Model(**{**body.model_dump(), "列": …})`（显式写出的键覆盖入参，覆盖值仍取自入参的照算）；值不必是裸的
    `body.字段`——`body.x or 默认`、`a if … else body.x`、`body.x.strip()` 这类原样取自入参的写法都算
    （见 `_sources`）。
    第五层补的是「转一手再写」：`upsert_unique(db, Model, {…}, values={…})` 的两个字典；原子累加
    `add_amount(db, Model, id, "列", 增量)`；`query(…).update({Model.列: …})` 与 `update(Model).values(…)`；
    同模块取行 helper（返回注解是 ORM 模型）取出来的对象；局部变量转手（`amount = round(body.amount, 2)`、
    `payload = body.model_dump()` 之后 `Model(**payload)`，`payload["k"] = …` 的覆盖照算）；推导式里的列表子项；
    以及把入参字段 / 请求体 / 取出来的行传给同模块 helper，在 helper 里写库（往下看一层）。
    数值、可空、出参约束三族（`test_body_numeric_capacity.py`、`test_body_raw_dict.py` 第二层、
    `test_response_constraint_writers.py`）共用这一份。
    """
    lengths = _column_lengths()
    out = []
    for modname, mod, text in (modules if modules is not None else _router_modules()):
        tree = ast.parse(text)
        functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        endpoints = [fn for fn in functions if any(
            isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr in ("post", "put", "patch")
            for d in fn.decorator_list)]
        ctx = {
            "models": lengths,
            "getters": {fn.name: names[0] for fn in functions if fn.returns is not None and len(
                names := [n.id for n in ast.walk(fn.returns) if isinstance(n, ast.Name) and n.id in lengths]) == 1},
            "helpers": {fn.name: fn for fn in functions if fn not in endpoints},
        }
        for fn in endpoints:
            params = {}
            for a in fn.args.args:
                cls = getattr(mod, ast.unparse(a.annotation), None) if a.annotation is not None else None
                if inspect.isclass(cls) and issubclass(cls, BaseModel):
                    params[a.arg] = cls
            if not params:
                continue
            ctx["params"] = set(params)
            for cls, field, model, column in dict.fromkeys(_function_writes(fn, params, {}, {}, ctx)):
                out.append((modname, cls, field, model, column))
    return out


def unbounded_body_strings(modules=None) -> list[str]:
    """`模块:请求模型.字段→ORM模型.列(N)`：写接口把这个字符串字段写进定长列，入参却挡不住超长。

    写库形状见 `body_column_writes`。同一个请求模型字段被几个端点写进同一列，只算一处（修一次就全好了）。
    """
    lengths = _column_lengths()
    found = set()
    for modname, cls, field_name, model, column in body_column_writes(modules):
        limit = lengths.get(model, {}).get(column)
        field = cls.model_fields[field_name]
        if limit is None or not _is_str(field) or _bounded(field, limit):
            continue
        found.add(f"{modname.removeprefix('app.')}:{cls.__name__}.{field_name}→{model}.{column}({limit})")
    return sorted(found)


def test_入参无上限写进定长列_只减不增():
    bad = unbounded_body_strings()
    assert len(bad) <= BASELINE, (
        f"入参挡不住超长、却写进定长列的字段 {len(bad)} 处，超过基线 {BASELINE}——新增的是：\n  "
        + "\n  ".join(bad)
        + "\n\n生产库（PG）上超长即 500。给请求模型字段补 `max_length=列长`（或锚定的枚举 pattern / 日期类型）。"
    )


def test_修完请把基线调小():
    """基线只许变小：实测比基线少了，说明修过一批却没调——调小它，别让欠账名义上还挂着。"""
    assert len(unbounded_body_strings()) >= BASELINE, (
        f"实测 {len(unbounded_body_strings())} 处，比基线 {BASELINE} 少——把 BASELINE 调小并写上是哪一批"
    )


def test_判据自证_五种写库形状都点名_挡得住的不报():
    import types

    from pydantic import Field

    snippet = '''
class NoteIn(BaseModel):
    patient_id: int
    note: str = ""
    code: str = Field(default="", max_length=64)
    kind: str = Field(default="a", pattern="^(a|b)$")

class NotePatch(BaseModel):
    note: str | None = None

class LineIn(BaseModel):
    diagnosis_name: str = ""

class BatchIn(BaseModel):
    lines: list[LineIn] = []

@router.post("/n")
def create(body: NoteIn, db=None):
    db.add(Encounter(**body.model_dump(exclude={"code", "kind"})))

@router.post("/m")
def create_m(body: NoteIn, db=None):
    db.add(Encounter(patient_id=body.patient_id, summary=body.note))

@router.patch("/n/{i}")
def patch(i: int, body: NotePatch, db=None):
    e = db.get(Encounter, i)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(e, k, v)

@router.post("/b")
def batch(body: BatchIn, db=None):
    for line in body.lines:
        db.add(Encounter(**line.model_dump()))

@router.post("/n/{i}/amend")
def amend(i: int, body: NotePatch, db=None):
    e = db.get(Encounter, i)
    e.diagnosis_name = body.note
'''

    class _Router:
        def __getattr__(self, _name):
            return lambda *a, **k: (lambda fn: fn)

    mod = types.ModuleType("自证")
    mod.__dict__.update({"BaseModel": BaseModel, "Field": Field, "router": _Router()})
    exec(compile(snippet, "自证", "exec"), mod.__dict__)
    got = unbounded_body_strings([("自证", mod, snippet)])
    # Encounter 上没有 note 列：只有显式 `summary=body.note` 那一处写进了定长列；
    # 请求体列表里的子项逐个写库（第四种）、取出来的对象上显式赋值（第五种）同样点名
    assert got == ["自证:LineIn.diagnosis_name→Encounter.diagnosis_name(256)",
                   "自证:NoteIn.note→Encounter.summary(1024)",
                   "自证:NotePatch.note→Encounter.diagnosis_name(256)"], got


def test_判据自证_第四层_查出来的对象_字典字面量_原样取值都点名():
    import types

    from pydantic import Field

    snippet = '''
class FourIn(BaseModel):
    diagnosis_name: str = ""
    summary: str = ""
    note: str = ""

class ConstIn(BaseModel):
    summary: str = ""

@router.patch("/q/{code}")
def by_key(code: str, body: FourIn, db=None):
    e = db.query(Encounter).filter(Encounter.id == 1).first()
    e.diagnosis_name = body.diagnosis_name

@router.post("/lit")
def literal(body: FourIn, db=None):
    db.add(Encounter(**{**body.model_dump(exclude={"note"}), "summary": body.summary or "无"}))

@router.post("/strip")
def stripped(body: FourIn, db=None):
    db.add(Encounter(diagnosis_name=body.note.strip()))

@router.post("/const")
def const(body: ConstIn, db=None):
    db.add(Encounter(**{**body.model_dump(), "summary": ""}))
'''

    class _Router:
        def __getattr__(self, _name):
            return lambda *a, **k: (lambda fn: fn)

    mod = types.ModuleType("自证")
    mod.__dict__.update({"BaseModel": BaseModel, "Field": Field, "router": _Router()})
    exec(compile(snippet, "自证", "exec"), mod.__dict__)
    # 被常量覆盖的键（ConstIn.summary → ""）不算入参写库
    assert unbounded_body_strings([("自证", mod, snippet)]) == [
        "自证:FourIn.diagnosis_name→Encounter.diagnosis_name(256)",
        "自证:FourIn.note→Encounter.diagnosis_name(256)",
        "自证:FourIn.summary→Encounter.summary(1024)",
    ]


def test_判据自证_第五层_转一手再写库都点名():
    """helper 取出来的行、`ensure_present` 包一层、局部变量转手、`payload` 字典转手（有条件的覆盖不顶掉原值、
    顶层无条件覆盖才顶掉）、`upsert_unique` 的两个字典、传给同模块 helper 再写、推导式里的列表子项、
    `query(…).update({M.列: …})` 与 `update(M).values(…)`——都要点名。"""
    import types

    from pydantic import Field

    snippet = '''
class FiveIn(BaseModel):
    f_getter: str = ""
    f_ensure: str = ""
    f_alias: str = ""
    f_key: str = ""
    f_upsert: str = ""
    f_helper: str = ""
    f_update: str = ""
    f_values: str = ""
    f_bounded: str = Field(default="", max_length=64)

class PayloadIn(BaseModel):
    summary: str = ""
    diagnosis_name: str = ""

class LineIn(BaseModel):
    note: str = ""

class ListIn(BaseModel):
    lines: list[LineIn] = []

def _enc(db, i) -> Encounter:
    return db.get(Encounter, i)

def _write(db, text):
    db.add(Encounter(summary=text))

@router.patch("/getter/{i}")
def via_getter(i: int, body: FiveIn, db=None):
    e = _enc(db, i)
    e.doctor_name = body.f_getter
    e.diagnosis_code = body.f_bounded

@router.patch("/ensure/{i}")
def via_ensure(i: int, body: FiveIn, db=None):
    e = ensure_present(db.query(Encounter).filter(Encounter.id == i).first(), "就诊")
    e.summary = body.f_ensure

@router.post("/alias")
def via_alias(body: FiveIn, db=None):
    text = body.f_alias.strip() if body.f_alias else "无"
    db.add(Encounter(diagnosis_name=text))

@router.post("/payload")
def via_payload(body: PayloadIn, db=None):
    payload = body.model_dump()
    if not payload.get("summary"):
        payload["summary"] = "自动建议"
    payload["diagnosis_name"] = "常量"
    db.add(Encounter(**payload))

@router.post("/upsert")
def via_upsert(body: FiveIn, db=None):
    upsert_unique(db, Encounter, {"diagnosis_code": body.f_key}, values={"summary": body.f_upsert})

@router.post("/helper")
def via_helper(body: FiveIn, db=None):
    _write(db, body.f_helper)

@router.post("/comp")
def via_comp(body: ListIn, db=None):
    db.add_all([Encounter(summary=line.note) for line in body.lines])

@router.patch("/update/{i}")
def via_update(i: int, body: FiveIn, db=None):
    db.query(Encounter).filter(Encounter.id == i).update({Encounter.summary: body.f_update})
    db.execute(update(Encounter).where(Encounter.id == i).values(doctor_name=body.f_values))
'''

    class _Router:
        def __getattr__(self, _name):
            return lambda *a, **k: (lambda fn: fn)

    mod = types.ModuleType("自证")
    # `-> Encounter` 在定义时求值；判据只读 AST，这里给个占位名就够
    mod.__dict__.update({"BaseModel": BaseModel, "Field": Field, "router": _Router(), "Encounter": object})
    exec(compile(snippet, "自证", "exec"), mod.__dict__)
    # 带 max_length 的 f_bounded、被顶层常量顶掉的 diagnosis_name 不报
    assert unbounded_body_strings([("自证", mod, snippet)]) == [
        "自证:FiveIn.f_alias→Encounter.diagnosis_name(256)",
        "自证:FiveIn.f_ensure→Encounter.summary(1024)",
        "自证:FiveIn.f_getter→Encounter.doctor_name(64)",
        "自证:FiveIn.f_helper→Encounter.summary(1024)",
        "自证:FiveIn.f_key→Encounter.diagnosis_code(64)",
        "自证:FiveIn.f_update→Encounter.summary(1024)",
        "自证:FiveIn.f_upsert→Encounter.summary(1024)",
        "自证:FiveIn.f_values→Encounter.doctor_name(64)",
        "自证:LineIn.note→Encounter.summary(1024)",
        "自证:PayloadIn.summary→Encounter.summary(1024)",
    ]


def test_判据自证_定长量词的pattern按宽度算():
    """`^[0-9]{4}$` 原先因为带 `{` 被当成无界（预算年度误报）；宽度超过列长的 pattern 照样要点名。"""
    from pydantic import BaseModel as _BM
    from pydantic import Field

    class M(_BM):
        year: str = Field(pattern=r"^[0-9]{4}$")
        wide: str = Field(pattern=r"^[a-z]{2,20}$")
        open_: str = Field(pattern=r"^[a-z]+$")

    assert _bounded(M.model_fields["year"], 4) and _bounded(M.model_fields["year"], 16)
    assert not _bounded(M.model_fields["wide"], 16) and _bounded(M.model_fields["wide"], 20)
    assert not _bounded(M.model_fields["open_"], 1024)


# ================================================================ 第一批：核心诊疗
@pytest.fixture(scope="module")
def world(client, admin):
    county = client.post("/api/organizations", json={"name": "长度校验县医院", "org_type": "lead_hospital",
                                                     "level": "county"}, headers=admin).json()["id"]
    township = client.post("/api/organizations", json={"name": "长度校验卫生院", "org_type": "township",
                                                        "level": "township"}, headers=admin).json()["id"]
    patient = client.post("/api/patients", json={"name": "长度校验患者", "id_card": "330191198001010091",
                                                 "gender": "男", "birth_date": "1980-01-01"}, headers=admin).json()["id"]
    ward = client.post("/api/inpatient/wards", json={"org_id": county, "name": "长度校验病区"}, headers=admin).json()["id"]
    bed = client.post("/api/inpatient/beds", json={"ward_id": ward, "bed_no": "L01"}, headers=admin).json()["id"]
    return {"county": county, "township": township, "patient": patient, "ward": ward, "bed": bed}


def _cases(w):
    """(路径, 请求体（填 X 的那个字段待定）, 字段, 列长, 恰好到上限时是否也发一遍)"""
    return [
        ("/api/encounters", {"patient_id": w["patient"], "org_id": w["county"]}, "summary", 1024, True),
        ("/api/encounters", {"patient_id": w["patient"], "org_id": w["county"]}, "diagnosis_name", 256, True),
        ("/api/referrals", {"patient_id": w["patient"], "from_org_id": w["township"], "to_org_id": w["county"],
                            "direction": "up"}, "reason", 512, True),
        ("/api/inpatient/admissions", {"patient_id": w["patient"], "ward_id": w["ward"], "bed_id": w["bed"]},
         "diagnosis_name", 256, False),
        ("/api/infectious/cases", {"org_id": w["county"], "disease_code": "A09", "onset_date": "2031-01-01"},
         "disease_name", 128, True),
        ("/api/vaccination/records", {"patient_id": w["patient"], "vaccine_code": "V01", "org_id": w["county"]},
         "vaccine_name", 128, True),
        ("/api/surveys", {"target_type": "encounter", "target_id": 1, "patient_id": w["patient"], "score": 5},
         "comment", 512, True),
    ]


CASE_IDS = ["就诊摘要", "就诊诊断", "转诊理由", "入院诊断", "传染病名", "疫苗名", "满意度意见"]


@pytest.mark.parametrize("index", range(len(CASE_IDS)), ids=CASE_IDS)
def test_第一批_超长_422而不是生产库500(client, admin, world, index):
    path, body, field, limit, _ = _cases(world)[index]
    r = client.post(path, json={**body, field: "长" * (limit + 1)}, headers=admin)
    assert r.status_code == 422, (path, field, r.status_code, r.text[:200])
    assert field in r.text


@pytest.mark.parametrize("index", range(len(CASE_IDS)), ids=CASE_IDS)
def test_第一批_恰好到上限照常收(client, admin, world, index):
    path, body, field, limit, send = _cases(world)[index]
    if not send:
        pytest.skip("入院会占床，恰好到上限那一遍由就诊诊断同一列长代表")
    r = client.post(path, json={**body, field: "长" * limit}, headers=admin)
    assert r.status_code in (200, 201), (path, field, r.status_code, r.text[:200])



# ================================================================ 第二层：请求体列表里的子项逐个写库
# 判据原先只认「请求体参数本身」写库，`for item in body.items: Model(**item.model_dump())` 整个在它视野之外——
# 处方明细与批量号源模板四个字段因此漏过了三批（`body_column_writes` 的第四种形状）。
@pytest.mark.parametrize("field,limit", [("drug_code", 64), ("drug_name", 128)])
def test_第二层_处方明细超长_422而不是生产库500(client, admin, world, field, limit):
    item = {"drug_code": "P191RX", "drug_name": "长度校验药", "daily_dose": 1, "days": 1}
    body = {"patient_id": world["patient"], "org_id": world["county"]}
    r = client.post("/api/prescriptions", json={**body, "items": [{**item, field: "长" * (limit + 1)}]},
                    headers=admin)
    assert r.status_code == 422, (field, r.status_code, r.text[:200])
    assert field in r.text
    r = client.post("/api/prescriptions", json={**body, "items": [{**item, field: "长" * limit}]}, headers=admin)
    assert r.status_code == 201, (field, r.status_code, r.text[:200])


@pytest.mark.parametrize("field,limit", [("resource_name", 128), ("slot_time", 16)])
def test_第二层_批量号源模板超长_422而不是生产库500(client, admin, world, field, limit):
    template = {"resource_type": "outpatient", "resource_name": "长度校验门诊", "slot_time": "08:00"}
    body = {"org_id": world["township"], "date_from": "2031-03-03", "date_to": "2031-03-03"}
    r = client.post("/api/appointments/slots/batch",
                    json={**body, "templates": [{**template, field: "长" * (limit + 1)}]}, headers=admin)
    assert r.status_code == 422, (field, r.status_code, r.text[:200])
    assert field in r.text
    r = client.post("/api/appointments/slots/batch",
                    json={**body, "templates": [{**template, field: "长" * limit}]}, headers=admin)
    assert r.status_code == 201 and r.json()["created"] == 1, (field, r.status_code, r.text[:200])


# ================================================================ 第三层：取出来的对象上显式赋值
# `waste.handler_name = body.handler_name` 这种流转端点的写法，判据原先只认 setattr 循环——检查报告修订、
# 上门派单与完成、医废交接、整改进度与验证、远程咨询回复共 10 个字段整个在视野之外（`body_column_writes`
# 的第五种形状）。
def test_第三层_医废交接人超长_422而不是生产库500(client, admin, world):
    loc = client.post("/api/medwaste/locations", json={"org_id": world["county"], "name": "长度校验产生点",
                                                       "location_type": "source"}, headers=admin)
    assert loc.status_code == 201, loc.text
    waste = client.post("/api/medwaste", json={"org_id": world["county"], "waste_type": "infectious", "weight_kg": 1,
                                               "collected_date": "2031-05-05", "source_location_id": loc.json()["id"]},
                        headers=admin)
    assert waste.status_code == 201, waste.text
    wid = waste.json()["id"]
    r = client.post(f"/api/medwaste/{wid}/handover", json={"handler_name": "长" * 65}, headers=admin)
    assert r.status_code == 422 and "handler_name" in r.text, (r.status_code, r.text[:200])
    r = client.post(f"/api/medwaste/{wid}/handover", json={"handler_name": "长" * 64}, headers=admin)
    assert r.status_code == 200, (r.status_code, r.text[:200])


def test_改成长键自定义角色_留痕列装得下(client, admin):
    """角色变更留痕的两列原先只有 16，自定义角色键最长 32：PG 上改成 / 改离长键角色即 500（迁移 c3e4f5a6b7d9）。"""
    key = "p191_custom_role_ab"   # 19 位，合法的自定义角色键
    r = client.post("/api/rbac/roles", json={"key": key, "name": "长键自定义角色"}, headers=admin)
    assert r.status_code == 201, r.text
    r = client.post("/api/users", json={"username": "p191_roleuser", "password": "passw0rd1",
                                        "full_name": "长键角色用户", "role": "operator"}, headers=admin)
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    r = client.patch(f"/api/users/{uid}/role", json={"role": key}, headers=admin)
    assert r.status_code == 200, (r.status_code, r.text[:200])
    r = client.patch(f"/api/users/{uid}/role", json={"role": "operator"}, headers=admin)
    assert r.status_code == 200, (r.status_code, r.text[:200])


# ================================================================ 第四层：按业务键查出来的对象 / 字典字面量 / 原样取值
def test_第四层_药品入库编码药名超长_422而不是生产库500(client, admin, world):
    """入库按 (机构, 药品编码) 查出已有库存再改药名，建档走 `DrugStock(**{**body.model_dump(), …})`——两处原先都看不见。"""
    base = {"org_id": world["county"], "drug_code": "P191-4", "drug_name": "第四层药", "quantity": 1}
    for field, limit in (("drug_code", 64), ("drug_name", 128)):
        r = client.post("/api/pharmacy/stocks", json={**base, field: "长" * (limit + 1)}, headers=admin)
        assert r.status_code == 422 and field in r.text, (field, r.status_code, r.text[:200])
    r = client.post("/api/pharmacy/stocks", json={**base, "drug_code": "码" * 64, "drug_name": "药" * 128},
                    headers=admin)
    assert r.status_code == 200, (r.status_code, r.text[:200])


def test_第四层_绩效指标改名超长_422(client, admin):
    key = client.get("/api/performance/indicators", headers=admin).json()[0]["key"]
    r = client.patch(f"/api/performance/indicators/{key}", json={"name": "长" * 65}, headers=admin)
    assert r.status_code == 422 and "name" in r.text, (r.status_code, r.text[:200])


def test_第四层_住院护理记录人超长_422(client, admin, world):
    """`nurse_name=body.nurse_name or user.full_name`：值不是裸的 `body.字段`，原先判据认不出来。"""
    bed = client.post("/api/inpatient/beds", json={"ward_id": world["ward"], "bed_no": "L4-01"}, headers=admin)
    assert bed.status_code == 201, bed.text
    patient = client.post("/api/patients", json={"name": "第四层护理患者", "id_card": "330191198001010491",
                                                 "gender": "女", "birth_date": "1980-01-01"}, headers=admin)
    assert patient.status_code == 201, patient.text
    adm = client.post("/api/inpatient/admissions",
                      json={"patient_id": patient.json()["id"], "ward_id": world["ward"], "bed_id": bed.json()["id"]},
                      headers=admin)
    assert adm.status_code == 201, adm.text
    url = f"/api/inpatient/admissions/{adm.json()['id']}/nursing-records"
    r = client.post(url, json={"content": "第四层护理", "nurse_name": "长" * 65}, headers=admin)
    assert r.status_code == 422 and "nurse_name" in r.text, (r.status_code, r.text[:200])
    r = client.post(url, json={"content": "第四层护理", "nurse_name": "长" * 64}, headers=admin)
    assert r.status_code == 201, (r.status_code, r.text[:200])


# ================================================================ 第五层：转一手再写库（修前开发库照存 / 照过，生产库 500）
def test_第五层_登录用户名超长_422而不是写登录留痕时500(client):
    """登录不论成败都写登录留痕 `login_logs.username`(64)：原先超长用户名照收，生产库写留痕即 500——
    不用登录就能打出来的 500。恰好 64 字的照常走口令校验（401，或被同进程其它用例触发的限流 / 锁定）。"""
    r = client.post("/api/auth/login", json={"username": "长" * 65, "password": "whatever"})
    assert r.status_code == 422 and "username" in r.text, (r.status_code, r.text[:200])
    r = client.post("/api/auth/login", json={"username": "满" * 64, "password": "whatever"})
    assert r.status_code in (401, 423, 429), (r.status_code, r.text[:200])


def test_第五层_体检总结超长_422_恰好到上限照常收(client, admin, world):
    """体检建档经 `payload = body.model_dump()` 转手写库，判据原先看不见。"""
    body = {"patient_id": world["patient"], "org_id": world["township"], "exam_date": "2026-09-01"}
    r = client.post("/api/checkups", headers=admin, json={**body, "summary": "长" * 1025})
    assert r.status_code == 422 and "summary" in r.text, (r.status_code, r.text[:200])
    r = client.post("/api/checkups", headers=admin, json={**body, "summary": "满" * 1024})
    assert r.status_code == 201, (r.status_code, r.text[:200])


# ================================================================ P2-229：审方意见（系统意见与药师意见拼在同一列）
# 系统审方意见（命中的禁忌 / 相互作用 / 特殊人群逐条拼起来）与药师意见接在同一列 `prescriptions.review_comment`(1024)。
# 原先两段都不设限：禁忌诊断每条都带着药名与整段诊断名，命中三四条就超过列长，生产库建处方即 500；药师意见无上限，
# 审方即 500。这两段的值都不是原样取自入参（拼接 / SQL 追加），上面的判据量不到，逐条钉在这里。
def test_审方意见_系统意见超长截断注明_药师意见有上限_两段合起来装得进列(client, admin, world):
    from app.models import Prescription
    from app.routers.prescriptions import REVIEW_COLUMN_MAX, SYSTEM_REVIEW_MAX
    from app.schemas import REVIEW_COMMENT_MAX

    assert Prescription.__table__.c.review_comment.type.length == REVIEW_COLUMN_MAX
    codes = [f"P2229-{i}" for i in range(4)]
    for code in codes:
        r = client.post("/api/prescriptions/rules", headers=admin, json={
            "drug_code": code, "max_daily_dose": 100, "contraindicated_diagnoses": "P2229禁忌"})
        assert r.status_code == 201, r.text
    rx = client.post("/api/prescriptions", headers=admin, json={
        "patient_id": world["patient"], "org_id": world["county"],
        "diagnosis_name": "P2229禁忌" + "长" * (256 - len("P2229禁忌")),
        "items": [{"drug_code": code, "drug_name": "药" * 128, "daily_dose": 1} for code in codes]})
    assert rx.status_code == 201, (rx.status_code, rx.text[:200])   # 修前生产库 500：四条禁忌拼出来一千六百多字
    assert rx.json()["status"] == "pending_review"
    system = rx.json()["review_comment"]
    assert len(system) == SYSTEM_REVIEW_MAX and system.endswith("……（共 4 条，余下从略）"), len(system)

    url = f"/api/prescriptions/{rx.json()['id']}/review"
    r = client.post(url, headers=admin, json={"approve": False, "comment": "长" * (REVIEW_COMMENT_MAX + 1)})
    assert r.status_code == 422 and "comment" in r.text, (r.status_code, r.text[:200])
    r = client.post(url, headers=admin, json={"approve": False, "comment": "满" * REVIEW_COMMENT_MAX})
    assert r.status_code == 200, (r.status_code, r.text[:200])
    # 系统意见 + 「；药师意见：」+ 满额意见，恰好装满这一列
    assert r.json()["review_comment"] == f"{system}；药师意见：{'满' * REVIEW_COMMENT_MAX}"
    assert len(r.json()["review_comment"]) == REVIEW_COLUMN_MAX


def test_审方意见_修前存下的长系统意见_药师意见装不下就说清还能写几个字(client, admin, world):
    from app.database import SessionLocal
    from app.models import Prescription, User

    with SessionLocal() as db:
        creator = db.query(User).filter_by(username="admin").one().id
        rx = Prescription(patient_id=world["patient"], org_id=world["county"], status="pending_review",
                          review_comment="旧" * 1000, created_by=creator)
        db.add(rx)
        db.commit()
        url = f"/api/prescriptions/{rx.id}/review"
    r = client.post(url, headers=admin, json={"approve": True, "comment": "意" * 19})
    assert r.status_code == 422, (r.status_code, r.text[:200])   # 修前生产库 500
    assert r.json() == {"detail": "药师意见最多还能写 18 字（这张处方的系统审方意见已占 1000 字）"}
    r = client.post(url, headers=admin, json={"approve": True, "comment": "意" * 18})
    assert r.status_code == 200 and len(r.json()["review_comment"]) == 1024, (r.status_code, r.text[:200])
