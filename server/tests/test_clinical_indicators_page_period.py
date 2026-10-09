"""医疗质量指标页（药占比、抗菌药物使用强度的唯一页面入口）的用药结构能切期间，缺省仍是本月（P2-1708，第五十批扫描 AN3-9）。

`renderClinicalIndicators` 原先写死 `period = localToday().slice(0, 7)`，没有切换框：每月初打开，强度和药占比几乎全是 0
或「样本不足」，要上报上个整月的考核数只能直接调接口（后端 `drug-use` 本来就收 `period`）。修后照决策指标 / 会计 / 成本页
给用药结构加期间切换框：缺省本月；切换时先让后端判再存（P2-586），存下的被拒只对 422 回落本月并清掉坏值。同页的质量指标
面板照旧不传期间、取全期（改成按月会让月初打开时多半样本为空，那是另一处口径，本条不动）。

这里把 `renderClinicalIndicators` 原样拿到 node 里跑，页面的 `api` 经管道转给真接口（被拒照真 `api` 的样子抛带 status 的错）。
"""
import json
import os
import shutil
import subprocess

import pytest

from conftest import business_today

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
KEY = "medplat_clinind_period"

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
"""


def _render(client, headers, store: dict, submit: str | None = None) -> dict:
    core = _read("core.js")
    script = (
        _HARNESS + _read("shared.js")
        + _top_level(core, "function table(") + _top_level(core, "function panel(")
        + _top_level(core, "function setMsg(") + _top_level(core, "function barChart(")
        + _top_level(_read("pages-mgmt.js"), "async function renderClinicalIndicators(")
        + "\n(async () => { await renderClinicalIndicators();\n"
        "  const body = els['#page-body'].innerHTML;\n"
        "  if (SUBMIT) await els['#clinind-period'].onsubmit({ preventDefault() {}, target: { fields: { period: SUBMIT } } });\n"
        "  process.stdout.write(JSON.stringify({ result: { body, requested, store: STORE,\n"
        "    msg: (els['#clinind-period-msg'] || {}).textContent || '' } }) + '\\n');\n"
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


def _months() -> tuple[str, str]:
    today = business_today()
    last = today.replace(year=today.year - 1, month=12) if today.month == 1 else today.replace(day=1, month=today.month - 1)
    return today.isoformat()[:7], last.isoformat()[:7]


def test_缺省本月_两块面板同一期间(client, admin):
    this_month, _ = _months()
    got = _render(client, admin, {"medplat_role": "admin"})
    assert f"/api/analytics/drug-use?period={this_month}" in got["requested"]
    assert "/api/quality/clinical-indicators" in got["requested"]   # 质量指标照旧取全期，不跟这个期间走
    assert not [r for r in got["requested"] if r.startswith("/api/quality/clinical-indicators?")]
    assert f"用药结构（{this_month}）" in got["body"] and f"医疗质量指标（{this_month}）" not in got["body"]
    assert f'id="clinind-period"><input name="period" value="{this_month}"' in got["body"]   # 修前没有切换框


def test_存下上月_两块面板都取上月(client, admin):
    _, last_month = _months()
    got = _render(client, admin, {"medplat_role": "admin", KEY: last_month})
    assert f"/api/analytics/drug-use?period={last_month}" in got["requested"]   # 修前锁死本月
    assert "/api/quality/clinical-indicators" in got["requested"]
    assert f"用药结构（{last_month}）" in got["body"]


def test_切换先验再存_被拒页内报错不存(client, admin):
    _, last_month = _months()
    ok = _render(client, admin, {"medplat_role": "admin"}, submit=last_month)
    assert ok["store"].get(KEY) == last_month and ok["msg"] == ""
    assert ok["requested"][-1] == f"/api/analytics/drug-use?period={last_month}"
    bad = _render(client, admin, {"medplat_role": "admin"}, submit="2026/09")
    assert KEY not in bad["store"]
    assert bad["msg"] == "period 须为 YYYY-MM 格式"


def test_存下的坏值被拒_回落本月并清掉(client, admin):
    this_month, _ = _months()
    got = _render(client, admin, {"medplat_role": "admin", KEY: "2026/09"})
    assert f"用药结构（{this_month}）" in got["body"]
    assert KEY not in got["store"]
