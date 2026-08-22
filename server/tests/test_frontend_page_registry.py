"""管理端 SPA 的**页面注册表**守卫：写了页面忘了注册，当场变红。

免构建的原生 JS SPA 有三份"要靠人记得同步"的清单，漏一处的后果都是**静默**的
——后端全绿、前端也不报错，只是那个页面**打不开或看不见**：

1. `app.js` 的 `const PAGES = [...]`：新写一个 `renderXxx()` 却忘了登记，
   这个页面在导航里根本不存在。没有任何测试会因此变红。
2. `index.html` 的 `<script src>` 列表：新拆一个 `pages-*.js` 却忘了挂脚本，
   它里面所有页面在 `PAGES` 求值时全部 `ReferenceError`——整个前端白屏。
   （`app.js` 的文件头注释已经写明"注册表必须最后加载"，说明这一坑踩过。）
3. `PAGES` 条目里的 `roles: [...]`：角色名是**裸字符串**，与后端
   `deps.ROLE_NAMES` 各存一份。写错一个字母（`dircetor`），`pageAllowed()`
   永远返回 false，该页面对除 admin 外所有人消失——不报错，就是没有。

这三条都能从代码结构推导，不需要任何人工清单：

* 页面注册与函数定义的对账，从 JS 源码里扫 `function render*` 与 `render:` 引用；
* 脚本清单与静态目录对账；
* 角色名与后端 `ROLE_NAMES` 对账（后端是唯一真源，前端不再另立）。

判据（本轮全仓排查用的同一条）：忘记更新它，会静默出错还是当场变红？
改造之后，这三样都是后者。
"""
from __future__ import annotations

import re
import warnings
from pathlib import Path

from app.deps import ROLE_NAMES

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
APP_JS = STATIC / "app.js"
INDEX_HTML = STATIC / "index.html"

#: 管理端 SPA 加载的脚本（`m/` 是另一套移动端页面，各自的 html 自己挂）。
ADMIN_JS = sorted(p for p in STATIC.glob("*.js"))

#: `function renderXxx(` —— 顶层函数声明。SPA 全用函数声明，没有 const 箭头函数。
DEF_RE = re.compile(r"^(?:async\s+)?function\s+(render[A-Za-z0-9_$]*)\s*\(", re.M)
#: `render: renderXxx` —— 注册表里的引用。
REG_RE = re.compile(r"render:\s*([A-Za-z0-9_$]+)")
#: 注册表条目（一条一行，仓库现状如此）。
ENTRY_RE = re.compile(r"\{[^{}]*\}")


def _pages_block() -> str:
    """截出 `const PAGES = [ … ];` 的真身。

    定位用行首锚点：文件头的注释里也写着 `const PAGES = [{ render: renderDashboard }]`
    作为示例，按子串找会切到注释上（第一版就切错了，用例当场报"条目缺 id/title"）。
    """
    source = APP_JS.read_text(encoding="utf-8")
    match = re.search(r"^const PAGES = \[", source, re.M)
    assert match, "app.js 里找不到 `const PAGES = [`——注册表被改名/改写法了，本守卫需同步"
    end = source.index("\n];", match.start())
    return source[match.start():end]


def _entries() -> list[dict[str, str]]:
    """把 PAGES 的每个条目粗解析成 {字段名: 原文}。不引 JS 解析器（无构建约定）。"""
    out = []
    for raw in ENTRY_RE.findall(_pages_block()):
        fields = {}
        for key in ("id", "title", "group", "render", "roles"):
            m = re.search(rf"\b{key}:\s*(\[[^\]]*\]|\"[^\"]*\"|[A-Za-z0-9_$]+)", raw)
            if m:
                fields[key] = m.group(1)
        out.append(fields)
    return out


def _defined_renders() -> dict[str, str]:
    """``{函数名: 所在文件}``——全部静态 JS 里的 `render*` 函数声明。"""
    out: dict[str, str] = {}
    for path in ADMIN_JS:
        for name in DEF_RE.findall(path.read_text(encoding="utf-8")):
            out[name] = path.name
    return out


def _all_admin_source() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in ADMIN_JS)


ENTRIES = _entries()
DEFINED = _defined_renders()
REGISTERED = set(REG_RE.findall(_pages_block()))


def test_覆盖面自证():
    pages = [e for e in ENTRIES if "id" in e]
    groups = [e for e in ENTRIES if "group" in e]
    summary = "\n".join([
        "",
        "[SPA 页面注册表守卫] 覆盖面自证",
        f"  扫描：{len(ADMIN_JS)} 个管理端 JS 文件（{', '.join(p.name for p in ADMIN_JS)}）",
        f"  注册表：{len(ENTRIES)} 条 = 页面 {len(pages)} + 分组标题 {len(groups)}",
        f"  render 函数：源码里定义 {len(DEFINED)} 个 / 注册表引用 {len(REGISTERED)} 个"
        f" / 既未注册也未被调用 {len(_orphans())} 个",
        f"  角色名真源：后端 deps.ROLE_NAMES（{len(ROLE_NAMES)} 个），前端不另立清单",
    ])
    print(summary)
    warnings.warn(summary, UserWarning, stacklevel=2)
    assert ENTRIES and DEFINED, "注册表或函数定义解析为空 = 这道闸门什么也没守"


def _orphans() -> list[str]:
    """定义了、既没注册进 PAGES、也没有被别处调用的 `render*` 函数。

    "被别处调用"这一支是必须的：`renderCaseTable` / `renderSettlement` 这类是
    页面内部的局部渲染件，本来就不该出现在注册表里。判据不是命名，而是
    **它到底有没有入口**——没有入口的 render 函数就是一个写好了却进不去的页面。
    """
    source = _all_admin_source()
    orphans = []
    for name in sorted(DEFINED):
        if name in REGISTERED:
            continue
        # 定义处自己算一次引用，出现两次以上才说明有人调它
        if len(re.findall(rf"\b{re.escape(name)}\b", source)) > 1:
            continue
        orphans.append(f"{name}（{DEFINED[name]}）")
    return orphans


def test_写了页面就必须有入口():
    orphans = _orphans()
    assert orphans == [], (
        "以下 render 函数既没登记进 app.js 的 PAGES，也没有被任何地方调用——"
        "写好了却进不去的页面，不会有任何测试因此变红：\n  " + "\n  ".join(orphans)
        + "\n（页面内部的局部渲染件不受影响：它们被自己的页面调用，有入口。）"
    )


def test_注册表引用的函数都存在():
    missing = sorted(REGISTERED - set(DEFINED))
    assert missing == [], (
        f"PAGES 引用了不存在的 render 函数：{missing}。"
        " PAGES 是在**求值时**取函数引用的，缺一个就是 ReferenceError，整个前端白屏。"
    )


def test_页面id唯一():
    ids = [e["id"] for e in ENTRIES if "id" in e]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    assert dup == [], f"PAGES 里出现重复的页面 id：{dup}——后一条永远路由不到（`find` 取第一条）。"


def test_页面条目字段完整():
    """页面条目必须同时有 id/title/render；分组标题只能有 group（+可选 roles）。"""
    broken = []
    for raw, fields in zip(ENTRY_RE.findall(_pages_block()), ENTRIES):
        if "group" in fields:
            continue
        if not {"id", "title", "render"} <= set(fields):
            broken.append(raw.strip()[:80])
    assert broken == [], (
        "以下 PAGES 条目缺 id/title/render：\n  " + "\n  ".join(broken)
        + "\n缺 render 的条目点进去会 `page.render is not a function`；缺 title 的标题是 undefined。"
    )


def test_页面声明的角色名以后端为准():
    """前端 `roles:` 里的角色名必须来自后端 `deps.ROLE_NAMES`。

    这是"同一份口径在两处各存一份"的典型：写错一个字母不会报错，只会让这个
    页面对所有非 admin 角色**消失**。把前端这一份改成对后端真源的核对之后，
    角色名不再需要有人两边记得同步。
    """
    unknown = []
    for entry in ENTRIES:
        raw = entry.get("roles")
        if not raw:
            continue
        for role in re.findall(r"\"([^\"]+)\"", raw):
            if role not in ROLE_NAMES:
                unknown.append(f"{entry.get('id') or entry.get('group')}: {role!r}")
    assert unknown == [], (
        "以下页面声明了后端不存在的角色名（该页面会对所有非 admin 角色消失）：\n  "
        + "\n  ".join(unknown)
        + f"\n后端 deps.ROLE_NAMES 只有：{sorted(ROLE_NAMES)}"
    )


def test_静态JS都被index挂上():
    """新拆一个 `pages-*.js` 却忘了在 index.html 加 `<script src>`，
    它里面的页面会在 PAGES 求值时全部 ReferenceError——整个管理端白屏。

    清单不手写：以 `app/static/*.js` 这个目录事实为准逐个核对。
    """
    html = INDEX_HTML.read_text(encoding="utf-8")
    missing = [p.name for p in ADMIN_JS if f"/static/{p.name}" not in html]
    assert missing == [], (
        f"以下管理端脚本没有被 index.html 挂上：{missing}。"
        " PAGES 在求值时就要拿到函数引用，缺一个文件就是整页白屏。"
    )


def test_index里挂的脚本都存在():
    html = INDEX_HTML.read_text(encoding="utf-8")
    refs = re.findall(r'<script src="/static/([^"]+)"', html)
    missing = [r for r in refs if not (STATIC / r).exists()]
    assert missing == [], f"index.html 挂了不存在的脚本（404，后续脚本全部不执行）：{missing}"


def test_脚本加载顺序_注册表在最后():
    """`app.js` 必须最后加载：它在**求值时**就要取到各页面的函数引用，
    而函数声明的提升只在同一个文件内生效（见 app.js 文件头注释）。"""
    html = INDEX_HTML.read_text(encoding="utf-8")
    refs = re.findall(r'<script src="/static/([^"]+)"', html)
    assert refs[-1] == "app.js", f"index.html 的最后一个脚本应是 app.js，实为 {refs[-1]}"
