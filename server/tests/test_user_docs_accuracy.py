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
