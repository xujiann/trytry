"""慢病、随访中心、专病三页原样拿到 node 里跑的夹具（P2-1541 起共用，第四十五批扫描 AI2）。

页面函数（`core.js::renderChronic`、`pages-mgmt.js::renderFollowups` / `renderDiseasePrograms`）与它们用到的 `spdModal`、
`table`、`panel`、`setMsg`、`currentRole`、`pickedId`、`formJson`、`postAction` 都取自源文件原文，只垫最小的 DOM（同
`cssd_medwaste_page.py`）：`document.querySelector` 按选择器给一个记得住 innerHTML / onsubmit / onclick 的对象；`spdModal`
建的遮罩记下它的 HTML，`submitModal` 照浏览器的规矩交表（`<select>` 没有 selected 项时取第一项）。

页面的请求经管道转给真接口（同 `test_materials_cssd_page_reach._run_page_fetch`）：GET 一律照发；写请求记下（方法、地址、
请求体），缺省不发、回 `{}`，`send_writes=True` 时照发、把真回执（或报错）交回页面。`alert` 的文字记在 `alerts` 里。
"""
import json
import re
import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

PRELUDE = r"""
const ARGS = JSON.parse(process.argv[1]);
const posts = [];
const alerts = [];
const elements = {};
const overlays = [];
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
globalThis.localStorage = { getItem: (k) => (k === "medplat_role" ? ARGS.role : (ARGS.storage[k] ?? null)),
  setItem() {}, removeItem() {} };
globalThis.alert = (text) => { alerts.push(String(text)); };
/** 表单对象就是 `{字段名: 值}`：`get` 给页面的 `new FormData(e.target)`，`entries` 给 `formJson`。 */
globalThis.FormData = class {
  constructor(form) { this.form = form; }
  get(k) { return k in this.form ? this.form[k] : null; }
  *entries() { for (const [k, v] of Object.entries(this.form)) if (typeof v !== "function") yield [k, v]; }
};
const element = () => ({ dataset: {}, innerHTML: "", textContent: "", className: "", children: [],
  classList: { add() {}, remove() {} }, appendChild(child) { this.children.push(child); } });
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
  return reply.data;
}
function route() {}
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
/** 提交页面上的一张表单：`fields` 是 `{字段名: 值}`（值一律字符串，同浏览器）。等提交处理落定。 */
async function submitForm(sel, fields) {
  await elements[sel].onsubmit({ preventDefault() {}, target: { ...fields, querySelectorAll: () => [] } });
  await tick();
}
/** 点页面上一个按钮（各页的 onclick 读 `e.target.dataset`）。返回点击处理的 Promise——开了框就要等框交了才落定。 */
function click(dataset) {
  return elements["#page-body"].onclick({ target: { dataset, closest: () => null } });
}
const lastModal = () => overlays[overlays.length - 1] || null;
/** 一段 HTML 里每个 `<select>`：`{name: {required, options: [{value, label, selected}]}}`。 */
function selectsIn(html) {
  const out = {};
  for (const m of html.matchAll(/<select name="([^"]+)"([^>]*)>([\s\S]*?)<\/select>/g)) {
    out[m[1]] = { required: /\brequired\b/.test(m[2]), options: [...m[3].matchAll(/<option value="([^"]*)"( selected)?>([^<]*)<\/option>/g)]
      .map((o) => ({ value: o[1], label: o[3].trim(), selected: Boolean(o[2]) })) };
  }
  return out;
}
/** 某张表单（`id="…"`）里的 `<select>`，同 `selectsIn`。 */
function formSelects(html, formId) {
  const form = html.match(new RegExp(`<form[^>]*\\bid="${formId}"[^>]*>([\\s\\S]*?)</form>`));
  if (!form) throw new Error(`页面上没有 #${formId}`);
  return selectsIn(form[1]);
}
/** 不动下拉时浏览器交的值：selected 项，没有就是第一项。 */
function untouched(select) {
  const chosen = select.options.find((o) => o.selected) || select.options[0];
  return chosen ? chosen.value : "";
}
/** 照浏览器交框：`picks` 里给了的按给的，没给的 `<select>` 按 `untouched`，输入框取 value。 */
async function submitModal(overlay, picks = {}) {
  const target = {};
  for (const [name, select] of Object.entries(selectsIn(overlay.html))) {
    target[name] = { value: name in picks ? String(picks[name]) : untouched(select) };
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
const modalMsg = (overlay) => { const el = overlay.querySelector("[data-modal-msg]"); return el ? el.textContent : null; };
const pageHtml = () => elements["#page-body"].innerHTML;
const msgOf = (sel) => (elements[sel] ? elements[sel].textContent : "");
"""


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def function_source(source: str, head: str) -> str:
    """顶层函数原文：从 `head` 到它之后第一个顶格的 `}`。"""
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def _top_line(source: str, head: str) -> str:
    """顶层单行声明的原文（`const NAME = …;`、`let NAME = …;`、单行函数）。"""
    return re.search(rf"^{re.escape(head)}.*$", source, re.M).group(0) + "\n"


def script(steps: str) -> str:
    core, mgmt = _read("core.js"), _read("pages-mgmt.js")
    clinical, spd = _read("pages-clinical.js"), _read("pages-spd.js")
    return (
        PRELUDE + _read("shared.js") + "\n"
        + function_source(spd, "function spdModal(")
        + function_source(core, "function table(") + function_source(core, "function panel(")
        + function_source(core, "function setMsg(") + function_source(core, "function pickedId(")
        + _top_line(core, "function currentRole()")
        + function_source(clinical, "function formJson(") + function_source(clinical, "async function postAction(")
        + "".join(_top_line(core, head) for head in (
            "let DISEASES = ", "const TYPE_STATUS = ", "const RISK_TREND = ", "const RISK_LEVEL = "))
        + function_source(core, "async function renderChronic(")
        + function_source(mgmt, "async function renderFollowups(")
        + function_source(mgmt, "async function renderDiseasePrograms(")
        + f"\n(async () => {{\n{steps}\n}})().then((r) => {{ process.stdout.write(JSON.stringify({{ result: r }}) + '\\n');"
        + " rl.close(); }, (e) => { console.error(e); process.exit(1); });\n"
    )


def _detail(resp) -> str:
    try:
        detail = resp.json().get("detail")
    except ValueError:
        return resp.text
    return detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)


def run(client, headers, role: str, steps: str, params: dict | None = None, *, send_writes: bool = False,
        storage: dict | None = None):
    """在 node 里加载三页的代码，以 `role`（`currentRole()` 读到的角色）跑 `steps`——async 函数体，return 一个可 JSON 化的值；
    `params` 在 node 里是 `ARGS.params`，`storage` 是页面从 localStorage 读到的其余键（如 `medplat_program`）。页面的请求以
    `headers` 的身份转给真接口（写请求见模块说明）。"""
    payload = json.dumps({"role": role, "params": params or {}, "storage": storage or {}}, ensure_ascii=False)
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
                reply = ({"data": resp.json()} if resp.status_code < 400
                         else {"error": _detail(resp), "status": resp.status_code})
            else:
                reply = {"data": {}}
            proc.stdin.write(json.dumps(reply, ensure_ascii=False) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
