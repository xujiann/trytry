"""监测页「资源台账」的机构列印的是机构编号（P2-1467，P2-1437 机构列印名称的同类跟进）。

P2-1437 把同一页多点预警、症候群日报、病原日报三张表的机构列换成了机构名（同页保障情况表早就印名称），资源台账
（`renderSurveillance` 里的 `drawResources`）还是 `${r.org_id}`——补货、调下限、换效期都在这张台账上逐条改，看到的却是
「3」「7」，得拿编号回机构表去对。修后照另外四张表：按本页已取的机构清单映射出机构名，映射不到（页面打开之后才建的机构）
回显编号，一律 `esc()`。这里把 `renderSurveillance` 原样拿到 node 里跑，取台账那一块的 HTML。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
B = "/api/surveillance"
ORG_NAME = "P21467 西镇<中心>卫生院"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


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


def _ledger(get: dict) -> str:
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    page = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    script = (_HARNESS + (STATIC / "shared.js").read_text(encoding="utf-8")
              + _top_level(core, "function table(") + _top_level(core, "function panel(")
              + _top_level(core, "function setMsg(") + _top_level(page, "async function renderSurveillance(")
              + "(async () => { await renderSurveillance(); process.stdout.write(els['#res-list'].innerHTML); })();\n")
    out = subprocess.run(["node", "-e", script, json.dumps({"get": get}, ensure_ascii=False)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return out.stdout


@pytest.fixture(scope="module")
def get(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": ORG_NAME, "org_type": "township", "level": "township"}).json()["id"]
    created = client.post(f"{B}/resources", headers=admin, json={
        "org_id": org, "resource_type": "material", "name": "P21467 医用口罩", "quantity": 20, "unit": "箱",
        "min_quantity": 50})
    assert created.status_code == 201, created.text
    rows = [r for r in client.get(f"{B}/resources", headers=admin).json() if r["org_id"] == org]
    assert len(rows) == 1, rows
    # 页面先画多点预警与保障情况（要 window / caliber），这两块照取真接口；症候群、病原日报给空表即可
    return {"org": org, f"{B}/resources": rows, f"{B}/alerts": client.get(f"{B}/alerts", headers=admin).json(),
            f"{B}/resources/readiness": client.get(f"{B}/resources/readiness", headers=admin).json(),
            "/api/organizations": client.get("/api/organizations", headers=admin).json()}


def test_资源台账的机构列印机构名_一律转义(get):
    html = _ledger({k: v for k, v in get.items() if k != "org"})
    assert f"<tr><td>{ORG_NAME.replace('<', '&lt;').replace('>', '&gt;')}</td><td>应急物资</td><td>P21467 医用口罩</td>" in html, html
    assert f"<tr><td>{get['org']}</td>" not in html   # 修前印编号


def test_映射不到的机构回显编号(get):
    data = {k: v for k, v in get.items() if k != "org"}
    data["/api/organizations"] = [o for o in data["/api/organizations"] if o["id"] != get["org"]]
    assert f"<tr><td>{get['org']}</td><td>应急物资</td>" in _ledger(data)
