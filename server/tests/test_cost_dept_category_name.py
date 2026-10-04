"""成本页科室成本表的「类别」列印英文 clinical / medtech / admin（P2-1439，第四十二批扫描 AF3-5）。

修前 `GET /api/cost/departments` 每行只有 `dept_category`（模型列的英文码），页面「类别」列原样印出来——同一页上面的科室
下拉早就写「临床」：P2-74 ② 把科室表与成本页下拉改成了科室清单的 `category_name`（`admin_mgmt.DEPT_CATEGORY_NAMES`），
P2-74 是按「模型列名 × 页面字段名」推的，这里字段叫 `dept_category` 不叫 `category`，没推到。扫描两个脚本都返回
`"dept_category": "clinical"`。

修法：出参末尾只增 `dept_category_name`，取值用科室表同一份 `DEPT_CATEGORY_NAMES`（不另造一份），原有键与次序不动
（`test_cost_contract.py` 跟着补这个键）；页面「类别」列改显示它。
"""
import json
import os
import re
import shutil
import subprocess

import pytest

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
PERIOD = "2025-06"
DEPTS = (("NK", "内科", "clinical", 30000), ("JY", "检验科", "medtech", 12000), ("XZ", "院办", "admin", 8000))


@pytest.fixture(scope="module")
def org(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21439 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    for code, name, category, amount in DEPTS:
        dept = client.post("/api/mgmt/departments", headers=admin, json={
            "org_id": org, "code": code, "name": name, "category": category})
        assert dept.status_code == 201, dept.text
        cost = client.post("/api/cost/departments", headers=admin, json={
            "dept_id": dept.json()["id"], "period": PERIOD, "cost_type": "labor", "amount": amount})
        assert cost.status_code == 201, cost.text
    return org


def test_科室成本出参带中文类别名_与科室清单同一份文案(client, admin, org):
    rows = client.get("/api/cost/departments", headers=admin, params={"period": PERIOD, "org_id": org}).json()
    # 修前没有这个键，页面只能印 dept_category 的英文码
    assert [(r["dept_name"], r["dept_category"], r["dept_category_name"]) for r in rows] == [
        ("内科", "clinical", "临床"), ("检验科", "medtech", "医技"), ("院办", "admin", "行政后勤")]
    assert all(list(r)[-1] == "dept_category_name" for r in rows)   # 只在末尾增键
    depts = {d["name"]: d["category_name"] for d in client.get(
        "/api/mgmt/departments", headers=admin, params={"org_id": org}).json()}
    assert {r["dept_name"]: r["dept_category_name"] for r in rows} == depts   # 与上面的科室下拉同一句


def test_类别文案只有一份_取自科室表():
    from app.routers import admin_mgmt, cost

    assert cost.DEPT_CATEGORY_NAMES is admin_mgmt.DEPT_CATEGORY_NAMES   # 不另造一份类别名称表


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


#: 成本页取数换成桩：`$()` 给记 innerHTML 的假元素，`localStorage` 从参数里读，`api()` 经管道转给真接口
_HARNESS = r"""
const els = {};
const STORE = JSON.parse(process.argv[1]);
const mk = () => ({ textContent: "", innerHTML: "", value: "", style: {}, classList: { add() {}, remove() {} } });
globalThis.document = { addEventListener() {}, cookie: "", querySelector(sel) { return (els[sel] ||= mk()); } };
globalThis.localStorage = { getItem: (k) => (k in STORE ? STORE[k] : null), setItem() {}, removeItem() {} };
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
async function api(path) {
  process.stdout.write(JSON.stringify({ get: path }) + "\n");
  return JSON.parse((await lines.next()).value);
}
function postAction() {}
function formJson() { return {}; }
function setMsg() {}
async function spdModal() { return null; }
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_成本页类别列显示中文_不再印英文码(client, admin, org):
    core, mgmt = _read("core.js"), _read("pages-mgmt.js")
    cost_types = re.search(r"^const COST_TYPES = .*?;\n", mgmt, re.M | re.S).group(0)
    section = mgmt[mgmt.index("/* ---------------- 成本核算 ---------------- */"):
                   mgmt.index("/* ---------------- 物资采购与高值耗材")]
    script = (_HARNESS + _read("shared.js")
              + "".join(_top_level(core, f"function {name}(") for name in ("table", "panel", "barChart"))
              + cost_types + section
              + "\n(async () => { await renderCost();\n"
                "  process.stdout.write(JSON.stringify({ body: els['#page-body'].innerHTML }) + '\\n'); rl.close(); })();\n")
    proc = subprocess.Popen(["node", "-e", script, json.dumps({"medplat_cost_period": PERIOD})], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "body" in message:
                break
            resp = client.get(message["get"], headers=admin)
            assert resp.status_code == 200, (message["get"], resp.text)
            proc.stdin.write(json.dumps(resp.json()) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
    start = message["body"].index(f"<h3>{PERIOD} 科室成本</h3>")
    table = message["body"][start:message["body"].index("</table>", start)]
    head = re.findall(r"<th>(.*?)</th>", table)
    rows = [re.findall(r"<td>(.*?)</td>", row, re.S) for row in re.findall(r"<tr>(.*?)</tr>", table, re.S)]
    at = head.index("类别")
    assert [cells[at] for cells in rows if cells] == ["临床", "医技", "行政后勤"]
    assert not re.search(r"<td>(clinical|medtech|admin)</td>", table)   # 修前三行印的就是这三个
