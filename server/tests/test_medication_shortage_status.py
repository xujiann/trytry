"""缺药登记的状态口径：前端展示与后端状态机不得各说各话。

这一页出过两个缺陷，根子是同一个——**前端手里那份状态清单比后端的短**：

1. `const [t, col] = SS[s.status]` 没有兜底。`SS` 只列了流转中的三个状态
   （registered / purchasing / delivered），而后端还会写 collected / no_show /
   cancelled（`_SHORTAGE_CLOSED`）。解构 undefined 抛 TypeError，且这一句在
   `table()` 的行渲染回调里，于是**整页白屏**——只要有一条缺药登记结了案，
   这一页就再也打不开。

2. 「流转」按钮的判据是 `s.status !== "delivered"`，把"终态"等同于"已配送"。
   结案的三个状态照样显示按钮，点下去必定 409（"状态 collected 已是终态"）。
   第 1 条没修之前这个看不出来——那些行根本渲染不出来。

所以这里不钉"前端写了哪几个字符串"，而是钉**两层的口径一致**：能流转的状态
必须恰好是后端 `_SHORTAGE_FLOW` 的键。后端加一个中间状态而前端忘了跟，
或者前端把判据写回"排除某一个终态"，都会让这条红。
"""
import pathlib
import re

from app.routers.medication import _SHORTAGE_CLOSED, _SHORTAGE_FLOW

CLINICAL = (
    pathlib.Path(__file__).resolve().parent.parent / "app" / "static" / "pages-clinical.js"
).read_text(encoding="utf-8")


def _render_medication() -> str:
    start = CLINICAL.index("async function renderMedication(")
    return CLINICAL[start: CLINICAL.index("\n}\n", start)]


def test_能流转的状态与后端状态机一致():
    """前端 `canAdvance` 列出的状态 == 后端 `_SHORTAGE_FLOW` 的键。"""
    fn = _render_medication()
    match = re.search(r"const canAdvance = ([^;]+);", fn)
    assert match, "renderMedication 里找不到 canAdvance——判据被改写了？"
    shown = set(re.findall(r's\.status === "(\w+)"', match.group(1)))
    assert shown == set(_SHORTAGE_FLOW), (
        f"前端认为可流转的状态 {sorted(shown)} 与后端 _SHORTAGE_FLOW "
        f"{sorted(_SHORTAGE_FLOW)} 不一致——不一致的那些状态要么少了按钮、"
        f"要么点下去必定 409"
    )


def test_终态一个都不显示流转按钮():
    """后端的终态（已配送 + 三个结案态）都不该出现「流转」。"""
    terminal = ({"delivered"} | set(_SHORTAGE_CLOSED)) - set(_SHORTAGE_FLOW)
    fn = _render_medication()
    match = re.search(r"const canAdvance = ([^;]+);", fn)
    shown = set(re.findall(r's\.status === "(\w+)"', match.group(1)))
    assert not (shown & terminal), (
        f"这些是终态却仍显示「流转」：{sorted(shown & terminal)}"
    )
    assert terminal, "终态集合算空了，这条用例失去了区分力"


def test_状态标签查表必须带兜底():
    """回归第 1 条：`SS[s.status]` 没有兜底会让整页白屏。

    通用形状守卫在 `test_frontend_escape_guard.py`；这里再钉一次具体位置，
    因为这一处有过真实故障，而通用守卫是按形状扫的、将来可能被调窄。
    """
    fn = _render_medication()
    assert re.search(r"const \[t, col\] = SS\[s\.status\] \|\| \[", fn), (
        "SS[s.status] 又没有兜底了——映射未命中时解构 undefined 抛 TypeError，"
        "整页白屏"
    )
    assert "${esc(t)}" in fn, "兜底出来的是后端原始状态码，必须 esc()"
