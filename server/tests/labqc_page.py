"""质控页（`pages-clinical.js::renderLabQc`）原样拿到 node 里跑的夹具（P2-1471 起共用，第四十三批扫描 AG1）。

照第四十三批扫描复现脚本 `r4_labqc_page_alert.py` 的 node 桩：页面函数与它用到的 `table`、`panel`、`setMsg` 取自源文件原文，
页面的 GET 回放事先按真接口取好的响应，写请求（录入测定值、失控处理）按顺序回放事先取好的真回执。DOM 只垫最小的一层，
整块重画有两种垫法（`rerender`）：
- 照浏览器的规矩（缺省）：给一个元素赋 innerHTML，新 HTML 里带 id 的元素都换成新的（旧对象上写的消息、挂的处理函数一并
  作废）——测定面板 `#lot-detail` 一重画，里面的 `#meas-msg` 就是空的，「先写回执再重画」写的字会被冲掉（P2-1013）；
- 照 r4 原样：同一个选择器始终是同一个对象，重画不换新——页面没写明清空的消息就一直挂着。
"""
import json
import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

PRELUDE = r"""
const ARGS = JSON.parse(process.argv[1]);
const els = {};
const posts = ARGS.posts.slice();
const calls = [];
function element() {
  return { textContent: "", className: "", listeners: {}, html: "", classList: { add() {}, remove() {} },
    addEventListener(type, fn) { this.listeners[type] = fn; },
    get innerHTML() { return this.html; },
    set innerHTML(html) {
      this.html = html;
      if (ARGS.rerender) for (const m of html.matchAll(/\bid="([^"]+)"/g)) delete els["#" + m[1]];
    } };
}
globalThis.document = { addEventListener() {}, cookie: "", querySelector(sel) { return (els[sel] ||= element()); } };
async function api(path, opts = {}) {
  calls.push([opts.method || "GET", path]);
  if (opts.method && opts.method !== "GET") return JSON.parse(JSON.stringify(posts.shift()));
  if (!(path in ARGS.get)) throw new Error(`没料到的请求：${path}`);
  return JSON.parse(JSON.stringify(ARGS.get[path]));
}
async function spdModal() { return ARGS.handleForm; }
function route() {}
function formJson() { return { value: 5.1 }; }
function postAction() {}
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
/** 点批号台账里的「测定值/L-J」。 */
async function openLot(id) {
  await document.querySelector("#page-body").listeners.click({ target: { dataset: { lot: String(id) } } });
}
/** 交测定面板的录入表单（下一个写请求回执按 ARGS.posts 的顺序给）。 */
async function submitMeasurement() {
  await document.querySelector("#meas-form").onsubmit({ preventDefault() {}, target: {} });
  await tick();
}
/** 点某个失控点的「失控处理」，框里填 ARGS.handleForm 交上去。 */
async function handlePoint(id) {
  await document.querySelector("#lot-detail").onclick({ target: { dataset: { handle: String(id) } } });
  await tick();
}
/** 顶部建批号面板与测定面板两个消息框此刻的字。 */
const messages = () => ({ top: document.querySelector("#labqc-msg").textContent,
  meas: document.querySelector("#meas-msg").textContent });
"""


def _function_source(source: str, head: str) -> str:
    """顶层函数原文：从 `head` 到它之后第一个顶格的 `}`。"""
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def responses(client, headers, lot_ids) -> dict:
    """页面会取的 GET（批号台账、各批号的 L-J 与测定值清单），以 `headers` 的身份取一遍真接口。"""
    paths = ["/api/labqc/lots"] + [f"/api/labqc/lots/{lot}/{tail}" for lot in lot_ids
                                   for tail in ("levey-jennings", "measurements")]
    out = {}
    for path in paths:
        resp = client.get(path, headers=headers)
        assert resp.status_code == 200, (path, resp.text)
        out[path] = resp.json()
    return out


def run(get: dict, steps: str, posts=(), handle_form: dict | None = None, params: dict | None = None,
        rerender: bool = True):
    """在 node 里加载质控页，跑 `steps`——async 函数体（先 `await renderLabQc()`），return 一个可 JSON 化的值；
    `params` 在 node 里是 `ARGS.params`，`rerender` 选整块重画的垫法（见模块说明）。"""
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    page = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    script = (
        PRELUDE + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n"
        + _function_source(core, "function table(") + _function_source(core, "function panel(")
        + _function_source(core, "function setMsg(") + _function_source(page, "async function renderLabQc(")
        + f"\n(async () => {{\n{steps}\n}})().then((r) => process.stdout.write(JSON.stringify(r)),"
        + " (e) => { console.error(e); process.exit(1); });\n"
    )
    payload = json.dumps({"get": get, "posts": list(posts), "handleForm": handle_form, "params": params or {},
                          "rerender": rerender}, ensure_ascii=False)
    done = subprocess.run(["node", "-e", script, payload], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)
