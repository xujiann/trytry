"""ADR-0009 第二步：`panel()` 组件与**第一页**迁移的守卫。

节奏按 ADR 走：组件与手写共存，一次只迁一页、人工过一页。本文件盯两件事——
组件自身的转义边界，以及已迁完的那一页不要退回手写。

顺带记一条方法论结论：**逐页人工过，找到了全量正则扫描找不到的东西。**
上一轮对三套前端做过一次插值扫描（见 test_frontend_chart_escaping.py 的文件注释），
结论是"文本字段类命中 30 条、真实漏洞 0 条"。那个结论**有盲区**：扫描只认
`x.y` 形式的属性访问，而这一页真正没转义的是一个**解构出来的局部变量**
（`const [text, color] = UNIFIED_STATUS[i.status] || [i.status, ""]` ——
查不到映射时 `text` 回落成后端原始状态码，裸插进 innerHTML）。
迁这一页时人眼看出来的。这正是 ADR 说"迁移一页、人工过一页，不要批量改"的理由。
"""
import pathlib
import re

import pytest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "app" / "static"
CORE = (STATIC / "core.js").read_text(encoding="utf-8")
MGMT = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")


def _fn(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    return src[start: src.index("\n}\n", start)]


# ------------------------------------------------------------ 组件自身
def test_panel转义标题():
    """标题由组件负责转义——这是抽组件的**全部理由**（CLAUDE.md §8）。"""
    body = _fn(CORE, "panel")
    assert "${esc(title)}" in body, "panel 没有转义 title，组件就白抽了"


def test_panel的accent也转义():
    """`accent` 插进 style 属性，同样不能裸插。"""
    assert "esc(accent)" in _fn(CORE, "panel")


def test_panel刻意不转义body_且这条边界写在注释里():
    """`body` 是调用方拼好的 HTML，组件不能替它转义。

    这不是疏漏，是必须讲清楚的边界——否则"用了组件就安全了"会变成错觉。
    要求注释里明确写出来，免得后来人以为组件包办了全部转义。
    """
    body = _fn(CORE, "panel")
    assert "${body}" in body, "body 应原样插入"
    doc = CORE[CORE.index("/**", CORE.index("面板外壳") - 400): CORE.index("function panel(")]
    assert "不转" in doc or "不能替它转义" in doc, "组件的转义边界没有写在文档注释里"


# ------------------------------------------------------------ 已迁移的第一页
MIGRATED = "renderServiceRequests"


def test_已迁移的页面不再手写panel外壳():
    fn = _fn(MGMT, MIGRATED)
    assert '<div class="panel"' not in fn, (
        f"{MIGRATED} 又出现了手写的 panel 外壳——迁过的页面不要退回去"
    )
    assert fn.count("panel(") >= 2, f"{MIGRATED} 应当有两处 panel() 调用"


def test_统一状态列的回落值必须转义():
    """`UNIFIED_STATUS` 查不到时 `text` 就是后端原始状态码，是服务端数据。

    这条是本轮真正修掉的那个裸插值的回归。
    """
    fn = _fn(MGMT, MIGRATED)
    assert re.search(r'class="tag \$\{color\}">\$\{esc\(text\)\}', fn), (
        "统一状态列的 text 没过 esc()——查不到映射时它是后端原始状态码"
    )
    assert "${text}</span>" not in fn, "还留着裸插的 ${text}"


@pytest.mark.parametrize("marker", ["ADR-0009", "迁一页"])
def test_迁移留了路标给下一页(marker):
    """下一个人得看得出"这页迁过了、按这个套路迁下一页"。"""
    assert marker in _fn(MGMT, MIGRATED), f"迁移注释里缺少 {marker}"


def test_守卫本身没瞎():
    """`_fn` 取错范围时，上面几条会在空字符串或超长字符串上失去区分力。"""
    fn = _fn(MGMT, MIGRATED)
    assert 500 < len(fn) < 4000, f"{MIGRATED} 取到 {len(fn)} 字符，函数体范围不对"
    assert "sr-form" in fn, "取到的不是这个函数"
    assert len(_fn(CORE, "panel")) < 400, "panel 函数体取得过长，范围不对"
