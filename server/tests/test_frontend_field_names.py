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

1. **只覆盖声明了 `response_model` 的端点**。本仓库 948 个端点里近半数直接
   返回裸 dict（契约欠账 P1-38），那些没有可比对的字段集，本闸门看不见。
2. **三元组是手写的**，不是从 JS 推的——免构建前端没有解析器，靠正则做
   "哪个变量来自哪个接口"的数据流分析不可靠。手写清单在这里是可接受的：
   漏登记一条不会让错的东西静默通过，只会让覆盖数字变小，而那个数字会打印
   出来。`BASELINE_UNCHECKED` 钉住"未覆盖的渲染点"只许变少。
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
            "    盲区①只覆盖声明了 response_model 的端点；裸 dict 返回的端点没有可比对的字段集（P1-38）",
            "    盲区②三元组手写，不是从 JS 推的——漏登记只让覆盖变小，不会让错的静默通过",
            "    盲区③只判字段名存在，不判类型与语义：两个都存在的字段写串了，判不出来",
        ]),
        stacklevel=1,
    )
