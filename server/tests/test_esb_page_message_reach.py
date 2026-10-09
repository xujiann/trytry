"""集成平台页的消息表只取最新 50 条、不读总数也不能翻页：更早的「失败待重试」在页面上没有行，点不了重试（P2-1729，
第五十一批扫描 AO1-3）。

修前（c67fa3b 实测，扫描脚本 r6_walk.py 的 (e) 段）：`renderEsb` 的 `drawMessages` 写死 `limit: "50"`、不读 X-Total-Count，
执行记录（`drawRuns`）同样 `limit=50`。62 条失败消息时，页面那一调回 50 行、X-Total-Count 62：最早失败、最该先处理的那批
在页面上没有行，点不了「消费/重试」，标题也看不出被截断；按状态筛「失败待重试」照样只回编号最大的 50 条。入站失败只能手工
重试（P2-709），一次故障就是几十上百条。后端清单早就发 X-Total-Count、收 offset。

修后（后端不动）：消息表与执行记录都经 `api(…, { withTotal: true })` 读总数，列不全时标题写「已列 N / 共 M」（同 P2-1547 /
P2-1693）；筛「失败待重试」时按状态 `fetchAllPages` 续页取全（失败积压要逐条处理），其余状态照旧只取最新一页。

页面原样拿到 node 里跑：`renderEsb` 与 `api()`、`table()`、`panel()`、`setMsg()` 都取自源文件原文，只垫最小的 DOM；
`fetch` 经管道转给真接口（以管理员身份），响应头照真接口给——`withTotal` 读到的是真的 X-Total-Count。
"""
import json
import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import EsbFlowRun, EsbMessage, utcnow

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面")

#: 失败待重试的条数：比页面一页（50）多 12 条，最早失败的那 12 条挤出缺省那一页
FAILED = 62
#: 失败之后又进来的待处理消息（编号最大，排在缺省那一页最前）
QUEUED = 2
#: 一条编排的执行记录条数：比执行记录一页（50）多两条
RUNS = 52


@pytest.fixture(scope="module")
def world(client, admin):
    """一个入站接入方：62 条失败待重试（透传消息，重试一次就成功）、其后 2 条待处理、1 条成功（挂 52 条执行记录）。"""
    endpoint = client.post("/api/esb/endpoints", headers=admin, json={
        "code": "P21729_HIS", "name": "P21729 县医院", "system_type": "his", "rate_limit_per_min": 1000})
    assert endpoint.status_code == 201, endpoint.text
    flow = client.post("/api/esb/flows", headers=admin, json={
        "code": "P21729_F", "name": "P21729 校验", "steps": [{"type": "validate", "config": {"required": ["seq"]}}]})
    assert flow.status_code == 201, flow.text
    endpoint_id, flow_id = endpoint.json()["id"], flow.json()["id"]
    with SessionLocal() as db:   # 逐条走接口太慢，直接落库造量（形状与第一次手工消费失败之后一样）
        failed = [EsbMessage(endpoint_id=endpoint_id, msg_type="generic", payload={"seq": i}, status="failed",
                             retry_count=1, max_retries=3, last_error="第一次处理失败",
                             next_retry_at=utcnow() + timedelta(minutes=1)) for i in range(FAILED)]
        db.add_all(failed)
        db.flush()
        queued = [EsbMessage(endpoint_id=endpoint_id, msg_type="generic", payload={"late": i}) for i in range(QUEUED)]
        done = EsbMessage(endpoint_id=endpoint_id, msg_type="generic", payload={"done": 1}, status="succeeded")
        db.add_all([*queued, done])
        db.flush()
        db.add_all([EsbFlowRun(flow_id=flow_id, message_id=done.id, status="succeeded", step_results=[], error="")
                    for _ in range(RUNS)])
        ids = {"failed": [m.id for m in failed], "queued": [m.id for m in queued], "done": done.id}
        db.commit()
    return {**ids, "flow": flow_id, "total": FAILED + QUEUED + 1}


PRELUDE = r"""
const ARGS = JSON.parse(process.argv[1]);
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const requests = [];
const els = {};
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { textContent: "", className: "", innerHTML: "", dataset: {}, fields: {} }); } };
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
const listHtml = () => els["#esb-messages"].innerHTML;
const heads = (html) => [...html.matchAll(/<h3[^>]*>([^<]*)<\/h3>/g)].map((m) => m[1]);
const listedIds = () => [...listHtml().matchAll(/data-esbpayload="(\d+)"/g)].map((m) => Number(m[1]));
/* 在消息队列的筛选栏里选好、点「查询」 */
async function filter(fields) {
  els["#esb-msg-filter"].fields = fields;
  await els["#esb-msg-filter"].onsubmit({ preventDefault() {} });
}
/* 点页面上某个按钮（按钮的 data-* 即 dataset） */
async function click(dataset) {
  await els["#page-body"].onclick({ target: { dataset } });
}
"""


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _function_source(source: str, head: str) -> str:
    """顶层函数原文：从 `head` 到它之后第一个顶格的 `}`。"""
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def run_page(client, headers: dict, steps: str, params: dict | None = None):
    """在 node 里加载 `api()` 与集成平台页、跑 `steps`（async 函数体，return 一个可 JSON 化的值）；请求以 `headers` 的身份转给真接口。"""
    core, public = _read("core.js"), _read("pages-public.js")
    script = (
        PRELUDE + _read("shared.js") + "\n"
        + "".join(_function_source(core, head) for head in (
            "async function api(", "function table(", "function panel(", "function setMsg("))
        + public[public.index("const ESB_SYSTEMS = "):public.index("async function renderEsb(")]   # 页面用的四个常量
        + _function_source(public, "async function renderEsb(")
        + f"\n(async () => {{\n{steps}\n}})().then("
        + "(r) => { process.stdout.write(JSON.stringify({ result: r }) + '\\n'); rl.close(); },"
        + " (e) => { console.error(e); process.exit(1); });\n"
    )
    proc = subprocess.Popen(["node", "-e", script, json.dumps(params or {})], stdin=subprocess.PIPE,
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


def test_缺省只列最新一页_标题写明已列与共几条(client, admin, world):
    out = run_page(client, admin, """
      await renderEsb();
      const first = { heads: heads(listHtml()), ids: listedIds(), html: listHtml() };
      await filter({ status: "queued", endpoint_id: "" });
      return { requests, first, queued: { heads: heads(listHtml()), ids: listedIds(), html: listHtml() } };
    """)
    assert out["requests"] == ["GET /api/esb/stats", "GET /api/esb/endpoints", "GET /api/esb/flows",
                               "GET /api/esb/messages?limit=50", "GET /api/esb/messages?limit=50&status=queued"]
    first = out["first"]
    assert f"消息（已列 50 / 共 {world['total']}）" in first["heads"], first["heads"]   # 修前没有标题，看不出被截断了
    assert "续页列全" in first["html"]
    assert len(first["ids"]) == 50 and world["failed"][0] not in first["ids"]   # 前提：最早失败的那条在缺省那一页之外
    queued = out["queued"]
    assert f"消息（{QUEUED}）" in queued["heads"] and sorted(queued["ids"]) == world["queued"]   # 列全了就只写条数
    assert "续页列全" not in queued["html"]


def test_筛失败待重试_续页取全_最早失败的那条能点消费重试(client, admin, world):
    first = world["failed"][0]
    out = run_page(client, admin, """
      await renderEsb();
      await filter({ status: "failed", endpoint_id: "" });
      const listed = { heads: heads(listHtml()), ids: listedIds(), html: listHtml() };
      await click({ esbproc: String(ARGS.first) });
      return { requests, listed, msg: els["#esb-msg"].textContent, after: { heads: heads(listHtml()), ids: listedIds() } };
    """, {"first": first})
    listed = out["listed"]
    assert f'data-esbproc="{first}">消费/重试</button>' in listed["html"]   # 修前「最新 50 条」：最早那 12 条不在
    assert sorted(listed["ids"]) == world["failed"] and f"消息（{FAILED}）" in listed["heads"], listed["heads"]
    all_failed = "GET /api/esb/messages?status=failed&limit=500&offset=0"
    assert out["requests"][4:] == [all_failed, f"POST /api/esb/messages/{first}/process", all_failed], out["requests"]
    assert out["msg"].startswith(f"消息 {first} → 成功：透传消息已处理"), out["msg"]
    assert f"消息（{FAILED - 1}）" in out["after"]["heads"] and first not in out["after"]["ids"]
    with SessionLocal() as db:
        assert db.get(EsbMessage, first).status == "succeeded"


def test_执行记录过一页_标题写明已列与共几条(client, admin, world):
    out = run_page(client, admin, """
      await renderEsb();
      await click({ esbruns: String(ARGS.flow) });
      return { requests, runs: els["#esb-runs"].innerHTML };
    """, {"flow": world["flow"]})
    assert out["requests"][-1] == f"GET /api/esb/flow-runs?flow_id={world['flow']}&limit=50"
    runs_heads = re.findall(r"<h3[^>]*>([^<]*)</h3>", out["runs"])
    assert f"流程 {world['flow']} 的执行记录（已列 50 / 共 {RUNS}）" in runs_heads, runs_heads   # 修前不读总数，看不出前面还有
