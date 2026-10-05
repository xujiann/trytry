"""流程引擎页（`pages-mgmt.js::renderWorkflows`）与流程画布原样拿到 node 里跑的夹具（P2-1473 起共用，第四十三批扫描 AG4）。

页面一段（「流程引擎与统一申请单中心」节头到统一申请单的 `SR_FILTER` 之前）与画布一段（「流程图形化编排」节头到文件末尾）
取自源文件原文，连同 `shared.js` 与 `core.js` 的 `table` / `panel` / `setMsg`；只垫最小的 DOM：`document.querySelector`
按选择器给一个记得住 innerHTML / textContent / onclick 的对象，画布容器的 `querySelectorAll(".wf-node")` 按它的 innerHTML
里的 `data-node` 现造节点（记进 `NODE_EL`，脚本里 `NODE_EL.pharmacy.onclick()` 就是点了那个节点）。

页面的 GET 回放事先按真接口取好的响应（`data["get"]`，先按带查询串的原地址找、找不到再按去掉查询串的找，都没有就抛错），
每次调用记进 `CALLS`；`postAction` 只记下（地址、请求体）、不发；`spdModal` 依次交出 `MODAL` 里排好的表单值。
"""
import json
import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def function_source(source: str, head: str) -> str:
    """顶层函数原文：从 `head` 到它之后第一个顶格的 `}`。"""
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def workflow_sources() -> str:
    """流程引擎页与画布两段原文。"""
    page = _read("pages-mgmt.js")
    section = page[page.index("/* ---------------- 流程引擎与统一申请单中心 ---------------- */"):page.index("const SR_FILTER")]
    canvas = page[page.index("/* ---------------- 流程图形化编排（阶段十）"):]
    return section + "\n" + canvas


PRELUDE = r"""
const DATA = JSON.parse(process.argv[1]);
const els = {};
let NODE_EL = {};
const CALLS = [], POSTED = [], MODAL = [];
let ROUTED = 0;
function element(sel) {
  const el = { sel, textContent: "", innerHTML: "", className: "", value: "", dataset: {}, hidden: false,
    querySelectorAll(q) {
      if (q !== ".wf-node") return [];
      NODE_EL = {};
      return [...el.innerHTML.matchAll(/data-node="([^"]*)"/g)].map((m) => (NODE_EL[m[1]] = { dataset: { node: m[1] } }));
    } };
  el.classList = { add(c) { if (c === "hidden") el.hidden = true; }, remove(c) { if (c === "hidden") el.hidden = false; },
    toggle() {} };
  if (sel === "#wfc-meta") Object.assign(el, { key: { value: "" }, name: { value: "" } });
  return el;
}
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= element(sel)); } };
async function api(path, options = {}) {
  CALLS.push({ path, method: (options.method || "GET").toUpperCase() });
  const found = DATA.get[path] ?? DATA.get[path.split("?")[0]];
  if (found === undefined) throw new Error(`夹具里没有 ${path}`);
  return found;
}
async function spdModal() { return MODAL.shift() ?? null; }
function postAction(path, body) { POSTED.push({ path, body: JSON.parse(JSON.stringify(body)) }); }
async function route() { ROUTED += 1; }
function formJson() { return {}; }
const ROLE_NAMES = { doctor: "医师", pharmacist: "药师", director: "管理层" };
const EV = { preventDefault() {} };
"""


def run(body: str, data: dict) -> dict:
    """在 node 里跑 `body`（一段 async 函数体，`return` 一个可 JSON 化的对象），回它的返回值。"""
    core = _read("core.js")
    script = (PRELUDE + _read("shared.js")
              + function_source(core, "function table(") + function_source(core, "function panel(")
              + function_source(core, "function setMsg(") + workflow_sources()
              + "\n(async () => {\n" + body + "\n})().then((r) => process.stdout.write(JSON.stringify(r)),"
              + " (e) => { console.error(e && e.stack || e); process.exit(1); });\n")
    out = subprocess.run(["node", "-e", script, json.dumps(data, ensure_ascii=False)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)
