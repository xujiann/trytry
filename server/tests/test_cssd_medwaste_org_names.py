"""消毒供应页的接收机构、响应批次，医废页的机构一律印名称而不是内部编号（P2-1445，第四十二批扫描 AF4-7）。

修前消毒供应页批次表「接收机构」列印 `dispatched_to_org_id`（同一函数里早有 `orgNames`）、申领台账「响应批次」列印
`batch_id`（同页已取到批次表）：召回或核对时看到的是「接收机构 2」「响应批次 2」（那一批的批号其实是 CSSD-62），得拿编号回
机构表、批次表去对。医废页根本不取机构清单，滞留预警、医废清单、点位台账的机构列，收集登记的产生点下拉，扫码追溯框，
印的都是「机构 3」。

修法（只改页面，接口不动）：接收机构按本页的 `orgNames` 映射、响应批次按本页的批次表映射出批号；医废页多取一份
`/api/organizations` 映射机构名；映射不到的回显编号，一律 `esc()`。
回归照 P2-1411 的 node 渲染法：两页原样拿到 node 里跑（夹具见 `tests/cssd_medwaste_page.py`）。
"""
import re
import shutil
from datetime import timedelta

import pytest

from conftest import business_today
from cssd_medwaste_page import responses, run

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")

#: 机构名、批号里夹一段标签：显示时必须转义
WEST = "P21445 西镇<b>卫生院</b>"
WEST_ESC = "P21445 西镇&lt;b&gt;卫生院&lt;/b&gt;"
BATCH = "P21445<i>01</i>"
BATCH_ESC = "P21445&lt;i&gt;01&lt;/i&gt;"

RENDER_STEPS = r"""
await renderCssd();
const cssd = pageHtml();
elements["#page-body"] = element();
await renderMedwaste();
const medwaste = pageHtml();
await elements["#trace-form"].onsubmit({ preventDefault() {}, target: { trace_code: ARGS.params.code } });
return { cssd, medwaste, trace: elements["#trace-box"].innerHTML };
"""


@pytest.fixture(scope="module")
def world(client, admin):
    """中心两批：一批发给西镇（之后以它响应西镇的申领），一批已灭菌待发；西镇另有一笔未响应的申领。
    西镇一个产生点、一间暂存间、一包五天前收的医废（进滞留预警）。"""
    orgs = {}
    for key, name, level, kind in (("center", "P21445 县人民医院（消毒供应中心）", "county", "lead_hospital"),
                                   ("west", WEST, "township", "township")):
        resp = client.post("/api/organizations", headers=admin, json={"name": name, "org_type": kind, "level": level})
        assert resp.status_code in (200, 201), resp.text
        orgs[key] = resp.json()["id"]
    batches = {}
    for batch_no, item, steps in ((BATCH, "产包", 2), ("P21445-02", "换药包", 1)):
        made = client.post("/api/cssd/batches", headers=admin, json={
            "batch_no": batch_no, "center_org_id": orgs["center"], "item_name": item, "quantity": 10})
        assert made.status_code == 201, made.text
        for step in range(steps):
            qs = f"?dispatched_to_org_id={orgs['west']}" if step == 1 else ""
            assert client.post(f"/api/cssd/batches/{made.json()['id']}/advance{qs}", headers=admin).status_code == 200
        batches[batch_no] = made.json()["id"]
    requests = {}
    for item in ("产包", "缝合包"):
        made = client.post("/api/cssd/requests", headers=admin, json={"org_id": orgs["west"], "item_name": item})
        assert made.status_code == 201, made.text
        requests[item] = made.json()["id"]
    done = client.post(f"/api/cssd/requests/{requests['产包']}/fulfill?batch_id={batches[BATCH]}", headers=admin)
    assert done.status_code == 200, done.text
    locations = {}
    for name, kind in (("P21445 门诊", "source"), ("P21445 暂存间", "storage")):
        loc = client.post("/api/medwaste/locations", headers=admin, json={
            "org_id": orgs["west"], "name": name, "location_type": kind})
        assert loc.status_code == 201, loc.text
        locations[name] = loc.json()["id"]
    waste = client.post("/api/medwaste", headers=admin, json={
        "org_id": orgs["west"], "waste_type": "sharp", "weight_kg": 1,
        "collected_date": (business_today() - timedelta(days=5)).isoformat(),
        "source_location_id": locations["P21445 门诊"]})
    assert waste.status_code == 201, waste.text
    code = waste.json()["trace_code"]
    pages = responses(client, admin, "renderCssd", "drawCssdCosts", "renderMedwaste")
    traced = client.get(f"/api/medwaste/trace/{code}", headers=admin)
    assert traced.status_code == 200, traced.text
    pages[f"/api/medwaste/trace/{code}"] = traced.json()
    return {"orgs": orgs, "batches": batches, "requests": requests, "locations": locations,
            "waste": waste.json()["id"], "code": code, "responses": pages}


def _tables(html: str) -> dict:
    """`{表头元组: [[单元格 innerHTML, …], …]}`：`table()` 画的每张表。"""
    out = {}
    for head, body in re.findall(r"<table><thead><tr>(.*?)</tr></thead><tbody>(.*?)</tbody></table>", html, re.S):
        cols = tuple(re.findall(r"<th>(.*?)</th>", head))
        rows = [[cell.strip() for cell in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
                for row in re.findall(r"<tr>(.*?)</tr>", body, re.S)]
        out[cols] = rows
    return out


def _column(table: list[list[str]], key_col: int, col: int) -> dict:
    return {row[key_col]: row[col] for row in table}


def _render(world, drop_org=None, drop_batch=None):
    pages = {path: body for path, body in world["responses"].items()}
    if drop_org is not None:   # 机构清单里没有这家：照旧回显编号
        pages["/api/organizations"] = [o for o in pages["/api/organizations"] if o["id"] != drop_org]
    if drop_batch is not None:   # 本页批次表里没有这批（更早已回收、挤出了最新 200 个）：照旧回显编号
        for path in ("/api/cssd/batches", "/api/cssd/batches?status=sterile", "/api/cssd/batches?status=dispatched"):
            pages[path] = [b for b in pages[path] if b["id"] != drop_batch]
    got = run(pages, "admin", RENDER_STEPS, {"code": world["code"]})
    return _tables(got["cssd"]), _tables(got["medwaste"]), got["medwaste"], got["trace"]


BATCH_COLS = ("ID", "批次号", "器械", "数量", "接收机构", "状态", "操作")
REQUEST_COLS = ("ID", "申领机构", "物品", "数量", "状态", "响应批次", "操作")
ALERT_COLS = ("ID", "机构", "追溯码", "类别", "重量", "收集日期", "暂存点", "超期天数", "状态", "操作")
WASTE_COLS = ("ID", "机构", "追溯码", "类别", "重量", "收集日期", "转运人", "状态", "操作")
LOCATION_COLS = ("ID", "机构", "名称", "类型", "负责人", "状态", "操作")


def test_消毒供应页_接收机构印机构名_响应批次印批号(world):
    cssd, _, _, _ = _render(world)
    batches, requests = world["batches"], world["requests"]
    # 修前：发给西镇的那批印西镇的机构编号，响应批次印批次编号
    assert _column(cssd[BATCH_COLS], 0, 4) == {str(batches[BATCH]): WEST_ESC, str(batches["P21445-02"]): "—"}
    assert _column(cssd[REQUEST_COLS], 0, 5) == {str(requests["产包"]): BATCH_ESC, str(requests["缝合包"]): "—"}


def test_消毒供应页_映射不到的回显编号(world):
    west, batch = world["orgs"]["west"], world["batches"][BATCH]
    cssd, _, _, _ = _render(world, drop_org=west, drop_batch=batch)
    assert _column(cssd[BATCH_COLS], 0, 4) == {str(world["batches"]["P21445-02"]): "—"}   # 那批不在批次表里了
    assert _column(cssd[REQUEST_COLS], 0, 5)[str(world["requests"]["产包"])] == str(batch)
    cssd, _, _, _ = _render(world, drop_org=west)
    assert _column(cssd[BATCH_COLS], 0, 4)[str(batch)] == str(west)


def test_医废页_预警清单点位台账产生点与追溯框都印机构名(world):
    _, med, html, trace = _render(world)
    waste, locations = str(world["waste"]), world["locations"]
    # 修前这五处印的都是西镇的机构编号
    assert _column(med[ALERT_COLS], 0, 1) == {waste: WEST_ESC}
    assert _column(med[WASTE_COLS], 0, 1) == {waste: WEST_ESC}
    assert _column(med[LOCATION_COLS], 0, 1) == {str(i): WEST_ESC for i in locations.values()}
    assert f'<option value="{locations["P21445 门诊"]}">P21445 门诊（{WEST_ESC}）</option>' in html
    assert f"{world['code']} · 损伤性 · 1kg · 机构 {WEST_ESC}</p>" in trace


def test_医废页_映射不到的回显编号(world):
    west = world["orgs"]["west"]
    _, med, html, trace = _render(world, drop_org=west)
    assert _column(med[WASTE_COLS], 0, 1) == {str(world["waste"]): str(west)}
    assert _column(med[ALERT_COLS], 0, 1) == {str(world["waste"]): str(west)}
    assert f"机构 {west}</p>" in trace
