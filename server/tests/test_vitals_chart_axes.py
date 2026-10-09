"""体温单把体温和脉搏画在同一根从 0 起的纵轴上，热型看不出来（P2-1766，第五十二批扫描 AP3-2）。

`core.js` 的 `lineChart` 所有序列共用一个最大值、纵轴从 0 起；住院临床文书的体温单把体温和脉搏一起传进去。修前实测（扫描
的数据：一天四次，体温 36.8 / 38.2 / 39.5 / 37.1，脉搏 80 / 96 / 112 / 84）：体温四点纵坐标 121.5 / 119.4 / 117.5 / 121.0，
36.8→39.5℃ 的热峰在 166px 高的作图区里只差 4.0px；纵轴只标「112」「0」，没有体温刻度；同一组体温这几次没测脉搏，纵向差
变成 11.3px——曲线形状随有没有测脉搏变。P2-158（缺测不画成 0）、P2-1336（横轴按测量时刻）两次修这个组件都是为保住热型。

修法：`lineChart` 收可选的第五个参数 `axes`（按序列名给自己的值域、刻度间隔与标在哪一侧），给了的序列只按自己的值域映射、
在那一侧逐格标刻度，测得超出的按整格往外扩；体温单给体温 35–42℃（每格 1℃，左）、脉搏 40–180 次/分（每格 20，右），照纸质
体温单的刻度（每 1℃ 对 20 次/分，37℃ 与 80 次/分同高）。不传 `axes` 的驾驶舱、审计两处输出逐字节不变（下面的字面量取自修前
的代码）。

跑法照 `test_frontend_line_chart_axis.py`：在 node 里原样取 `shared.js` 的 `esc`、`core.js` 的 `lineChart`、`pages-mgmt.js` 的
`vitalTimeMs`，再把三处调用方的那段 `lineChart(...)` 调用从源码里原样取出来跑。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
SHARED = (STATIC / "shared.js").read_text(encoding="utf-8")
CORE = (STATIC / "core.js").read_text(encoding="utf-8")
MGMT = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 可执行这段前端函数")

#: 作图区上下沿（padT = 10，h - padB = 200 - 24）：高 166px
PLOT_TOP, PLOT_BOTTOM = 10, 176
RED, TEAL = "#c0392b", "#0b6e6e"   # 体温、脉搏两条线的颜色（页面传的）
#: 扫描复现用的那组数据：（测量时刻, 体温, 脉搏）
SCAN = [("2026-10-09 08:00", 36.8, 80), ("2026-10-09 12:00", 38.2, 96), ("2026-10-09 16:00", 39.5, 112),
        ("2026-10-09 20:00", 37.1, 84)]

#: 修前（dfc9b20）的代码对下面 `_dashboard()` / `_audit()` 两组输入的输出，逐字节钉住
DASHBOARD_BEFORE = (
    '<svg width="640" height="200" role="img"><polyline points="36,104.85714285714285 630,10" fill="none" stroke="#0b6e6e"'
    ' stroke-width="2"/><circle cx="36" cy="104.85714285714285" r="2.5" fill="#0b6e6e"/><circle cx="630" cy="10" r="2.5"'
    ' fill="#0b6e6e"/><polyline points="630,128.57142857142856" fill="none" stroke="#0a4d78" stroke-width="2"/><circle'
    ' cx="630" cy="128.57142857142856" r="2.5" fill="#0a4d78"/><text x="36" y="194" font-size="10.5" fill="#5b6773"'
    ' text-anchor="middle">26-09</text><text x="630" y="194" font-size="10.5" fill="#5b6773" text-anchor="middle">26-10'
    '</text><text x="4" y="14" font-size="10.5" fill="#5b6773">7</text><text x="4" y="180" font-size="10.5"'
    ' fill="#5b6773">0</text></svg>'
)
AUDIT_BEFORE = (
    '<svg width="640" height="200" role="img"><polyline points="36,10 630,176" fill="none" stroke="#0b6e6e"'
    ' stroke-width="2"/><circle cx="36" cy="10" r="2.5" fill="#0b6e6e"/><circle cx="630" cy="176" r="2.5"'
    ' fill="#0b6e6e"/><polyline points="36,162.16666666666666 630,176" fill="none" stroke="#c0392b" stroke-width="2"/>'
    '<circle cx="36" cy="162.16666666666666" r="2.5" fill="#c0392b"/><circle cx="630" cy="176" r="2.5" fill="#c0392b"/>'
    '<text x="36" y="194" font-size="10.5" fill="#5b6773" text-anchor="middle">10-01</text><text x="630" y="194"'
    ' font-size="10.5" fill="#5b6773" text-anchor="middle">10-02</text><text x="4" y="14" font-size="10.5"'
    ' fill="#5b6773">12</text><text x="4" y="180" font-size="10.5" fill="#5b6773">0</text></svg>'
)


def _function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    return src[start:src.index("\n}\n", start) + 2]


def _call(src: str, marker: str) -> str:
    """从 `marker`（以 `lineChart(` 开头）起按括号配平，原样取出整段调用；跳过字符串与 `//` 行注释。"""
    start = src.index(marker)
    depth, quote, i = 0, None, start
    while i < len(src):
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif src.startswith("//", i):
            i = src.index("\n", i)
            continue
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1
    raise AssertionError(f"{marker} 后面的括号没配平")


def _render(context: str, expr: str) -> str:
    script = "\n".join([_function(SHARED, "esc"), _function(CORE, "lineChart"), _function(MGMT, "vitalTimeMs"),
                        context, f"process.stdout.write(String({expr}));"])
    return subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True, timeout=60).stdout


def _vitals(rows) -> str:
    vitals = [{"measured_at": at, "temperature": t, "pulse": p} for at, t, p in rows]
    return _render(f"const vitals = {json.dumps(vitals)};", _call(MGMT, "lineChart(vitals.map("))


def _dashboard() -> str:
    labels_line = re.search(r"^\s*const trendLabels = .+;$", CORE, re.M).group(0)
    trends = {"months": ["2026-09", "2026-10"], "series": {"encounters": [3, 7], "referrals": [None, 2]}}
    context = (f"const trends = {json.dumps(trends)};\n"
               f'const trendColors = ["#0b6e6e", "#0a4d78", "#b26a00", "#8d4bab"];\n{labels_line}')
    return _render(context, _call(CORE, "lineChart(trendLabels"))


def _audit() -> str:
    daily = [{"date": "2026-10-01", "ok": 12, "failed": 1}, {"date": "2026-10-02", "ok": 0, "failed": 0}]
    return _render(f"const auditDaily = {json.dumps(daily)};", _call(MGMT, "lineChart(auditDaily.map("))


def _dots(svg: str, color: str) -> list[tuple[float, float]]:
    return [(float(x), float(y))
            for x, y in re.findall(rf'<circle cx="([^"]+)" cy="([^"]+)" r="2.5" fill="{color}"/>', svg)]


def _ticks(svg: str, color: str) -> list[tuple[float, float, str]]:
    """纵轴刻度（字用那条线的颜色）：(x, y, 文字)。"""
    return [(float(x), float(y), text) for x, y, text in
            re.findall(rf'<text x="([^"]+)" y="([^"]+)" font-size="10.5" fill="{color}">([^<]*)</text>', svg)]


@needs_node
def test_热峰的纵向差不小于作图区的三成():
    ys = [y for _, y in _dots(_vitals(SCAN), RED)]
    assert len(ys) == 4
    assert max(ys) - min(ys) >= 0.3 * (PLOT_BOTTOM - PLOT_TOP), ys   # 修前 4.0px（作图区 166px）
    assert min(ys) == ys[2]   # 39.5℃ 那一次画得最高


@needs_node
def test_有没有测脉搏_体温的纵坐标都不变():
    with_pulse = _dots(_vitals(SCAN), RED)
    without_pulse = _dots(_vitals([(at, t, None) for at, t, _ in SCAN]), RED)
    assert with_pulse == without_pulse   # 修前同一组体温纵向差 4.0px 对 11.3px


@needs_node
def test_体温刻度标在左_脉搏刻度标在右_37度与80次同高():
    svg = _vitals(SCAN)
    left, right = _ticks(svg, RED), _ticks(svg, TEAL)
    assert [text for _, _, text in left] == [str(v) for v in range(35, 43)]   # 修前没有体温刻度
    assert {x for x, _, _ in left} == {4}
    assert [text for _, _, text in right] == [str(v) for v in range(40, 181, 20)]
    width = int(re.search(r'<svg width="(\d+)"', svg).group(1))
    assert all(630 < x < width - 18 for x, _, _ in right), (right, width)   # 画在作图区右边、留得下三位数
    left_y = {text: y for _, y, text in left}
    right_y = {text: y for _, y, text in right}
    assert left_y["37"] == pytest.approx(right_y["80"]) and left_y["42"] == pytest.approx(right_y["180"])
    assert ">112</text>" not in svg   # 修前纵轴只标「112」「0」


@needs_node
def test_测得超出值域的按整格往外扩_点不画出作图区():
    svg = _vitals(SCAN[:3] + [("2026-10-09 20:00", 43.1, 35)])
    for color in (RED, TEAL):
        assert all(PLOT_TOP <= y <= PLOT_BOTTOM for _, y in _dots(svg, color)), color
    assert _ticks(svg, RED)[-1][2] == "44" and _ticks(svg, TEAL)[0][2] == "20"


@needs_node
def test_体温单横坐标照旧按测量时刻摆():
    assert [x for x, _ in _dots(_vitals(SCAN), RED)] == [36, 234, 432, 630]   # 作图区横向不变（P2-1336）


@needs_node
def test_驾驶舱与审计不传纵轴_输出与修前逐字节相同():
    assert _dashboard() == DASHBOARD_BEFORE
    assert _audit() == AUDIT_BEFORE


def test_体温单给体温与脉搏各传一根纵轴_其余调用方不传():
    vitals = _call(MGMT, "lineChart(vitals.map(")
    assert vitals.endswith('{ "体温": { min: 35, max: 42, step: 1, side: "left" },'
                           ' "脉搏": { min: 40, max: 180, step: 20, side: "right" } })')   # 修前不传，两条线共用纵轴
    assert _call(CORE, "lineChart(trendLabels") == "lineChart(trendLabels, trends.series, trendColors)"
    assert _call(MGMT, "lineChart(auditDaily.map(").endswith('["#0b6e6e", "#c0392b"])')
