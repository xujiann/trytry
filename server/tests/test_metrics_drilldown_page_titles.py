"""驾驶舱下钻面板与「可下钻指标目录」把页面 hash、指标键原样印给用户（P2-1512，第四十四批扫描 AH4-9）。

下钻面板（`core.js::openDrilldown`）写「点击明细行跳转「critical」业务页」「「archive」业务页列不出、也筛不出这一类」——印的是后端
`METRIC_QUERIES` 里的页面 hash；驾驶舱底部的目录表「指标」列印 `critical_values` 这种键（中文名另占一列「名称」），「业务页」列也是
hash。页面注册表 `app.js::PAGES` 里每页都有中文标题（critical 是危急值操作台、archive 是患者360视图），明细行的编码 P2-646 早已换成
后端文案。修前（scan44 ah4/r2 的 10)、11)，模板按代码读；本条复现照真接口的返回在 node 里渲染）：面板与目录印的正是上面那几个
hash 与键。

修法：页名取 `PAGES` 的 title（`pageTitle`，注册表里查不到的原样回显 hash，与 statusTag 同一个口径——下钻指标的目标页都在注册表里，
下面钉着）；目录的「指标」列印后端已有的中文指标名 `label`，原先单列的「名称」并进来。行跳转照旧按 hash（`data-drillgo`）。

页面这一半把驾驶舱与下钻面板原样拿到 node 里跑，页面的 `api` 经管道转给真接口（同 test_accounting_cost_project_org_names）；
`PAGES` 取 app.js 注册表里的 id 与 title（注册表求值要拿到全部页面函数，这里只取这两项）。
"""
import json
import os
import re
import shutil
import subprocess
from datetime import datetime

import pytest

from app.database import SessionLocal
from app.models import ExamReport, ExamRequest, User
from app.routers.metrics import METRIC_QUERIES

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def _registry() -> dict[str, str]:
    """页面注册表：id → title。"""
    return dict(re.findall(r'\{ id: "([\w-]+)", title: "([^"]*)"', _read("app.js")))


@pytest.fixture(scope="module")
def critical(client, admin):
    """一条未闭环的危急值：下钻面板里有一行可跳的明细。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21512 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21512 患者", "id_card": "330281198806061512"}).json()["id"]
    with SessionLocal() as db:
        doctor = db.query(User).filter(User.username == "admin").one()
        request = ExamRequest(patient_id=patient, from_org_id=org, center_type="ecg", item_code="ECG",
                              item_name="P21512 心电", status="reported", created_by=doctor.id)
        db.add(request)
        db.flush()
        report = ExamReport(request_id=request.id, conclusion="P21512 危急", critical=True, critical_status="notified",
                            reported_by="李医生", reported_at=datetime.utcnow())
        db.add(report)
        db.commit()
        return report.id


#: 页面取数换成桩：`$()` 按选择器给一个记 innerHTML 的假元素；`api()` 经标准输出把地址交给测试进程、从标准输入读回真接口的响应
_HARNESS = r"""
const els = {};
const mk = () => ({ textContent: "", innerHTML: "", dataset: {}, classList: { add() {}, remove() {} } });
globalThis.document = { addEventListener() {}, cookie: "", querySelector(sel) { return (els[sel] ||= mk()); } };
const PAGES = JSON.parse(process.argv[1]).map(([id, title]) => ({ id, title }));
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
async function api(path) {
  process.stdout.write(JSON.stringify({ get: path }) + "\n");
  return JSON.parse((await lines.next()).value);
}
function nav() {}
"""

#: 渲染驾驶舱（底部是可下钻指标目录），再打开两个下钻面板：危急值（目标页列得出，行可跳）、基层诊疗人次（列不出，行不跳）
_RUN = r"""
(async () => {
  await renderDashboard();
  const dashboard = els["#page-body"].innerHTML;
  await openDrilldown("critical_values");
  const go = els["#drill-panel"].innerHTML;
  await openDrilldown("grassroots_encounters");
  const nogo = els["#drill-panel"].innerHTML;
  process.stdout.write(JSON.stringify({ result: { dashboard, go, nogo } }) + "\n");
  rl.close();
})();
"""


def _render(client, headers) -> dict:
    core = _read("core.js")
    # 「块2：指标下钻」整段：DRILL_GO、页名帮手、下钻面板、驾驶舱
    start = core.index("/* 块2：指标下钻")
    drill_block = core[start:core.index("\n// available 是 bool", start)]
    script = (_HARNESS + _read("shared.js")
              + "".join(_top_level(core, head) for head in (
                  "function table(", "function panel(", "function barChart(", "function lineChart("))
              + drill_block + _RUN)
    proc = subprocess.Popen(["node", "-e", script, json.dumps(list(_registry().items()), ensure_ascii=False)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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


def _visible(html: str) -> str:
    """去掉标签（连同属性）之后用户看得见的字。"""
    return re.sub(r"<[^>]+>", " ", html)


def _bare_codes(text: str) -> list[str]:
    """`text` 里成词出现的下钻指标键与目标页 hash。"""
    codes = set(METRIC_QUERIES) | {meta["page"] for meta in METRIC_QUERIES.values()}
    return sorted(code for code in codes if re.search(rf"(?<![\w-]){re.escape(code)}(?![\w-])", text))


def _desc(panel_html: str) -> str:
    return re.search(r'<p class="desc"[^>]*>(.*?)</p>', panel_html, re.S).group(1)


def test_下钻指标的目标页都在页面注册表里():
    """`pageTitle` 查不到才回显 hash：下钻指标的目标页都得在注册表里，面板上才不会出现裸 hash。"""
    registry = _registry()
    assert {meta["page"] for meta in METRIC_QUERIES.values()} <= set(registry)
    assert (registry["critical"], registry["archive"]) == ("危急值操作台", "患者360视图")


def test_下钻面板印中文页名_跳转仍按hash(client, admin, critical):
    out = _render(client, admin)
    # 修前：点击明细行跳转「critical」业务页 /「archive」业务页列不出……
    assert _desc(out["go"]).startswith("点击明细行跳转「危急值操作台」业务页")
    assert _desc(out["nogo"]).startswith("「患者360视图」业务页列不出、也筛不出这一类，明细行不跳转")
    for panel in (out["go"], out["nogo"]):
        assert _bare_codes(_visible(panel)) == [], _visible(panel)
    assert re.search(r'<tr data-drillgo="critical" style="cursor:pointer"><td>%d</td>' % critical, out["go"])   # 行跳转照旧按 hash


def test_目录的指标列印中文指标名_业务页列印页名(client, admin, critical):
    out = _render(client, admin)
    registry = _registry()
    section = out["dashboard"][out["dashboard"].index("<h3>可下钻指标目录"):]
    table = section[section.index("<table>"):section.index("</table>") + len("</table>")]
    assert re.findall(r"<th>(.*?)</th>", table) == ["指标", "当前计数", "业务页", "卡片入口"]   # 修前多一列「名称」
    rows = [re.findall(r"<td>(.*?)</td>", row, re.S) for row in re.findall(r"<tr>(.*?)</tr>", table, re.S)]
    shown = [(cells[0].strip(), cells[2].strip()) for cells in rows if cells]
    # 修前：(<span class="tag">critical_values</span>, critical) 这样的键与 hash
    assert shown == [(meta["label"], registry[meta["page"]]) for meta in METRIC_QUERIES.values()]
    assert _bare_codes(_visible(table)) == [], _visible(table)
    # 「下钻」按钮照旧按指标键取明细（属性里，不显示）
    assert 'data-drill="critical_values"' in table
