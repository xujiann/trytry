"""传染病两页看不出是哪家机构（P2-1630，第四十八批扫描 AL3-8）。

迟报清单（`pages-public.js` 的 `renderInfectiousDir`）表头没有机构列，`LateReportOut` 也不带机构名——迟报通报本就面向辖区
各报告单位，疾控看着清单不知道该通报哪家；病例列表（`pages-clinical.js` 的 `renderInfectious`）的机构列只印 `org_id`；
多点预警（`AlertOut`）只给机构数，看到「手足口病 5 例、2 家」点不进是哪两家。监测页同类问题已按「印机构名」修过
（P2-1436 / P2-1437 / P2-1467）。

修后出参只在末尾追加：`LateReportOut.org_name`、病例清单出参 `org_name`（只加在清单这一侧，`POST /cases` 的出参字节不变）、
`AlertOut.org_names`（按机构编号排定序）；页面三处印机构名，一律 `esc()`，迟报清单与病例列表取不到名称回显编号。
页面那几块把 `renderInfectiousDir` / `renderInfectious` 原样拿到 node 里跑。
"""
import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
B = "/api/infectious"
EAST = "P21630 东镇<中心>卫生院"
WEST = "P21630 西镇卫生院"
CODE = "P21630X"   # 目录外编码，自成一组，不和别的用例的病例混在一起
POST_KEYS = ["org_id", "disease_code", "disease_name", "onset_date", "id", "category"]   # 修前清单与报卡的出参键序

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")


def _escaped(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@pytest.fixture(scope="module")
def ctx(client, admin):
    from app import clock

    def org(name):
        r = client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": "township", "level": "township"})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    east, west = org(EAST), org(WEST)
    today = clock.today().isoformat()
    posted = []
    for org_id in (west, east, east):   # 编号小的东镇后报：预警的机构名按编号排、不按报告先后
        r = client.post(f"{B}/cases", headers=admin, json={
            "org_id": org_id, "disease_code": CODE, "disease_name": "P21630 聚集性发热", "onset_date": today})
        assert r.status_code == 201, r.text
        posted.append(r.json())
    tb = next(d for d in client.get(f"{B}/diseases", headers=admin).json() if d["report_hours"] == 24)
    late = client.post(f"{B}/cases", headers=admin, json={
        "org_id": east, "disease_code": tb["code"], "disease_name": tb["name"],
        "onset_date": (clock.today() - timedelta(days=3)).isoformat()})
    assert late.status_code == 201, late.text
    return {"east": east, "west": west, "posted": posted, "late_id": late.json()["id"],
            "cases": client.get(f"{B}/cases", headers=admin).json(),
            "alerts": client.get(f"{B}/alerts", headers=admin, params={"threshold": 3}).json(),
            "late": client.get(f"{B}/late-reports", headers=admin).json()}


# ---------- 出参：只在末尾追加 ----------

def test_报卡出参不变_清单在末尾追加机构名(ctx):
    for body in ctx["posted"]:
        assert list(body) == POST_KEYS, body   # POST /cases 与修前同一组键：机构名只加在清单这一侧
    mine = {r["id"]: r for r in ctx["cases"] if r["disease_code"] == CODE}
    assert len(mine) == 3
    for row in mine.values():
        assert list(row) == POST_KEYS + ["org_name"], row   # 修前没有 org_name
        assert row["org_name"] == {ctx["east"]: EAST, ctx["west"]: WEST}[row["org_id"]]


def test_预警在末尾追加机构名_按机构编号排(ctx):
    alert = next(a for a in ctx["alerts"] if a["disease_code"] == CODE)
    assert list(alert) == ["disease_code", "disease_name", "case_count", "org_count", "window_days", "severity",
                           "org_names"], alert
    assert (alert["case_count"], alert["org_count"]) == (3, 2)
    assert alert["org_names"] == [EAST, WEST]   # 东镇编号小，排前；修前只给机构数


def test_迟报清单在末尾追加报告机构名(ctx):
    row = next(r for r in ctx["late"] if r["case_id"] == ctx["late_id"])
    assert list(row) == ["case_id", "org_id", "disease_code", "disease_name", "category", "report_hours",
                         "onset_date", "reported_at", "days_late", "org_name"], row
    assert (row["org_id"], row["org_name"]) == (ctx["east"], EAST)


# ---------- 页面：三处印机构名，一律 esc() ----------

def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


_HARNESS = """
let els = {};
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { textContent: "", innerHTML: "", className: "", listeners: {}, dataset: {},
    classList: { add() {}, remove() {} }, addEventListener(type, fn) { this.listeners[type] = fn; } }); } };
globalThis.FormData = class { get() { return null; } };
const DATA = JSON.parse(process.argv[1]);
async function api(path) { return DATA.get[path.split("?")[0]] ?? []; }
async function route() {}
function currentRole() { return "admin"; }
function downloadCsv() {}
"""


def _render(page_file: str, fn: str, get: dict) -> str:
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    page = (STATIC / page_file).read_text(encoding="utf-8")
    script = (_HARNESS + (STATIC / "shared.js").read_text(encoding="utf-8")
              + _top_level(core, "function table(") + _top_level(core, "function panel(")
              + _top_level(core, "function setMsg(") + _top_level(page, f"async function {fn}(")
              + f"(async () => {{ await {fn}(); process.stdout.write(els['#page-body'].innerHTML); }})();\n")
    out = subprocess.run(["node", "-e", script, json.dumps({"get": get}, ensure_ascii=False)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


@needs_node
def test_迟报清单有报告机构列_印机构名(ctx):
    rows = [r for r in ctx["late"] if r["case_id"] == ctx["late_id"]]
    html = _render("pages-public.js", "renderInfectiousDir", {f"{B}/late-reports": rows, f"{B}/diseases": []})
    assert "<th>报告机构</th>" in html   # 修前表头没有机构列
    assert f"<tr><td>{ctx['late_id']}</td><td>{_escaped(EAST)}</td>" in html, html


@needs_node
def test_迟报清单取不到机构名回显编号(ctx):
    rows = [dict(r, org_name="") for r in ctx["late"] if r["case_id"] == ctx["late_id"]]
    html = _render("pages-public.js", "renderInfectiousDir", {f"{B}/late-reports": rows, f"{B}/diseases": []})
    assert f"<tr><td>{ctx['late_id']}</td><td>{ctx['east']}</td>" in html, html


@needs_node
def test_病例列表与预警印机构名(ctx):
    cases = [r for r in ctx["cases"] if r["disease_code"] == CODE]
    alerts = [a for a in ctx["alerts"] if a["disease_code"] == CODE]
    html = _render("pages-clinical.js", "renderInfectious", {f"{B}/cases": cases, f"{B}/alerts": alerts})
    for row in cases:
        name = {ctx["east"]: EAST, ctx["west"]: WEST}[row["org_id"]]
        assert f"<tr><td>{row['id']}</td><td>{_escaped(name)}</td>" in html, html   # 修前印编号
    assert "<th>报告机构</th>" in html
    assert f"<td>{_escaped(EAST)}、{_escaped(WEST)}</td>" in html, html   # 修前预警只有机构数


@needs_node
def test_病例列表取不到机构名回显编号(ctx):
    cases = [dict(r, org_name="") for r in ctx["cases"] if r["disease_code"] == CODE]
    html = _render("pages-clinical.js", "renderInfectious", {f"{B}/cases": cases, f"{B}/alerts": []})
    for row in cases:
        assert f"<tr><td>{row['id']}</td><td>{row['org_id']}</td>" in html, html
