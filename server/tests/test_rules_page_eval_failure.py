"""规则页「在线试算」失败：先清空试算结果区，原因写在试算这一块（P2-1738，第五十一批扫描 AO3-9）。

修前：`renderRules` 的试算表单两条失败分支——变量 JSON 解析失败、接口报错（如 422）——都 `setMsg("#rule-msg")`，写到上方
「新增统一规则」面板里；试算结果区 `#eval-result` 不清空，还挂着上一次的命中表与「存在拦截级命中」，看的人会当成这一组
变量也被拦截了。P2-1010 定过的规矩是「查询失败先清空、原因写在这一块」。

修法：提交时先清空 `#eval-result`，两条失败分支都把原因写进 `#eval-result`。

页面那条把 `core.js` 的 `api()` 与 `renderRules` 原文放进 node 跑，`fetch` 经管道转给真接口（管理员身份）。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from jssrc import strip_comments

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

PRELUDE = r"""
const ARGS = JSON.parse(process.argv[1]);
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const requests = [];
const els = {};
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { textContent: "", className: "", innerHTML: "", value: "", dataset: {},
    classList: { add() {}, remove() {} } }); } };
globalThis.FormData = class { constructor(form) { this.fields = form.fields; } get(k) { return k in this.fields ? this.fields[k] : null; } };
/* 真 `api()` 要的几样：迁移期令牌（空 = Cookie 模式）、CSRF、登出（走到就是用例写错了） */
let token = "";
function csrfToken() { return ""; }
function logout() { throw new Error("不该走到登出"); }
globalThis.fetch = async (path, init = {}) => {
  const method = init.method || "GET";
  requests.push(`${method} ${path}`);
  process.stdout.write(JSON.stringify({ method, path, body: init.body ? JSON.parse(init.body) : null }) + "\n");
  const reply = JSON.parse((await lines.next()).value);
  return { status: reply.status, ok: reply.status < 400, json: async () => reply.body, headers: { get: () => null } };
};
/* 在试算表单里填好变量、点「试算」 */
async function evaluate(variables) {
  await els["#eval-form"].onsubmit({ preventDefault() {}, target: { fields: { domain: "prescription", variables } } });
  return { result: $("#eval-result").innerHTML, ruleMsg: $("#rule-msg").textContent };
}
"""


def _function_source(source: str, head: str) -> str:
    """顶层函数原文：从 `head` 到它之后第一个顶格的 `}`。"""
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def _const(source: str, name: str) -> str:
    found = re.search(rf"^(?:const|let) {name} = .*?;\n", source, re.M | re.S)
    assert found, f"找不到顶层声明 {name}"
    return found.group(0)


def run_page(client, headers: dict, steps: str):
    """在 node 里加载 `api()` 与规则页、跑 `steps`（async 函数体，return 一个可 JSON 化的值）；请求以 `headers` 的身份转给真接口。"""
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    mgmt = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    script = (
        PRELUDE + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n"
        + "".join(_function_source(core, head) for head in (
            "async function api(", "function table(", "function panel(", "function setMsg("))
        + _const(mgmt, "SEVERITY") + _function_source(mgmt, "async function renderRules(")
        + f"\n(async () => {{\n{steps}\n}})().then("
        + "(r) => { process.stdout.write(JSON.stringify({ result: r }) + '\\n'); rl.close(); },"
        + " (e) => { console.error(e); process.exit(1); });\n"
    )
    proc = subprocess.Popen(["node", "-e", script, "{}"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "result" in message:
                return message["result"]
            resp = client.request(message["method"], message["path"], headers=headers, json=message["body"])
            proc.stdin.write(json.dumps({"status": resp.status_code, "body": resp.json()}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_试算失败先清空结果区_原因写在试算这一块(client, admin):
    created = client.post("/api/rules", headers=admin, json={
        "key": "p21738-elderly", "name": "P21738 高龄拦截", "domain": "prescription", "condition": "age >= 65",
        "severity": "error"})
    assert created.status_code == 201, created.text
    out = run_page(client, admin, """
      await renderRules();
      const hit = await evaluate('{"age": 78}');
      const badJson = await evaluate('{"age": 78');
      await evaluate('{"age": 78}');
      const rejected = await evaluate('[78]');   // 合法 JSON、不是对象：接口 422
      return { requests, hit, badJson, rejected };
    """)
    hit, bad_json, rejected = out["hit"], out["badJson"], out["rejected"]
    assert "存在拦截级命中" in hit["result"] and "P21738 高龄拦截" in hit["result"]   # 前提：上一次试算是拦截级命中
    # JSON 解析失败：原因写在试算这一块，上一次的命中表与「存在拦截级命中」不再挂着（修前写进上方 #rule-msg、结果区照旧）
    assert bad_json["result"] == '<p class="msg err">变量必须是合法 JSON</p>'
    assert bad_json["ruleMsg"] == ""
    # 接口 422：同样先清空，原因写在试算这一块
    assert out["requests"].count("POST /api/rules/evaluate") == 3   # 解析失败那次不发请求
    assert rejected["result"].startswith('<p class="msg err">') and "variables" in rejected["result"], rejected
    assert "存在拦截级命中" not in rejected["result"] and "P21738 高龄拦截" not in rejected["result"]
    assert rejected["ruleMsg"] == ""


def test_两条失败分支都不再写到新增规则面板():
    """静态补一道：试算处理函数先清空结果区（在发请求之前），整个函数里不再出现 `#rule-msg`。"""
    src = strip_comments((STATIC / "pages-mgmt.js").read_text(encoding="utf-8"))
    start = src.index('$("#eval-form").onsubmit')
    body = src[start:src.index("\n  };\n", start)]
    clear = '$("#eval-result").innerHTML = "";'
    assert clear in body and body.index(clear) < body.index("JSON.parse(") < body.index("await api(")
    assert '"#rule-msg"' not in body
