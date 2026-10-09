"""人财物页的员工、物资两张表不再只看到缺省的第一页（P2-1594，第四十七批扫描 AK1-3）。

修前：人财物页 `renderHrFinance` 取员工、物资都是 `api("/api/mgmt/employees")`、`api("/api/mgmt/assets")`——不带参数，只拿
清单缺省那一页（`deps.paginate` 一页 500、按编号升序），也不读 X-Total-Count。第 501 位起正是最新入职、最新建档的：挂科室、
登记变动、签合同、出入库、调拨、报废这些按钮只摆在表的行上，他们在页面上没有行，页面也不提示截断。扫描实测（`r4_trunc.py`）：
本机构 520 名员工，清单 500 行、X-Total-Count 520、最大编号 500；再登记一位（第 521 号）照样不在这一页；物资 510 件同理。

修法（同家医签约页 P2-1547）：两个清单接口加可选 `keyword`——员工按姓名、物资按名称或编码包含匹配（`%` / `_` 按字面），叠在
可见范围（`scope_org_list`）之后、只收窄，缺省的排序与分页一字不动；页面两张表各加一个查找栏（查找条件只留在内存里），经
`api(…, { withTotal: true })` 读 X-Total-Count，列不全时标题写「已列 N / 共 total」。没走续页取全（P2-1333）：离职的员工、
报废的物资都留在表里，只增不减。

页面那几条把 `core.js` 的 `api()` 与 `renderHrFinance` 原文放进 node 跑，`fetch` 经管道转给真接口（以甲院经办的身份），
响应头照真接口给——`withTotal` 读到的是真的 X-Total-Count。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest

from app.database import SessionLocal
from app.models import Asset, Employee
from conftest import login

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

#: 甲院直接落库造的员工、物资件数：都比清单一页（500）多
N_EMP = 520
N_ASSET = 510
NEWEST_NAME = "P21594 新入职"
NEWEST_CODE = "P21594_NEW"
#: 与 NEWEST_CODE 只差在下划线那一位：`_` 若被当通配符，按 NEWEST_CODE 查会把它也带出来
LOOKALIKE_CODE = "P21594XNEW"


@pytest.fixture(scope="module")
def world(client, admin):
    """甲院 520 名员工 + 第 521 号（经接口登记）、512 件物资；乙院一位同名员工；甲院一个科室。"""
    orgs = {}
    for key, name in (("a", "P21594 甲卫生院"), ("b", "P21594 乙卫生院")):
        resp = client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": "township", "level": "township"})
        assert resp.status_code == 201, resp.text
        orgs[key] = resp.json()["id"]
    for username, org in (("p21594_opa", orgs["a"]), ("p21594_opb", orgs["b"])):
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "role": "operator", "org_id": org})
        assert resp.status_code == 201, resp.text
    # 直接落库造量（逐个走接口太慢）
    with SessionLocal() as db:
        db.add_all([Employee(org_id=orgs["a"], name=f"P21594 职工{i:03d}") for i in range(N_EMP)])
        db.add_all([Asset(org_id=orgs["a"], code=f"P21594-ZC-{i:04d}", name=f"P21594 物资{i}", quantity=1)
                    for i in range(N_ASSET)])
        db.commit()
    opa = login(client, "p21594_opa", "passw0rd1")
    newest = client.post("/api/mgmt/employees", headers=opa, json={"org_id": orgs["a"], "name": NEWEST_NAME})
    namesake = client.post("/api/mgmt/employees", headers=admin, json={"org_id": orgs["b"], "name": NEWEST_NAME})
    assets = {}
    for code, name in ((LOOKALIKE_CODE, "P21594 打印机"), (NEWEST_CODE, "P21594 新建档监护仪")):
        resp = client.post("/api/mgmt/assets", headers=opa, json={"org_id": orgs["a"], "code": code, "name": name})
        assert resp.status_code == 201, resp.text
        assets[code] = resp.json()["id"]
    dept = client.post("/api/mgmt/departments", headers=opa, json={
        "org_id": orgs["a"], "code": "P21594-NK", "name": "P21594 内科"})
    for resp in (newest, namesake, dept):
        assert resp.status_code == 201, resp.text
    return {"orgs": orgs, "opa": opa, "opb": login(client, "p21594_opb", "passw0rd1"),
            "newest": newest.json()["id"], "namesake": namesake.json()["id"], "assets": assets,
            "dept": dept.json()["id"]}


# ------------------------------------------------------------------ 接口


def test_不带参数照旧_一页500按编号升序_最新入职的不在这一页(client, world):
    resp = client.get("/api/mgmt/employees", headers=world["opa"])
    assert resp.status_code == 200, resp.text
    ids = [e["id"] for e in resp.json()]
    assert resp.headers["X-Total-Count"] == str(N_EMP + 1) and len(ids) == 500 and ids == sorted(ids)
    assert world["newest"] not in ids   # 前提：第 521 号在缺省那一页之外
    assets = client.get("/api/mgmt/assets", headers=world["opa"])
    assert assets.headers["X-Total-Count"] == str(N_ASSET + 2) and len(assets.json()) == 500
    assert world["assets"][NEWEST_CODE] not in [a["id"] for a in assets.json()]


def test_按姓名查得到第521号_只收窄不越出可见范围(client, admin, world):
    resp = client.get("/api/mgmt/employees", headers=world["opa"], params={"keyword": "新入职"})
    assert resp.status_code == 200, resp.text
    # 修前 keyword 被忽略，照回缺省那一页 500 行；乙院那位同名员工不出来
    assert [e["id"] for e in resp.json()] == [world["newest"]]
    assert resp.headers["X-Total-Count"] == "1"
    far = client.get("/api/mgmt/employees", headers=world["opb"], params={"keyword": NEWEST_NAME})
    assert [e["id"] for e in far.json()] == [world["namesake"]]
    everyone = client.get("/api/mgmt/employees", headers=admin, params={"keyword": NEWEST_NAME})   # 全域账号两家都见
    assert [e["id"] for e in everyone.json()] == [world["newest"], world["namesake"]]
    # 带了别家机构号照旧 403（查找参数不绕过 scope_org_list）
    assert client.get("/api/mgmt/employees", headers=world["opb"], params={
        "keyword": NEWEST_NAME, "org_id": world["orgs"]["a"]}).status_code == 403


def test_物资按名称或编码查_通配符按字面(client, world):
    by_code = client.get("/api/mgmt/assets", headers=world["opa"], params={"keyword": NEWEST_CODE.lower()})
    assert by_code.status_code == 200, by_code.text
    # `_` 按字面：只差下划线那一位的 P21594XNEW 不出来；大小写不分（keyword_like 两边转小写）
    assert [a["id"] for a in by_code.json()] == [world["assets"][NEWEST_CODE]]
    by_name = client.get("/api/mgmt/assets", headers=world["opa"], params={"keyword": "监护仪"})
    assert [a["id"] for a in by_name.json()] == [world["assets"][NEWEST_CODE]]
    # 单敲一个 `%` 不列出全部
    pct = client.get("/api/mgmt/assets", headers=world["opa"], params={"keyword": "%"})
    assert pct.json() == [] and pct.headers["X-Total-Count"] == "0"
    assert client.get("/api/mgmt/assets", headers=world["opb"], params={"keyword": NEWEST_CODE}).json() == []


# ------------------------------------------------------------------ 页面

PRELUDE = r"""
const ARGS = JSON.parse(process.argv[1]);
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const requests = [];
const pending = [];
let els = {};
globalThis.localStorage = { getItem: (k) => (k === "medplat_role" ? "operator" : null), setItem() {}, removeItem() {} };
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { textContent: "", className: "", innerHTML: "", dataset: {},
    classList: { add() {}, remove() {} } }); } };
globalThis.FormData = class { constructor(form) { this.fields = form.fields; } get(k) { return k in this.fields ? this.fields[k] : null; } };
/* 真 `api()` 要的几样：迁移期令牌（空 = Cookie 模式）、CSRF、登出（走到就是用例写错了） */
let token = "";
function csrfToken() { return ""; }
function logout() { throw new Error("不该走到登出"); }
/* 浏览器的 fetch：经管道转给真接口，响应头只垫 X-Total-Count（大小写不敏感，照 Headers.get） */
globalThis.fetch = async (path, init = {}) => {
  const method = init.method || "GET";
  requests.push(`${method} ${path}`);
  process.stdout.write(JSON.stringify({ method, path, body: init.body ? JSON.parse(init.body) : null }) + "\n");
  const reply = JSON.parse((await lines.next()).value);
  return { status: reply.status, ok: reply.status < 400, json: async () => reply.body,
    headers: { get: (name) => (name.toLowerCase() === "x-total-count" ? reply.total : null) } };
};
/* 挂科室的框：选甲院那个科室 */
async function spdModal() { return { dept_id: String(ARGS.dept) }; }
/* route() 照真页面整页重画：已有的元素全部作废、再画这一页 */
function route() { els = {}; pending.push(renderHrFinance()); }
async function settle() { while (pending.length) await pending.shift(); }
const page = () => els["#page-body"].innerHTML;
const titles = () => [...page().matchAll(/<h3>([^<]*)<\/h3>/g)].map((m) => m[1]);
const ids = (attr) => [...page().matchAll(new RegExp(`data-${attr}="(\\d+)"`, "g"))].map((m) => Number(m[1]));
/* 在查找栏里填好、点「查找」 */
async function find(sel, keyword) {
  els[sel].onsubmit({ preventDefault() {}, target: { fields: { keyword } } });
  await settle();
}
"""


def _function_source(source: str, head: str) -> str:
    """顶层函数原文：从 `head` 到它之后第一个顶格的 `}`。"""
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def _const(source: str, name: str) -> str:
    found = re.search(rf"^(?:const|let) {name} = .*?;\n", source, re.M | re.S)
    assert found, f"找不到顶层声明 {name}"
    return found.group(0)


def run_page(client, headers: dict, steps: str, params: dict):
    """在 node 里加载 `api()` 与人财物页、跑 `steps`（async 函数体，return 一个可 JSON 化的值）；请求以 `headers` 的身份转给真接口。"""
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    clinical = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    mgmt = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    script = (
        PRELUDE + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n"
        + "".join(_function_source(core, head) for head in (
            "async function api(", "function table(", "function panel(", "function setMsg(", "function currentRole("))
        + "".join(_function_source(clinical, head) for head in ("function formJson(", "async function postAction("))
        + _const(mgmt, "ASSIGN_TYPES") + _const(clinical, "PAYROLL_PERIOD") + _const(clinical, "HRF_FILTER")
        + _function_source(clinical, "async function renderHrFinance(")
        + f"\n(async () => {{\n{steps}\n}})().then("
        + "(r) => { process.stdout.write(JSON.stringify({ result: r }) + '\\n'); rl.close(); },"
        + " (e) => { console.error(e); process.exit(1); });\n"
    )
    proc = subprocess.Popen(["node", "-e", script, json.dumps(params)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "result" in message:
                return message["result"]
            resp = client.request(message["method"], message["path"], headers=headers, json=message["body"])
            reply = {"status": resp.status_code, "body": resp.json(), "total": resp.headers.get("x-total-count")}
            proc.stdin.write(json.dumps(reply, ensure_ascii=False) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def _render_body() -> str:
    clinical = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    return _function_source(clinical, "async function renderHrFinance(")


def test_页面两张清单都读总数_查找参数只在查找时带():
    body = _render_body()
    assert "api(`/api/mgmt/employees${findQuery(HRF_FILTER.employee)}`, { withTotal: true })" in body
    assert "api(`/api/mgmt/assets${findQuery(HRF_FILTER.asset)}`, { withTotal: true })" in body
    assert 'api("/api/mgmt/employees")' not in body and 'api("/api/mgmt/assets")' not in body   # 修前的取法
    for form in ('id="emp-find"', 'id="asset-find"'):
        assert form in body, form


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_标题写明列不全_查找得到第521号并挂上科室(client, world):
    out = run_page(client, world["opa"], """
      await renderHrFinance();
      const before = { titles: titles(), emp: ids("empdept"), asset: ids("assetmv") };
      await find("#emp-find", ARGS.name);
      const found = { titles: titles(), emp: ids("empdept"), html: page() };
      await els["#page-body"].onclick({ target: { dataset: { empdept: String(ARGS.newest) } } });
      await settle();
      const hung = page();
      await find("#asset-find", ARGS.code);
      const asset = { titles: titles(), mv: ids("assetmv"), xfer: ids("assetxfer"), scrap: ids("assetscrap") };
      return { requests, before, found, hung, asset };
    """, {"name": NEWEST_NAME, "newest": world["newest"], "dept": world["dept"], "code": NEWEST_CODE})
    before = out["before"]
    # 缺省照旧取第一页，标题写明截断（修前标题不带数，看不出被截断了）
    assert out["requests"][:7] == [
        "GET /api/mgmt/employees", "GET /api/mgmt/secondments/stats", "GET /api/mgmt/finance/summary",
        "GET /api/mgmt/assets", "GET /api/mgmt/departments", "GET /api/mgmt/staff-contracts/expiring?days=60",
        "GET /api/organizations"]
    assert f"员工（已列 500 / 共 {N_EMP + 1}）· 变动留痕联动机构与状态" in before["titles"]
    assert any(t.startswith(f"物资（已列 500 / 共 {N_ASSET + 2}）") for t in before["titles"]), before["titles"]
    assert world["newest"] not in before["emp"] and len(before["emp"]) == 500
    # 按姓名查：只有甲院这一位（乙院同名的那位不出来），行上摆着挂科室
    found = out["found"]
    assert f"GET /api/mgmt/employees?keyword={quote(NEWEST_NAME, safe='')}" in out["requests"]
    assert found["emp"] == [world["newest"]] and "员工查找结果（1）· 变动留痕联动机构与状态" in found["titles"]
    assert f'name="keyword" value="{NEWEST_NAME}"' in found["html"]   # 查的是谁写在查找栏里
    # 挂上科室：请求发出去、重画后这一行显示科室名
    assert f"POST /api/mgmt/employees/{world['newest']}/department?dept_id={world['dept']}" in out["requests"]
    row = re.search(rf"<tr><td>{world['newest']}</td>.*?</tr>", out["hung"], re.S)
    assert row and "P21594 内科" in row.group(0), out["hung"][-2000:]
    # 物资按编码查得到最新建档的那件，出入库、调拨、报废都有入口
    asset = out["asset"]
    newest_asset = world["assets"][NEWEST_CODE]
    assert asset["mv"] == asset["xfer"] == asset["scrap"] == [newest_asset]
    assert "物资查找结果（1）· 出入库全程留痕；报废与调拨都不可逆，后端无反向端点" in asset["titles"]
    # 页面那一下挂科室真的落了库
    rows = client.get("/api/mgmt/employees", headers=world["opa"], params={"keyword": NEWEST_NAME}).json()
    assert [(r["id"], r["dept_id"]) for r in rows] == [(world["newest"], world["dept"])]
