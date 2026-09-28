"""新窗口要在点击手势里同步开：`window.open` 之前不得有 `await`（P2-682，第十六批「导出 / 打印 vs 页面」扫描线索）。

浏览器只在用户手势（点击）的同步阶段放行 `window.open`：await 之后再开，Safari 一律拦、Chrome 在临时激活过期后拦，
页面只剩一句「浏览器拦截了新窗口」。`spdOpenSvg` 的注释写着「开窗必须在点击手势里同步做（await 之后再开会被弹窗
拦截），与 openPrintPage 同一口径」——可 `openPrintPage`（报告、处方笺、申请单、证明等 14 处打印入口共用）原先是
fetch 回来、读完正文之后才开。端到端档的 Chromium 带 `--disable-popup-blocking` 启动，拦截复现不出来，只能静态钉。

判据：静态脚本（含医生端 / 居民端）里每个调用 `window.open(` 的函数，从函数开头到调用点之间不得出现 `await`。
"""
import re
from pathlib import Path

from jssrc import strip_comments

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
FUNC = re.compile(r"^(?:async )?function (\w+)\s*\(", re.M)


def window_open_sites(sources: dict[str, str] | None = None) -> dict[tuple[str, str], bool]:
    """`{(文件, 所在函数): 调用点之前有没有 await}`。"""
    if sources is None:
        sources = {p.relative_to(STATIC).as_posix(): p.read_text(encoding="utf-8") for p in sorted(STATIC.rglob("*.js"))}
    sites: dict[tuple[str, str], bool] = {}
    for name, raw in sources.items():
        src = strip_comments(raw)
        funcs = [(m.start(), m.group(1)) for m in FUNC.finditer(src)]
        for call in re.finditer(r"\bwindow\.open\(", src):
            start, owner = max(((s, f) for s, f in funcs if s < call.start()), default=(0, "?"))
            late = bool(re.search(r"\bawait\b", src[start:call.start()]))
            sites[(name, owner)] = sites.get((name, owner), False) or late
    return sites


def test_新窗口都在第一个await之前开():
    late = sorted(k for k, v in window_open_sites().items() if v)
    assert late == [], f"这几处 await 之后才 window.open，会被弹窗拦截——先开窗、取回再写（P2-682）：{late}"


def test_判据自证_认得出与放得过():
    src = {"x.js": """
async function bad(path) {
  const r = await fetch(path);
  const win = window.open("", "_blank");
}
async function good(path) {
  // await 写在注释里不算
  const win = window.open("", "_blank");
  const r = await fetch(path);
}
"""}
    assert window_open_sites(src) == {("x.js", "bad"): True, ("x.js", "good"): False}


def test_扫描面_两处开窗函数都在():
    assert {("pages-clinical.js", "openPrintPage"), ("pages-spd.js", "spdOpenSvg")} <= set(window_open_sites())
