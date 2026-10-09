"""决策指标页的期间输入「2026-9」补零成 2026-09，不再被悄悄退回本月（P2-1707，第五十批扫描 AN3-7）。

切换框先拿 efficiency 校验再存（P2-586）——它走 `deps.month_bounds`，`strptime` 收不补零的「2026-9」，于是原样存下；
进页面 `flowRange` 拼出 `start=2026-9-01`，patient-flow 的日期校验 422「start 参数须为 YYYY-MM-DD 格式」，整页那个
Promise.all 被拒，又被「只对 422 回落本月并清掉坏值」那一道接住：显示的是本月、存下的值没了，页面上没有任何提示。
修后页面在校验之前、读出存值之后都把个位月份补成 `YYYY-MM`，显示与请求都是 2026-09；后端口径不动。

这里把 `renderAnalytics` 原样拿到 node 里跑，页面的 `api` 经管道转给真接口（被拒照真 `api` 的样子抛带 status 的错）。
"""
import json
import os
import shutil
import subprocess

import pytest

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


#: 页面取数换成桩：`$()` 按选择器给一个记 innerHTML 的假元素；`localStorage` 从参数里读；`api()` 经标准输出把地址交给
#: 测试进程、从标准输入读回真接口的状态码与响应体，非 2xx 照 core.js 的 `api` 抛带 `status` 的错
_HARNESS = r"""
const els = {};
const STORE = JSON.parse(process.argv[1]);
const SUBMIT = process.argv[2];
const mk = () => ({ textContent: "", innerHTML: "", value: "", className: "", style: {}, dataset: {} });
globalThis.document = { addEventListener() {}, cookie: "", querySelector(sel) { return (els[sel] ||= mk()); } };
globalThis.localStorage = { getItem: (k) => (k in STORE ? STORE[k] : null),
  setItem(k, v) { STORE[k] = String(v); }, removeItem(k) { delete STORE[k]; } };
globalThis.FormData = class { constructor(form) { this.form = form; } get(k) { return this.form.fields[k]; } };
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const requested = [];
async function api(path) {
  requested.push(path);
  process.stdout.write(JSON.stringify({ get: path }) + "\n");
  const { status, body } = JSON.parse((await lines.next()).value);
  if (status >= 400) { const err = new Error(errorText(body.detail, `请求失败(${status})`)); err.status = status; throw err; }
  return body;
}
function currentRole() { return STORE.medplat_role || ""; }
function route() {}
function postAction() {}
function formJson() { return {}; }
async function spdModal() { return null; }
"""


def _render(client, headers, store: dict, submit: str | None = None) -> dict:
    core = _read("core.js")
    script = (
        _HARNESS + _read("shared.js")
        + _top_level(core, "function table(") + _top_level(core, "function panel(")
        + _top_level(core, "function setMsg(")
        + _top_level(_read("pages-mgmt.js"), "async function renderAnalytics(")
        + "\n(async () => { await renderAnalytics();\n"
        "  const body = els['#page-body'].innerHTML;\n"
        "  if (SUBMIT) await els['#ana-period'].onsubmit({ preventDefault() {}, target: { fields: { period: SUBMIT } } });\n"
        "  process.stdout.write(JSON.stringify({ result: { body, requested, store: STORE,\n"
        "    msg: (els['#ana-period-msg'] || {}).textContent || '' } }) + '\\n');\n"
        "  rl.close(); })();\n"
    )
    proc = subprocess.Popen(["node", "-e", script, json.dumps(store), submit or ""], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "result" in message:
                return message["result"]
            resp = client.get(message["get"], headers=headers)
            proc.stdin.write(json.dumps({"status": resp.status_code, "body": resp.json()}) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def test_存下的个位月份_显示与请求都补零(client, admin):
    got = _render(client, admin, {"medplat_ana_period": "2026-9", "medplat_role": "admin"})
    assert "就医流向（2026-09）" in got["body"] and "运行效率（2026-09）" in got["body"]   # 修前悄悄换成本月
    assert "/api/analytics/patient-flow?start=2026-09-01&end=2026-10-01" in got["requested"]   # 修前 start=2026-9-01，422
    assert "/api/analytics/efficiency?period=2026-09" in got["requested"]
    assert not any("2026-9-01" in path for path in got["requested"]), got["requested"]
    assert got["store"].get("medplat_ana_period") == "2026-9"   # 存值留着、读出来补零；修前被当坏值清掉


def test_切换框输入个位月份_补零后再验再存(client, admin):
    got = _render(client, admin, {"medplat_role": "admin"}, submit="2026-9")
    assert got["store"].get("medplat_ana_period") == "2026-09", got   # 修前存下 "2026-9"
    assert got["requested"][-1] == "/api/analytics/efficiency?period=2026-09"
    assert got["msg"] == ""


def test_补零的照旧_坏值照旧页内报错不存(client, admin):
    assert _render(client, admin, {"medplat_role": "admin"}, submit="2026-11")["store"]["medplat_ana_period"] == "2026-11"
    bad = _render(client, admin, {"medplat_role": "admin"}, submit="2026/09")
    assert "medplat_ana_period" not in bad["store"]   # P2-586：先验再存，被拒不存
    assert bad["msg"] == "period 须为 YYYY-MM 格式"
