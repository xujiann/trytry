"""统一编码字典的条目清单能翻页、给总数，字典页能检索（P2-1733，第五十一批扫描 AO3-2）。

修前：`GET /api/dictionaries/{system}/entries` 写死 `.limit(200)`、不收 offset、不给 X-Total-Count。`docs/接口对接规范.md`
叫外部系统从这里「下载对照」，第 201 条起的平台编码在对接方的映射表里全成了「平台没有」；字典页也没有检索框，CLI 导入
全量 ICD-10 后永远只看得到 A 开头的 200 条、搜不到 I10，也看不出被截断。扫描实测（`r1_dict.py`）：药品字典 300 条，接口
回 200 条、最后一条 ZZ0149、没有 X-Total-Count；offset 被忽略。

修法：改走 `deps.paginate`——缺省一页 200 条、按编码排，不带参数的调用返回的行与字节照旧（只多一个 X-Total-Count 头），
一页上限照 `paginate` 的 500；字典页加检索栏（接口本来就收 keyword），列不全时标题写「已列 N / 共 M」。

页面那条把 `core.js` 的 `api()` 与 `renderDicts` 原文放进 node 跑，`fetch` 经管道转给真接口（管理员身份），响应头照真接口给
——`withTotal` 读到的是真的 X-Total-Count。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.deps import keyword_like
from app.models import CodeEntry, CodeSystem
from app.routers.dictionaries import CodeEntryDetailOut

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

#: 耗材字典启动时不种子（只种诊断与药品）：清单里恰好是本文件导入的这 201 条
N_CONSUMABLE = 201
CONSUMABLE_CODES = [f"P21733-{i:03d}" for i in range(N_CONSUMABLE)]
#: 诊断字典再导这么多条排在 I10 之前的编码（A 开头）：连同启动种子，I10 落在缺省那一页之外
N_DIAGNOSIS_EXTRA = 230


def _system_id(code: str) -> int:
    with SessionLocal() as db:
        return db.query(CodeSystem).filter(CodeSystem.code == code).one().id


@pytest.fixture(scope="module")
def consumables(client, admin):
    with SessionLocal() as db:   # 前提：耗材字典是空的
        assert db.query(CodeEntry).filter(CodeEntry.system_id == _system_id("consumable")).count() == 0
    # 倒着导：编号顺序与编码顺序正相反，清单若按编号排就看得出来
    resp = client.post("/api/dictionaries/consumable/import", headers=admin,
                       json=[{"code": code, "name": f"P21733 耗材{code[-3:]}"} for code in reversed(CONSUMABLE_CODES)])
    assert resp.status_code == 200 and resp.json()["imported"] == N_CONSUMABLE, resp.text
    return CONSUMABLE_CODES


# ------------------------------------------------------------------ 接口


def test_201条时offset200取到第201条_总数头201(client, admin, consumables):
    page2 = client.get("/api/dictionaries/consumable/entries", headers=admin, params={"offset": 200})
    assert page2.status_code == 200, page2.text
    # 修前 offset 被忽略、照回前 200 条，也没有总数头
    assert [e["code"] for e in page2.json()] == [consumables[200]]
    assert page2.headers.get("X-Total-Count") == "201"
    first = client.get("/api/dictionaries/consumable/entries", headers=admin)
    assert [e["code"] for e in first.json()] == consumables[:200]   # 缺省一页 200 条、按编码排，照旧
    assert first.headers.get("X-Total-Count") == "201"
    # 一页上限照 paginate 的 500：这 201 条一次取得全
    whole = client.get("/api/dictionaries/consumable/entries", headers=admin, params={"limit": 500})
    assert [e["code"] for e in whole.json()] == consumables
    # 检索与翻页叠着用，总数是检索后的
    hits = client.get("/api/dictionaries/consumable/entries", headers=admin,
                      params={"keyword": "P21733-19", "offset": 5, "limit": 3})
    assert [e["code"] for e in hits.json()] == ["P21733-195", "P21733-196", "P21733-197"]
    assert hits.headers.get("X-Total-Count") == "10"   # P21733-190 到 199


@pytest.mark.parametrize("system, params", [("consumable", None), ("diagnosis", None), ("diagnosis", {"keyword": "高血压"})])
def test_不带分页参数的调用_行与字节照旧(client, admin, consumables, system, params):
    """照修前的查询（按编码排、前 200 条）自己序列化一遍，与接口的响应体逐字节比：只多一个总数头，响应体一个字节不变。"""
    resp = client.get(f"/api/dictionaries/{system}/entries", headers=admin, params=params)
    assert resp.status_code == 200, resp.text
    with SessionLocal() as db:
        query = db.query(CodeEntry).filter(CodeEntry.system_id == _system_id(system))
        if params:
            keyword = params["keyword"]
            query = query.filter(keyword_like(CodeEntry.code, keyword) | keyword_like(CodeEntry.name, keyword))
        rows = query.order_by(CodeEntry.code).limit(200).all()
        expected = [CodeEntryDetailOut.model_validate(r).model_dump(mode="json") for r in rows]
    assert expected, "前提：这份字典里得有条目"
    assert resp.content == json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# ------------------------------------------------------------------ 页面

PRELUDE = r"""
const ARGS = JSON.parse(process.argv[1]);
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const requests = [];
let inflight = 0;
const els = {};
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { textContent: "", className: "", innerHTML: "", value: "", dataset: {},
    classList: { add() {}, remove() {} } }); } };
globalThis.FormData = class { constructor(form) { this.fields = form.fields; } get(k) { return k in this.fields ? this.fields[k] : null; } };
/* 真 `api()` 要的几样：迁移期令牌（空 = Cookie 模式）、CSRF、登出（走到就是用例写错了） */
let token = "";
function csrfToken() { return ""; }
function logout() { throw new Error("不该走到登出"); }
/* 浏览器的 fetch：经管道转给真接口，响应头只垫 X-Total-Count（大小写不敏感，照 Headers.get） */
globalThis.fetch = async (path, init = {}) => {
  inflight += 1;
  try {
    const method = init.method || "GET";
    requests.push(`${method} ${path}`);
    process.stdout.write(JSON.stringify({ method, path, body: init.body ? JSON.parse(init.body) : null }) + "\n");
    const reply = JSON.parse((await lines.next()).value);
    return { status: reply.status, ok: reply.status < 400, json: async () => reply.body,
      headers: { get: (name) => (name.toLowerCase() === "x-total-count" ? reply.total : null) } };
  } finally { inflight -= 1; }
};
/* 检索栏的处理函数不回 promise：等管道上没有在途的请求、微任务排空 */
async function settle() {
  do { await new Promise((r) => setImmediate(r)); } while (inflight);
  await new Promise((r) => setImmediate(r));
}
const tableHtml = () => els["#dict-table"].innerHTML;
const titles = () => [...tableHtml().matchAll(/<h3>([^<]*)<\/h3>/g)].map((m) => m[1]);
const codes = () => [...tableHtml().matchAll(/<span class="tag">([^<]*)<\/span>/g)].map((m) => m[1]);
/* 在检索栏里填好、点「检索」 */
async function search(keyword) {
  els["#dict-search"].onsubmit({ preventDefault() {}, target: { fields: { keyword } } });
  await settle();
}
"""


def _function_source(source: str, head: str) -> str:
    """顶层函数原文：从 `head` 到它之后第一个顶格的 `}`。"""
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def run_page(client, headers: dict, steps: str, params: dict):
    """在 node 里加载 `api()` 与字典页、跑 `steps`（async 函数体，return 一个可 JSON 化的值）；请求以 `headers` 的身份转给真接口。"""
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    script = (
        PRELUDE + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n"
        + "".join(_function_source(core, head) for head in (
            "async function api(", "function table(", "function panel(", "function setMsg(", "async function renderDicts("))
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


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_字典页标题写明列不全_检索得到I10(client, admin):
    extra = [{"code": f"A00.P21733-{i:03d}", "name": f"P21733 诊断{i}"} for i in range(N_DIAGNOSIS_EXTRA)]
    resp = client.post("/api/dictionaries/diagnosis/import", headers=admin, json=extra)
    assert resp.status_code == 200 and resp.json()["imported"] == N_DIAGNOSIS_EXTRA, resp.text
    with SessionLocal() as db:
        total = db.query(CodeEntry).filter(CodeEntry.system_id == _system_id("diagnosis")).count()
    assert total > 200
    out = run_page(client, admin, """
      $("#dict-system").value = "diagnosis";   // 下拉的第一项
      await renderDicts();
      const before = { titles: titles(), codes: codes() };
      await search("I10");
      return { requests, before, after: { titles: titles(), codes: codes() } };
    """, {})
    before, after = out["before"], out["after"]
    # 缺省照旧取第一页，标题写明截断（修前标题不带数，看不出被截断了）；I10 排在这一页之外
    assert out["requests"][0] == "GET /api/dictionaries/diagnosis/entries"
    assert before["titles"] == [f"条目（已列 200 / 共 {total}）"]
    assert len(before["codes"]) == 200 and "I10" not in before["codes"]
    # 检索栏按编码查得到 I10（修前页面上没有检索栏）
    assert "GET /api/dictionaries/diagnosis/entries?keyword=I10" in out["requests"]
    assert "I10" in after["codes"]
    assert after["titles"] == [f"检索结果（{len(after['codes'])}）"]   # 没截断就只写条数


def test_字典类型缺种子时_空清单也带总数头(client, admin):
    """存量库未经启动初始化、缺了字典类型那一行：读路径照旧只读、回空清单，总数头也照样给（同一个端点别有时带头、有时不带）。"""
    with SessionLocal() as db:
        db.query(CodeSystem).filter(CodeSystem.code == "charge").delete()
        db.commit()
    try:
        resp = client.get("/api/dictionaries/charge/entries", headers=admin)
        assert resp.status_code == 200 and resp.json() == []
        assert resp.headers.get("X-Total-Count") == "0"   # 修前没有这个头
    finally:
        with SessionLocal() as db:
            db.add(CodeSystem(code="charge", name="收费"))
            db.commit()
