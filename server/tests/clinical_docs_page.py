"""住院临床文书页（`pages-mgmt.js::renderClinicalDocs`）原样拿到 node 里跑的夹具（P2-1767 起共用，第五十二批扫描 AP3）。

页面函数与它用到的 `table`、`panel`、`setMsg`、`pickedId`、`lineChart`（core.js）、`formJson`、`postAction`（pages-clinical.js）、
`NOTE_TYPES` 等常量（pages-public.js）、本页的模块级常量与 `vitalTimeMs`（pages-mgmt.js）都取自源文件原文（连同 `shared.js`），
只垫最小的 DOM（同 `inpatient_page.py`）：`document.querySelector` 按选择器给一个记得住 innerHTML / textContent / onsubmit /
onchange 的对象；`localStorage` 是一张内存表，初值取 `ARGS.storage`。

`submitForm` 照浏览器交表：只交页面上那张表单里**真有**的栏——给了表单里没有的栏就报错（修前没有的输入框，不能绕过页面直接
塞进请求里）；没给的栏照浏览器取缺省（下拉取 selected 项、没有就第一项，输入框取 value）；日期时间控件交给 `formJson` 换写法。
页面的请求经管道转给真接口：GET 照发，带 `withTotal` 的连同 X-Total-Count 一起交回（同 core.js 的 `api`）；写请求照发，把真回执
交回页面，失败照 core.js 的 `api` 抛 `errorText` 的话。`route()` 只计次（`ROUTED`）：要看重画后的页面，就再调一次
`renderClinicalDocs()`。
"""
import json
import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

PRELUDE = r"""
const ARGS = JSON.parse(process.argv[1]);
const posts = [];
const elements = {};
let ROUTED = 0;
let REPLIES = 0;
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const store = new Map(Object.entries(ARGS.storage || {}));
globalThis.localStorage = {
  getItem: (k) => (store.has(k) ? store.get(k) : null),
  setItem: (k, v) => { store.set(k, String(v)); },
  removeItem: (k) => { store.delete(k); },
};
/** 表单对象就是 `{字段名: 值}`：`entries` 给 `formJson`。 */
globalThis.FormData = class {
  constructor(form) { this.form = form; }
  get(k) { return k in this.form ? this.form[k] : null; }
  *entries() { for (const [k, v] of Object.entries(this.form)) if (typeof v !== "function") yield [k, v]; }
};
const element = () => ({ dataset: {}, innerHTML: "", textContent: "", className: "", value: "",
  classList: { add() {}, remove() {} } });
globalThis.document = {
  addEventListener() {}, removeEventListener() {}, cookie: "",
  querySelector(sel) { return (elements[sel] ||= element()); },
};
async function api(path, options = {}) {
  const method = options.method || "GET";
  const body = options.body ? JSON.parse(options.body) : null;
  if (method !== "GET") posts.push([method, path, body]);
  process.stdout.write(JSON.stringify({ method, path, body }) + "\n");
  const reply = JSON.parse((await lines.next()).value);
  REPLIES += 1;
  if (reply.status >= 400) {
    throw Object.assign(new Error(errorText(reply.body.detail, `请求失败(${reply.status})`)), { status: reply.status });
  }
  return options.withTotal ? { rows: reply.body, total: reply.total } : reply.body;
}
function route() { ROUTED += 1; }
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
/** 等到 `cond()` 成立（最多约 1 秒）。 */
async function until(cond) { for (let i = 0; i < 200 && !cond(); i++) await new Promise((r) => setTimeout(r, 5)); }
const pageHtml = () => elements["#page-body"].innerHTML;
const msgOf = (sel) => (elements[sel] ? elements[sel].textContent : "");
/** 页面上 `#id` 那张表单的 HTML（到 `</form>` 为止）；页面上没有这张表单就报错。 */
function formHtml(id) {
  const html = pageHtml();
  const start = html.indexOf(`<form class="inline" id="${id}">`);
  if (start < 0) throw new Error(`页面上没有表单 #${id}`);
  return html.slice(start, html.indexOf("</form>", start));
}
/** 照浏览器取一栏的缺省值：下拉取 selected 项（没有就第一项），输入框取 value（没有就空串）。 */
function defaultValue(html, name) {
  const select = new RegExp(`<select name="${name}"[^>]*>([\\s\\S]*?)</select>`).exec(html);
  if (select) {
    const options = [...select[1].matchAll(/<option value="([^"]*)"([^>]*)>/g)];
    const chosen = options.find((o) => /\bselected\b/.test(o[2])) || options[0];
    return chosen ? chosen[1] : "";
  }
  const input = new RegExp(`<input name="${name}"[^>]*?value="([^"]*)"`).exec(html);
  return input ? input[1] : "";
}
/** 提交页面上的 `#id` 表单：`fields` 是 `{字段名: 值}`（值一律字符串，同浏览器），只能是表单里真有的栏。等回执落定再返回。 */
async function submitForm(id, fields) {
  const html = formHtml(id);
  const names = [...html.matchAll(/<(?:input|select|textarea) name="([^"]+)"/g)].map((m) => m[1]);
  const extra = Object.keys(fields).filter((k) => !names.includes(k));
  if (extra.length) throw new Error(`表单 #${id} 里没有这几栏：${extra.join("、")}`);
  const target = {};
  for (const name of names) target[name] = name in fields ? String(fields[name]) : defaultValue(html, name);
  const dates = [...html.matchAll(/<input name="([^"]+)" type="datetime-local"/g)].map((m) => ({ name: m[1] }));
  target.querySelectorAll = (sel) => (sel === 'input[type="datetime-local"]' ? dates : []);
  const before = REPLIES;
  elements[`#${id}`].onsubmit({ preventDefault() {}, target });
  await until(() => REPLIES > before);
  await tick();
  await tick();
}
"""


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def function_source(source: str, head: str) -> str:
    """顶层函数原文：从 `head` 到它之后第一个顶格的 `}`。"""
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def const_source(source: str, name: str) -> str:
    """顶层常量原文：从 `const name = ` 到它之后第一个 `;` 行尾。"""
    start = source.index(f"const {name} = ")
    return source[start:source.index(";\n", start) + 2]


#: 本页的模块级常量（pages-mgmt.js）：页面函数读写它们
PAGE_CONSTS = ("HANDOVER_FILTER",)


def script(steps: str) -> str:
    core, clinical, public, mgmt = _read("core.js"), _read("pages-clinical.js"), _read("pages-public.js"), _read("pages-mgmt.js")
    return (
        PRELUDE + _read("shared.js") + "\n"
        + "".join(function_source(core, f"function {name}(") for name in ("table", "panel", "setMsg", "pickedId", "lineChart"))
        + function_source(clinical, "function formJson(") + function_source(clinical, "async function postAction(")
        + "".join(const_source(public, name) for name in
                  ("NOTE_TYPES", "PROGRESS_NOTE_DEFAULT", "NURSING_LEVELS", "INPATIENT_NURSING_DEFAULT"))
        + "".join(const_source(mgmt, name) for name in PAGE_CONSTS)
        + function_source(mgmt, "function vitalTimeMs(") + function_source(mgmt, "async function renderClinicalDocs(")
        + f"\n(async () => {{\n{steps}\n}})().then((r) => {{ process.stdout.write(JSON.stringify({{ result: r }}) + '\\n');"
        + " rl.close(); }, (e) => { console.error(e); process.exit(1); });\n"
    )


def run(client, headers, steps: str, params: dict | None = None, storage: dict | None = None):
    """在 node 里加载住院临床文书页的代码跑 `steps`——async 函数体，return 一个可 JSON 化的值；`params` 在 node 里是
    `ARGS.params`，`storage` 是 localStorage 的初值。页面的请求以 `headers` 的身份转给真接口（读写都照发）。

    返回 `(result, requests)`：`requests` 是页面依次发过的（方法, 地址）。"""
    payload = json.dumps({"params": params or {}, "storage": storage or {}}, ensure_ascii=False)
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
                return message["result"], requests
            requests.append((message["method"], message["path"]))
            assert len(requests) <= 80, f"请求停不下来：{requests}"
            resp = client.request(message["method"], message["path"], json=message["body"], headers=headers)
            total = resp.headers.get("X-Total-Count")
            reply = {"status": resp.status_code, "body": resp.json(), "total": None if total is None else int(total)}
            proc.stdin.write(json.dumps(reply, ensure_ascii=False) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
