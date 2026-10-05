"""用血页（`pages-public.js::renderBlood`）原样拿到 node 里跑的夹具（P2-1468 起共用，第四十三批扫描 AG1）。

页面函数与它用到的 `table`、`panel`、`currentRole`、`BLOOD_COMPONENTS`、`BLOOD_REQ_STATUS` 都取自源文件原文，只垫最小的
DOM：`document.querySelector` 按选择器给一个记得住 innerHTML / onsubmit / onclick 的对象，`currentRole()` 读到的角色由调用方
给。页面的 GET 按地址回放调用方给的响应（没给的回空表），写请求不发。
"""
import json
import re
import subprocess
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

PRELUDE = r"""
const ARGS = JSON.parse(process.argv[1]);
const elements = {};
globalThis.localStorage = { getItem: (k) => (k === "medplat_role" ? ARGS.role : null), setItem() {}, removeItem() {} };
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (elements[sel] ||= { dataset: {}, innerHTML: "", textContent: "", className: "" }); } };
async function api(path) { return JSON.parse(JSON.stringify(ARGS.responses[path] ?? [])); }
function route() {}
function formJson() { return {}; }
function postAction() {}
"""


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def function_source(source: str, head: str) -> str:
    """顶层函数原文：从 `head` 到它之后第一个顶格的 `}`。"""
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def top_const(source: str, name: str) -> str:
    """顶层单行常量的原文（`const NAME = …;`）。"""
    return re.search(rf"^const {name} = .*;$", source, re.M).group(0)


def render(role: str, responses: dict | None = None) -> str:
    """以 `role`（`currentRole()` 读到的角色）渲染用血页，返回 `#page-body` 的 HTML。"""
    core, public = _read("core.js"), _read("pages-public.js")
    script = (
        PRELUDE + _read("shared.js") + "\n"
        + function_source(core, "function table(") + function_source(core, "function panel(")
        + re.search(r"^function currentRole\(\).*$", core, re.M).group(0) + "\n"
        + top_const(public, "BLOOD_COMPONENTS") + "\n" + top_const(public, "BLOOD_REQ_STATUS") + "\n"
        + function_source(public, "async function renderBlood(")
        + "\n(async () => { await renderBlood(); process.stdout.write(elements['#page-body'].innerHTML); })()"
        + ".catch((e) => { console.error(e); process.exit(1); });\n"
    )
    payload = json.dumps({"role": role, "responses": responses or {}}, ensure_ascii=False)
    done = subprocess.run(["node", "-e", script, payload], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout


def form_selects(html: str, form_id: str) -> dict:
    """某张表单里的每个 `<select>`：`{name: (是否 required, [(value, 文字, 是否 selected), …])}`。

    没写 `value` 的 `<option>` 交的是它的文字（浏览器同此）；没有 selected 项时浏览器落在第一项。
    """
    form = re.search(rf'<form[^>]*\bid="{form_id}"[^>]*>([\s\S]*?)</form>', html)
    assert form, f"页面上没有 #{form_id}"
    out = {}
    for name, attrs, body in re.findall(r'<select name="([^"]+)"([^>]*)>([\s\S]*?)</select>', form.group(1)):
        options = []
        for option_attrs, label in re.findall(r"<option([^>]*)>([^<]*)</option>", body):
            value = re.search(r'value="([^"]*)"', option_attrs)
            options.append((value.group(1) if value else label, label, bool(re.search(r"\bselected\b", option_attrs))))
        out[name] = (bool(re.search(r"\brequired\b", attrs)), options)
    return out
