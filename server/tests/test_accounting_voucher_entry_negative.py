"""凭证录入页对负数前后不一致：实时合计把负数算进去，提交时却把只填负数的分录行悄悄丢掉（P2-1442，第四十二批扫描 AF3-12）。

修前 `renderAccounting` 的分录金额框没有 `min`；`refreshTotal` 逐行累加（负数照加），提交那一段另取一遍行、只留「借或贷大于 0」
的——财务做红字更正录 `借1000 / 贷1000 / 借−200 / 贷−200`，屏幕上是「800 / 800 ✓ 平衡」，存下来却是 1000 / 1000；全是负数时
行被丢光，后端只报「entries 至少 2 项」，不说是因为不收负数（后端分录金额 `ge=0`，冲销走作废）。

修法：金额框加 `min="0"`；实时合计与提交用同一个取行函数 `entryRows()`（借贷都没填的空行不算）；有负数时合计那一行直接写明、
提交前就提示「金额不能为负，红字更正请走作废后重录」，不再悄悄丢行。后端 `ge=0` 不动。这里把 `renderAccounting` 原样拿到 node
里跑（`api` 经管道转给真接口），往分录行里填数、触发合计与提交，看屏幕上的合计与交出去的是不是同一组行。
"""
import json
import os
import re
import shutil
import subprocess

import pytest

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
PERIOD = "2025-07"
NEGATIVE = "金额不能为负，红字更正请走作废后重录"


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def _accounting_section() -> str:
    source = _read("pages-mgmt.js")
    start = source.index("/* ---------------- 会计核算 ---------------- */")
    return source[start:source.index("/* ---------------- 成本核算 ---------------- */", start)]


#: 页面取数换成桩：`$()` / `createElement` 给记 innerHTML、按选择器挂子元素的假元素；新加的分录行收进 `rows`；
#: `formJson()` 回凭证头；`postAction()` 只记下交了什么；`api()` 经管道转给真接口
_HARNESS = r"""
const els = {};
const rows = [];
const posted = [];
const STORE = JSON.parse(process.argv[1]);
const mk = () => ({ textContent: "", innerHTML: "", value: "", className: "", style: {}, kids: {}, dataset: {},
  classList: { add() {}, remove() {} },
  querySelector(sel) { return (this.kids[sel] ||= mk()); },
  appendChild(child) { rows.push(child); return child; } });
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= mk()); },
  querySelectorAll(sel) { return sel === ".entry-row" ? rows : []; },
  createElement() { return mk(); } };
globalThis.localStorage = { getItem: (k) => (k in STORE ? STORE[k] : null), setItem() {}, removeItem() {} };
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
async function api(path) {
  process.stdout.write(JSON.stringify({ get: path }) + "\n");
  return JSON.parse((await lines.next()).value);
}
function currentRole() { return "admin"; }
function route() {}
function formJson() { return STORE.head; }
function postAction(path, body) { posted.push([path, body]); }
async function spdModal() { return null; }
/** 往分录行里填数（[借, 贷]，空串即没填），触发一次输入（合计重算），再点一次「保存凭证」，记下屏幕与交出去的东西。 */
async function fill(values) {
  while (rows.length < values.length) els["#add-entry"].onclick();
  values.forEach(([debit, credit], i) => {
    rows[i].querySelector(".e-subject").value = debit !== "" ? "1002" : "4001";
    rows[i].querySelector(".e-debit").value = debit;
    rows[i].querySelector(".e-credit").value = credit;
  });
  rows[0].oninput();
  const total = document.querySelector("#entry-total").textContent;
  const msg = document.querySelector("#acc-msg");
  msg.textContent = "";
  await els["#voucher-form"].onsubmit({ preventDefault() {}, target: {} });
  return { total, msg: msg.textContent, posted: posted.splice(0) };
}
"""


def _run(client, headers, org: int) -> dict:
    core = _read("core.js")
    script = (
        _HARNESS + _read("shared.js")
        + "".join(_top_level(core, f"function {name}(") for name in ("table", "panel", "actionableFirst", "setMsg"))
        + _accounting_section()
        + "\n(async () => { await renderAccounting();\n"
        "  const result = { rowHtml: rows[0].innerHTML,\n"
        "    mixed: await fill([['1000', ''], ['', '1000'], ['-200', ''], ['', '-200']]),\n"
        "    allNegative: await fill([['-100', ''], ['', '-100'], ['', ''], ['', '']]),\n"
        "    positive: await fill([['1000', ''], ['', '1000'], ['', ''], ['', '']]) };\n"
        "  process.stdout.write(JSON.stringify({ result }) + '\\n'); rl.close(); })();\n"
    )
    store = {"medplat_acc_period": PERIOD,
             "head": {"org_id": org, "voucher_no": "P21442-1", "voucher_date": f"{PERIOD}-05"}}
    proc = subprocess.Popen(["node", "-e", script, json.dumps(store)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "result" in message:
                return message["result"]
            resp = client.get(message["get"], headers=headers)
            assert resp.status_code == 200, (message["get"], resp.text)
            proc.stdin.write(json.dumps(resp.json()) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P21442 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]


@pytest.fixture(scope="module")
def page(client, admin, org):
    if shutil.which("node") is None:
        pytest.skip("没有 node 执行页面渲染")
    return _run(client, admin, org)


def test_金额框不收负数(page):
    assert '<input class="e-debit" type="number" step="0.01" min="0" placeholder="借方">' in page["rowHtml"]
    assert '<input class="e-credit" type="number" step="0.01" min="0" placeholder="贷方">' in page["rowHtml"]


def test_红字负数行_合计写明不收_提交前提示_不再悄悄丢行(page):
    mixed = page["mixed"]
    # 修前：屏幕「800.00 / 800.00 ✓ 平衡」，交出去的却只剩 1000 / 1000 两行
    assert mixed["total"] == f"借方合计 800.00　贷方合计 800.00　✗ {NEGATIVE}"
    assert mixed["posted"] == [] and mixed["msg"] == NEGATIVE


def test_全是负数_提交前提示_不交一张空凭证(page):
    negative = page["allNegative"]
    assert negative["posted"] == [] and negative["msg"] == NEGATIVE   # 修前交出 entries=[]，后端只报「至少 2 项」
    assert NEGATIVE in negative["total"]


def test_正数照交_交出去的正是合计里算的那几行_空行不算(client, admin, org, page):
    positive = page["positive"]
    assert positive["total"] == "借方合计 1000.00　贷方合计 1000.00　✓ 平衡"
    ((path, body),) = positive["posted"]
    assert path == "/api/accounting/vouchers"
    assert body["entries"] == [{"subject_code": "1002", "summary": "", "debit": 1000, "credit": 0},
                               {"subject_code": "4001", "summary": "", "debit": 0, "credit": 1000}]
    shown = [float(x) for x in re.findall(r"合计 ([\d.]+)", positive["total"])]
    assert shown == [sum(e["debit"] for e in body["entries"]), sum(e["credit"] for e in body["entries"])]
    saved = client.post(path, headers=admin, json=body)   # 页面交的这一份，接口照收
    assert saved.status_code == 201, saved.text
    assert (saved.json()["total_debit"], saved.json()["total_credit"]) == (1000, 1000)


def test_合计与提交用同一个取行函数():
    section = _accounting_section()
    assert section.count('document.querySelectorAll(".entry-row")') == 1   # 只有取行函数里取一次
    assert "const entryRows = () =>" in section
    total = section[section.index("const refreshTotal = () => {"):]
    assert total[:total.index("\n  };")].count("entryRows()") == 1
    submit = section[section.index('$("#voucher-form").onsubmit'):]
    assert submit[:submit.index("\n  };")].count("entryRows()") == 1


def test_后端分录金额照旧不收负数(client, admin, org):
    resp = client.post("/api/accounting/vouchers", headers=admin, json={
        "org_id": org, "voucher_no": "P21442-NEG", "voucher_date": f"{PERIOD}-06",
        "entries": [{"subject_code": "1002", "debit": -200}, {"subject_code": "4001", "credit": -200}]})
    assert resp.status_code == 422, resp.text
