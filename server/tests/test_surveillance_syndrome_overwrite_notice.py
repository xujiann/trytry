"""同一天第二个人报症候群日报，悄悄盖掉第一个人报的数：后端回了 `overwritten: true`，页面把回执整个丢掉（P2-1432，
第四十二批扫描 AF2-1 的页面提示一半）。

同机构同症候群同日只有一条、重复上报按覆盖（模块口径 2）。实测（修前）：甲镇发热门诊报发热 8 例、阈值 6，进了多点预警；
同一天儿科医生再报 5 例，201、`overwritten: true`——8 被改成 5，预警消失。回执只说覆盖了、不说盖掉的是几例；页面的
「症候群日报」表单走 `postAction`，成功即整页重画、不读回执，那一行悄悄变成 5，`app/static/` 里没有一处读 `overwritten`。

修法：回执末尾只加一个 `previous_case_count`（覆盖时是原例数，没覆盖为 null），原值由 `upsert_unique` 在覆盖那一路、
同一事务里改写之前读出；页面改为直接调 `api()`，先重画、再提示「已覆盖 <日期> <机构> 的「<症候群>」原上报（原 N 例）」。
上报人列要迁移，另登（随 P2-41），本条不做。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.concurrency import upsert_unique
from app.database import SessionLocal
from app.models import SyndromeMonitor

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
B = "/api/surveillance"
DAY = "2026-09-15"


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P21432 甲镇卫生院", "org_type": "township", "level": "township"}).json()["id"]


def _report(client, admin, body):
    resp = client.post(f"{B}/syndromes", headers=admin, json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_首报为null_同日再报带回原例数_原有键与次序不动(client, admin, org):
    body = {"org_id": org, "syndrome": "fever", "case_count": 8, "threshold": 6, "record_date": DAY}
    first = _report(client, admin, body)
    assert list(first) == ["id", "org_id", "syndrome", "syndrome_name", "case_count", "threshold", "record_date",
                           "alert", "note", "overwritten", "previous_case_count"]   # 只在末尾加一个键
    assert (first["overwritten"], first["previous_case_count"], first["alert"]) == (False, None, True)
    second = _report(client, admin, {**body, "case_count": 5, "threshold": None})
    assert (second["id"], second["case_count"], second["threshold"], second["alert"]) == (first["id"], 5, 6, False)
    assert (second["overwritten"], second["previous_case_count"]) == (True, 8)   # 修前没有这个键：盖掉几例说不出来
    third = _report(client, admin, {**body, "case_count": 12, "threshold": None})
    assert (third["overwritten"], third["previous_case_count"]) == (True, 5)     # 原值是覆盖之前那一条的，不是首报的
    other_day = _report(client, admin, {**body, "record_date": "2026-09-16"})
    assert (other_day["overwritten"], other_day["previous_case_count"]) == (False, None)


def test_upsert_unique的previous_新建不动_覆盖时记下改写之前的原值(org):
    with SessionLocal() as db:
        keys = {"org_id": org, "syndrome": "rash", "record_date": DAY}
        previous: dict = {}
        record, overwritten = upsert_unique(db, SyndromeMonitor, keys=keys,
                                            values={"case_count": 3, "threshold": 2, "note": "首报"}, previous=previous)
        assert (overwritten, previous) == (False, {})
        record, overwritten = upsert_unique(db, SyndromeMonitor, keys=keys,
                                            values={"case_count": 1, "note": "补报"}, previous=previous)
        assert overwritten is True
        assert previous == {"case_count": 3, "note": "首报"}   # 只记 values 里的列
        assert (record.case_count, record.threshold, record.note) == (1, 2, "补报")
        _record, again = upsert_unique(db, SyndromeMonitor, keys=keys, values={"case_count": 4})   # 不给照旧
        assert again is True


# ---------- 页面：先重画、再提示覆盖 ----------


def _top_level(source: str, head: str, end: str = "\n}\n") -> str:
    start = source.index(head)
    return source[start:source.index(end, start) + len(end)]


#: `$()` 按选择器给一个记 textContent / innerHTML 的假元素；`api()` GET 按路径回给定数据、POST 依次回给定的回执；
#: `route()` 照真页面整页重画的效果把提示行换成一个新元素——提示写在重画之前就被冲掉（P2-1013）
_HARNESS = """
let els = {};
const calls = [];
let routed = 0;
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { textContent: "", innerHTML: "", className: "", listeners: {},
    classList: { add() {}, remove() {} }, addEventListener(type, fn) { this.listeners[type] = fn; } }); } };
globalThis.FormData = class { get() { return null; } };
const DATA = JSON.parse(process.argv[1]);
const receipts = [...DATA.receipts];
async function api(path, opts = {}) {
  calls.push([opts.method || "GET", path, opts.body ? JSON.parse(opts.body) : null]);
  if (opts.method === "POST") return receipts.shift();
  return DATA.get[path.split("?")[0]] ?? [];
}
async function route() { routed += 1; delete els["#syn-msg"]; }
function formJson() { return DATA.body; }
function postAction() { throw new Error("症候群日报不该再走 postAction"); }
"""


def _run_page(get: dict, receipts: list, body: dict) -> dict:
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    page = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    script = (_HARNESS + (STATIC / "shared.js").read_text(encoding="utf-8")
              + _top_level(core, "function table(") + _top_level(core, "function panel(")
              + _top_level(core, "function setMsg(") + _top_level(page, "async function renderSurveillance(")
              + "(async () => { await renderSurveillance();\n"
              "  const msgs = [];\n"
              "  for (const _ of DATA.receipts) {\n"
              "    await els['#syn-form'].onsubmit({ preventDefault() {}, target: {} });\n"
              "    const msg = document.querySelector('#syn-msg');\n"
              "    msgs.push([msg.textContent, msg.className, routed]);\n"
              "  }\n"
              "  process.stdout.write(JSON.stringify({ msgs, posts: calls.filter((c) => c[0] === 'POST') })); })();\n")
    out = subprocess.run(["node", "-e", script, json.dumps({"get": get, "receipts": receipts, "body": body},
                                                           ensure_ascii=False)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_页面_首报不提示_同日再报先重画再提示已覆盖原上报(client, admin):
    org_id = client.post("/api/organizations", headers=admin, json={
        "name": "P21432 乙镇卫生院", "org_type": "township", "level": "township"}).json()["id"]
    body = {"org_id": org_id, "syndrome": "diarrhea", "case_count": 8, "threshold": 6, "record_date": DAY}
    receipts = [_report(client, admin, body), _report(client, admin, {**body, "case_count": 5})]   # 真接口的回执
    get = {path: client.get(path, headers=admin).json() for path in (
        f"{B}/syndromes", f"{B}/pathogens", f"{B}/alerts", f"{B}/resources/readiness", "/api/organizations")}
    out = _run_page(get, receipts, body)
    assert [p[1] for p in out["posts"]] == [f"{B}/syndromes"] * 2
    assert out["posts"][0][2] == body
    assert out["msgs"][0] == ["", "", 1]   # 首报：照旧重画，不提示
    # 修前回执被 postAction 丢掉、什么都不说；提示写在重画之后（重画前写的会被冲掉）
    assert out["msgs"][1] == [f"已覆盖 {DAY} P21432 乙镇卫生院 的「腹泻」原上报（原 8 例）", "msg err", 2]


def test_页面_症候群表单读回执的overwritten_不再走postAction():
    page = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    body = _top_level(page, "async function renderSurveillance(")
    handler = body[body.index('$("#syn-form").onsubmit'):]
    handler = handler[:handler.index("\n  };\n")]
    assert "postAction(" not in handler   # 修前：postAction 成功即重画、不读回执
    assert handler.index("await route();") < handler.index("if (r.overwritten)") < handler.index('setMsg("#syn-msg"')
    assert "r.previous_case_count" in handler
