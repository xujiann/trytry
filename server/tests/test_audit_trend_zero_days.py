"""运维监控页的审计按日趋势把没有写操作的日子整天丢掉（P2-417）。

`GET /api/audit/stats` 的 `daily` 只回有写操作的日子（按 UTC 日分桶；这份出参有特征化用例按字节钉着，不动）。
页面原样拿它画折线：国庆放假一周没人改档，折线把节前、节后两天挨着画，趋势上看不出停摆，「近 30 天」的横轴也
不是 30 天。修后页面按统计窗口逐日补 0 再画；日期同样按 UTC 拼（与后端分桶一致，不是「今天」，不走 localToday，
也不拿 toISOString 截——那是 P2-228 闸门禁止的写法）。
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


def _render_monitor() -> str:
    source = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    start = source.index("async function renderMonitor()")
    return source[start:source.index("\nasync function ", start + 1)]


def test_按统计窗口逐日补零再画():
    body = _render_monitor()
    assert "Array.from({ length: audit.days + 1 }" in body            # 窗口内每一天
    assert "auditByDay[day] || { date: day, ok: 0, failed: 0 }" in body   # 缺的日子补 0
    chart = body[body.index("lineChart("):]
    chart = chart[:chart.index('["#0b6e6e", "#c0392b"]')]
    assert "auditDaily.map((d) => d.ok)" in chart and "audit.daily.map" not in chart   # 修前直接画 audit.daily


def test_补零按UTC日拼日期_与后端分桶一致():
    body = _render_monitor()
    assert "getUTCFullYear()" in body and "getUTCDate()" in body
    assert not re.search(r"toISOString\(\)\s*\.\s*slice", body)
