"""家医签约页不再只看到最新 500 份（P2-1547，第四十五批扫描 AI1-2）。

修前：签约清单 `contracts.list_contracts` 只收 `org_id` / `patient_id`，按编号倒序缺省 500 条，`?status=` 被静默忽略；
签约页 `api("/api/contracts")` 不带参数只取这一页，「患者」「机构」两列只印编号，「记录履约 / 解约 / 履约记录」三个
按钮只在这张表里，页面又读不到 X-Total-Count。扫描实测（`r2_page_limit.py`）：502 份履约中的签约，页面那一调回 500 行、
X-Total-Count 502，最早签的两份不在；按协议号直接记履约 201——协议本身没问题，只是页面上没有这一行。

修法：接口加可选 `status`（取值照签约状态列注释 active / terminated，`pattern` 限定、写错 422），叠在可见范围之后；
清单出参末尾加 `patient_name`、`org_name`（`ContractRowOut`，按一页一批取）；签约、解约的回执照旧不带——签约接口
不判调用方与患者的关系（P1-45，待裁定），回执带姓名就成了按患者号查姓名的口子。页面加筛选栏（状态下拉 +
按患者号查，筛选只留在内存里），表上印患者姓名与机构名，经 `api(…, { withTotal: true })` 读 X-Total-Count，列不全时
标题写「已列 N / 共 total」。

页面那几条把 `core.js` 的 `api()` 与 `renderContracts` 原文放进 node 跑，`fetch` 经管道转给真接口（以甲镇医生的身份），
响应头照真接口给——`withTotal` 读到的是真的 X-Total-Count。
"""
import json
import subprocess
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import FamilyDoctorContract, Patient
from conftest import login

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

#: 甲镇签约份数：比清单一页（500）多两份，最早签的两份（第 0、1 份）挤出缺省那一页
N = 502
#: 已解约的是第几份（从 0 数）：第 1 份在缺省那一页之外，第 10 份在页内
TERMINATED = (1, 10)
#: 签约 / 解约回执的键序：既有七个，一字不动
RECEIPT_KEYS = ["patient_id", "org_id", "doctor_name", "package", "signed_date", "id", "status"]
#: 清单行的键序：既有七个一字不动，新加的两个在末尾
CONTRACT_KEYS = RECEIPT_KEYS + ["patient_name", "org_name"]


@pytest.fixture(scope="module")
def world(client, admin):
    """甲镇 502 份签约（第 1、10 份已解约），一位与甲镇毫无关系的居民；甲镇医生、乙镇医生各一位。"""
    orgs = {}
    for key, name in (("a", "P21547 甲镇卫生院"), ("b", "P21547 乙镇卫生院")):
        resp = client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": "township", "level": "township"})
        assert resp.status_code == 201, resp.text
        orgs[key] = resp.json()["id"]
    for username, org in (("p21547_doc", orgs["a"]), ("p21547_far", orgs["b"])):
        resp = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "role": "doctor", "org_id": org})
        assert resp.status_code == 201, resp.text
    # 直接落库造量：一位居民一份签约，避开「同一居民同一机构」的唯一约束
    with SessionLocal() as db:
        patients = [Patient(name=f"P21547 居民{i:03d}", id_card=f"3301021960{i:08d}", ehc_no=f"EHC-P21547-{i:04d}")
                    for i in range(N)]
        stranger = Patient(name="P21547 无关居民", id_card="330102196099999999", ehc_no="EHC-P21547-X")
        db.add_all([*patients, stranger])
        db.flush()
        contracts = [FamilyDoctorContract(patient_id=p.id, org_id=orgs["a"], doctor_name="P21547 家庭医生",
                                          package="basic", signed_date="2026-01-01",
                                          status="terminated" if i in TERMINATED else "active")
                     for i, p in enumerate(patients)]
        db.add_all(contracts)
        db.flush()
        ids = {"contracts": [c.id for c in contracts], "patients": [p.id for p in patients], "stranger": stranger.id}
        db.commit()
    return {**ids, "orgs": orgs, "doctor": login(client, "p21547_doc", "passw0rd1"),
            "far": login(client, "p21547_far", "passw0rd1")}


# ------------------------------------------------------------------ 接口


def test_不带参数照旧_最新一页500份_新字段只加在末尾(client, world):
    resp = client.get("/api/contracts", headers=world["doctor"])
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert resp.headers["X-Total-Count"] == str(N) and len(rows) == 500
    assert [r["id"] for r in rows] == world["contracts"][::-1][:500]   # 按编号倒序，最早签的两份不在这一页
    assert [list(r) for r in rows] == [CONTRACT_KEYS] * 500
    first = rows[-1]
    assert (first["patient_name"], first["org_name"]) == ("P21547 居民002", "P21547 甲镇卫生院")   # 修前没有这两个键


def test_按患者号查得到最早签的那份(client, world):
    resp = client.get("/api/contracts", headers=world["doctor"], params={"patient_id": world["patients"][0]})
    assert resp.status_code == 200, resp.text
    assert [(r["id"], r["status"], r["patient_name"]) for r in resp.json()] == [
        (world["contracts"][0], "active", "P21547 居民000")]
    assert resp.headers["X-Total-Count"] == "1"


def test_按状态筛只回该状态(client, world):
    terminated = client.get("/api/contracts", headers=world["doctor"], params={"status": "terminated"})
    assert terminated.status_code == 200, terminated.text
    # 修前 ?status= 被忽略，照回最新 500 份；第 1 份在缺省那一页之外，按状态筛才取得到
    assert [r["id"] for r in terminated.json()] == [world["contracts"][10], world["contracts"][1]]
    assert terminated.headers["X-Total-Count"] == "2"
    active = client.get("/api/contracts", headers=world["doctor"], params={"status": "active", "limit": 5})
    assert active.headers["X-Total-Count"] == str(N - len(TERMINATED))
    assert {r["status"] for r in active.json()} == {"active"}


def test_状态写错_422(client, world):
    for bad in ("expired", "ACTIVE", "active "):
        resp = client.get("/api/contracts", headers=world["doctor"], params={"status": bad})
        assert resp.status_code == 422, (bad, resp.text)   # 修前 200，照回最新一页


def test_新参数叠在可见范围之后_不绕过它(client, world):
    """与甲镇毫无关系的乙镇医生：不带参数、带状态都取空；按甲镇居民的患者号查 403（清单越权探针同一个问法）。"""
    for params in ({}, {"status": "active"}, {"status": "terminated"}):
        resp = client.get("/api/contracts", headers=world["far"], params=params)
        assert resp.status_code == 200 and resp.json() == [], (params, resp.text)
    resp = client.get("/api/contracts", headers=world["far"],
                      params={"patient_id": world["patients"][0], "status": "active"})
    assert resp.status_code == 403, resp.text


def test_签约与解约的回执照旧不带姓名与机构名(client, admin, world):
    """姓名只进按可见范围收口的清单：签约接口不判调用方与患者的关系（P1-45，待裁定），回执要是带姓名，任意一个
    患者号签一下就能查出是谁——回执键序照旧七个，一字不动。

    签在另一家（丙镇）：不动甲镇那 502 份，页面那几条的总数才对得上。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21547 丙镇卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21547 新签居民", "id_card": "330102197001015470"}).json()["id"]
    signed = client.post("/api/contracts", headers=admin, json={
        "patient_id": patient, "org_id": org, "doctor_name": "P21547 家庭医生"})
    assert signed.status_code == 201, signed.text
    assert list(signed.json()) == RECEIPT_KEYS
    ended = client.post(f"/api/contracts/{signed.json()['id']}/terminate", headers=admin)
    assert ended.status_code == 200, ended.text
    assert ended.json() == {**signed.json(), "status": "terminated"}
    again = client.post("/api/contracts", headers=admin, json={   # 同机构重签走「重新激活」那条路
        "patient_id": patient, "org_id": org, "doctor_name": "P21547 另一位医生"})
    assert again.status_code == 201, again.text
    # 只钉键序：重签改写既有行还是新起一行待裁定（P1-45 / P2-1037），不在本条
    assert list(again.json()) == RECEIPT_KEYS
    # 清单里这一份带姓名与机构名
    listed = client.get(f"/api/contracts?patient_id={patient}", headers=admin).json()
    assert [(r["patient_name"], r["org_name"]) for r in listed] == [("P21547 新签居民", "P21547 丙镇卫生院")]


# ------------------------------------------------------------------ 页面

PRELUDE = r"""
const ARGS = JSON.parse(process.argv[1]);
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const requests = [];
const pending = [];
let els = {};
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { textContent: "", className: "", innerHTML: "", dataset: {} }); } };
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
/* route() 照真页面整页重画：已有的元素全部作废、再画这一页 */
function route() { els = {}; pending.push(renderContracts()); }
async function settle() { while (pending.length) await pending.shift(); }
async function drawHomeVisits() {}
const page = () => els["#page-body"].innerHTML;
const titles = () => [...page().matchAll(/<h3>([^<]*)<\/h3>/g)].map((m) => m[1]);
const listed = () => [...page().matchAll(/data-svclist="(\d+)"/g)].map((m) => Number(m[1]));
/* 在筛选栏里填好、点「查询」 */
async function search(fields) {
  els["#ct-filter"].onsubmit({ preventDefault() {}, target: { fields } });
  await settle();
}
"""


def _function_source(source: str, head: str) -> str:
    """顶层函数原文：从 `head` 到它之后第一个顶格的 `}`。"""
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def run_page(client, headers: dict, steps: str, params: dict):
    """在 node 里加载 `api()` 与签约页、跑 `steps`（async 函数体，return 一个可 JSON 化的值）；请求以 `headers` 的身份转给真接口。"""
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    start = core.index("async function renderContracts(")
    script = (
        PRELUDE + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n"
        + "".join(_function_source(core, head) for head in (
            "async function api(", "function table(", "function panel(", "function setMsg("))
        + core[core.rindex("\n}\n", 0, start) + 3:start]           # 页面函数之前的顶层声明（筛选条件）
        + _function_source(core, "async function renderContracts(")
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


def test_页面标题写明列不全_行上印姓名与机构名(client, world):
    out = run_page(client, world["doctor"], """
      await renderContracts();
      return { requests, titles: titles(), listed: listed(), html: page() };
    """, {})
    assert out["requests"] == ["GET /api/contracts"]   # 不带参数照旧（缺省那一页）
    assert "签约协议（已列 500 / 共 502）" in out["titles"]   # 修前没有标题，看不出被截断了
    assert out["listed"] == world["contracts"][::-1][:500]
    assert "P21547 居民501（" in out["html"] and "P21547 甲镇卫生院" in out["html"]   # 修前只印两个编号


def test_按患者号查得到最早那份_行上摆记录履约与解约(client, world):
    cid = world["contracts"][0]
    out = run_page(client, world["doctor"], """
      await renderContracts();
      const before = listed();
      await search({ status: "", patient_id: String(ARGS.patient) });
      return { requests, before, after: listed(), titles: titles(), html: page() };
    """, {"patient": world["patients"][0]})
    assert cid not in out["before"]   # 前提：最早签的那份已被挤出缺省那一页
    assert out["requests"] == ["GET /api/contracts", f"GET /api/contracts?patient_id={world['patients'][0]}"]
    assert out["after"] == [cid] and "筛选结果（1）" in out["titles"]
    for button in (f'data-svc="{cid}"', f'data-term="{cid}"'):   # 「记录履约」「解约」
        assert button in out["html"], button
    assert "P21547 居民000（" in out["html"]
    assert f'name="patient_id" type="number" value="{world["patients"][0]}"' in out["html"]   # 筛的是谁写在筛选栏里


def test_按状态筛_只列该状态(client, world):
    out = run_page(client, world["doctor"], """
      await renderContracts();
      await search({ status: "terminated", patient_id: "" });
      const terminated = { listed: listed(), titles: titles(), html: page() };
      await search({ status: "", patient_id: "" });
      return { requests, terminated, cleared: titles() };
    """, {})
    assert out["requests"] == ["GET /api/contracts", "GET /api/contracts?status=terminated", "GET /api/contracts"]
    terminated = out["terminated"]
    assert terminated["listed"] == [world["contracts"][10], world["contracts"][1]]
    assert "筛选结果（2）" in terminated["titles"]
    assert "data-svc=" not in terminated["html"] and "data-term=" not in terminated["html"]   # 已解约的不摆这两个按钮
    assert '<option value="terminated" selected>' in terminated["html"]
    assert "签约协议（已列 500 / 共 502）" in out["cleared"]   # 清空筛选回到缺省那一页


def test_查看不到的患者_只在筛选栏报错_不掀掉整页(client, world):
    """患者号是手输的：与甲镇毫无关系的居民，后端 403。筛选栏、签约表单照画，报错写在筛选栏下。"""
    out = run_page(client, world["doctor"], """
      await renderContracts();
      await search({ status: "", patient_id: String(ARGS.stranger) });
      return { titles: titles(), html: page() };
    """, {"stranger": world["stranger"]})
    assert 'id="ct-filter"' in out["html"] and 'id="ct-form"' in out["html"]
    assert '<p class="msg err">' in out["html"] and "筛选结果（0）" in out["titles"]
