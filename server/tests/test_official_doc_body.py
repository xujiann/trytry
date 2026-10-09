"""行政公文从页面起草也带正文，公文表看得到正文（P2-1595，第四十七批扫描 AK1-4）。

修前：「行政与质控」页 `renderOaQc` 的起草表单只有标题、类型、发文单位三栏，公文表也只列这三项——后端 `DocCreate.body`
是收的（最长 4096），`DocOut` 也带 `body`，可从页面起草的通知、政策文件、会议纪要 `body` 恒为空串；就算经接口带了正文，
页面上也没有任何地方读得到。扫描实测（`r3_oa.py`）：页面那样起草 201，回执 `'body': ''`。

修法：起草表单加正文多行框（`<textarea name="body" maxlength="4096">`，`formJson` 走 FormData，多行框照收）；公文表在标题下
折叠显示正文（`<details>`），一律 `esc()`、照原样换行（`white-space:pre-wrap`）。后端不动。

页面那条把 `core.js` 的 `api()` 与 `renderOaQc` 原文放进 node 跑，`fetch` 经管道转给真接口；起草时交上去的字段，按页面上
这张表单里带 `name` 的控件取（与浏览器 FormData 同一个取法），正文框不在表单里就交不上去。真浏览器里那一半见端到端
`test_公文起草带正文_公文表里展开看得到`。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

TITLE = "P21595 关于调整门诊收费的通知"
#: 正文：两行，夹一段标签——显示时要照原样换行、标签按字面显示
BODY = "一、门诊诊查费自 11 月 1 日起调整；\n二、<b>未尽事宜</b>另行通知。"
BODY_SHOWN = "一、门诊诊查费自 11 月 1 日起调整；\n二、&lt;b&gt;未尽事宜&lt;/b&gt;另行通知。"


def _function_source(source: str, head: str) -> str:
    """顶层函数原文：从 `head` 到它之后第一个顶格的 `}`。"""
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def _render_body() -> str:
    return _function_source((STATIC / "pages-clinical.js").read_text(encoding="utf-8"), "async function renderOaQc(")


def test_起草表单有正文框_公文表经esc照原样换行显示正文():
    body = _render_body()
    form = re.search(r'<form class="inline" id="doc-form">(.*?)</form>', body, re.S)
    assert form, "找不到起草表单"
    assert '<textarea name="body" maxlength="4096"' in form.group(1)   # 修前表单里没有正文
    assert '<div style="white-space:pre-wrap">${esc(d.body)}</div>' in body   # 修前公文表不显示正文
    assert 'postAction("/api/mgmt/docs", formJson(e.target), "#oa-msg")' in body   # 照旧整张表单交上去


PRELUDE = r"""
const ARGS = JSON.parse(process.argv[1]);
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const requests = [];
const pending = [];
let els = {};
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { textContent: "", className: "", innerHTML: "", dataset: {} }); } };
/* FormData：按假表单的 fields 给（字段由测试按页面上这张表单里带 name 的控件取） */
globalThis.FormData = class { constructor(form) { this.fields = form.fields; }
  get(k) { return k in this.fields ? this.fields[k] : null; } entries() { return Object.entries(this.fields); } };
let token = "";
function csrfToken() { return ""; }
function logout() { throw new Error("不该走到登出"); }
globalThis.fetch = async (path, init = {}) => {
  const method = init.method || "GET";
  requests.push({ method, path, body: init.body ? JSON.parse(init.body) : null });
  process.stdout.write(JSON.stringify({ method, path, body: init.body ? JSON.parse(init.body) : null }) + "\n");
  const reply = JSON.parse((await lines.next()).value);
  return { status: reply.status, ok: reply.status < 400, json: async () => reply.body,
    headers: { get: (name) => (name.toLowerCase() === "x-total-count" ? reply.total : null) } };
};
function route() { els = {}; pending.push(renderOaQc()); }
async function settle() { while (pending.length) await pending.shift(); }
const page = () => els["#page-body"].innerHTML;
/* 点「起草」：postAction 不回 promise 给 onsubmit，等它写完、调到 route() 再等重画 */
async function submit(sel, fields) {
  els[sel].onsubmit({ preventDefault() {}, target: { fields, querySelectorAll: () => [] } });
  for (let i = 0; i < 400 && !pending.length; i += 1) await new Promise((r) => setTimeout(r, 5));
  await settle();
}
"""


def run_page(client, headers: dict, steps: str, params: dict):
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    clinical = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    script = (
        PRELUDE + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n"
        + "".join(_function_source(core, head) for head in (
            "async function api(", "function table(", "function panel(", "function setMsg("))
        + "".join(_function_source(clinical, head) for head in (
            "function formJson(", "async function postAction(", "async function renderOaQc("))
        + f"\n(async () => {{\n{steps}\n}})().then("
        + "(r) => { process.stdout.write(JSON.stringify({ result: r }) + '\\n'); rl.close(); },"
        + " (e) => { console.error(e); process.exit(1); });\n"
    )
    proc = subprocess.Popen(["node", "-e", script, json.dumps(params, ensure_ascii=False)], stdin=subprocess.PIPE,
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


def _form_fields(html: str, form_id: str) -> list[str]:
    """表单里带 name 的控件（input / select / textarea）——浏览器 FormData 收的就是这些。"""
    form = re.search(rf'<form class="inline" id="{form_id}">(.*?)</form>', html, re.S)
    assert form, f"页面上没有 #{form_id}"
    return re.findall(r'<(?:input|select|textarea) name="([^"]+)"', form.group(1))


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_页面起草带正文_公文表里展开看得到(client, admin):
    first = run_page(client, admin, "await renderOaQc(); return { html: page() };", {})
    names = _form_fields(first["html"], "doc-form")
    assert names == ["title", "doc_type", "issuer", "body"]   # 修前没有 body
    values = {"title": TITLE, "doc_type": "notice", "issuer": "P21595 县卫生健康局", "body": BODY}
    out = run_page(client, admin, """
      await renderOaQc();
      await submit("#doc-form", ARGS.fields);
      return { requests, html: page(), msg: els["#oa-msg"] ? els["#oa-msg"].textContent : "" };
    """, {"fields": {name: values[name] for name in names}})
    posted = [r for r in out["requests"] if r["method"] == "POST"]
    assert posted == [{"method": "POST", "path": "/api/mgmt/docs", "body": values}], out["msg"]
    # 接口那一侧：清单读得到正文（修前页面起草的 body 恒为空串）
    listed = [d for d in client.get("/api/mgmt/docs", headers=admin).json() if d["title"] == TITLE]
    assert [d["body"] for d in listed] == [BODY]
    # 页面那一侧：这一行标题下折叠着正文，经 esc、换行照原样留着
    row = re.search(rf"<tr><td>{listed[0]['id']}</td>.*?</tr>", out["html"], re.S)
    assert row, out["html"][-1500:]
    assert f'<details><summary>查看正文</summary>\n         <div style="white-space:pre-wrap">{BODY_SHOWN}</div></details>' \
        in row.group(0)
    assert "<b>未尽事宜</b>" not in out["html"]
