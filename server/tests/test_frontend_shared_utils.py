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
