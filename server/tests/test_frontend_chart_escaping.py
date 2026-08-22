"""共享图表组件必须把**每一个**标签都转义。

CLAUDE.md §8 的红线是"前端渲染用户数据一律先 `esc()`"，而全仓库有近 3000 处
模板插值——对全部插值做静态扫描信噪比太差（实测：宽口径 1892 条命中、
收窄到文本字段仍有 30 条，逐条核对后**真实漏洞 0 条**，其余全是
`alert()`/`confirm()`/`prompt()`/`setMsg()`（走 textContent，转义了反而显示成
`&amp;`）、数字、坐标、以及下游已经 `esc()` 的中间变量）。把这种扫描做成门禁
只会天天误报，最后被加豁免加到失效。

所以门禁收在**信噪比真正好的地方**：`barChart` / `lineChart` 是三套前端共用的
渲染出口，标签直接来自后端数据，且函数很短、边界清楚。ADR-0009 的论点正是
"把转义收进组件，就漏不掉"——这两个函数是那个论点目前**唯一已经成立**的样本
（`barChart` 早就 `esc(label)` 了，本轮把 `lineChart` 的月份标签对齐）。
它们要是退化了，扫描口径再宽也补不回来。
"""
import pathlib
import re

import pytest

CORE = (pathlib.Path(__file__).resolve().parent.parent / "app" / "static" / "core.js").read_text(
    encoding="utf-8"
)


def _strip_line_comments(src: str) -> str:
    """去掉 `//` 行注释再扫描。

    不去掉会被自己的注释坑到：本文件的注释里写了一个字面的 `<text>` 标签，
    正则当场把它当成真标签，一路吞到下一个 `</text>`，把一段属性当成了标签内容
    并报"未转义"。注释里的字符不是渲染出来的 HTML，本就不该进扫描。
    """
    return "\n".join(re.sub(r"//.*$", "", line) for line in src.splitlines())


def _body(name: str) -> str:
    start = CORE.index(f"function {name}(")
    end = CORE.index("\n}\n", start)
    return _strip_line_comments(CORE[start:end])


@pytest.mark.parametrize("fn", ["barChart", "lineChart"])
def test_图表组件里进入text元素的插值必须过esc(fn):
    """只查 `<text>` 元素的内容——那是标签落地的地方。

    坐标、宽高、颜色不查：它们是本函数自己算出来的数字，转义它们没有意义，
    查了只会逼着后来人写一堆 `esc(Math.round(...))`。
    """
    body = _body(fn)
    # <text ...>内容</text> 里的每一段 ${...}
    unescaped = []
    for m in re.finditer(r"<text[^>]*>(.*?)</text>", body, re.S):
        for interp in re.finditer(r"\$\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}", m.group(1)):
            expr = interp.group(1).strip()
            if "esc(" not in expr:
                unescaped.append(expr)
    assert not unescaped, (
        f"{fn} 的 <text> 内容里这些插值没过 esc()：{unescaped}。"
        " 图表标签直接来自后端数据，组件是三套前端共用的渲染出口（CLAUDE.md §8）。"
    )


@pytest.mark.parametrize("fn", ["barChart", "lineChart"])
def test_扫描确实扫到了东西(fn):
    """防呆：`_body()` 抓错范围或正则写歪时，上面那条会在空字符串上恒真。"""
    body = _body(fn)
    assert "<text" in body, f"{fn} 里没找到 <text>，取函数体的方式坏了"
    assert body.count("${") >= 3, f"{fn} 的函数体只抓到 {body.count('${')} 处插值，范围不对"


def test_barChart的标签确实被esc包着():
    """把 ADR-0009 的正面样本钉死：这是"转义收进组件"目前唯一成立的例子。"""
    assert "${esc(label)}" in _body("barChart")
