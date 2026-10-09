"""住院管理页（`pages-clinical.js::renderInpatient`）原样拿到 node 里跑的夹具（P2-1693 起共用，第五十批扫描 AN4）。

页面函数与它用到的 `spdModal`、`table`、`panel`、`setMsg`、`formJson`、`postAction` 都取自源文件原文（连同 `shared.js`），
只垫最小的 DOM（同 `chronic_followup_pages.py`）：`document.querySelector` 按选择器给一个记得住 innerHTML / textContent /
onsubmit / onclick 的对象；`spdModal` 建的遮罩记下它的 HTML，`submitModal` 照浏览器的规矩交表、`cancelModal` 点「取消」。

页面的请求经管道转给真接口：GET 一律照发（带 `withTotal` 的连同 X-Total-Count 一起交回，同 core.js 的 `api`）；写请求记下
（方法、地址、请求体），缺省不发、回 `{}`，`send_writes=True` 时照发、把真回执（或报错）交回页面。`route()` 只计次（`ROUTED`）。
"""
import json
import re
import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

PRELUDE = r"""
const ARGS = JSON.parse(process.argv[1]);
const posts = [];
const elements = {};
const overlays = [];
let ROUTED = 0;
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
/** 表单对象就是 `{字段名: 值}`：`entries` 给 `formJson`。 */
globalThis.FormData = class {
  constructor(form) { this.form = form; }
  get(k) { return k in this.form ? this.form[k] : null; }
  *entries() { for (const [k, v] of Object.entries(this.form)) if (typeof v !== "function") yield [k, v]; }
};
const element = () => ({ dataset: {}, innerHTML: "", textContent: "", className: "",
  classList: { add() {}, remove() {} } });
globalThis.document = {
  addEventListener() {}, removeEventListener() {}, cookie: "",
  querySelector(sel) { return (elements[sel] ||= element()); },
  body: { appendChild(node) { overlays.push(node); } },
  createElement() {
    const nodes = {};
    return {
      style: {}, dataset: {}, html: "", removed: false,
      set innerHTML(h) { this.html = h; }, get innerHTML() { return this.html; },
      remove() { this.removed = true; }, addEventListener() {},
      querySelector(sel) {
        if (sel === "[data-modal-msg]" && !this.html.includes("data-modal-msg")) return null;
        return (nodes[sel] ||= { focus() {}, dataset: {}, textContent: "", disabled: false });
      },
    };
  },
};
async function api(path, options = {}) {
  const method = options.method || "GET";
  const body = options.body ? JSON.parse(options.body) : null;
  if (method !== "GET") posts.push([method, path, body]);
  process.stdout.write(JSON.stringify({ method, path, body }) + "\n");
  const reply = JSON.parse((await lines.next()).value);
  if ("error" in reply) throw Object.assign(new Error(reply.error), { status: reply.status });
  return options.withTotal ? { rows: reply.data, total: reply.total } : reply.data;
}
function route() { ROUTED += 1; }
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
/** 等到 `cond()` 成立（最多约 1 秒）：同步的提交处理（走 `postAction`）不回 Promise，等它的请求往返落定再收工。 */
async function until(cond) { for (let i = 0; i < 200 && !cond(); i++) await new Promise((r) => setTimeout(r, 5)); }
/** 提交页面上的一张表单：`fields` 是 `{字段名: 值}`（值一律字符串，同浏览器）。等提交处理落定。 */
async function submitForm(sel, fields) {
  await elements[sel].onsubmit({ preventDefault() {}, target: { ...fields, querySelectorAll: () => [] } });
  await tick();
}
/** 点页面上一个按钮（`onclick` 读 `e.target.dataset`）。返回点击处理的 Promise——开了框就要等框交了 / 取消了才落定。 */
function click(dataset) {
  return elements["#page-body"].onclick({ target: { dataset } });
}
const lastModal = () => overlays[overlays.length - 1] || null;
/** 一段 HTML 里每个 `<select>`：`{name: [{value, label, selected}]}`。 */
function selectsIn(html) {
  const out = {};
  for (const m of html.matchAll(/<select name="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)) {
    out[m[1]] = [...m[2].matchAll(/<option value="([^"]*)"( selected)?>([^<]*)<\/option>/g)]
      .map((o) => ({ value: o[1], label: o[3].trim(), selected: Boolean(o[2]) }));
  }
  return out;
}
/** 照浏览器交框：`picks` 里给了的按给的，没给的 `<select>` 取 selected 项（没有就第一项），输入框取 value。 */
async function submitModal(overlay, picks = {}) {
  const target = {};
  for (const [name, options] of Object.entries(selectsIn(overlay.html))) {
    const chosen = options.find((o) => o.selected) || options[0];
    target[name] = { value: name in picks ? String(picks[name]) : (chosen ? chosen.value : "") };
  }
  for (const m of overlay.html.matchAll(/<input name="([^"]+)"[^>]*?value="([^"]*)"/g)) {
    target[m[1]] = { value: m[1] in picks ? String(picks[m[1]]) : m[2] };
  }
  for (const m of overlay.html.matchAll(/<textarea name="([^"]+)"[^>]*>([^<]*)<\/textarea>/g)) {
    target[m[1]] = { value: m[1] in picks ? String(picks[m[1]]) : m[2] };
  }
  await overlay.querySelector("form").onsubmit({ preventDefault() {}, target });
  await tick();
}
/** 点框里的「取消」。 */
async function cancelModal(overlay) {
  overlay.querySelector("[data-cancel]").onclick();
  await tick();
}
const pageHtml = () => elements["#page-body"].innerHTML;
const htmlOf = (sel) => (elements[sel] ? elements[sel].innerHTML : "");
const msgOf = (sel) => (elements[sel] ? elements[sel].textContent : "");
"""


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def function_source(source: str, head: str) -> str:
    """顶层函数原文：从 `head` 到它之后第一个顶格的 `}`。"""
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def script(steps: str) -> str:
    core, clinical, spd = _read("core.js"), _read("pages-clinical.js"), _read("pages-spd.js")
    return (
        PRELUDE + _read("shared.js") + "\n"
        + function_source(spd, "function spdModal(")
        + function_source(core, "function table(") + function_source(core, "function panel(")
        + function_source(core, "function setMsg(")
        + function_source(clinical, "function formJson(") + function_source(clinical, "async function postAction(")
        + function_source(clinical, "async function renderInpatient(")
        + f"\n(async () => {{\n{steps}\n}})().then((r) => {{ process.stdout.write(JSON.stringify({{ result: r }}) + '\\n');"
        + " rl.close(); }, (e) => { console.error(e); process.exit(1); });\n"
    )


def _detail(resp) -> str:
    try:
        detail = resp.json().get("detail")
    except ValueError:
        return resp.text
    return detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)


def run(client, headers, steps: str, params: dict | None = None, *, send_writes: bool = False):
    """在 node 里加载住院管理页的代码跑 `steps`——async 函数体，return 一个可 JSON 化的值；`params` 在 node 里是
    `ARGS.params`。页面的请求以 `headers` 的身份转给真接口（写请求见模块说明）。"""
    payload = json.dumps({"params": params or {}}, ensure_ascii=False)
    proc = subprocess.Popen(["node", "-e", script(steps), payload], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    requests = []
    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                proc.wait(timeout=30)
                raise AssertionError(f"node 没给出结果就退出了：{proc.stderr.read()}")
            message = json.loads(line)
            if "result" in message:
                return message["result"]
            requests.append((message["method"], message["path"]))
            assert len(requests) <= 50, f"请求停不下来：{requests}"
            if message["method"] == "GET" or send_writes:
                resp = client.request(message["method"], message["path"], json=message["body"], headers=headers)
                total = resp.headers.get("X-Total-Count")
                reply = ({"data": resp.json(), "total": None if total is None else int(total)} if resp.status_code < 400
                         else {"error": _detail(resp), "status": resp.status_code})
            else:
                reply = {"data": {}}
            proc.stdin.write(json.dumps(reply, ensure_ascii=False) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def selects_in(html: str) -> dict:
    """一段 HTML 里每个 `<select>`：`{name: [(value, label), …]}`。"""
    out = {}
    for m in re.finditer(r'<select name="([^"]+)"[^>]*>([\s\S]*?)</select>', html):
        out[m.group(1)] = re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', m.group(2))
    return out
