"""「统一资源视图」表只画前 100 行、号源排在最前：手术间、检查资源、通用资源一行都不出（P2-1507，第四十四批扫描 AH3-3）。

`renderResources` 原先画 `catalog.items.slice(0, 100)`：接口（`resources.resource_catalog`）按号源、检查资源、手术间、血制品、
通用资源的次序拼、每类最多 500 行，号源最先——一个门诊每天 8 个时段 × 14 天就是 112 个号源，表里 100 行全是号源；计数卡
（P2-164 起取全量 by_kind）写着「手术间 2/2」「通用资源 1/1」，表里一行都没有，也没有截断提示。扫描实测：计数卡号源 112、
手术间 2、通用资源 1，表格画出的 100 行全是号源。

修法：每类各列前 20 行（合计不超过原先的 100 行），没列全的类别写明「共几个，列前几个」，计数卡照旧全量。选这一种而不是
按类别分页签：接口 docstring 定的是「五类资源一处看全」，每类同屏露面才对得上，也不添页签状态与点击交互。
这里把整个 `renderResources` 原样拿到 node 里跑（`panel` / `table` 取自 core.js，取数管道复用 `test_materials_cssd_page_reach`
的夹具，不另造一份），请求转给真接口，再拆画出来的表与计数卡。
"""
import re
import shutil
from collections import Counter
from datetime import timedelta
from pathlib import Path

import pytest

from conftest import business_today
from test_materials_cssd_page_reach import _run_page_fetch

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面的渲染")


def _core_function(name: str) -> str:
    found = re.search(rf"\nfunction {name}\(.*?\n}}\n", (STATIC / "core.js").read_text(encoding="utf-8"), re.S)
    assert found, f"core.js 里找不到 {name}"
    return found.group(0)


def _render_resources_source() -> str:
    """pages-clinical.js 里整个 `renderResources`（到下一个页面函数为止）；它画页面只用到 panel / table / esc / api / $。"""
    source = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    start = source.index("async function renderResources()")
    return source[start:source.index("\nasync function ", start + 1)]


def _render(client, headers) -> tuple[dict, list]:
    """在 node 里原样跑一遍 `renderResources()`（`$` 落到一组假元素上），回（统一资源视图表每类几行 / 截断提示 / 计数卡）。"""
    block = (
        "const elements = {};\n"
        "document.querySelector = (sel) => (elements[sel] ||= { dataset: {}, classList: { add() {}, remove() {} } });\n"
        + _core_function("panel") + _core_function("table") + _render_resources_source()
        + "\nawait renderResources();\n"
    )
    html, requested = _run_page_fetch(client, headers, block, 'document.querySelector("#page-body").innerHTML')
    view = html[html.index("<h3>统一资源视图</h3>"):]
    view = view[:view.index("</div>")]   # 这个面板里没有嵌套的 div：第一个 </div> 就是面板结尾
    table, note = view[:view.index("</table>")], view[view.index("</table>"):]
    return {
        "rows": Counter(re.findall(r"<tr><td>([^<]*)</td>", table)),
        "note": re.sub(r"<[^>]+>|\s+", "", note),
        "cards": dict(re.findall(r'<div class="label">([^<]*)</div><div class="value">([^<]*)</div>', html)),
    }, requested


def test_号源占满前100行时_手术间与通用资源仍在表里_号源写明截断_计数卡照旧全量(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21507 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    for name in ("P21507 1号手术间", "P21507 2号手术间"):
        assert client.post("/api/surgery/rooms", headers=admin, json={"org_id": org, "name": name}).status_code == 201
    room = client.post("/api/resources", headers=admin, json={
        "org_id": org, "resource_type": "meeting_room", "code": "P21507-MR", "name": "P21507 第一会议室"})
    assert room.status_code == 201, room.text
    assert client.post(f"/api/resources/{room.json()['id']}/publish", headers=admin).status_code == 200

    got, requested = _render(client, admin)   # 每类都没超过 20 个：全列、不提截断
    assert got["rows"] == {"手术间": 2, "会议室": 1}, requested
    assert got["note"] == "", got["note"]

    start = business_today() + timedelta(days=1)   # 一个门诊 × 每天 8 个时段 × 14 天 = 112 个号源
    batch = client.post("/api/appointments/slots/batch", headers=admin, json={
        "org_id": org, "date_from": start.isoformat(), "date_to": (start + timedelta(days=13)).isoformat(),
        "templates": [{"resource_type": "outpatient", "resource_name": "P21507 内科门诊",
                       "slot_time": f"{h:02d}:00-{h:02d}:30", "capacity": 10} for h in range(8, 16)]})
    assert (batch.status_code, batch.json()["created"]) == (201, 112), batch.text

    got, requested = _render(client, admin)
    # 修前 {"号源": 100}：两间手术间与会议室一行都没有
    assert got["rows"] == {"号源": 20, "手术间": 2, "会议室": 1}, requested
    # 修前不提截断；只点名没列全的那一类
    assert got["note"] == "每类最多列前20行，以下几类没列全：号源共112个，列前20个。完整清单请到各自的模块查询。", got["note"]
    assert got["cards"] == {"号源": "112/112", "手术间": "2/2", "通用资源": "1/1"}   # 计数卡照旧全量（P2-164）
