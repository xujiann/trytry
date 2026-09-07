"""P2-20：默认页靠 `PAGES[1]` 这个硬编码下标取，头部插一个分组就崩。

`PAGES[0]` 是 `{ group: "总览" }`——**分组标题不是页面**（没有 id、没有 title、
没有 render）。所以 `PAGES[1]` 指到驾驶舱纯属排列的巧合。往注册表头部插一个分组
（一个很正常的改动，加一组菜单而已），兜底就会指到一个分组项上：

    修之前 PAGES[1] : title=undefined render=undefined
    修之后 homePage(): title=决策驾驶舱 render=function
    修之前调 render() → TypeError: before.render is not a function

`page.render()` 直接抛 TypeError、`page.title` 渲染成 `undefined`
——**整个管理端路由不起来**，而插分组的人完全想不到会碰到这里。

而且同一个意图**上一行本来就是按 id 写的**（`location.hash… || "dashboard"`），
只有兜底那两处退回了下标。改成 `homePage()` 按 id 取，并留第二重兜底
（首页被改名/删掉时退到第一个真能渲染的页面）——那种情况退化成「进了别的页」
而不是「拿到分组项然后白屏」，前者看得见。

本文件三条：形状（不许再按下标取）、前提（首项确实是分组）、闭环（首页 id 真的在表里）。
"""
import os
import re

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


def _strip_comments(src: str) -> str:
    """去掉注释，**保行号**（块注释换等量换行）。

    这次是先写好再改代码的——本轮已经八次栽在「注释不是代码」上，
    而修 P2-20 的那段注释里就原样写着 `PAGES[1]` / `PAGES[0]` 在讲缺陷。
    """
    src = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), src, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", line) for line in src.splitlines())


def _read(name: str) -> str:
    return open(os.path.join(STATIC, name), encoding="utf-8").read()


def test_默认页不得按下标取():
    """形状：`core.js` 里不许再出现 `PAGES[数字]`。"""
    code = _strip_comments(_read("core.js"))
    hits = [
        f"core.js:{i}  {m.group(0)}"
        for i, line in enumerate(code.splitlines(), 1)
        for m in re.finditer(r"PAGES\[\s*\d+\s*\]", line)
    ]
    assert not hits, (
        "默认页/兜底页又按下标取了——往 PAGES 头部插一个分组就会指到分组项上，"
        "`render()` 抛 TypeError、整个管理端路由不起来。按 id 取（见 homePage()）：\n  "
        + "\n  ".join(hits)
    )


def test_注册表首项确实是分组_这正是下标脆的原因():
    """前提自证：`PAGES[0]` 是分组标题、不是页面。

    这条不是为了拦谁，是为了**把「为什么下标不能用」这件事钉在代码上**。
    哪天首项变成了真正的页面，这条会红——那时该重新读一遍路由再决定，
    而不是想当然地以为下标又安全了。
    """
    code = _strip_comments(_read("app.js"))
    first = code[code.index("const PAGES = [") :].splitlines()[1]
    assert "group:" in first and "id:" not in first, (
        f"PAGES 首项不再是分组了（现在是 {first.strip()}）——"
        "「下标取默认页不安全」的前提变了，重新评估 core.js 的路由兜底"
    )


def test_首页id必须真的在注册表里():
    """闭环：`HOME_PAGE_ID` 指的那个页面得存在，否则兜底静默退到第二重。"""
    core = _strip_comments(_read("core.js"))
    home_id = re.search(r'const HOME_PAGE_ID = "([^"]+)"', core).group(1)
    pages = _strip_comments(_read("app.js"))
    assert f'id: "{home_id}"' in pages, (
        f"HOME_PAGE_ID={home_id!r} 在 PAGES 里找不到——兜底会静默退到"
        "「第一个能渲染的页面」，默认页就悄悄换人了"
    )
