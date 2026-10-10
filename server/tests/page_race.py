"""页面原样拿到 node 里跑、能扣住个别回包的夹具（P2-1793 起共用，第五十三批扫描 AQ2）。

照 `inpatient_page.py` 的桩，多一样：先发的请求可以后到。页面每发一个请求带一个编号写到 stdout，python 立刻转给真接口
（或调用方给的 `responder`）、把回执按编号写回；node 收到回执时，地址命中 `hold(片段)` 登记过的先压着不交给页面，等步骤里
`await release(片段)` 再放行——「先点甲、立刻点乙，甲的回包晚到」这种先后是确定的。`idle()` 等到没被扣住的请求都已交回、
页面的后续代码跑完。

只垫最小的 DOM：
- `document.querySelector` 按选择器给一个记得住 innerHTML / textContent / value / on* 处理的元素；读它没有的属性（表单控件
  `form.overdue`）给一个子元素。元素自带的状态都不可枚举——`new FormData(元素)` 只枚举步骤里写上去的字段（`form.patient_id = "1"`），
  提交事件的 target 也可以直接是 `{字段: 值}`；
- `spdModal` 建的遮罩记下它的 HTML，`submitModal` 照浏览器的规矩交框（`<select>` 没有 selected 项时取第一项），`cancelModal` 点取消；
- `click(dataset)` 点 `#page-body` 上的按钮：`e.target.dataset` 与 `e.target.closest("[data-…]")` 两种写法都认。

页面的写请求记进 `posts`（方法、地址、请求体），照样转给接口；`route()` 只计次（`ROUTED`）。整份 `pages-spd.js` 塞进 `node -e`
会超过单个命令行参数的上限，脚本写进临时文件再跑。
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

PRELUDE = r"""
const ARGS = JSON.parse(process.argv[2]);
const elements = {};
const overlays = [];
const posts = [];
const ALERTS = [];
let ROUTED = 0;
const rl = require("readline").createInterface({ input: process.stdin });
const pending = new Map();
const holds = [];
let nextId = 0;
rl.on("line", (line) => {
  const msg = JSON.parse(line);
  const p = pending.get(msg.id);
  if (!p) return;
  pending.delete(msg.id);
  const settle = () => {
    if (msg.status >= 400) {
      p.reject(Object.assign(new Error(errorText(msg.body && msg.body.detail, `请求失败(${msg.status})`)),
        { status: msg.status }));
    } else p.resolve(p.withTotal ? { rows: msg.body, total: msg.total } : msg.body);
  };
  const h = holds.find((x) => x.active && hit(x.pattern, p.path));
  if (h) h.queue.push(settle); else settle();
});
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.sessionStorage = globalThis.localStorage;
globalThis.alert = (text) => { ALERTS.push(String(text)); };
globalThis.confirm = () => true;
globalThis.window = { open() { return null; }, scrollTo() {} };
globalThis.location = { hash: "", pathname: "/", search: "" };
globalThis.history = { replaceState() {} };
/** 表单只读字符串 / 数字字段：提交事件的 target 是 `{字段: 值}`，`$()` 取到的元素只有步骤写上去的字段可枚举。 */
globalThis.FormData = class {
  constructor(form) { this.form = form || {}; }
  get(k) { const v = this.form[k]; return typeof v === "string" || typeof v === "number" ? String(v) : null; }
  *entries() {
    for (const [k, v] of Object.entries(this.form)) if (typeof v === "string" || typeof v === "number") yield [k, String(v)];
  }
  append() {}
};
function makeEl(sel) {
  const kids = {};
  const cls = new Set();
  const el = {};
  const own = (key, value) => Object.defineProperty(el, key, { value, writable: true, enumerable: false, configurable: true });
  for (const [key, value] of Object.entries({ sel, dataset: {}, html: "", textContent: "", className: "", value: "",
    style: {}, disabled: false, checked: false, listeners: {} })) own(key, value);
  Object.defineProperty(el, "innerHTML", { get() { return this.html; }, set(h) { this.html = String(h); },
    enumerable: false, configurable: true });
  own("classList", { add: (c) => cls.add(c), remove: (c) => cls.delete(c), contains: (c) => cls.has(c),
    toggle: (c, on = !cls.has(c)) => { if (on) cls.add(c); else cls.delete(c); return on; } });
  own("addEventListener", function (type, fn) { this.listeners[type] = fn; });
  own("querySelector", (s) => (kids["?" + s] ||= makeEl(`${sel} ${s}`)));
  own("querySelectorAll", () => []);
  own("closest", () => null);
  own("insertAdjacentHTML", function (_, h) { this.html += h; });
  for (const key of ["focus", "scrollIntoView", "reset", "remove"]) own(key, () => {});
  return new Proxy(el, {
    get(target, prop, receiver) {
      if (prop in target || typeof prop === "symbol" || prop === "then" || prop === "toJSON") {
        return Reflect.get(target, prop, receiver);
      }
      return (kids[prop] ||= makeEl(`${sel}.${String(prop)}`));
    },
  });
}
globalThis.document = {
  cookie: "", addEventListener() {}, removeEventListener() {},
  querySelector: (sel) => (elements[sel] ||= makeEl(sel)),
  querySelectorAll: () => [],
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
function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const body = options.body ? JSON.parse(options.body) : null;
  if (method !== "GET") posts.push([method, path, body]);
  const id = ++nextId;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject, path, withTotal: Boolean(options.withTotal) });
    process.stdout.write(JSON.stringify({ id, method, path, body }) + "\n");
  });
}
function route() { ROUTED += 1; }
function nav() {}
function pollTodos() {}
/** 片段：字符串按「地址以它结尾」认（免得 `/records/1/context` 吃掉 `/records/11/context`），正则按 test 认。 */
function hit(pattern, path) { return pattern instanceof RegExp ? pattern.test(path) : path.endsWith(pattern); }
/** 从此刻起，地址命中片段的回执先压着（`release` 放行）。 */
function hold(pattern) { holds.push({ pattern, active: true, queue: [] }); }
/** 放行压着的回执、等页面把后续代码跑完；返回放行了几个。 */
async function release(pattern) {
  const h = holds.find((x) => x.active && String(x.pattern) === String(pattern));
  if (!h) throw new Error("没有扣住 " + pattern);
  h.active = false;
  for (const settle of h.queue) settle();
  await idle();
  return h.queue.length;
}
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
/** 等到没被扣住的请求都已交回、页面后续代码跑完（连续 6 拍没有在途请求）。 */
async function idle() {
  let quiet = 0;
  for (let i = 0; i < 4000; i++) {
    await sleep(5);
    quiet = pending.size === 0 ? quiet + 1 : 0;
    if (quiet >= 6) return;
  }
  throw new Error("idle 超时，在途：" + [...pending.values()].map((p) => p.path).join(", "));
}
/** 点 `#page-body` 上的一个按钮：返回点击处理的 Promise（开了框要等框交了 / 取消了才落定，步骤里别 await 它）。 */
function click(dataset) {
  const target = {
    dataset,
    closest(sel) {
      const m = /^\[data-([\w-]+)\]$/.exec(sel);
      if (!m) return null;
      return m[1].replace(/-([a-z])/g, (_, c) => c.toUpperCase()) in dataset ? target : null;
    },
  };
  return elements["#page-body"].onclick({ target });
}
/** 提交页面上的一张表单：`fields` 是 `{字段名: 值}`。返回提交处理的 Promise（同 click）。 */
function submitForm(sel, fields) {
  return elements[sel].onsubmit({ preventDefault() {}, target: { ...fields, querySelectorAll: () => [] } });
}
function selectsIn(html) {
  const out = {};
  for (const m of html.matchAll(/<select name="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)) {
    out[m[1]] = [...m[2].matchAll(/<option value="([^"]*)"( selected)?>([^<]*)<\/option>/g)]
      .map((o) => ({ value: o[1], label: o[3].trim(), selected: Boolean(o[2]) }));
  }
  return out;
}
/** 照浏览器交框：`picks` 里给了的按给的，没给的 `<select>` 取 selected 项（没有就第一项），输入框取 value；复选框一个不勾。 */
async function submitModal(overlay, picks = {}) {
  const target = { elements: [] };
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
  await idle();
}
async function cancelModal(overlay) {
  overlay.querySelector("[data-cancel]").onclick();
  await idle();
}
const openModals = () => overlays.filter((o) => !o.removed);
const titleOf = (o) => ((/<h3>([\s\S]*?)<\/h3>/.exec(o.html) || [])[1] || "").trim();
const introOf = (o) => ((/<div class="desc"[^>]*>([\s\S]*?)<\/div>/.exec(o.html) || [])[1] || "").trim();
const htmlOf = (sel) => (elements[sel] ? elements[sel].innerHTML : "");
const textOf = (sel) => (elements[sel] ? elements[sel].textContent : "");
"""


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def function_source(source: str, head: str) -> str:
    """顶层声明原文：从 `head` 到它之后第一个顶格的 `}`。"""
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def spd_page_js() -> str:
    """慢专病页面要的全部源码：`shared.js`、整份 `pages-spd.js`，以及它们用到的 core.js / pages-clinical.js 里的几个函数。"""
    core, clinical = read("core.js"), read("pages-clinical.js")
    return (read("shared.js") + "\n"
            + "".join(function_source(core, head) for head in (
                "function table(", "function panel(", "function actionableFirst(", "function setMsg(",
                "function barChart("))
            + function_source(clinical, "function formJson(") + function_source(clinical, "async function postAction(")
            + read("pages-spd.js"))


def run(client, headers, js: str, steps: str, *, params: dict | None = None, responder=None):
    """在 node 里加载 `js` 跑 `steps`——async 函数体，return 一个可 JSON 化的值；`params` 在 node 里是 `ARGS.params`。

    页面的每个请求以 `headers` 的身份转给 `client`（真接口）；给了 `responder(method, path, body)` 就先问它：回
    `(状态码, 响应体)` 就用它的、不发真请求，回 None 照旧转给真接口（只改写个别请求，如让某一次查询报错）。"""
    payload = json.dumps({"params": params or {}}, ensure_ascii=False)
    script = (PRELUDE + js + "\n(async () => {\n" + steps + "\n})().then((r) => {"
              " process.stdout.write(JSON.stringify({ result: r }) + '\\n'); process.exit(0); },"
              " (e) => { console.error(e && e.stack || e); process.exit(1); });\n")
    fd, path = tempfile.mkstemp(suffix=".js", prefix="page_race_")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(script)
    proc = subprocess.Popen(["node", path, payload], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8")
    requests = 0
    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                proc.wait(timeout=30)
                raise AssertionError(f"node 没给出结果就退出了：{proc.stderr.read()}")
            message = json.loads(line)
            if "result" in message:
                return message["result"]
            requests += 1
            assert requests <= 300, f"请求停不下来：{message}"
            total = None
            reply = None if responder is None else responder(message["method"], message["path"], message["body"])
            if reply is not None:
                status, body = reply
            else:
                resp = client.request(message["method"], message["path"], json=message["body"], headers=headers)
                status = resp.status_code
                try:
                    body = resp.json()
                except ValueError:
                    body = None
                count = resp.headers.get("X-Total-Count")
                total = None if count is None else int(count)
            try:
                proc.stdin.write(json.dumps({"id": message["id"], "status": status, "body": body, "total": total},
                                            ensure_ascii=False) + "\n")
                proc.stdin.flush()
            except BrokenPipeError:
                proc.wait(timeout=30)
                raise AssertionError(f"node 中途退出：{proc.stderr.read()}") from None
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        proc.wait(timeout=30)
        os.unlink(path)
