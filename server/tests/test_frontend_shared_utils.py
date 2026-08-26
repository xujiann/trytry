"""ADR-0009 第一步的守卫：`$` / `esc` 只许有一份实现。

为什么这条值得单独立一个用例：`esc()` 是 CLAUDE.md §8 的红线
（"前端渲染用户数据一律先 esc()"），而三套前端有近百处手写 innerHTML 插值。
同一个函数存三份，就有三个地方可能被改坏、被漏改——本轮之前已经出过一次
"改一处漏两处"。合成一份之后，这条用例负责让它**回不去**。

顺带钉住加载顺序：`shared.js` 必须是每个 HTML 入口的第一个 script。
后面的文件若再声明一次 `const $`，同作用域重复声明是 SyntaxError，
整个页面白屏——这种错误在免构建前端里没有编译期能拦，只能靠这里。
"""
import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
SHARED = STATIC / "shared.js"

#: 三套前端的入口脚本。它们都依赖 shared.js 提供的 `$` / `esc`。
CONSUMERS = [
    STATIC / "core.js",
    STATIC / "app.js",
    STATIC / "pages-clinical.js",
    STATIC / "pages-mgmt.js",
    STATIC / "pages-public.js",
    STATIC / "pages-spd.js",
    STATIC / "m" / "m.js",
    STATIC / "m" / "doctor.js",
]

#: 三个 HTML 入口，shared.js 必须排在第一个 script。
ENTRIES = [
    STATIC / "index.html",
    STATIC / "m" / "index.html",
    STATIC / "m" / "doctor.html",
]


def test_shared_js_存在且定义了两个工具():
    source = SHARED.read_text(encoding="utf-8")
    assert re.search(r"^const \$ = ", source, re.M), "shared.js 应定义 $"
    assert re.search(r"^function esc\(", source, re.M), "shared.js 应定义 esc"


@pytest.mark.parametrize("path", CONSUMERS, ids=lambda p: p.name)
def test_消费方不得再自定义esc或美元符(path):
    """重复 `const $` 是 SyntaxError（整页白屏）；重复 `esc` 是安全隐患。"""
    if not path.exists():          # 文件被拆分/改名时不要假红
        pytest.skip(f"{path.name} 不存在")
    source = path.read_text(encoding="utf-8")
    assert not re.search(r"^\s*function esc\(", source, re.M), (
        f"{path.name} 又自己定义了 esc()——转义逻辑只许有一份（shared.js），"
        "三份实现意味着三个可能被改坏的地方"
    )
    assert not re.search(r"^\s*(const|let|var) \$\s*=", source, re.M), (
        f"{path.name} 又自己声明了 $——与 shared.js 同作用域重复 const 声明是 "
        "SyntaxError，整个页面会白屏"
    )


@pytest.mark.parametrize("path", ENTRIES, ids=lambda p: p.name)
def test_shared_js_必须最先加载(path):
    html = path.read_text(encoding="utf-8")
    scripts = re.findall(r'<script src="([^"]+)"', html)
    assert scripts, f"{path.name} 里没有 script 标签？"
    assert scripts[0] == "/static/shared.js", (
        f"{path.name} 的第一个 script 是 {scripts[0]}，应为 /static/shared.js——"
        "后面的文件在求值时就要用到 $ / esc"
    )


def test_esc_覆盖五个危险字符():
    """转义表漏一个字符就是一个 XSS 入口，逐个钉住。"""
    source = SHARED.read_text(encoding="utf-8")
    for char, entity in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"),
                         ('"', "&quot;"), ("'", "&#39;")):
        assert entity in source, f"esc 的转义表少了 {char!r} → {entity}"
    assert "?? \"\"" in source, "null/undefined 应转成空串，而不是字面量 'null'"


# ---------------------------------------------------------------------------
# ADR-0009 第二步 / P2-26：`statusTag()` 也收成一份实现
#
# 起因是本轮那 33 处未转义：全都是在手抄同一段三行代码，而其中的 `esc()` 谁都
# 可以忘。收进组件之后，调用点连"要不要转义"这个问题都不会遇到——这正是
# ADR-0009 的论点（`barChart` 是第一个样本，这是第二个，也是有血的那个）。
#
# 放 shared.js 而不是 core.js：`.panel` 是管理端独有的标记，而 `.tag` 三端都在用
# （style.css 与 m/m.css 各自定义 `.tag` 与 `.tag.red/.green/.orange`，
# 配色不同、类名约定一致）。判据始终是"三端是不是真的都在用"。

def test_shared_js_定义了statusTag():
    source = SHARED.read_text(encoding="utf-8")
    assert re.search(r"^function statusTag\(", source, re.M), "shared.js 应定义 statusTag"


def test_statusTag把文案与配色都转义了():
    """转义收在组件里是它存在的**全部理由**——漏一个就白抽了。"""
    source = SHARED.read_text(encoding="utf-8")
    body = source[source.index("function statusTag("):]
    body = body[: body.index("\n}")]
    assert "esc(hit ? hit[0] : key ?? \"\")" in body, "statusTag 没有转义文案"
    assert "esc(hit ? hit[1] : \"\")" in body, "statusTag 没有转义配色类名"


def test_statusTag兜底用空值合并而不是或():
    """`key || ""` 会把数字 0 吞成空白，而本仓库有数字状态码（慢病分级 1/2/3）。

    这不是假想：`scripts/statustag_equiv.js` 的等价性矩阵当场抓到过这一条。
    """
    source = SHARED.read_text(encoding="utf-8")
    body = source[source.index("function statusTag("):]
    body = body[: body.index("\n}")]
    assert "key ?? \"\"" in body, "兜底应当用 ?? 而不是 ||，否则数字 0 会被吞掉"
    assert "key || " not in body, "statusTag 内部不该用 || 兜底 key"


@pytest.mark.parametrize(
    "path,helper",
    [(STATIC / "pages-spd.js", "spdTag"), (STATIC / "m" / "m.js", "spdTagOf")],
    ids=["pages-spd", "m.js"],
)
def test_慢专病的标签助手必须委托而不是再抄一遍(path, helper):
    """两个助手此前是**逐字相同**的两份实现，现在都只许委托给 statusTag。

    它们各自保留"空状态显示 `—`"的约定（管理端历来显示空白），那条约定写在
    调用点上即可——合并实现不等于统一行为，后者是改字节。
    """
    source = path.read_text(encoding="utf-8")
    body = source[source.index(f"function {helper}("):]
    body = body[: body.index("\n}")]
    assert "statusTag(" in body, f"{helper} 应当委托给 statusTag"
    assert "<span" not in body, (
        f"{helper} 里又出现了 <span> 标记——它应该只委托，不再自己拼 HTML"
    )
    assert "esc(" not in body, f"{helper} 不该再自己转义，那是 statusTag 的事"
