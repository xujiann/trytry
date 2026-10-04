"""多点预警的症候群表按绝对例数排序，机构列只印编号（P2-1437，第四十二批扫描 AF2-11）。

模块口径 1：「县医院发热门诊日均 50 人不算异常，村卫生室 5 人就该看一眼」——阈值是各机构自设的，不同机构的绝对例数没法直接比。
`multi_point_alerts` 却按例数从大到小排。实测（修前）：县医院 52 例（阈值 50，104%）排在村卫生室 15 例（阈值 5，300%）前面，
预警一多，村卫生室的突增就沉到表底。页面上多点预警表、症候群日报表、病原日报表的机构列印的是 `org_id`，同一页的保障情况表
印的却是机构名。

修法：症候群预警按「例数 ÷ 阈值」从高到低排（分数比，不经浮点），比值相同再按例数降序、机构 id，最后按行 id 倒序（取数没有
ORDER BY，末位键唯一才是全序）；页面三张表的机构列按 `/api/organizations` 映射印机构名，映射不到回显编号，一律 `esc()`。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
B = "/api/surveillance"


def _org(client, admin, name, level="township"):
    return client.post("/api/organizations", headers=admin, json={
        "name": name, "org_type": "lead_hospital" if level == "county" else level, "level": level}).json()["id"]


def _syndrome(client, admin, org, case_count, threshold, day, syndrome="fever"):
    resp = client.post(f"{B}/syndromes", headers=admin, json={
        "org_id": org, "syndrome": syndrome, "case_count": case_count, "threshold": threshold, "record_date": day})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _alerts(client, admin, day):
    return client.get(f"{B}/alerts", headers=admin, params={"today": day, "days": 1}).json()["syndrome_alerts"]


@pytest.fixture(scope="module")
def world(client, admin):
    county = _org(client, admin, "P21437 县人民医院<总院>", "county")
    village = _org(client, admin, "P21437 丁村卫生室", "village")
    _syndrome(client, admin, county, 52, 50, "2026-09-20")
    _syndrome(client, admin, village, 15, 5, "2026-09-20")
    pathogen = client.post(f"{B}/pathogens", headers=admin, json={
        "org_id": village, "pathogen_name": "甲型流感", "tested_count": 20, "positive_count": 8, "record_date": "2026-09-20"})
    assert pathogen.status_code == 201, pathogen.text
    return {"county": county, "village": village}


def test_按例数除以阈值从高到低排_村卫生室15比5排在县医院52比50前面(client, admin, world):
    order = [(a["org_id"], a["case_count"], a["threshold"]) for a in _alerts(client, admin, "2026-09-20")]
    assert order == [(world["village"], 15, 5), (world["county"], 52, 50)]   # 修前 [(县医院, 52, 50), (村卫生室, 15, 5)]


def test_比值相同按例数降序_再按机构id_同一机构再按行id倒序(client, admin):
    day = "2026-09-21"
    a, b, c = (_org(client, admin, f"P21437 并列{n}") for n in "甲乙丙")
    a_fever = _syndrome(client, admin, a, 10, 5, day)
    b_fever = _syndrome(client, admin, b, 6, 3, day)                       # 比值同为 2，例数少，排在后面
    c_fever = _syndrome(client, admin, c, 10, 5, day)                      # 比值、例数都与甲相同：机构 id 大的在后
    a_diarrhea = _syndrome(client, admin, a, 10, 5, day, "diarrhea")      # 同一机构同比值同例数：后报的（行 id 大）在前
    assert [x["id"] for x in _alerts(client, admin, day)] == [a_diarrhea, a_fever, c_fever, b_fever]


# ---------- 页面：三张表的机构列印机构名 ----------


def _top_level(source: str, head: str, end: str = "\n}\n") -> str:
    start = source.index(head)
    return source[start:source.index(end, start) + len(end)]


_HARNESS = """
let els = {};
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { textContent: "", innerHTML: "", className: "", listeners: {},
    classList: { add() {}, remove() {} }, addEventListener(type, fn) { this.listeners[type] = fn; } }); } };
globalThis.FormData = class { get() { return null; } };
const DATA = JSON.parse(process.argv[1]);
async function api(path) { return DATA.get[path.split("?")[0]] ?? []; }
async function route() {}
function formJson() { return {}; }
function postAction() {}
"""


def _render(get: dict) -> str:
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    page = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    script = (_HARNESS + (STATIC / "shared.js").read_text(encoding="utf-8")
              + _top_level(core, "function table(") + _top_level(core, "function panel(")
              + _top_level(core, "function setMsg(") + _top_level(page, "async function renderSurveillance(")
              + "(async () => { await renderSurveillance(); process.stdout.write(els['#page-body'].innerHTML); })();\n")
    out = subprocess.run(["node", "-e", script, json.dumps({"get": get}, ensure_ascii=False)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


def _section(html: str, start: str, end: str) -> str:
    return html[html.index(start):html.index(end, html.index(start))]


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_页面_多点预警与两张日报表的机构列印机构名_映射不到回显编号(client, admin, world):
    get = {f"{B}/alerts": client.get(f"{B}/alerts", headers=admin, params={"today": "2026-09-20", "days": 1}).json(),
           **{path: client.get(path, headers=admin, params=params).json() for path, params in (
               (f"{B}/syndromes", {"start_date": "2026-09-20", "end_date": "2026-09-20"}),
               (f"{B}/pathogens", {}), (f"{B}/resources/readiness", {}), ("/api/organizations", {}))}}
    html = _render(get)
    county, village = "P21437 县人民医院&lt;总院&gt;", "P21437 丁村卫生室"   # 机构名一律转义
    alerts = _section(html, "<h4>症候群达到阈值</h4>", "<h4>病原阳性率抬头</h4>")
    # 修前机构列印编号；排序与接口一致：村卫生室（300%）在前
    assert alerts.index(f"<tr><td>{village}</td><td>发热</td><td><b>15</b></td>") \
        < alerts.index(f"<tr><td>{county}</td><td>发热</td><td><b>52</b></td>")
    assert f"<tr><td>{village}</td><td>甲型流感</td>" in _section(html, "<h4>病原阳性率抬头</h4>", "症候群日报")
    daily = _section(html, 'id="syn-form"', "病原监测")
    assert f"<tr><td>{county}</td><td>发热</td><td>52</td>" in daily
    assert f"<tr><td>{village}</td><td>发热</td><td>15</td>" in daily
    assert f"<tr><td>{village}</td><td>甲型流感</td>" in _section(html, 'id="pat-form"', "应急资源保障")

    get["/api/organizations"] = [o for o in get["/api/organizations"] if o["id"] != world["village"]]
    daily = _section(_render(get), 'id="syn-form"', "病原监测")
    assert f"<tr><td>{world['village']}</td><td>发热</td><td>15</td>" in daily   # 映射不到：回显编号
