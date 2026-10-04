"""消毒供应 / 医废两页原样拿到 node 里跑的夹具（P2-1443 起共用，第四十二批扫描 AF4）。

页面函数（`core.js::renderCssd` / `renderMedwaste`、`pages-public.js::drawCssdCosts`）与它们用到的 `spdModal`、`table`、
`panel`、`actionableFirst`、`setMsg`、`currentRole`、`appendSection` 都取自源文件原文，只垫最小的 DOM：
`document.querySelector` 按选择器给一个记得住 innerHTML / onsubmit / onclick 的对象；`spdModal` 建的遮罩记下它的 HTML，
`submitModal` 照浏览器的规矩交表（`<select>` 没有 selected 项时取第一项）。页面的 GET 回放事先按真接口取好的响应，
写请求只记下（方法、地址、请求体）、不发。
"""
import json
import re
import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

PAGE_FUNCS = {
    "renderCssd": ("core.js", "async function renderCssd("),
    "renderMedwaste": ("core.js", "async function renderMedwaste("),
    "drawCssdCosts": ("pages-public.js", "async function drawCssdCosts("),
}


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def function_source(source: str, head: str) -> str:
    """顶层函数原文：从 `head` 到它之后第一个顶格的 `}`。"""
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def page_source(name: str) -> str:
    filename, head = PAGE_FUNCS[name]
    return function_source(_read(filename), head)


def page_paths(name: str) -> list[str]:
    """页面函数里写死的 GET 地址（`api("…")`，不带插值的那几个）。"""
    return re.findall(r'\bapi\("([^"]+)"\)', page_source(name))


def responses(client, headers, *names: str) -> dict:
    """按页面原样的地址、以 `headers` 的身份取一遍真接口，留给 node 里的 `api` 回放。"""
    out = {}
    for name in names:
        for path in page_paths(name):
            resp = client.get(path, headers=headers)
            assert resp.status_code == 200, (path, resp.text)
            out[path] = resp.json()
    return out


PRELUDE = r"""
const ARGS = JSON.parse(process.argv[1]);
const posts = [];
const elements = {};
const overlays = [];
globalThis.localStorage = { getItem: (k) => (k === "medplat_role" ? ARGS.role : null), setItem() {}, removeItem() {} };
globalThis.FormData = class { constructor(form) { this.form = form; } get(k) { return k in this.form ? this.form[k] : null; } };
const element = () => ({ dataset: {}, innerHTML: "", textContent: "", className: "", children: [],
  appendChild(child) { this.children.push(child); } });
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
  if (options.method && options.method !== "GET") {
    posts.push([options.method, path, options.body ? JSON.parse(options.body) : null]);
    return {};
  }
  if (!(path in ARGS.responses)) throw new Error(`没料到的请求：${path}`);
  return JSON.parse(JSON.stringify(ARGS.responses[path]));
}
function route() {}
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
/** 整页 HTML：`#page-body` 的 innerHTML，加上 appendSection 追加的各块（成本面板）。 */
const pageHtml = () => [elements["#page-body"].innerHTML,
  ...elements["#page-body"].children.map((c) => c.innerHTML)].join("\n");
/** 点页面上一个按钮：renderCssd 读 `e.target.dataset`，renderMedwaste 用 `e.target.closest("[data-…]")`。
 *  返回点击处理的 Promise——开了框就要等框交了才落定，别直接 await。 */
function click(attr, dataset) {
  const el = { dataset };
  return elements["#page-body"].onclick({ target: { dataset, closest: (sel) => (sel === `[${attr}]` ? el : null) } });
}
const lastModal = () => overlays[overlays.length - 1] || null;
/** 一段 HTML 里每个 `<select>` 的选项：`{name: [{value, label, selected}]}`。 */
function selectsIn(html) {
  const out = {};
  for (const m of html.matchAll(/<select name="([^"]+)"[^>]*>([\s\S]*?)<\/select>/g)) {
    out[m[1]] = [...m[2].matchAll(/<option value="([^"]*)"( selected)?>([^<]*)<\/option>/g)]
      .map((o) => ({ value: o[1], label: o[3], selected: Boolean(o[2]) }));
  }
  return out;
}
const modalSelects = (overlay) => selectsIn(overlay.html);
/** 不动下拉时浏览器交的值：selected 项，没有就是第一项。 */
function untouched(options) {
  const chosen = options.find((o) => o.selected) || options[0];
  return chosen ? chosen.value : "";
}
/** 照浏览器交表：`picks` 里给了的按给的，没给的 `<select>` 按 `untouched`，输入框取 value。 */
async function submitModal(overlay, picks = {}) {
  const target = {};
  for (const [name, options] of Object.entries(modalSelects(overlay))) {
    target[name] = { value: name in picks ? String(picks[name]) : untouched(options) };
  }
  for (const m of overlay.html.matchAll(/<input name="([^"]+)"[^>]*?value="([^"]*)"/g)) {
    target[m[1]] = { value: m[1] in picks ? String(picks[m[1]]) : m[2] };
  }
  await overlay.querySelector("form").onsubmit({ preventDefault() {}, target });
  await tick();
}
const modalMsg = (overlay) => { const el = overlay.querySelector("[data-modal-msg]"); return el ? el.textContent : null; };
"""


def _top_const(source: str, name: str) -> str:
    return re.search(rf"^const {name} = .*;$", source, re.M).group(0)


def run(responses: dict, role: str, steps: str, params: dict | None = None):
    """在 node 里加载两页的代码，以 `role`（`currentRole()` 读到的角色）跑 `steps`——async 函数体，return 一个可 JSON 化的值；
    `params` 在 node 里是 `ARGS.params`。"""
    core, spd, public = _read("core.js"), _read("pages-spd.js"), _read("pages-public.js")
    script = (
        PRELUDE + _read("shared.js") + "\n"
        + function_source(spd, "function spdModal(")
        + function_source(core, "function table(") + function_source(core, "function panel(")
        + function_source(core, "function actionableFirst(") + function_source(core, "function setMsg(")
        + re.search(r"^function currentRole\(\).*$", core, re.M).group(0) + "\n"
        + function_source(public, "function appendSection(")
        + _top_const(public, "CSSD_COST_TYPES") + "\n" + _top_const(core, "WASTE_TRACE_STEPS") + "\n"
        + page_source("drawCssdCosts") + page_source("renderCssd") + page_source("renderMedwaste")
        + f"\n(async () => {{\n{steps}\n}})().then((r) => process.stdout.write(JSON.stringify(r)),"
        + " (e) => { console.error(e); process.exit(1); });\n"
    )
    payload = json.dumps({"responses": responses, "role": role, "params": params or {}}, ensure_ascii=False)
    done = subprocess.run(["node", "-e", script, payload], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)
