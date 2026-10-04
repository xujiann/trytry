"""会计、成本、项目三页把全县各家的凭证、科室、分摊规则、项目混在一起给全域账号看，却哪里都不写是哪家（P2-1438，第四十二批
扫描 AF3-2）。

修前（扫描实测）：两家各建内科 / 外科 / 行政后勤，成本页归集下拉是 `内科（临床）…内科（临床）` 两条一模一样的，选成第二个
「内科」录 8000，接口按所选科室的机构照收 201，科室成本表出现两行「内科」，乙院单位成本的总成本变成 8000；分摊下拉只写科室名，
规则表与科室成本表没有机构列（科室成本行后端其实给了 `org_name`）。会计页凭证清单里两张「记-1」分不清是哪家的，作废时容易点错；
试算平衡不带机构，只看得到全县合计（甲 1000 + 乙 20 = 1020），看不了单独一家的账。项目清单同样没有机构列。

修法只加显示与筛选、不改接口：成本页三个科室下拉写「机构名 · 科室名」（同 P2-1402 手术间），规则表、科室成本表加「机构」列；
会计页凭证清单加「机构」列，「会计期间」那一栏加机构下拉（缺省全部），选了就带 `org_id` 取凭证清单与试算平衡（合并报表本来
各家分列，不跟着筛）；项目清单加「机构」列。后端给了 `org_name` 的直接用，没给的按 `/api/organizations` 映射，映射不到回显
编号，一律 `esc()`。

这里把三个页面函数原样拿到 node 里跑，页面的 `api` 经管道转给真接口。
"""
import html as htmllib
import json
import os
import re
import shutil
import subprocess

import pytest

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
PERIOD = "2025-05"
ORG_A = "P21438 甲县医院"
#: 机构名里夹一段标签：显示时必须转义
ORG_B = "P21438 乙卫生院<b>x</b>"
ORG_B_SHOWN = htmllib.escape(ORG_B, quote=False)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def _section(source: str, head: str, next_head: str) -> str:
    start = source.index(head)
    return source[start:source.index(next_head, start)]


def _const(source: str, name: str) -> str:
    found = re.search(rf"^const {name} = .*?;\n", source, re.M | re.S)
    assert found, f"找不到常量 {name}"
    return found.group(0)


#: 页面取数换成桩：`$()` 按选择器给一个记 innerHTML 的假元素；`localStorage` 从参数里读；`FormData` 读假表单的 `fields`；
#: `api()` 经标准输出把地址交给测试进程、从标准输入读回真接口的响应；`route()` 只记次数（重画由脚本自己再调一次页面函数）
_HARNESS = r"""
const els = {};
const rows = [];
const STORE = JSON.parse(process.argv[1]);
const mk = () => ({ textContent: "", innerHTML: "", value: "", className: "", style: {}, kids: {}, dataset: {},
  classList: { add() {}, remove() {} },
  querySelector(sel) { return (this.kids[sel] ||= mk()); },
  appendChild(child) { rows.push(child); return child; } });
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= mk()); },
  querySelectorAll(sel) { return sel === ".entry-row" ? rows : []; },
  createElement() { return mk(); } };
globalThis.localStorage = { getItem: (k) => (k in STORE ? STORE[k] : null),
  setItem(k, v) { STORE[k] = String(v); }, removeItem(k) { delete STORE[k]; } };
globalThis.FormData = class { constructor(form) { this.fields = form.fields || {}; }
  get(k) { return k in this.fields ? this.fields[k] : null; } };
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const requested = [];
async function api(path) {
  requested.push(path);
  process.stdout.write(JSON.stringify({ get: path }) + "\n");
  return JSON.parse((await lines.next()).value);
}
let routed = 0;
function currentRole() { return STORE.medplat_role || ""; }
function route() { routed += 1; }
function postAction() {}
function formJson() { return {}; }
async function spdModal() { return null; }
const done = (result) => { process.stdout.write(JSON.stringify({ result }) + "\n"); rl.close(); };
"""


def _run(client, headers, code: str, store: dict, drop_org: int | None = None) -> dict:
    """在 node 里跑 `code`（先加载 shared.js 与 core.js 的几个组件）。`drop_org` 给了，就从机构清单里拿掉这一家（映射不到）。"""
    core = _read("core.js")
    script = (_HARNESS + _read("shared.js")
              + "".join(_top_level(core, f"function {name}(")
                        for name in ("table", "panel", "actionableFirst", "setMsg", "barChart"))
              + code)
    proc = subprocess.Popen(["node", "-e", script, json.dumps({"medplat_role": "admin", **store})],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "result" in message:
                return message["result"]
            resp = client.get(message["get"], headers=headers)
            assert resp.status_code == 200, (message["get"], resp.text)
            body = resp.json()
            if drop_org is not None and message["get"] == "/api/organizations":
                body = [o for o in body if o["id"] != drop_org]
            proc.stdin.write(json.dumps(body) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def _options(page: str, name: str) -> list[tuple[str, str]]:
    """`<select name=…>` 的选项：[(value, 显示文字)]。"""
    found = re.search(rf'<select name="{name}">(.*?)</select>', page, re.S)
    assert found, f"页面上没有 select[name={name}]"
    return re.findall(r'<option value="([^"]*)"[^>]*>(.*?)</option>', found.group(1), re.S)


def _table(page: str, marker: str) -> tuple[list[str], list[list[str]]]:
    """`marker` 之后的第一张表：(表头, [每行的各格])。"""
    start = page.index(marker)
    table = page[page.index("<table>", start):page.index("</table>", start)]
    head = re.findall(r"<th>(.*?)</th>", table)
    body = [re.findall(r"<td>(.*?)</td>", row, re.S) for row in re.findall(r"<tr>(.*?)</tr>", table, re.S)]
    return head, [cells for cells in body if cells]


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = {}
    for name, otype, level in ((ORG_A, "lead_hospital", "county"), (ORG_B, "township", "township")):
        resp = client.post("/api/organizations", headers=admin, json={"name": name, "org_type": otype, "level": level})
        assert resp.status_code == 201, resp.text
        orgs[name] = resp.json()["id"]
    a, b = orgs[ORG_A], orgs[ORG_B]
    depts = {}
    for org, tag, code, name, category in ((a, "甲", "NK", "内科", "clinical"), (a, "甲", "XZ", "行政后勤", "admin"),
                                           (b, "乙", "NK", "内科", "clinical")):
        resp = client.post("/api/mgmt/departments", headers=admin, json={
            "org_id": org, "code": code, "name": name, "category": category})
        assert resp.status_code == 201, resp.text
        depts[tag + code] = resp.json()["id"]
    for key, cost_type, amount in (("甲NK", "labor", 30000), ("甲XZ", "labor", 10000), ("乙NK", "drug", 8000)):
        resp = client.post("/api/cost/departments", headers=admin, json={
            "dept_id": depts[key], "period": PERIOD, "cost_type": cost_type, "amount": amount})
        assert resp.status_code == 201, resp.text
    rule = client.post("/api/cost/allocation-rules", headers=admin, json={
        "from_dept_id": depts["甲XZ"], "to_dept_id": depts["甲NK"], "ratio_pct": 60})
    assert rule.status_code == 201, rule.text
    vouchers = {}
    for org, amount in ((a, 1000), (b, 20)):   # 两家都有「记-1」
        resp = client.post("/api/accounting/vouchers", headers=admin, json={
            "org_id": org, "voucher_no": "记-1", "voucher_date": f"{PERIOD}-06", "summary": "收门诊款",
            "entries": [{"subject_code": "1002", "debit": amount}, {"subject_code": "4001", "credit": amount}]})
        assert resp.status_code == 201, resp.text
        vouchers[org] = resp.json()["id"]
        assert client.post(f"/api/accounting/vouchers/{vouchers[org]}/post", headers=admin).status_code == 200
    projects = {}
    for org in (a, b):   # 两家同名的项目
        resp = client.post("/api/projects", headers=admin, json={"org_id": org, "name": "P21438 胸痛中心建设"})
        assert resp.status_code == 201, resp.text
        projects[org] = resp.json()["id"]
    return {"a": a, "b": b, "depts": depts, "vouchers": vouchers, "projects": projects}


# ---------- 成本核算 ----------


def _cost_page(client, admin, drop_org=None) -> str:
    mgmt = _read("pages-mgmt.js")
    code = (_const(mgmt, "COST_TYPES")
            + _section(mgmt, "/* ---------------- 成本核算 ---------------- */", "/* ---------------- 物资采购与高值耗材")
            + "\n(async () => { await renderCost(); done({ body: els['#page-body'].innerHTML }); })();\n")
    return _run(client, admin, code, {"medplat_cost_period": PERIOD}, drop_org)["body"]


def test_成本页_归集与分摊下拉写机构名加科室名_同名内科分得清(client, admin, world):
    page = _cost_page(client, admin)
    d = world["depts"]
    # 修前两条都是「内科（临床）」，选错了成本就记到别家
    assert _options(page, "dept_id") == [
        (str(d["甲NK"]), f"{ORG_A} · 内科（临床）"), (str(d["甲XZ"]), f"{ORG_A} · 行政后勤（行政后勤）"),
        (str(d["乙NK"]), f"{ORG_B_SHOWN} · 内科（临床）")]
    for name in ("from_dept_id", "to_dept_id"):
        assert _options(page, name) == [
            (str(d["甲NK"]), f"{ORG_A} · 内科"), (str(d["甲XZ"]), f"{ORG_A} · 行政后勤"),
            (str(d["乙NK"]), f"{ORG_B_SHOWN} · 内科")]


def test_成本页_规则表与科室成本表有机构列(client, admin, world):
    page = _cost_page(client, admin)
    head, rules = _table(page, "<h3>分摊规则</h3>")
    assert head[:3] == ["机构", "来源科室", "目标科室"]
    assert [cells[:3] for cells in rules] == [[ORG_A, "行政后勤", "内科"]]
    head, costs = _table(page, f"<h3>{PERIOD} 科室成本</h3>")
    assert head[:2] == ["机构", "科室"]
    # 按总成本倒序：甲内科 30000 + 分入 6000、乙内科 8000、甲行政后勤 10000 − 分出 6000；后端给的 org_name 直接用
    assert [cells[:2] for cells in costs] == [[ORG_A, "内科"], [ORG_B_SHOWN, "内科"], [ORG_A, "行政后勤"]]


def test_成本页_机构清单里映射不到的回显编号(client, admin, world):
    page = _cost_page(client, admin, drop_org=world["b"])
    assert (str(world["depts"]["乙NK"]), f"{world['b']} · 内科（临床）") in _options(page, "dept_id")


# ---------- 会计核算 ----------


def _accounting(client, admin, pick: int, drop_org=None) -> dict:
    """渲染一遍；再照页面的写法在「会计期间与机构」里选 `pick` 那家、点切换，按 `route()` 重画一遍。"""
    mgmt = _read("pages-mgmt.js")
    code = (_section(mgmt, "/* ---------------- 会计核算 ---------------- */", "/* ---------------- 成本核算 ---------------- */")
            + "\n(async () => {\n"
            "  await renderAccounting();\n"
            "  const before = { body: els['#page-body'].innerHTML, requested: requested.splice(0) };\n"
            "  await els['#acc-period'].onsubmit({ preventDefault() {},\n"
            "    target: { fields: { period: STORE.medplat_acc_period, org_id: STORE.pick } } });\n"
            "  const submitted = { requested: requested.splice(0), routed };\n"
            "  await renderAccounting();\n"
            "  done({ before, submitted, after: { body: els['#page-body'].innerHTML, requested: requested.splice(0) } });\n"
            "})();\n")
    return _run(client, admin, code, {"medplat_acc_period": PERIOD, "pick": str(pick)}, drop_org)


def test_会计页_凭证清单有机构列_两张记1分得清(client, admin, world):
    out = _accounting(client, admin, world["a"])
    head, rows = _table(out["before"]["body"], f"<h3>{PERIOD} 凭证")
    assert head[:3] == ["ID", "凭证号", "机构"]
    assert sorted((int(cells[0]), cells[1], cells[2]) for cells in rows) == sorted([
        (world["vouchers"][world["a"]], "记-1", ORG_A), (world["vouchers"][world["b"]], "记-1", ORG_B_SHOWN)])
    # 机构下拉缺省「全部机构」，列出机构清单里的各家
    options = _options(out["before"]["body"], "org_id")
    assert options[0] == ("", "全部机构")
    assert {(str(world["a"]), ORG_A), (str(world["b"]), ORG_B_SHOWN)} <= set(options)
    assert ' selected' not in re.search(r'<select name="org_id">(.*?)</select>', out["before"]["body"], re.S).group(1)
    tb = re.search(r"借方合计 ([\d.]+)", out["before"]["body"]).group(1)
    assert tb == "1020.00"   # 不选机构：全县合计（甲 1000 + 乙 20），与修前一样


def test_会计页_选了机构_凭证清单与试算平衡带org_id只看这一家(client, admin, world):
    a = world["a"]
    out = _accounting(client, admin, a)
    assert out["submitted"]["routed"] == 1
    requested = out["after"]["requested"]
    assert f"/api/accounting/vouchers?period={PERIOD}&limit=50&org_id={a}" in requested
    assert f"/api/accounting/vouchers?period={PERIOD}&status=draft&org_id={a}&limit=500&offset=0" in requested
    assert f"/api/accounting/trial-balance?period={PERIOD}&org_id={a}" in requested
    assert f"/api/accounting/consolidated-statements?period={PERIOD}" in requested   # 合并报表不跟着筛
    after = out["after"]["body"]
    _head, rows = _table(after, f"<h3>{PERIOD} {ORG_A} 凭证")   # 标题写上是哪家
    assert [(int(cells[0]), cells[2]) for cells in rows] == [(world["vouchers"][a], ORG_A)]
    assert "试算平衡表（" + ORG_A + "，仅统计已过账）" in after
    assert re.search(r"借方合计 ([\d.]+)", after).group(1) == "1000.00"   # 修前只看得到 1020 的全县合计
    assert f'<option value="{a}" selected>{ORG_A}</option>' in after


def test_会计页_机构清单里映射不到的回显编号(client, admin, world):
    out = _accounting(client, admin, world["a"], drop_org=world["b"])
    _head, rows = _table(out["before"]["body"], f"<h3>{PERIOD} 凭证")
    assert (str(world["vouchers"][world["b"]]), str(world["b"])) in [(cells[0], cells[2]) for cells in rows]


# ---------- 项目管理 ----------


def _projects_page(client, admin, drop_org=None) -> str:
    clinical = _read("pages-clinical.js")
    code = (_const(clinical, "PROJECT_STATUS_OPTS") + _const(clinical, "MS_STATUS")
            + _top_level(clinical, "async function renderProjects(")
            + "\n(async () => { await renderProjects(); done({ body: els['#page-body'].innerHTML }); })();\n")
    return _run(client, admin, code, {}, drop_org)["body"]


def _project_rows(page: str) -> dict[str, list[str]]:
    head, rows = _table(page, "<h3>项目清单")
    assert head[:2] == ["名称", "机构"]
    return {cells[1]: cells for cells in rows}


def test_项目清单有机构列_同名项目分得清(client, admin, world):
    rows = _project_rows(_projects_page(client, admin))
    assert set(rows) == {ORG_A, ORG_B_SHOWN}
    assert all(cells[0] == "P21438 胸痛中心建设" for cells in rows.values())


def test_项目清单_机构清单里映射不到的回显编号(client, admin, world):
    rows = _project_rows(_projects_page(client, admin, drop_org=world["b"]))
    assert set(rows) == {ORG_A, str(world["b"])}
