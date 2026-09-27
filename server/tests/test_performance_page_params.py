"""绩效考核页选不了周期与计分参数，手册却说可以调（P2-474）。

`GET /api/performance/orgs` 早就收 `period`（YYYY / YYYY-MM）、`group_id`、`volume_cap`、`include_auto_passed`，
页面（与驾驶舱）一律不带参数——永远是当年、全县、默认口径。1 月 1 日一过，基金分配冻结用的上一年排名
（`fund` 按 `period=pool.year` 取）在页面上就看不到了；用户手册「量类封顶次数与处方合格口径可按考核要求调整参数」
在界面上无从做起。

修法：「机构评分排名」一块补四个参数（只留在内存里、不进存储）；参数写错时说清楚、回到缺省口径重算，不把整页掀掉。
"""
from pathlib import Path

CORE = (Path(__file__).resolve().parents[1] / "app" / "static" / "core.js").read_text(encoding="utf-8")


def _render_performance() -> str:
    start = CORE.index("async function renderPerformance()")
    return CORE[start:CORE.index("\nasync function ", start + 1)]


def test_四个计分参数都有入口_按参数取数():
    body = _render_performance()
    form = body[body.index('<form class="inline" id="perf-filter">'):]
    form = form[:form.index("</form>")]
    for name in ("period", "group_id", "volume_cap", "include_auto_passed"):
        assert f'name="{name}"' in form, name   # 修前一个都没有
    assert "new URLSearchParams(Object.entries(PERF_FILTER)" in body
    assert "api(`/api/performance/orgs${perfQuery.toString() ? `?${perfQuery}` : \"\"}`)" in body
    assert 'const PERF_FILTER = { period: "", group_id: "", volume_cap: "", include_auto_passed: "" };' in CORE


def test_参数写错回到缺省口径_不掀整页():
    body = _render_performance()
    assert ".catch((err) => ({ error: err.message }))" in body
    assert 'Object.keys(PERF_FILTER).forEach((k) => { PERF_FILTER[k] = ""; });' in body
    assert "（已回到缺省口径）" in body


def test_接口端_四个参数照收(client, admin):
    base = client.get("/api/performance/orgs", headers=admin)
    assert base.status_code == 200, base.text
    year = base.json()["period"]
    for params in ({"period": str(int(year) - 1)}, {"period": f"{year}-01"}, {"volume_cap": 1},
                   {"include_auto_passed": "false"}):
        resp = client.get("/api/performance/orgs", headers=admin, params=params)
        assert resp.status_code == 200, (params, resp.text)
    assert client.get("/api/performance/orgs", headers=admin, params={"period": f"{year}-13"}).status_code == 422
