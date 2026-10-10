"""用户手册写的界面与操作照现行页面（第五十三批「手册与规范写的 vs 现行行为」扫描 AQ4）。

用户照着手册找按钮、等提醒，手册写错了，现场就是「找不到」「没等到」。每条用例的 docstring 写条目号、扫描编号与修前的现象；
能从页面源码数出来的（页签、导航分组、轮询间隔）就从源码现取，不在用例里另写一份。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANUAL = (ROOT / "docs" / "用户手册.md").read_text(encoding="utf-8")
STATIC = ROOT / "server" / "app" / "static"


def _numbered_item(text: str, marker: str) -> str:
    """以 `marker` 起头的那一个有序列表项（到下一项或空行为止）。"""
    start = text.index(marker)
    end = re.search(r"\n(?:\d+\. |\s*\n)", text[start + 1:])
    return text[start:start + 1 + end.start()] if end else text[start:]


def test_用户手册不写页面上没有的实时推送与弹窗():
    """P2-1807（第五十三批扫描 AQ4-7）：用户手册第零章写「登录后自动建立推送通道，危急值（发往申请机构）与缺药预警会弹出
    提醒」「急事仍以铃铛与弹窗为准」。前端没有 WebSocket：管理端铃铛约 30 秒轮询一次 `/api/todos` 与未读数，只改角标与下拉
    （core.js 的 `pollTodos`），不弹窗、不出声；运维手册早按 P2-276 写成「内置页面未接 WS，看铃铛」，两份手册说法相反，医师
    照用户手册等弹窗，危急值只体现在角标数字上。

    两头一起钉：手册不再出现这几种说法、写的刷新间隔与 core.js 一致；前端（static 下的页面与脚本）里确实没有 WebSocket。
    日后 P2-276 定了页面接 WS 弹窗并落地，后一条会先红——那时把手册第零章改回去，连同本用例一起改。"""
    stale = [phrase for phrase in ("推送通道", "弹出提醒", "弹窗为准") if phrase in MANUAL]
    assert not stale, f"用户手册还写着页面上没有的提醒方式：{stale}"
    sources = sorted(p for p in STATIC.rglob("*") if p.suffix in (".js", ".html"))
    assert len(sources) >= 10, f"只找到 {len(sources)} 个前端文件，扫描对象变了？"
    with_ws = [p.name for p in sources if re.search(r"WebSocket|/ws/", p.read_text(encoding="utf-8"))]
    assert not with_ws, f"前端接了 WebSocket（{with_ws}）：用户手册第零章的提醒方式要跟着改，再改这条用例"
    interval = re.search(r"setInterval\(pollTodos, (\d+)\)", (STATIC / "core.js").read_text(encoding="utf-8"))
    assert interval, "core.js 里找不到铃铛的轮询，扫描对象变了？"
    item = _numbered_item(MANUAL, "5. **危急值与缺药提醒**")
    assert f"约 {int(interval.group(1)) // 1000} 秒" in item, f"手册写的刷新间隔与 core.js（{interval.group(1)} ms）不一致：{item}"


_CN_DIGITS = "零一二三四五六七八九"


def _cn(n: int) -> str:
    """1–99 的中文数字（手册里的页签数用中文写）。"""
    tens, ones = divmod(n, 10)
    return (("" if tens == 1 else _CN_DIGITS[tens]) + "十" if tens else "") + (_CN_DIGITS[ones] if ones else "")


def _tab_labels(html_name: str) -> list[str]:
    """移动端页面底栏的页签名（`tab-btn` 链接里图标之后的文字）。"""
    html = (STATIC / "m" / html_name).read_text(encoding="utf-8")
    return re.findall(r'class="tab-btn[^"]*" data-tab="[^"]+"><span class="ico">[^<]*</span>([^<]+)<', html)


def test_用户手册的导航分组与页签照页面源码():
    """P2-1812（第五十三批扫描 AQ4-12）：用户手册第零章的导航分组漏了「全域慢专病」（`app.js` 的 `PAGES` 有 8 个分组），
    第三章写医生移动端「七个页签」、第七章写居民端「五个标签页」，页面上是 8 个与 6 个——两端都加了「慢专病」页签，手册的
    页签表也没有这一行。这里从页面源码现数：导航分组按顺序一一对上；两端页签数写对、每个页签都在手册那一处出现（居民端那张
    表的行就是页面上的页签，顺序也一样）。"""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    pages = js[js.index("const PAGES = ["):]
    groups = re.findall(r'\{ group: "([^"]+)"', pages[:pages.index("\n];")])
    nav = re.sub(r"\s+", "", _numbered_item(MANUAL, "2. **导航**"))
    listed = re.search(r'"([^"]+)"分组', nav)
    assert listed and listed.group(1).split("/") == groups, f"手册写的导航分组与 app.js 不一致：{nav}；应为 {groups}"

    doctor = _tab_labels("doctor.html")
    line = next(ln for ln in MANUAL.splitlines() if ln.startswith("移动端 `/m/doctor`"))
    assert f"{_cn(len(doctor))}个页签" in line, f"医生移动端是 {len(doctor)} 个页签：{line}"
    assert not [label for label in doctor if label not in line], f"手册没写到这些医生移动端页签：{line}"

    resident = _tab_labels("index.html")
    chapter = MANUAL[MANUAL.index("## 第七章 居民端"):]
    assert f"{_cn(len(resident))}个标签页" in chapter.split("\n\n", 2)[1], f"居民端是 {len(resident)} 个标签页"
    table = chapter[chapter.index("| 标签页 |"):].split("\n\n", 1)[0].splitlines()[2:]
    rows = [row.split("|")[1].strip() for row in table]
    assert rows == resident, f"手册居民端页签表与页面不一致：{rows}；页面上是 {resident}"
