"""绩效考核页的维度表头写死，权重和启停不显示：改名到不了考核页，停用的维度照样列出（P2-1510，第四十四批扫描 AH2-10 / AH4-8）。

「绩效指标调权」页能改名（改名框写「报表与考核明细里显示的名字」）、能停用、能把权重调成 0，可考核页（`core.js::renderPerformance`）
的排名表表头是常量「转诊结案 / 共享诊断 / 慢病随访 / 处方合格 / 家医履约」，`GET /api/performance/orgs` 的响应里也没有任何指标名。
修前实测（scan44 ah2/r7、ah4/r3）：把 referral 改名为「双向转诊结案率」、停用 rx 之后，`weights` 里已经没有 rx，响应里找不到新名字，
考核页照旧印写死的五列，rx 那列看不出它不计分。用户手册还写着早已改名的「远程诊断量」。

修法：`/api/performance/orgs` 的响应只增一个键 `dimensions`（排在最后，原有键与次序不动）：各维度的名称（取指标目录）与归一化后的
权重（与 `weights` 同值，停用或权重为 0 的记 0.0）；考核页表头取它，列照旧五列，不计分的维度在表头标「（不计分）」，计分的标
权重。手册的「远程诊断量」改成后端的名称「共享诊断协同量」。

页面这一半把 `renderPerformance` 原样拿到 node 里跑，页面的 `api` 经管道转给真接口（同 test_accounting_cost_project_org_names）。
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
ROOT = Path(__file__).resolve().parents[2]
RENAMED = "双向转诊结案率"


@pytest.fixture(scope="module")
def tuned(client, admin):
    """照扫描的复现：referral 改名、停用 rx。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21510 县人民医院", "org_type": "lead_hospital", "level": "county"})
    assert org.status_code == 201, org.text
    renamed = client.patch("/api/performance/indicators/referral", headers=admin, json={"name": RENAMED})
    assert renamed.status_code == 200, renamed.text
    disabled = client.patch("/api/performance/indicators/rx", headers=admin, json={"active": False})
    assert disabled.status_code == 200, disabled.text
    return org.json()["id"]


def test_出参只增dimensions_原有键与次序不动(client, admin, tuned):
    payload = client.get("/api/performance/orgs", headers=admin).json()
    assert list(payload) == ["period", "weights", "scorecards", "dimensions"]   # 修前没有 dimensions
    card = next(c for c in payload["scorecards"] if c["org_id"] == tuned)
    assert list(card) == ["org_id", "org_name", "level", "score", "detail"]
    assert list(card["detail"]) == ["referral_completion", "remote_exams", "remote_exams_requested",
                                    "remote_exams_provided", "chronic_followup", "rx_pass", "contract_services"]
    assert payload["weights"] == {"referral": 25.0, "remote_exam": 25.0, "chronic": 31.25, "contract": 18.75}


def test_dimensions带指标目录的名称与归一化权重_停用的记0(client, admin, tuned):
    payload = client.get("/api/performance/orgs", headers=admin).json()
    assert payload["dimensions"] == [
        {"key": "referral", "name": RENAMED, "weight": 25.0},
        {"key": "remote_exam", "name": "共享诊断协同量", "weight": 25.0},
        {"key": "chronic", "name": "慢病随访覆盖", "weight": 31.25},
        {"key": "rx", "name": "处方合格率", "weight": 0.0},
        {"key": "contract", "name": "家医签约履约量", "weight": 18.75},
    ]
    assert {d["key"]: d["weight"] for d in payload["dimensions"] if d["weight"]} == payload["weights"]


def test_权重调成0的同样记0(client, admin, tuned):
    zeroed = client.patch("/api/performance/indicators/contract", headers=admin, json={"weight": 0})
    assert zeroed.status_code == 200, zeroed.text
    try:
        payload = client.get("/api/performance/orgs", headers=admin).json()
        assert "contract" not in payload["weights"]
        assert next(d for d in payload["dimensions"] if d["key"] == "contract")["weight"] == 0.0
    finally:
        assert client.patch("/api/performance/indicators/contract", headers=admin, json={"weight": 15}).status_code == 200


# ---------- 页面：表头取后端名称，不计分的标出来 ----------


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def _const(source: str, name: str) -> str:
    found = re.search(rf"^const {name} = .*?;\n", source, re.M | re.S)
    assert found, f"找不到常量 {name}"
    return found.group(0)


#: 页面取数换成桩：`$()` 按选择器给一个记 innerHTML 的假元素；`api()` 经标准输出把地址交给测试进程、从标准输入读回真接口的
#: 响应；页面末尾画整改任务的那一段不在本条范围内，换成空函数
_HARNESS = r"""
const els = {};
const mk = () => ({ textContent: "", innerHTML: "", value: "", className: "", dataset: {},
  classList: { add() {}, remove() {} } });
globalThis.document = { addEventListener() {}, cookie: "", querySelector(sel) { return (els[sel] ||= mk()); } };
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
async function api(path) {
  process.stdout.write(JSON.stringify({ get: path }) + "\n");
  return JSON.parse((await lines.next()).value);
}
function route() {}
function setMsg() {}
function downloadCsv() {}
async function spdModal() { return null; }
async function drawImprovementTasks() {}
"""

_RUN = r"""
(async () => {
  await renderPerformance();
  process.stdout.write(JSON.stringify({ result: els["#page-body"].innerHTML }) + "\n");
  rl.close();
})();
"""


def _render(client, headers) -> str:
    core = _read("core.js")
    script = (_HARNESS + _read("shared.js") + _const(core, "LEVELS") + _const(core, "PERF_FILTER")
              + "".join(_top_level(core, head) for head in (
                  "function table(", "function panel(", "function barChart(", "async function renderPerformance("))
              + _RUN)
    proc = subprocess.Popen(["node", "-e", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "result" in message:
                return message["result"]
            resp = client.get(message["get"], headers=headers)
            assert resp.status_code == 200, (message["get"], resp.text)
            proc.stdin.write(json.dumps(resp.json()) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_考核页表头印新名_停用的rx标不计分(client, admin, tuned):
    page = _render(client, admin)
    heads = [re.findall(r"<th>(.*?)</th>", row) for row in re.findall(r"<thead><tr>(.*?)</tr></thead>", page)]
    ranking = next(cols for cols in heads if cols[:1] == ["排名"])
    # 修前恒为「转诊结案 / 共享诊断(申请/出报告) / 慢病随访 / 处方合格(可审) / 家医履约」，改名与停用一样都看不出来
    assert ranking == ["排名", "机构", "层级", "总分", f"{RENAMED}（权重 25%）", "共享诊断协同量(申请/出报告)（权重 25%）",
                       "慢病随访覆盖（权重 31.25%）", "处方合格率(可审)（不计分）", "家医签约履约量（权重 18.75%）"]


def test_用户手册的维度名与后端一致():
    manual = (ROOT / "docs" / "用户手册.md").read_text(encoding="utf-8")
    (row,) = [line for line in manual.splitlines() if line.startswith("| 绩效考核 |")]
    assert "远程诊断量" not in row and "共享诊断协同量" in row   # 修前写的是改名之前的「远程诊断量」
