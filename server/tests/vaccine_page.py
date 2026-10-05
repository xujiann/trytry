"""疫苗两页（`pages-clinical.js` 的 `renderVaccination` / `renderVaccineSupply`）原样拿到 node 里跑的夹具（P2-1499 起共用，
第四十四批扫描 AH1）。

照扫描复现脚本 `vac_page.py` / `r5_hint_page.py` 的 node 桩：两个页面函数，连同它们之前、之间的顶层声明（`vacAefiJump`、
`AEFI_OUTCOMES`），以及用到的 `table`、`panel`、`setMsg`、`formJson`、`postAction`，都取自源文件原文。页面发出的每个请求
（GET 与写请求）经管道转给真接口、以调用方给的身份请求（同 test_fund_prepayment_warning.py），接口改了什么页面就看到什么。
DOM 只垫最小的一层：
- 给一个元素赋 innerHTML，新 HTML 里带 id 的元素都换成新的（同 labqc_page.py 的缺省垫法）；下拉赋了选项，`value` 落到首项
  （浏览器的缺省选中）；
- `route()` 照真页面整页重画的效果，把已有的元素全部作废——写在重画之前的回执会被冲掉（P2-1013）；
- `nav(id)` 只记下去哪一页，换页由用例自己 `await goto(renderXxx)`（先作废全部元素、再画那一页）；
- 表单用 `form({ 字段: 值 })` 垫：`FormData` 读它的字段，`formJson` 原文照常转数字。

`steps` 是 async 函数体，return 一个可 JSON 化的值；`ARGS.params` 是调用方给的参数。`overrides` 按路径改写个别 GET 的
回放（`{路径: (状态码, 响应体)}`，如机构清单缺一家、取失败），其余照转真接口。
"""
import json
import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

PRELUDE = r"""
const ARGS = JSON.parse(process.argv[1]);
let els = {};
let routed = 0;
let inflight = 0;
const navs = [];
const alerts = [];
const requests = [];
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
function element() {
  return { textContent: "", className: "", value: "", html: "", style: {}, listeners: {}, scrolled: false,
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener(type, fn) { this.listeners[type] = fn; },
    scrollIntoView() { this.scrolled = true; },
    get innerHTML() { return this.html; },
    set innerHTML(html) {
      this.html = html;
      for (const m of html.matchAll(/\bid="([^"]+)"/g)) delete els["#" + m[1]];
      if (html.includes("<option")) this.value = (html.match(/<option value="([^"]*)"/) || ["", ""])[1];
    } };
}
globalThis.document = { addEventListener() {}, cookie: "", querySelector(sel) { return (els[sel] ||= element()); },
  querySelectorAll() { return []; } };
globalThis.alert = (m) => alerts.push(String(m));
globalThis.FormData = class {
  constructor(form) { this.fields = (form && form.fields) || {}; }
  get(k) { return k in this.fields ? this.fields[k] : null; }
  entries() { return Object.entries(this.fields)[Symbol.iterator](); }
};
const form = (fields) => ({ fields, querySelectorAll() { return []; } });
async function api(path, opts = {}) {
  const method = opts.method || "GET";
  const body = opts.body ? JSON.parse(opts.body) : null;
  requests.push([method, path]);
  inflight += 1;
  process.stdout.write(JSON.stringify({ method, path, body }) + "\n");
  const reply = JSON.parse((await lines.next()).value);
  inflight -= 1;
  if (reply.status >= 400) {
    const detail = reply.body && reply.body.detail;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return reply.body;
}
async function settled() { while (inflight) await new Promise((resolve) => setTimeout(resolve, 5)); }
function route() { routed += 1; els = {}; }
function nav(id) { navs.push(id); }
async function goto(render) { els = {}; await render(); }
async function spdModal() { return ARGS.modal; }
async function openPrintPage() {}
const text = (html) => html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
"""


def _function_source(source: str, head: str) -> str:
    """顶层函数原文：从 `head` 到它之后第一个顶格的 `}`。"""
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def run(client, headers: dict, steps: str, params: dict | None = None, modal=None, overrides: dict | None = None):
    """在 node 里加载疫苗两页、跑 `steps`；页面的请求以 `headers` 的身份转给 `client`（`overrides` 里的 GET 除外）。"""
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    page = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    start = page.index("async function renderVaccination(")
    vaccination = _function_source(page, "async function renderVaccination(")
    supply_start = page.index("async function renderVaccineSupply(")
    script = (
        PRELUDE + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n"
        + "".join(_function_source(core, f"function {name}(") for name in ("table", "panel", "setMsg"))
        + _function_source(page, "function formJson(") + _function_source(page, "async function postAction(")
        + page[page.rindex("\n}\n", 0, start) + 3:start]           # 页面函数之前的顶层声明
        + vaccination + page[start + len(vaccination):supply_start]   # 两个页面函数之间的顶层声明
        + _function_source(page, "async function renderVaccineSupply(")
        + f"\n(async () => {{\n{steps}\n}})().then("
        + "(r) => { process.stdout.write(JSON.stringify({ result: r }) + '\\n'); rl.close(); },"
        + " (e) => { console.error(e); process.exit(1); });\n"
    )
    payload = json.dumps({"params": params or {}, "modal": modal}, ensure_ascii=False)
    proc = subprocess.Popen(["node", "-e", script, payload], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "result" in message:
                return message["result"]
            if message["method"] == "GET" and message["path"] in (overrides or {}):
                status, body = overrides[message["path"]]
            else:
                resp = client.request(message["method"], message["path"], headers=headers, json=message["body"])
                status, body = resp.status_code, resp.json()
            proc.stdin.write(json.dumps({"status": status, "body": body}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
