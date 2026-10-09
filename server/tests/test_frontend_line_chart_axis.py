"""共用折线图把每个横轴标签都按「YYYY-MM」切掉前两位，点按条目等距排（P2-1336，第三十九批「趋势、环比与同比」扫描 AC1-4）。

`core.js` 的 `lineChart` 注释写「月份标签来自后端、格式固定（YYYY-MM）」，代码一律 `String(mo).slice(2)`。三处调用方
只有驾驶舱「近6月业务量趋势」传的是 YYYY-MM；住院文书的体温单传 `measured_at.slice(5, 10)`（MM-DD），运行监控的审计
按日趋势传 `d.date.slice(5)`（MM-DD）——修前横轴只剩「-30」「-03」：审计 31 天的首尾是同一个「-03」，体温单 09-30
与 10-30 的点都标「-30」。另外横坐标按下标等距：体温单 09-30 08:00、14:00、10-01 08:00、10-30 08:00 四个点画在
36 / 234 / 432 / 630，6 小时、18 小时、29 天三个间隔一样宽——一天测 6 次与之后每天测 1 次占的宽度相同，热型曲线
被压变形，中间隔了几天没测也看不出。

修法：组件原样画标签，驾驶舱自己传 YY-MM；`lineChart` 可选地收每个点的时刻（毫秒数），给了按时间比例摆点，不给的
调用方（驾驶舱、审计）照旧按下标等距、坐标逐值不变；体温单把测量时刻传进去。标签与上一个已画的相同（同一天的几次
测量）不重画、离得太近会叠上的跳过（MM-DD 实测约 27px 宽，审计 31 天按下标的间距只有 19.8px）。

跑法照扫描的复现脚本：在 node 里原样取 `shared.js` 的 `esc`、`core.js` 的 `lineChart`、`pages-mgmt.js` 的
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

#: 横轴标签所在的那一行（`h - 6`，h=200）
LABEL = re.compile(r'<text x="([^"]+)" y="194"[^>]*>([^<]*)</text>')

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 可执行这段前端函数")


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


def _labels(svg: str) -> list[tuple[float, str]]:
    return [(float(x), text) for x, text in LABEL.findall(svg)]


def _dots(svg: str, color: str) -> list[float]:
    return [float(x) for x in re.findall(rf'<circle cx="([^"]+)" cy="[^"]+" r="2.5" fill="{color}"/>', svg)]


def _dashboard(months: list[str]) -> str:
    labels_line = re.search(r"^\s*const trendLabels = .+;$", CORE, re.M).group(0)
    context = (f"const trends = {json.dumps({'months': months, 'series': {'encounters': [1] * len(months)}})};\n"
               f'const trendColors = ["#0b6e6e"];\n{labels_line}')
    return _render(context, _call(CORE, "lineChart(trendLabels"))


def _vitals(rows: list[tuple[str, float]]) -> str:
    vitals = [{"measured_at": at, "temperature": t, "pulse": None} for at, t in rows]
    return _render(f"const vitals = {json.dumps(vitals)};", _call(MGMT, "lineChart(vitals.map("))


def _audit(days: list[str]) -> str:
    daily = [{"date": d, "ok": i, "failed": 0} for i, d in enumerate(days)]
    return _render(f"const auditDaily = {json.dumps(daily)};", _call(MGMT, "lineChart(auditDaily.map("))


# ---------------------------------------------------------------- 源码形状


def test_组件不再替调用方切标签():
    body = _function(CORE, "lineChart")
    code = "\n".join(re.sub(r"//.*$", "", line) for line in body.splitlines())   # 注释里会提到修前的写法
    assert "slice(" not in code   # 修前 esc(String(mo).slice(2))
    assert "${esc(text)}</text>" in body
    # 第五个参数 `axes`（各序列自己的纵轴）是 P2-1766 加的，见 test_vitals_chart_axes.py
    assert body.startswith("function lineChart(labels, series, colors, times = null, axes = null) {")


def test_三处调用方各自传什么():
    assert "const trendLabels = trends.months.map((mo) => mo.slice(2));" in CORE   # 驾驶舱自己缩成 YY-MM
    assert _call(CORE, "lineChart(trendLabels") == "lineChart(trendLabels, trends.series, trendColors)"
    vitals = _call(MGMT, "lineChart(vitals.map(")
    assert vitals.startswith("lineChart(vitals.map((v) => v.measured_at.slice(5, 10)),")
    # 修前不传时刻，按条目等距；时刻之后还跟着体温、脉搏各自的纵轴（P2-1766，见 test_vitals_chart_axes.py）
    assert "vitals.map((v) => vitalTimeMs(v.measured_at))," in vitals
    audit = _call(MGMT, "lineChart(auditDaily.map(")
    assert audit.startswith("lineChart(auditDaily.map((d) => d.date.slice(5)),")
    assert "vitalTimeMs" not in audit   # 按日补零的序列本来就等间隔，照旧按下标


# ---------------------------------------------------------------- 原样跑


@needs_node
def test_驾驶舱_标签与坐标都不变():
    svg = _dashboard(["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"])
    assert [text for _, text in _labels(svg)] == ["26-05", "26-06", "26-07", "26-08", "26-09", "26-10"]
    assert _dots(svg, "#0b6e6e") == [36 + i * 594 / 5 for i in range(6)]   # 按下标等距，与修前逐值相同


@needs_node
def test_审计按日_首尾标签带月份_不叠在一起():
    days = [f"2026-09-{d:02d}" for d in range(3, 31)] + ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert len(days) == 31   # 页面取 days=30，补零后 31 天
    svg = _audit(days)
    labels = _labels(svg)
    assert labels[0][1] == "09-03" and labels[-1][1] == "10-03"   # 修前首尾都是「-03」
    assert all(re.fullmatch(r"[0-9]{2}-[0-9]{2}", text) for _, text in labels), labels
    assert all(b[0] - a[0] >= 35 for a, b in zip(labels, labels[1:])), labels   # 「09-03」约 27px 宽，挨着画会叠上
    assert _dots(svg, "#0b6e6e") == [36 + i * 594 / 30 for i in range(31)]   # 坐标照旧按下标等距


@needs_node
def test_体温单_跨月两个点标签不同_横坐标与时间间隔成比例():
    rows = [("2026-09-30 08:00", 38.6), ("2026-09-30 14:00", 39.1), ("2026-10-01 08:00", 37.2),
            ("2026-10-30 08:00", 36.8)]
    svg = _vitals(rows)
    xs = _dots(svg, "#c0392b")
    labels = dict(_labels(svg))
    assert labels[xs[0]] == "09-30" and labels[xs[-1]] == "10-30"   # 修前两个都标「-30」
    hours = [0, 6, 24, 720]   # 距第一次测量的小时数
    assert xs[0] == 36 and xs[-1] == 630
    for x, hour in zip(xs, hours):
        assert x - 36 == pytest.approx(594 * hour / 720)   # 修前 36 / 234 / 432 / 630，三个间隔一样宽


@needs_node
def test_体温单_一天测6次与之后每天1次_宽度按时间分():
    rows = [(f"2026-09-30 {h:02d}:00", 38.0) for h in (2, 6, 10, 14, 18, 22)]
    rows += [(f"2026-10-0{d} 08:00", 37.0) for d in (1, 2, 3, 4)]
    svg = _vitals(rows)
    xs = _dots(svg, "#c0392b")
    # 头一天 02:00 → 22:00 是 20 小时，之后 10-01 08:00 → 10-04 08:00 是 72 小时；修前按条目是 5 : 3
    assert (xs[5] - xs[0]) / (xs[9] - xs[6]) == pytest.approx(20 / 72)
    texts = [text for _, text in _labels(svg)]
    assert texts[0] == "09-30" and texts.count("09-30") == 1   # 同一天的几次测量只标一次
    assert texts[-1] == "10-04"


@needs_node
def test_体温单_时刻形状不对就整张回落成按条目等距():
    rows = [("2026-09-30 08:00", 38.6), ("8:00", 39.1), ("2026-10-01 08:00", 37.2)]   # P1-100 之前的存量自由文本
    assert _dots(_vitals(rows), "#c0392b") == [36, 36 + 594 / 2, 630]


@needs_node
def test_测量时刻的两种分隔符算同一时刻():
    script = _function(MGMT, "vitalTimeMs") + "\nconsole.log(JSON.stringify(process.argv.slice(1).map(vitalTimeMs)));"
    args = ["2026-09-30 08:00", "2026-09-30T08:00", "2026-09-30", "2026-09-30 08:00:30", "2026-10-1 8:00", ""]
    out = json.loads(subprocess.run(["node", "-e", script, *args], capture_output=True, text=True, check=True,
                                    timeout=60).stdout)
    assert out[0] == out[1] == out[2] + 8 * 3600 * 1000 == out[3] - 30 * 1000
    assert out[4:] == [None, None]   # NaN 序列化成 null：形状不对
