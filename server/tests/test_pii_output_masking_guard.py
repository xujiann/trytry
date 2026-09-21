"""PII **响应出口**的脱敏守卫：分母从路由与代码结构推导，漏脱敏一处当场变红。

## 为什么要有这一条（P1-33）

`app/privacy.py` 的 docstring 写着"新增返回身份证号/电话的接口必须复用本模块"，
`CLAUDE.md` §4 写着"出口一律经 privacy.py 脱敏"。**但没有任何闸门。**
这条纪律此前只靠人记得——而实测下来，慢专病侧的七个工作人员端接口
（任务中心、随访前置资料、候选/纳管列表、专病 360 档案）与居民端专病档案
确实把 `patients.phone` **明文**放进了响应体。漏一处的后果不是报错，是
身份证号/手机号明文出网：个保法口径下属上线阻断项。

## 三道闸门的分工（别合并，也别互相重复判定）

| 闸门 | 守的方向 | 判据 |
|---|---|---|
| `test_pii_query_point_guard.py` | **检索入口**（值进 WHERE） | 加密列等值必须走 `pii_filter`/`pii_index_match` |
| **本文件** | **响应出口**（值进 HTTP body） | 响应里带 PII 的端点必须经 `app/privacy.py` |
| `test_log_pii_leak_guard.py` | **日志出口**（值进 stdout/留存文件） | 日志行不得含手机号/验证码明文 |

检索闸门只看 `filter()`/`filter_by()` 里的比较，本文件只看 `return` 出去的结构；
两者共用的只有**列名的来源**（`EncryptedPII` 列类型），判定互不重叠。

## 分母怎么来的（两条路径并集，都不手写）

1. **路由元数据（运行期）**：遍历真实挂载的 `APIRoute`，递归展开其
   `response_model` 的字段；字段名里带 `id_card`/`phone` 的，这条路由进分母。
   新写一个 `response_model=PatientOut` 的端点，自动被看见。
2. **返回结构（AST）**：从每个路由处理函数出发，沿"返回值会流进响应"的调用关系
   递归展开出**响应构造函数集合**，其中任何**带 PII 键的 dict 字面量**都是一个
   出口点。新写一个 `return {"id_card": p.id_card}` 的裸 dict 端点，一样被看见。

   "会流进响应"**跟多跳**：`briefs = ...` → `out = f(..., briefs.get(...))` →
   `return out`。只跟一跳会漏看隔了一跳的脱敏点，把
   `GET /api/spd/enrollments/{id}` 误判成"声明了 phone 却没脱敏"
   （脱敏在 `_patient_brief` 里）。多跳复用同一套父链规则，见
   `_flows_to_response`；**元组解包处止步**，理由写在那里。

PII 列名本身也不手写：取自 `EncryptedPII` 列类型（与检索闸门同一个真源），
实测为 `id_card` / `phone`；响应字段名**包含**这两个词即算（于是
`guardian_id_card` / `caller_phone` 自动纳入）。

## 判定（分子）

出口点的值表达式按"根名字"分三类：

* **本地根**（`p.phone`、`patient.phone`、局部变量）：必须出现 `app/privacy.py`
  的公开脱敏函数（`desensitize` / `mask_*` / `visible_*`），否则**违规**；
* **形参根**（`brief.get("phone")` 这种把上游给的值搬运出去）：责任在调用方，
  记为"委派"，并在调用方的实参位置上继续判（dict 字面量作为实参传给一个
  "把该形参的 PII 搬进响应"的函数时，那个实参 dict 自己就是出口点）；
* 路径 1 命中、但整个闭包里一个 dict 出口点都没有（例如直接返回 ORM 对象由
  `response_model` 序列化）：要求闭包里**真的调用**脱敏函数（认 AST 的 Call 节点，
  不认字符串包含——docstring 里写着"走 desensitize"不算数）。

例外按"好清单"形态逐条写理由（`EXCEPTIONS`），**只许减少**；且例外必须仍然
命中分母——端点改名/下线后例外不清理，会被 `test_例外清单没有陈旧条目` 拦下。

## 认不出的形态（如实声明，见第 17 章第 4 条）

AST 只认"字面量键的 dict"。以下五类**认不出**，本守卫把实测数量打印出来，
让它们先变得可见，而不是悄悄绕过：

1. `**expr` 展开（`{**brief, ...}`）——键名不在 AST 里；
2. 非字面量键（f-string / 变量作 key）；
3. `getattr(obj, name)` 动态取值；
4. 手写 JSON / `Response(content=...)` 字符串出口；
5. PII 值挂在**非 PII 名**的键下——典型是 FHIR/HL7 这类外部规范命名
   （`identifier[].value` 放身份证号、`telecom[].value` 放电话）。
   实测 `integration.export_fhir_patient` 正是这个形状：它**已经**按角色脱敏
   （`app/routers/integration.py:356-357`），但本守卫的分母看不见它。
   P1-33 原文提到的 `integration.fhir_patient_resource` 这个符号在仓库里
   **不存在**，且它说的"按设计的明文导出"与现状不符——出站导出早在 H1 整改时
   就改成了非 admin 掩码。故该条例外**不予登记**（登记一条假例外比没有更坏）。
"""
from __future__ import annotations

import ast
import inspect
import pathlib
import typing
import warnings

from fastapi import APIRouter
from fastapi.routing import APIRoute
from pydantic import BaseModel

from app import privacy
from app.database import Base
from app.main import app
from app.pii import EncryptedPII

SERVER_DIR = pathlib.Path(__file__).resolve().parents[1]
APP_DIR = SERVER_DIR / "app"

# ---------------------------------------------------------------- 清单：推导

#: PII 列名的真源与检索闸门同一个：列类型是 `EncryptedPII` 的那些列。
PII_COLUMN_NAMES: set[str] = {
    c.name
    for cls in list(Base.registry._class_registry.values())
    if hasattr(cls, "__table__")
    for c in cls.__table__.columns
    if isinstance(c.type, EncryptedPII) and not c.name.endswith("_idx")
}

#: 脱敏入口的真源：`app/privacy.py` 里定义（而非导入）的公开函数。
#: 往 privacy.py 加一个新的脱敏函数，本守卫自动认得，不需要有人来改清单。
MASK_HELPERS: set[str] = {
    name
    for name, obj in vars(privacy).items()
    if not name.startswith("_")
    and inspect.isfunction(obj)
    and obj.__module__ == privacy.__name__
}


def is_pii_name(name: str) -> bool:
    """响应字段/字典键名是否承载 PII：包含某个加密列名即算。

    `guardian_id_card` / `caller_phone` 因此自动进分母——它们装的确实是
    同一类东西，只是挂在别的主体名下。
    """
    return isinstance(name, str) and any(col in name for col in PII_COLUMN_NAMES)


# ------------------------------------------------------- 全量函数索引（AST）


def _module_name(path: pathlib.Path) -> str:
    rel = path.relative_to(SERVER_DIR).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _python_files() -> list[pathlib.Path]:
    return [p for p in sorted(APP_DIR.rglob("*.py")) if "__pycache__" not in p.parts]


FILES = _python_files()
#: ``"模块:函数名" -> FunctionDef``（只收模块级函数，嵌套函数不是可被调用的出口构造器）
FUNCS: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
#: ``模块 -> {本地名: (目标模块, 原名)}``，用来把跨文件调用解析到定义处。
IMPORT_ALIASES: dict[str, dict[str, tuple[str, str]]] = {}


def _resolve_relative(module: str, node: ast.ImportFrom, is_pkg: bool) -> str:
    if not node.level:
        return node.module or ""
    parts = module.split(".")
    base = parts if is_pkg else parts[:-1]
    if node.level > 1:
        base = base[: len(base) - (node.level - 1)]
    return ".".join([*base, node.module]) if node.module else ".".join(base)


def _index() -> None:
    for path in FILES:
        module = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases: dict[str, tuple[str, str]] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                FUNCS[f"{module}:{node.name}"] = node
            elif isinstance(node, ast.ImportFrom):
                target = _resolve_relative(module, node, path.name == "__init__.py")
                for alias in node.names:
                    aliases[alias.asname or alias.name] = (target, alias.name)
        IMPORT_ALIASES[module] = aliases


_index()


def resolve_call(module: str, name: str, depth: int = 0) -> str | None:
    """把调用点上的名字解析成 ``"模块:函数名"``；跨文件再导出最多跟 5 层。"""
    if depth > 5:
        return None
    key = f"{module}:{name}"
    if key in FUNCS:
        return key
    target = IMPORT_ALIASES.get(module, {}).get(name)
    if target is None:
        return None
    return resolve_call(target[0], target[1], depth + 1)


def _callee_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
        # `platform.visible_phone(...)` 一类；按属性名解析即可
        return node.func.attr
    return None


# ------------------------------------------------------------ 出口点与闭包


def _walk_routes(routes) -> typing.Iterator[APIRoute]:
    """穿过 `include_router` 的运行期封装，取到真正挂载的每一条 APIRoute。"""
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        original = getattr(route, "original_router", None)
        if original is not None and getattr(original, "routes", None):
            yield from _walk_routes(original.routes)
        elif isinstance(route, APIRouter) and getattr(route, "routes", None):
            yield from _walk_routes(route.routes)


def _model_pii_fields(model, seen: set | None = None) -> set[str]:
    """递归展开 pydantic 模型，返回承载 PII 的字段路径（`patient.phone` 这种）。"""
    seen = seen if seen is not None else set()
    if model in seen:
        return set()
    seen.add(model)
    out: set[str] = set()
    fields = getattr(model, "model_fields", None)
    if not fields:
        return out
    for name, field in fields.items():
        if is_pii_name(name):
            out.add(name)
        for sub in [field.annotation, *typing.get_args(field.annotation)]:
            if isinstance(sub, type) and issubclass(sub, BaseModel):
                out |= {f"{name}.{x}" for x in _model_pii_fields(sub, seen)}
    return out


def _response_model_pii(route: APIRoute) -> set[str]:
    model = route.response_model
    if model is None:
        return set()
    out: set[str] = set()
    for sub in [model, *typing.get_args(model)]:
        if isinstance(sub, type) and issubclass(sub, BaseModel):
            out |= _model_pii_fields(sub)
    return out


def _params(fn) -> list[str]:
    a = fn.args
    return [x.arg for x in [*a.posonlyargs, *a.args, *a.kwonlyargs]]


def _parents(fn) -> dict[int, ast.AST]:
    out: dict[int, ast.AST] = {}
    for node in ast.walk(fn):
        for child in ast.iter_child_nodes(node):
            out[id(child)] = node
    return out


def _root_name(node: ast.AST) -> str | None:
    while True:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, (ast.Attribute, ast.Subscript)):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Await):
            node = node.value
        else:
            return None


def _returned_names(fn) -> set[str]:
    """出现在任何 `return` 表达式里的名字（含 `return [f(x) for x in briefs]`）。"""
    out: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and node.value is not None:
            out |= {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
    return out


def _name_uses(fn) -> dict[str, list[ast.Name]]:
    """局部名字 -> 它被**读**的那些节点（Store 的赋值目标不算）。

    用来把"值流进响应"的判定从一跳扩到多跳，见 `_flows_to_response`。
    """
    out: dict[str, list[ast.Name]] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            out.setdefault(node.id, []).append(node)
    return out


def _pii_dicts(fn) -> list[tuple[ast.Dict, str, ast.AST]]:
    """函数体内所有"带 PII 字面量键"的 dict：``(dict 节点, 键名, 值表达式)``。"""
    out = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and is_pii_name(key.value):
                out.append((node, key.value, value))
    return out


def _param_rooted_pii_params(key: str) -> set[str]:
    """函数把**形参**里的 PII 搬进返回结构时，那些形参的名字。

    用来判断"一个 dict 字面量作为实参传进去，是喂给它（入参）还是经它出网（出口）"：
    `create_patient_idempotent(db, {"id_card": ...})` 的那个 dict 是入参
    （该函数不把它搬进返回结构），`_task_out(task, {"phone": ...})` 的则是出口。

    """
    fn = FUNCS.get(key)
    if fn is None:
        return set()
    params = set(_params(fn))
    return {
        root
        for _d, _k, value in _pii_dicts(fn)
        if (root := _root_name(value)) is not None and root in params
    }


class Site:
    """一个 PII 出口点。"""

    def __init__(self, func_key: str, line: int, key: str, expr: str, kind: str) -> None:
        self.func_key, self.line, self.key, self.expr, self.kind = func_key, line, key, expr, kind

    def __str__(self) -> str:
        module, name = self.func_key.split(":")
        return f"{module.replace('.', '/')}.py:{self.line} {name}() \"{self.key}\": {self.expr[:80]}"


class Blind:
    """认不出的形态：按类计数，不假装看见。"""

    KINDS = ("dict展开", "非字面量键", "getattr动态取值", "手写JSON/Response", "处理函数无源码")

    def __init__(self) -> None:
        self.items: dict[str, list[str]] = {k: [] for k in self.KINDS}

    def add(self, kind: str, where: str) -> None:
        self.items[kind].append(where)

    def total(self) -> int:
        return sum(len(v) for v in self.items.values())


BLIND = Blind()


def _scan_blind(key: str) -> None:
    fn = FUNCS.get(key)
    if fn is None:
        return
    module = key.split(":")[0]
    for node in ast.walk(fn):
        where = f"{module}:{getattr(node, 'lineno', 0)}"
        if isinstance(node, ast.Dict) and any(k is None for k in node.keys):
            BLIND.add("dict展开", where)
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if k is not None and not isinstance(k, ast.Constant):
                    BLIND.add("非字面量键", where)
        if isinstance(node, ast.Call):
            name = _callee_name(node)
            if name == "getattr":
                BLIND.add("getattr动态取值", where)
            elif name in {"dumps", "Response", "PlainTextResponse", "HTMLResponse"}:
                BLIND.add("手写JSON/Response", where)


def constructors(entry: str) -> list[str]:
    """响应构造函数闭包：从处理函数出发，沿"返回值会流进响应"的调用关系展开。

    判"会流进响应"的两种形状（其余一律不跟，免得把入参解析函数
    `integration.parse_hl7v2_patient` 这类算进出口）：

    * 调用本身长在 `return` 表达式里（`return _task_out(...)`）；
    * 调用的结果赋给某个名字，而该名字出现在某条 `return` 表达式里
      （`briefs = _patient_brief(...)` → `return [_candidate_out(r, briefs.get(...))]`）。
    """
    out, stack = [entry], [entry]
    while stack:
        key = stack.pop()
        fn = FUNCS.get(key)
        if fn is None:
            continue
        module = key.split(":")[0]
        parents = _parents(fn)
        returned = _returned_names(fn)
        uses = _name_uses(fn)
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            name = _callee_name(node)
            if name is None:
                continue
            target = resolve_call(module, name)
            if target is None or target in out:
                continue
            if _flows_to_response(node, parents, returned, module, uses):
                out.append(target)
                stack.append(target)
    return out


def _flows_to_response(
    node: ast.AST,
    parents: dict[int, ast.AST],
    returned: set[str],
    module: str,
    uses: dict[str, list[ast.Name]] | None = None,
    seen: set[str] | None = None,
) -> bool:
    """这个表达式的值会不会流进响应体。

    沿父链往上走，三种终止：`return` → 会；赋值 → 看被赋的名字能不能走到某条
    `return`；函数边界 → 不会。**中途遇到外层调用要停**：那说明本表达式的值
    是喂给别人的**实参**，不是自己往外走——`_upsert_patient(db, parse_fhir_patient(x))`
    里的 `parse_fhir_patient` 解析的是入站报文，它的明文 dict 最终进的是数据库，
    不是响应体（真正的响应体那一段走 `desensitize`）。只有当外层函数确实把这个
    形参里的 PII 搬进自己的返回结构时，才继续往上跟。

    赋值那一支**要跟多跳**：被赋的名字不直接出现在 `return` 里时，接着看它后面
    被读的每一处能不能走到 `return`——

        briefs = _patient_brief(db, [e.patient_id], user)   # ① 不在 return 里
        out = _enroll_out(enrollment, briefs.get("..."))    # ② briefs 的读点
        return out                                          # ③ out 在 return 里

    只跟一跳会把 `_patient_brief` 挡在响应构造闭包外，于是
    `GET /api/spd/enrollments/{id}` 看起来"声明了 phone 却全程没脱敏"——
    脱敏就在 `_patient_brief` 里（`visible_phone`），只是隔了一跳。
    这是本闸门的一处**漏跟**（少看见一个脱敏点 → 误报违规）。

    多跳**必须复用同一套父链规则**，不能简单地"赋值就传递"：②里 `briefs` 是
    喂给 `_enroll_out` 的实参，能继续往上全靠 `_enroll_out` 确实把它的 PII 搬进
    了返回结构；`data = parse_hl7v2_patient(raw)` 后面那些 `data` 的读点
    （喂给入库函数）在同一条规则下照样停住。递归而不是放宽，差别就在这里。
    """
    cur: ast.AST | None = parents.get(id(node))
    thunk = False
    seen = seen if seen is not None else set()
    while cur is not None:
        if isinstance(cur, ast.Return):
            return True
        if isinstance(cur, ast.Assign):
            names = {n.id for t in cur.targets for n in ast.walk(t) if isinstance(n, ast.Name)}
            if names & returned:
                return True
            if uses is None or not all(isinstance(t, ast.Name) for t in cur.targets):
                # 元组解包（`data, control_id = parse_hl7v2_patient(...)`）在这里止步：
                # 调用的值是整个元组，AST 上看不出是哪一个元素流向了响应——
                # 实际上 `data` 是进库的、`control_id` 才进响应。跟下去会把
                # `parse_hl7v2_patient` 里那个明文 dict 算成 `hl7v2_adt` 的出口，
                # 报一条不存在的泄漏。宁可退回原来的一跳，不假装知道是哪一路。
                return False
            for name in sorted(names - seen):
                seen.add(name)
                if any(
                    _flows_to_response(use, parents, returned, module, uses, seen)
                    for use in uses.get(name, [])
                ):
                    return True
            return False
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
        if isinstance(cur, ast.Lambda):
            # `_run_inbound(..., lambda: _do_hl7v2_adt(...))` 这种延迟执行的壳：
            # 外层调用返回的就是 lambda 算出来的东西，别在这儿把链断掉。
            thunk = True
        elif isinstance(cur, ast.Call):
            if thunk:
                thunk = False
            elif node is cur.func:
                # `briefs.get("phone")`：调用长在**自己身上**，结果是自己的一部分，
                # 不是"被喂给别人当实参"。在这儿停会把 `briefs` 的去向判成
                # 「没流进响应」，而它恰恰是经 `_enroll_out` 进响应的那一支。
                pass
            elif not _param_echoes_pii(cur, node, module):
                return False
        node, cur = cur, parents.get(id(cur))
    return False


def _param_echoes_pii(call: ast.Call, arg_node: ast.AST, module: str) -> bool:
    """`call` 是否把 `arg_node` 这个实参里的 PII 搬进自己的返回结构。"""
    name = _callee_name(call)
    target = resolve_call(module, name) if name else None
    if target is None:
        return False
    rooted = _param_rooted_pii_params(target)
    if not rooted:
        return False
    names = _params(FUNCS[target])
    for i, arg in enumerate(call.args):
        if arg is arg_node:
            return i < len(names) and names[i] in rooted
    for kw in call.keywords:
        if kw.value is arg_node:
            return kw.arg in rooted
    return False


def _calls_mask_helper(node: ast.AST) -> bool:
    """这段 AST 里有没有**真的调用** `app/privacy.py` 的脱敏函数。

    刻意不做字符串包含判断：函数的 docstring / 注释里写着"走 desensitize"
    在文本里一样命中，于是"写了要脱敏"会被当成"脱敏了"。变异验证正是踩在
    这一脚上——把 `return desensitize(patient, user)` 改回 `return patient`、
    只留下 docstring 里那个词，字符串判据照样放行。只认 Call 节点。
    """
    return any(
        isinstance(n, ast.Call) and _callee_name(n) in MASK_HELPERS for n in ast.walk(node)
    )


def _is_masked(value: ast.AST, fn) -> bool:
    """值表达式是否经过 `app/privacy.py`（直接写在表达式里，或来自本函数内的赋值）。"""
    if _calls_mask_helper(value):
        return True
    root = _root_name(value)
    if root is None:
        return False
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and any(
            isinstance(n, ast.Name) and n.id == root for t in node.targets for n in ast.walk(t)
        ):
            if _calls_mask_helper(node.value):
                return True
    return False


def collect_sites(closure: list[str]) -> tuple[list[Site], list[Site]]:
    """闭包里的出口点，拆成「本地根（要判）」与「形参根（委派给调用方）」两堆。"""
    judged: list[Site] = []
    delegated: list[Site] = []
    for key in closure:
        fn = FUNCS.get(key)
        if fn is None:
            continue
        module = key.split(":")[0]
        params = set(_params(fn))
        parents = _parents(fn)
        for node, name, value in _pii_dicts(fn):
            if not _dict_is_outbound(node, parents, module):
                continue
            root = _root_name(value)
            site = Site(key, node.lineno, name, ast.unparse(value), "")
            if root is not None and root in params:
                site.kind = "委派"
                delegated.append(site)
            elif _is_masked(value, fn):
                site.kind = "已脱敏"
                judged.append(site)
            else:
                site.kind = "明文"
                judged.append(site)
    return judged, delegated


def _dict_is_outbound(node: ast.Dict, parents: dict[int, ast.AST], module: str) -> bool:
    """dict 字面量是"出口"还是"入参"。

    直接作为某个调用的实参时：只有当被调函数确实把**这个形参**里的 PII 搬进
    自己的返回结构（`_param_rooted_pii_params`），才算出口；否则它是喂给
    建档/落库函数的入参（`create_patient_idempotent`、ORM 构造器），不是出口。
    """
    parent = parents.get(id(node))
    if not isinstance(parent, ast.Call):
        return True
    name = _callee_name(parent)
    if resolve_call(module, name) if name else None:
        return _param_echoes_pii(parent, node, module)
    # 被调方解析不到（`JSONResponse({...})`、ORM 构造器、第三方函数……）：
    # 不臆断它会不会把这个 dict 吐出去，如实计入盲区。
    BLIND.add("手写JSON/Response", f"{module}:{node.lineno} -> {name}(...)")
    return False


# ----------------------------------------------------------------- 逐端点判

class Endpoint:
    def __init__(self, route: APIRoute) -> None:
        self.methods = sorted(route.methods - {"HEAD", "OPTIONS"})
        self.path = route.path
        self.key = f"{route.endpoint.__module__}:{route.endpoint.__name__}"
        self.model_pii = _response_model_pii(route)
        self.closure: list[str] = []
        self.judged: list[Site] = []
        self.delegated: list[Site] = []
        self.reason = ""

    @property
    def label(self) -> str:
        return f"{'/'.join(self.methods)} {self.path}  [{self.key}]"


def analyse() -> tuple[list[Endpoint], list[Endpoint], int]:
    """返回（分母内的端点, 违规端点, 扫过的路由数）。"""
    endpoints: list[Endpoint] = []
    total = 0
    for route in _walk_routes(app.routes):
        if not route.path.startswith("/api") or not route.include_in_schema:
            continue
        total += 1
        ep = Endpoint(route)
        if ep.key not in FUNCS:
            if ep.model_pii:
                BLIND.add("处理函数无源码", ep.key)
            continue
        ep.closure = constructors(ep.key)
        for key in ep.closure:
            _scan_blind(key)
        ep.judged, ep.delegated = collect_sites(ep.closure)
        if not ep.model_pii and not ep.judged and not ep.delegated:
            continue  # 两条路径都没命中：这条端点不在分母里
        endpoints.append(ep)

    violations: list[Endpoint] = []
    for ep in endpoints:
        plain = [s for s in ep.judged if s.kind == "明文"]
        if plain:
            ep.reason = "出口点未经 app/privacy.py 脱敏：\n      " + "\n      ".join(
                str(s) for s in plain
            )
            violations.append(ep)
        elif ep.model_pii and not ep.judged:
            # response_model 声明了 PII 字段，闭包里又看不到任何 dict 出口点
            # （典型：直接返回 ORM 对象交给 FastAPI 序列化）——要求闭包里出现脱敏函数
            if not any(_calls_mask_helper(FUNCS[k]) for k in ep.closure if k in FUNCS):
                ep.reason = (
                    f"response_model 声明了 PII 字段 {sorted(ep.model_pii)}，"
                    "而整个响应构造闭包里没有出现任何 app/privacy.py 的脱敏函数"
                )
                violations.append(ep)
    return endpoints, violations, total


ENDPOINTS, VIOLATIONS, ROUTES_SCANNED = analyse()


# --------------------------------------------------------------- 例外清单

#: **好清单**：按设计返回明文的端点，逐条写理由。**只许减少，不许增加**——
#: 新增一条就是新开一个明文出口，必须在 PR 里单独说明并请人复核（CLAUDE.md §8）。
#: 例外失效（端点改名/下线/已改成脱敏）会被 `test_例外清单没有陈旧条目` 拦下。
EXCEPTIONS: dict[str, str] = {
    # —— 号码本身就是工单内容：脱敏等于把功能关掉 ——
    "app.spd.routers.followup:create_call_task": (
        "外呼工单：`get_call_provider().dispatch(task.id, phone, ...)` 之后要把号码交还"
        "给发起方核对；manual 通道就是人照着这个号码拨。掩码后工单不可用。"
    ),
    "app.spd.routers.followup:list_call_tasks": (
        "外呼任务列表：同上，坐席/村医照着列表拨号。列表另有 phone 模糊筛选入口，"
        "掩码后筛选与拨号两个功能一起失效。"
    ),
    "app.routers.emergency:dispatch": (
        "急救呼救回拨号码（caller_phone）：调度与出车环节按设计需要真值回拨呼救人，"
        "且呼救人未必是居民档案里的人。该列不是 EncryptedPII，数据模型侧同样按"
        "非居民主数据对待。路由器层已限角色（`/api/emergency` 整体 get_current_user）。"
    ),
    "app.routers.emergency:list_cases": "同 dispatch：调度台列表要能直接回拨呼救人。",
    "app.routers.emergency:set_rescue_outcome": "同 dispatch：救治结局回写后原样回显本案，含回拨号码。",
    "app.routers.emergency:advance": "同 dispatch：绿道节点推进后原样回显本案，含回拨号码。",
    # —— 不是居民 PII：工作人员通讯录 ——
    "app.spd.routers.config.teams:create_village_doctor": (
        "乡村医生通讯录：这是**工作人员**的工作联系方式，不是居民 PII——"
        "`SpdVillageDoctor.phone` 不是 EncryptedPII 列，仓库的数据模型侧也这么认定。"
        "端点限 CONFIG_ROLES；团队配置页要照着这个号码联系村医。"
    ),
    "app.spd.routers.config.teams:list_village_doctors": (
        "同 create_village_doctor：村医通讯录列表，配置端照着它联系/核对村医，"
        "且 SpdVillageDoctor.phone 不是 EncryptedPII 列（非居民主数据）。"
    ),
    "app.spd.routers.config.teams:update_village_doctor": (
        "同 create_village_doctor：改完原样回显该村医，回显的是调用方刚提交的通讯录条目。"
    ),
    # —— 回显调用方刚提交的值 ——
    "app.routers.portal:bind_phone": (
        "绑定成功后回显**本人刚提交的**号码（`body.phone` 经 `_check_phone` 归一化），"
        "没有向调用方透露任何它尚不知道的东西。居民端展示口径由 `/api/portal/me` 负责，"
        "那一条是掩码的。"
    ),
}

#: 存量违规基线（棘轮）：**只许调小**。本轮已清零——再出现就是新增的明文出口。
BASELINE_VIOLATIONS = 0

UNEXPECTED = [ep for ep in VIOLATIONS if ep.key not in EXCEPTIONS]


# ------------------------------------------------------------------- 用例

def _report() -> str:
    delegated = sum(len(ep.delegated) for ep in ENDPOINTS)
    judged = sum(len(ep.judged) for ep in ENDPOINTS)
    masked = sum(1 for ep in ENDPOINTS for s in ep.judged if s.kind == "已脱敏")
    by_model = [ep for ep in ENDPOINTS if ep.model_pii]
    by_ast = [ep for ep in ENDPOINTS if ep.judged or ep.delegated]
    return "\n".join(
        [
            "",
            "[PII 响应出口脱敏守卫] 覆盖面自证",
            f"  PII 列名来源：EncryptedPII 列类型（与检索闸门同一真源），"
            f"实测 {sorted(PII_COLUMN_NAMES)}；字段名**包含**其一即进分母",
            f"  脱敏入口来源：app/privacy.py 里定义的公开函数，实测 {sorted(MASK_HELPERS)}",
            f"  扫描范围：{len(FILES)} 个 .py 文件（app/ 全量）→ 索引出 {len(FUNCS)} 个模块级函数",
            f"  路由面：{ROUTES_SCANNED} 条已挂载的 /api 路由（运行期 app.routes，非抽样）",
            f"  分母（两条路径并集）：{len(ENDPOINTS)} 个端点 = "
            f"response_model 带 PII {len(by_model)} 个 ∪ 返回结构带 PII 键 {len(by_ast)} 个",
            f"  出口点：{judged + delegated} 处 = 本地根 {judged} 处"
            f"（已脱敏 {masked} / 明文 {judged - masked}） + 形参根委派 {delegated} 处",
            f"  分子（违规端点）：{len(VIOLATIONS)} 个 —— 其中按设计的例外 "
            f"{len(VIOLATIONS) - len(UNEXPECTED)} 个、未登记 {len(UNEXPECTED)} 个",
            f"  例外清单：{len(EXCEPTIONS)} 条（好清单，只减不增）",
            f"  认不出的形态（声明的盲区）：{BLIND.total()} 处 —— "
            + "、".join(f"{k} {len(v)}" for k, v in BLIND.items.items()),
        ]
    )


def test_覆盖面自证():
    """把「看了多少、认出多少、认不出多少」打印出来——只报覆盖率不报分子是自欺。"""
    summary = _report()
    print(summary)
    warnings.warn(summary, UserWarning, stacklevel=2)

    # 每类扫描的落脚点都不许为 0：为 0 就是空转，而不是"干净"。
    assert PII_COLUMN_NAMES, "没推导出任何 PII 列名 = 分母是空的"
    assert MASK_HELPERS, "没推导出任何脱敏函数 = 判据是空的"
    assert len(FILES) > 100, f"只扫到 {len(FILES)} 个文件，扫描范围塌了"
    assert len(FUNCS) > 500, f"只索引到 {len(FUNCS)} 个函数，索引塌了"
    assert ROUTES_SCANNED > 800, f"只走到 {ROUTES_SCANNED} 条路由，路由展开塌了"
    assert len(ENDPOINTS) >= 20, f"分母只剩 {len(ENDPOINTS)} 个端点，推导塌了"
    assert [ep for ep in ENDPOINTS if ep.model_pii], "路径①（response_model）一个都没命中"
    assert [ep for ep in ENDPOINTS if ep.judged or ep.delegated], "路径②（返回结构）一个都没命中"
    assert any(s.kind == "已脱敏" for ep in ENDPOINTS for s in ep.judged), (
        "一处「已脱敏」都没认出来——判据（MASK_HELPERS 匹配）多半已经失灵，"
        "此时「零违规」只是因为什么都没判"
    )


def test_返回PII的端点必须经privacy脱敏():
    """分母里的每个端点，要么脱敏，要么在例外清单里带着书面理由。"""
    assert UNEXPECTED == [], (
        "以下端点把身份证号/电话**明文**放进响应体——个保法口径下属上线阻断项"
        "（CLAUDE.md §4「出口一律经 privacy.py 脱敏」、§8 安全红线）：\n\n  "
        + "\n\n  ".join(f"{ep.label}\n    → {ep.reason}" for ep in UNEXPECTED)
        + "\n\n修法：值经 `privacy.visible_phone(v, user)` / `visible_id_card(v, user)`"
        "（admin 明文、其余掩码），整条患者档案用 `privacy.desensitize(patient, user)`。"
        "确属按设计的明文出口，把它加进本文件的 EXCEPTIONS 并写清理由 + 请人复核。"
    )


def test_违规基线只减不增():
    """棘轮：存量欠账只许变小。本轮已清零，这条盯住它别涨回去。"""
    unexpected_count = len(UNEXPECTED)
    assert unexpected_count <= BASELINE_VIOLATIONS, (
        f"未登记的明文出口从基线 {BASELINE_VIOLATIONS} 涨到 {unexpected_count}。"
        "基线只许调小。"
    )


def test_例外清单没有陈旧条目():
    """例外必须仍然命中分母——端点改名/下线/已改成脱敏了，例外就该删掉。

    陈旧例外比没有例外更坏：它让下一个人以为"这条已经有人想过了"。
    """
    live = {ep.key for ep in VIOLATIONS}
    stale = sorted(set(EXCEPTIONS) - live)
    assert stale == [], (
        f"以下例外已经不再命中任何明文出口，请连同理由一起删掉：{stale}。"
        "（端点被删/改名，或者它其实已经脱敏了。）"
    )


def test_每条例外都写了理由():
    empty = sorted(k for k, v in EXCEPTIONS.items() if len(v.strip()) < 20)
    assert empty == [], f"以下例外没写清理由（好清单的每一条都要能被单独复核）：{empty}"


def test_慢专病侧的患者电话出口确实走了平台脱敏口径():
    """回归：P1-33 实测到的那批明文出口（慢专病任务/随访/人群/档案）。

    它们是本轮修掉的东西。这条按"函数级"钉住，防的是有人把
    `visible_phone(...)` 改回 `patient.phone` 而恰好没顶破别的用例。
    """
    expected = {
        "app.spd.routers.population:_patient_brief",
        "app.spd.routers.population:patient_profile",
        "app.spd.routers.tasks:list_tasks",
        "app.spd.routers.tasks:get_task",
        "app.spd.routers.followup:followup_context",
        "app.spd.routers.portal:archive",
    }
    masked_funcs = {
        s.func_key for ep in ENDPOINTS for s in ep.judged if s.kind == "已脱敏"
    }
    missing = sorted(expected - masked_funcs)
    assert missing == [], (
        f"以下函数不再被认定为「已脱敏的 PII 出口」：{missing}。"
        "要么脱敏被改回明文，要么出口点整个消失了（那也得有人确认是有意的）。"
    )


# =========================================================== 运行期回归证据
#
# 上面全是静态判定。静态判定能说"代码里调了脱敏函数"，说不了"响应体里真的
# 是掩码"。本轮改动**是有意改响应字节的**（明文 PII 出网就是缺陷本身，
# CLAUDE.md 第 7 条向后兼容的例外），所以这一段用真请求把改前改后的差别钉死：
# 非 admin 拿到掩码、admin 仍拿到明文（admin 那条字节未变）。

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from conftest import reset_database  # noqa: E402
from app.privacy import mask_id_card, mask_phone  # noqa: E402

_ID_CARD = "330481199203041256"
_PHONE = "13700001234"


@pytest.fixture(scope="module")
def _client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def _fixtures(_client):
    admin = _client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin123"}
    ).json()
    ah = {"Authorization": f"Bearer {admin['access_token']}"}
    org = _client.post(
        "/api/organizations",
        json={"name": "出口脱敏卫生院", "org_type": "township", "level": "township"},
        headers=ah,
    ).json()
    _client.post(
        "/api/users",
        json={"username": "masking_doctor", "password": "passw0rd1", "role": "doctor",
              "full_name": "脱敏医生", "org_id": org["id"]},
        headers=ah,
    )
    doctor = _client.post(
        "/api/auth/login", json={"username": "masking_doctor", "password": "passw0rd1"}
    ).json()
    dh = {"Authorization": f"Bearer {doctor['access_token']}"}
    patient = _client.post(
        "/api/patients",
        json={"name": "脱敏患者", "id_card": _ID_CARD, "gender": "女",
              "birth_date": "1992-03-04", "phone": _PHONE},
        headers=ah,
    ).json()
    return {"admin": ah, "doctor": dh, "org": org, "patient": patient}


def _register(client, headers):
    return client.post(
        "/api/patients",
        json={"name": "脱敏患者", "id_card": _ID_CARD, "gender": "女",
              "birth_date": "1992-03-04", "phone": _PHONE},
        headers=headers,
    ).json()


def test_建档回包对非admin是掩码_对admin仍是明文(_client, _fixtures):
    """`POST /api/patients` 原先直接返回 ORM 对象，明文出网（P1-33 本轮实测）。

    幂等语义让它更糟：提交一个已存在的身份证号，就把那个人的档案原样拿回来。
    改后与两个 GET 兄弟端点同口径。admin 那一支**字节不变**。
    """
    as_admin = _register(_client, _fixtures["admin"])
    assert (as_admin["id_card"], as_admin["phone"]) == (_ID_CARD, _PHONE)

    as_doctor = _register(_client, _fixtures["doctor"])
    assert as_doctor["id_card"] == mask_id_card(_ID_CARD)
    assert as_doctor["phone"] == mask_phone(_PHONE)


def test_慢专病列表里的患者电话对非admin是掩码(_client, _fixtures):
    """`GET /api/spd/enrollments` 走 `_patient_brief`——本轮修掉的那个共用出口。

    选纳管列表而不是 360 档案：后者另有 `assert_patient_visible` 把跨机构的
    医生挡在 403（那条边界本来就该在，不是本包要动的东西），跑不到脱敏这一步。
    列表这条同样经 `_patient_brief`，能直接量到出口形态。
    """
    pid = _fixtures["patient"]["id"]
    created = _client.post(
        "/api/spd/enrollments",
        json={"patient_id": pid, "program_code": "hypertension",
              "org_id": _fixtures["org"]["id"], "risk_level": "mid"},
        headers=_fixtures["admin"],
    )
    assert created.status_code in (200, 201), created.text

    def _phone(headers):
        resp = _client.get("/api/spd/enrollments", headers=headers)
        assert resp.status_code == 200, resp.text
        rows = [r for r in resp.json() if r["patient_id"] == pid]
        assert rows, "纳管列表为空，这条用例就什么也没量到"
        return rows[0]["phone"]

    assert _phone(_fixtures["admin"]) == _PHONE
    assert _phone(_fixtures["doctor"]) == mask_phone(_PHONE)
