"""决策指标页存下的期间被后端拒了能自己回落（P2-586，第十二批「表单提示 vs 后端校验」扫描 Z3-1）。

页面的期间切换框是自由文本，原先原样存进 localStorage：存下 `2026`（绩效考核页的「周期 YYYY 或 YYYY-MM」收它）、
`2026/09`、`2026-09 `（粘贴带空格）之后，效率指标 422，整页那个 Promise.all 抛出，`route()` 把整页换成一行报错——
切换框画在它之后，每次进来都是这样，只能清站点数据。会计、成本页早按 P1-62 修过，这一页漏了。
端到端取证见 `e2e/test_flows.py::test_决策指标页存下的期间被拒时回落本月_切换框先验再存`；这里钉住源码里的两道。
"""
from pathlib import Path

SOURCE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-mgmt.js").read_text(encoding="utf-8")


def _render_analytics() -> str:
    start = SOURCE.index("async function renderAnalytics()")
    return SOURCE[start:SOURCE.index("\n}\n", start)]


def test_存下的期间被拒_只对422回落本月并清掉坏值():
    body = _render_analytics()
    fallback = body[body.index("} catch (err) {"):]
    fallback = fallback[:fallback.index("loaded = await load(period);")]
    assert 'if (err.status !== 422 || period === thisMonth) throw err;' in fallback   # 别的失败照常抛
    assert 'localStorage.removeItem("medplat_ana_period");' in fallback
    assert "period = thisMonth;" in fallback


def test_切换时先让后端判_合法才记住():
    body = _render_analytics()
    handler = body[body.index('$("#ana-period").onsubmit'):]
    handler = handler[:handler.index("route();")]
    check = handler.index("await api(`/api/analytics/efficiency?period=${encodeURIComponent(value)}`)")
    store = handler.index('localStorage.setItem("medplat_ana_period", value)')
    assert check < store and "return; }" in handler[check:store]   # 被拒就不存
    assert 'setMsg("#ana-period-msg"' in handler and 'id="ana-period-msg"' in body
