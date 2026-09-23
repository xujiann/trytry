"""前端渲染的字段名必须在后端契约里存在——孤儿棘轮看不见的那一类错。

## 为什么要有这道闸门

补孤儿端点界面时连踩三次同一个坑：

| 写成 | 实际契约 | 后果 |
|---|---|---|
| `i.result` | `CheckupItemOut.result_value` | 体检分项那一列全是 `undefined` |
| `a.avg_days` | 预警行的 `baseline_avg_days` | 超长住院预警的"组均"一列空白 |
| `t.title` | `ConsentTextOut.content` | 同意文本版本库看不到文本 |

三次都**不会**被既有闸门抓到：孤儿棘轮判的是"这条路径有没有人调用过"，
路径调用点照样在；转义棘轮判的是"插值有没有过 esc()"，`esc(undefined)` 也叫
过了。渲染取证（`scripts/render_diff.js`）能看出来，但它只覆盖管理端页面、
且要人去看。于是错误的表现是**界面上一列 undefined 或空白**，而所有测试全绿。

## 判据

对下面每一条 `(页面块, JS 变量名, 契约模型)`，扫出该块里所有 `变量.字段` 的
写法，逐个要求字段在模型的 `model_fields` 里。契约模型是 pydantic 的
`response_model`——**字段集从模型现算，不抄清单**：后端改字段名，这里当场变红。

## 承认的边界（都在 test_覆盖面自证 里打印）

1. **只覆盖声明了 `response_model` 的端点**。这条盲区原先很大（近半数端点直接
   返回裸 dict，契约欠账 **P2-5**——此处原先误写成 P1-38，那是居民端转诊措辞、
   与契约无关）。P2-5 已清到 99.8%（946/948），所以这条盲区如今几乎不剩什么。
2. ~~三元组是手写的~~ —— **本轮改成推导为主、手写为辅**（P1-52）。原先的理由是
   "免构建前端没有解析器，数据流分析不可靠"。这话对了一半：**任意**变量的来源
   确实推不出来，但有一条链是**窄到可以推准**的——

       const 列表 = await api("<路径>")   →   table(列表, (行) => ...) / 列表.map((行) => ...)

   赋值与使用挨在一起、中间没有别的赋值，路径又能反查到 `response_model`。
   按这条链推出 243 处渲染点（覆盖 90 个 render 函数、443 个字段名），
   比手写的 9 条三元组多两个数量级，
   而且**前端改了会自己跟着变**，不需要谁记得回来加一行。
   推不准的一律**放弃而不是猜**（见下方四条收紧），放弃的部分计入盲区打印。
   手写三元组保留：它们覆盖的是推导链之外的形状（详情对象、嵌套行），只许增。
3. **只判字段名存在，不判类型与语义**：`${a.stayed_days}` 写成
   `${a.baseline_cases}` 两个都存在，本闸门判不出来。

不设"未覆盖渲染点"的数字基线：那个数会随任何一次无关的界面改动上下浮动，
钉住它只会制造噪声。真正只进不退的是 `CHECKED` 这份三元组清单——只许增。
"""
from __future__ import annotations

import re
import warnings
from pathlib import Path

import pytest
from typing import get_args, get_origin

from pydantic import BaseModel

from app.routers.checkups import CheckupItemOut
from app.routers.consents import ConsentTextOut
from app.routers.credentials import OneCodeIssueOut
from app.routers.education import ArticleListOut
from app.routers.exams import ReportTemplateOut
from app.routers.inpatient import ExecutionOut
from app.routers.infectious import CaseReportCardOut
from app.routers.users import LoginLogOut
from app.schemas import ConsultationOut

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
SRC = {name: (STATIC / name).read_text(encoding="utf-8") for name in
       ("core.js", "pages-clinical.js", "pages-mgmt.js", "pages-public.js", "pages-spd.js")}


def _block(file: str, fn: str) -> str:
    src = SRC[file]
    start = src.index(fn)
    rest = src[start + len(fn):]
    marker = "\nasync function render"
    return src[start:start + len(fn) + rest.index(marker)] if marker in rest else src[start:]


#: (页面文件, render 函数, JS 变量名, 契约模型)。**只许增，不许减**——
#: 删一条等于把一块渲染移出视野，而那正是这道闸门要防的事。
#:
#: 前提：变量名在该 render 块里**只指代这一种契约**。同一个 `c` 在一个函数里
#: 既当列表行又当详情对象时，这道闸门会把两边的字段混在一起判、当场误报——
#: 正确的处理是把 JS 里的变量改成能区分的名字（本轮就改了三处），而不是把
#: 判据放松。误报在这里是有用的：它逼出来的正是"这个 c 到底是什么"这个问题。
CHECKED: list[tuple[str, str, str, type[BaseModel]]] = [
    ("pages-public.js", "async function renderCerts()", "i", CheckupItemOut),
    ("pages-clinical.js", "async function renderConsents()", "t", ConsentTextOut),
    ("pages-clinical.js", "async function renderInfectious()", "card", CaseReportCardOut),
    ("pages-clinical.js", "async function renderInpatient()", "x", ExecutionOut),
    ("pages-clinical.js", "async function renderAudit()", "lg", LoginLogOut),
    ("core.js", "async function renderConsultations()", "c", ConsultationOut),
    ("core.js", "async function renderExams()", "t", ReportTemplateOut),
    ("pages-public.js", "async function drawHealthArticles()", "a", ArticleListOut),
    ("pages-mgmt.js", "async function renderCredentials()", "oc", OneCodeIssueOut),
]

#: JS 侧的局部量/内置量，不是契约字段——出现在 `变量.xxx` 里也不该按契约判。
JS_NOISE = {
    "length", "map", "filter", "join", "slice", "split", "replace", "includes",
    "forEach", "find", "toFixed", "trim", "dataset", "value", "target", "then",
    "catch", "sort", "reverse", "indexOf", "push", "toString", "repeat", "some",
    "every", "keys", "values", "entries", "startsWith", "endsWith", "padStart",
}

def _field_hits(block: str, var: str) -> set[str]:
    """块里所有 `var.field` 的 field 名（含模板插值里的）。"""
    return {
        m.group(1)
        for m in re.finditer(rf"\b{re.escape(var)}\.([A-Za-z_$][\w$]*)", block)
    } - JS_NOISE


@pytest.mark.parametrize(
    ("file", "fn", "var", "model"),
    CHECKED,
    ids=[f"{fn.split('function ')[1][:-2]}:{var}" for _f, fn, var, _m in CHECKED],
)
def test_渲染用的字段名都在契约里(file, fn, var, model):
    block = _block(file, fn)
    hits = _field_hits(block, var)
    assert hits, f"{fn} 里找不到任何 `{var}.字段` ——三元组写错了变量名，这条断言在空转"
    unknown = sorted(hits - set(model.model_fields))
    assert unknown == [], (
        f"{fn} 渲染了 {model.__name__} 上不存在的字段：{unknown}。\n"
        f"  契约字段：{sorted(model.model_fields)}\n"
        "  这类错孤儿棘轮看不见（路径调用点照样在），界面上的表现是一列 undefined。"
    )


def test_判据不空转_契约里没有的字段必须判成错():
    """反向证明：把一个不存在的字段塞进块里，必须被认出来。"""
    fake = 'x = `${i.definitely_not_a_field}`'
    assert _field_hits(fake, "i") == {"definitely_not_a_field"}
    assert "definitely_not_a_field" not in CheckupItemOut.model_fields


def test_覆盖面自证():
    total_sites = 0
    for src in SRC.values():
        total_sites += len(re.findall(r"\$\{[A-Za-z_$][\w$]*\.[A-Za-z_$][\w$]*", src))
    covered = sum(len(_field_hits(_block(f, fn), v)) for f, fn, v, _m in CHECKED)
    warnings.warn(
        "\n".join([
            "",
            "  [渲染字段名闸门] 覆盖面自证",
            f"    静态资源里的 `${{变量.字段}}` 插值点：{total_sites} 处（5 个 .js 全量）",
            f"    本闸门覆盖的字段名：{covered} 个，来自 {len(CHECKED)} 条 (页面, 变量, 契约) 三元组",
            "    —— 以下是这道闸门**看不见**的部分 ——",
            "    盲区①只覆盖声明了 response_model 的端点（契约欠账 P2-5 已清到 99.8%，此路几乎不剩）",
            f"    盲区②推导只认「赋值→紧接着的行回调」这一条链，认不准的放弃而不猜"
            f"（另见 test_推导覆盖面自证 打印的 {len(DERIVED)} 条链）",
            "    盲区③只判字段名存在，不判类型与语义：两个都存在的字段写串了，判不出来",
        ]),
        stacklevel=1,
    )


# ---------------------------------------------------------------------------
# 从 JS 推导渲染点（P1-52）：手写三元组改成推导为主
# ---------------------------------------------------------------------------
#
# 能推准的只有这一条链，**别的形状一律放弃**：
#
#     const 列表 = await api("<路径>")   →   table(列表, (行) => …) / 列表.map((行) => …)
#
# 赋值与使用挨在一起，路径能反查到 `response_model`，回调体又天然是"这个行变量
# 只可能是这一种契约"的作用域。三条收紧全是被**真实误报**逼出来的，不是预防性的：
#
# ① **同名变量在同一个 render 块里被赋过两次就整条放弃**。`renderBilling` 里
#    `rows` 先是押金流水、后是调价历史，两个不同接口；不放弃就会拿押金的契约去
#    判调价历史的字段，报出一个根本不存在的"缺陷"。歧义要放弃，不要猜。
#    记歧义时**不管那次赋值解析得出解析不出**——只记解析得出的，会让"另一次赋的
#    是个没有契约的接口"这种情况悄悄漏过去。
# ② **`Promise.all` 解构必须元素与变量名一一对得上，且每个元素都是裸 `api(...)`**。
#    `admissionId ? api(…) : Promise.resolve(null)` 这种条件元素会让 zip 错位，
#    把 A 接口的契约安到 B 变量头上。
# ③ **`.map` 的接收者不能是属性访问**。`stats.groups.map((g) => …)` 里的 `g` 是
#    `stats.groups` 的行，不是 `groups` 变量的行——少了这条收紧就会拿
#    `/api/drgs/groups` 的契约去判 `stats.groups` 的字段。
# ④ **行变量的括号要配平**。写成 `\(?…\)?` 会把**箭头形参表**也吃进去：
#    `(qc, title) =>` 里 `qc,` 后面跟着 `title)` 再跟 `=>`，于是第二个形参
#    被当成了行变量。
#
# 推导目前认出 243 条链，覆盖 90 个 render 函数、443 个字段名。这个数**只许多不许少**：
# 掉下去说明判据被哪次改动悄悄打断了，而闸门不会因此变红——它只会安静地少看几处。
BASELINE_DERIVED_CHAINS = 243

#: `api()` 路径 → 契约字段集。路径反查复用孤儿棘轮那份正则（同一个"前端怎么写
#: 这个调用"的问题不留两份答案）。
_ROUTE_FIELDS: list[tuple[str, re.Pattern, set[str]]] = []

_ASSIGN = re.compile(r"const\s+([A-Za-z_$][\w$]*)\s*=\s*await\s+api\(\s*[`\"']([^`\"'?]+)")
_MULTI = re.compile(r"const\s*\[([^\]]+)\]\s*=\s*await\s+Promise\.all\(\s*\[(.*?)\]\s*\)", re.S)
_BARE_API = re.compile(r"\s*api\(\s*[`\"']([^`\"'?]+)[^)]*\)\s*", re.S)
_BLOCK_HEAD = re.compile(r"^(?:async )?function (?:render|draw)\w*\(", re.M)


def _route_fields() -> list[tuple[str, re.Pattern, set[str]]]:
    if not _ROUTE_FIELDS:
        from test_frontend_endpoint_coverage import _iter_routes, _pattern_for

        for route in _iter_routes():
            if "GET" not in route.methods:
                continue
            model = route.response_model
            if get_origin(model) in (list, set, tuple):
                args = get_args(model)
                model = args[0] if args else None
            if isinstance(model, type) and issubclass(model, BaseModel):
                _ROUTE_FIELDS.append((route.path, _pattern_for(route.path), set(model.model_fields)))
    return _ROUTE_FIELDS


def _resolve(path: str) -> tuple[str, set[str]] | None:
    for route_path, pattern, fields in _route_fields():
        if pattern.match(path):
            return route_path, fields
    return None


def _split_top(text: str) -> list[str]:
    """按**顶层逗号**切数组元素——`api(\\`…${x}\\`)` 里的逗号不算。"""
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return out


def _callback_body(block: str, start: int) -> str:
    """从 `=>` 之后起取到括号配平为止——回调体就是行变量的作用域。"""
    depth, i = 0, start
    while i < len(block):
        if block[i] in "({[":
            depth += 1
        elif block[i] in ")}]":
            depth -= 1
            if depth <= 0:
                return block[start:i + 1]
        i += 1
    return block[start:]


def _all_blocks() -> list[tuple[str, str, str]]:
    """(文件, 函数名, 源码块)——按 `function render*/draw*` 切。"""
    out = []
    for file, src in SRC.items():
        starts = [m.start() for m in _BLOCK_HEAD.finditer(src)]
        for i, start in enumerate(starts):
            end = starts[i + 1] if i + 1 < len(starts) else len(src)
            name = src[start:src.index("(", start)].split()[-1]
            out.append((file, name, src[start:end]))
    return out


def _chains_in_block(block: str) -> list[tuple[str, str, set[str], str]]:
    """一个 render 块里推得出的 (路径, 行变量, 契约字段集, 回调体)。"""
    assigned: dict[str, set[str]] = {}
    for m in _ASSIGN.finditer(block):
        assigned.setdefault(m.group(1), set()).add(m.group(2))
    multi: list[tuple[str, str]] = []
    for m in _MULTI.finditer(block):
        names = [n.strip() for n in m.group(1).split(",") if n.strip()]
        elems = _split_top(m.group(2))
        if len(names) != len(elems):  # 收紧②：对不齐就整条放弃
            continue
        for name, elem in zip(names, elems):
            bare = _BARE_API.fullmatch(elem)
            if bare:
                assigned.setdefault(name, set()).add(bare.group(1))
                multi.append((name, bare.group(1)))

    binding: dict[str, tuple[str, set[str]]] = {}
    for name, paths in assigned.items():
        if len(paths) != 1:  # 收紧①：同名两次赋值 = 歧义，放弃
            continue
        resolved = _resolve(next(iter(paths)))
        if resolved:
            binding[name] = resolved

    chains = []
    for var, (route_path, fields) in binding.items():
        # 收紧③：`(?<![.\w$])` —— `stats.groups.map` 里的 `groups` 不是本变量
        # `table(表头, 列表, (行) => …)` 与 `列表.map((行) => …)` 两种写法。
        # 表头那个参数不去解析——只认"列表变量紧跟着一个箭头回调"这个形状。
        # 括号要**配平**：`\(?…\)?` 会把 `(qc, title) =>` 这种**箭头形参表**也吃进去
        # （`qc,` 后面跟着 `title)` 再跟 `=>`），于是把第二个形参当成了行变量。
        use = re.compile(
            rf"(?<![.\w$]){re.escape(var)}\s*(?:,\s*|\.map\(\s*)"
            rf"(?:\(\s*([A-Za-z_$][\w$]*)\s*\)|([A-Za-z_$][\w$]*))\s*=>"
        )
        for m in use.finditer(block):
            row_var = m.group(1) or m.group(2)
            chains.append((route_path, row_var, fields, _callback_body(block, m.end())))
    return chains


def _derived() -> list[tuple[str, str, str, str, set[str], str]]:
    """(文件, 函数, 路径, 行变量, 契约字段集, 回调体)。"""
    out = []
    for file, fn, block in _all_blocks():
        for route_path, var, fields, body in _chains_in_block(block):
            out.append((file, fn, route_path, var, fields, body))
    return out


DERIVED = _derived()


@pytest.mark.parametrize(
    ("fn", "route_path", "var", "fields", "body"),
    [(fn, rp, v, f, b) for _file, fn, rp, v, f, b in DERIVED],
    ids=[f"{fn}:{rp}:{v}" for _file, fn, rp, v, _f, _b in DERIVED],
)
def test_推导出的渲染点字段名都在契约里(fn, route_path, var, fields, body):
    hits = _field_hits(body, var)
    unknown = sorted(hits - fields)
    assert unknown == [], (
        f"{fn} 用 {route_path} 的行渲染了契约上没有的字段：{unknown}\n"
        f"  契约字段：{sorted(fields)}\n"
        "  这类错孤儿棘轮看不见（路径调用点照样在），界面上的表现是一列 undefined。"
    )


def test_推导链只许多不许少():
    """判据被哪次改动悄悄打断时，闸门不会变红——它只会安静地少看几处。"""
    assert len(DERIVED) >= BASELINE_DERIVED_CHAINS, (
        f"推导出的渲染点从 {BASELINE_DERIVED_CHAINS} 掉到 {len(DERIVED)}。"
        " 要么前端把某几处清单渲染改成了推不出来的写法（那就调低基线并说明），"
        " 要么推导判据被打断了（那是这道闸门在悄悄失明）。"
    )


# —— 判据不空转：三条收紧各自都要能被证明确实在起作用 ——

_FAKE_ROUTE = "/api/organizations"  # 真实存在、带 response_model 的清单端点


def test_推导不空转_合成块里的错字段必须报出来():
    block = (
        'async function renderFake() {\n'
        f'  const rows = await api("{_FAKE_ROUTE}");\n'
        '  $("#x").innerHTML = table(["名"], rows, (o) => `<tr><td>${o.definitely_not_a_field}</td></tr>`);\n'
        '}\n'
    )
    chains = _chains_in_block(block)
    assert len(chains) == 1, "这条最基本的链都推不出来，说明推导整个失效了"
    route_path, var, fields, body = chains[0]
    assert (route_path, var) == (_FAKE_ROUTE, "o")
    assert sorted(_field_hits(body, var) - fields) == ["definitely_not_a_field"]


def test_收紧一_同名变量赋过两次就放弃而不是猜():
    """`renderBilling` 真踩过：`rows` 先是押金流水、后是调价历史。"""
    block = (
        'async function renderFake() {\n'
        f'  const rows = await api("{_FAKE_ROUTE}");\n'
        '  const rows = await api("/api/users");\n'
        '  $("#x").innerHTML = table(["名"], rows, (o) => `${o.name}`);\n'
        '}\n'
    )
    assert _chains_in_block(block) == [], "同名两次赋值仍然推出了链——那是在猜"


def test_收紧二_PromiseAll里有条件元素时不许错位():
    """`admissionId ? api(…) : Promise.resolve(null)` 会让 zip 错位。"""
    block = (
        'async function renderFake() {\n'
        '  const [bal, rows] = await Promise.all([\n'
        '    cond ? api("/api/users") : Promise.resolve(null),\n'
        f'    api("{_FAKE_ROUTE}"),\n'
        '  ]);\n'
        '  $("#x").innerHTML = table(["名"], rows, (o) => `${o.name}`);\n'
        '}\n'
    )
    chains = _chains_in_block(block)
    assert [(rp, v) for rp, v, _f, _b in chains] == [(_FAKE_ROUTE, "o")], (
        f"条件元素把契约安到了错的变量上：{[(rp, v) for rp, v, _f, _b in chains]}"
    )


def test_收紧三_属性访问不算本变量():
    """`stats.groups.map((g) => …)` 里的 `g` 是 `stats.groups` 的行，不是 `groups` 的。"""
    block = (
        'async function renderFake() {\n'
        f'  const groups = await api("{_FAKE_ROUTE}");\n'
        '  $("#x").innerHTML = barChart(stats.groups.map((g) => [g.drg_code, g.avg_cost]));\n'
        '}\n'
    )
    assert _chains_in_block(block) == [], "把 `stats.groups` 的行当成了 `groups` 的行"


def test_推导覆盖面自证(capsys):
    fns = {fn for _f, fn, _rp, _v, _fl, _b in DERIVED}
    field_names = sum(len(_field_hits(b, v)) for _f, _fn, _rp, v, _fl, b in DERIVED)
    blocks = _all_blocks()
    no_api = [fn for _f, fn, block in blocks if not _ASSIGN.search(block) and not _MULTI.search(block)]
    with capsys.disabled():
        print(f"\n  [渲染字段名闸门 · 推导部分] render/draw 块 {len(blocks)} 个")
        print(f"    推导出的 (清单接口 → 行变量) 链：{len(DERIVED)} 条，"
              f"覆盖 {len(fns)} 个函数、{field_names} 个字段名（基线 {BASELINE_DERIVED_CHAINS}）")
        print(f"    手写三元组另覆盖 {len(CHECKED)} 处（推导链之外的形状：详情对象、嵌套行）")
        print(f"    没有任何 `const x = await api(...)` 的块：{len(no_api)} 个（多是纯表单/纯图表）")
        print("    放弃而不猜的四类：同名变量重复赋值、Promise.all 元素对不齐、"
              "属性访问式接收者、括号不配平的箭头形参表")
    assert DERIVED


#: 回调体里一个字段都没取的链（回调只是转调另一个函数，字段在那边访问）。
#: 这类链**不是错**，但它什么也没断言——数量要可见，免得哪天回调体提取坏掉、
#: 243 条链全变成空转而闸门照样全绿。
MAX_EMPTY_CHAINS = 3


def test_空转的推导链要少而可见():
    empty = [
        f"{fn} {route_path} ({var})"
        for _file, fn, route_path, var, _fields, body in DERIVED
        if not _field_hits(body, var)
    ]
    assert len(empty) <= MAX_EMPTY_CHAINS, (
        f"回调体里取不到任何字段的链涨到 {len(empty)} 条：{empty}\n"
        "  少量是正常的（回调只转调另一个函数），成片出现说明回调体提取坏了——"
        "那时这道闸门会安静地全绿，什么也没判。"
    )
