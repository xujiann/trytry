"""人员下沉页锁在当年：1 月起，上一年度的下沉指标和「待补职称等级」筛选在页面上都看不到（P2-1509，第四十四批扫描 AH2-9）。

人员下沉页（`pages-mgmt.js::renderStaffing`）取下沉统计与台账的两次请求都不带年度：统计缺省取当年，台账的 `needs_level`
筛选固定取今年。下沉指标按年度上报，1 月 1 日一过，上一年度满半年的长期派驻在页面上就看不到了。修前实测（scan44 ah2/r4）：
一条 2026-01-05 起、12-20 结束的长期派驻，职称等级未填；业务日拨到 2027-01-10，页面那两次调用——统计回 year 2027、orgs 为空、
unknown 0，`needs_level=true` 回 0 条；直接调统计 `?year=2026` 甲镇「长期满半年」1、unknown 1，台账带 `year=2026` 却被忽略、
照旧 0 条。

修法：页面补年度选择（只留在内存里，照绩效页 PERF_FILTER，缺省当年），统计与台账两次请求带同一个年度；台账的 `needs_level`
收可选的 `year`，与统计取同一句（不带照旧取今年，原有语义不变）。统计年度越界（负数、五位数）两条路都 422（原先统计一侧
构造年初日期抛错、回 500）。

页面这一半把 `renderStaffing` 原样拿到 node 里跑，页面的 `api` 经管道转给真接口（同 test_accounting_cost_project_org_names）。
"""
import json
import os
import re
import shutil
import subprocess
from datetime import date

import pytest

from app.database import SessionLocal
from app.models import Employee, Secondment
from conftest import freeze_business_date

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")
#: 业务日拨到次年 1 月：上一年度的派驻已经满半年，当年还没有任何人满半年
NEXT_JANUARY = date(2027, 1, 10)
MID_YEAR = date(2026, 10, 4)
TOWN = "P21509 甲镇卫生院"
DOCTOR = "P21509 赵医生"


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = {}
    for name, otype, level in (("P21509 县人民医院", "lead_hospital", "county"), (TOWN, "township", "township")):
        resp = client.post("/api/organizations", headers=admin, json={"name": name, "org_type": otype, "level": level})
        assert resp.status_code == 201, resp.text
        orgs[level] = resp.json()["id"]
    with SessionLocal() as db:
        employee = Employee(org_id=orgs["county"], name=DOCTOR, title="主治医师", title_level="none")
        db.add(employee)
        db.flush()
        row = Secondment(employee_id=employee.id, from_org_id=orgs["county"], to_org_id=orgs["township"],
                         start_date="2026-01-05", end_date="2026-12-20", assignment_type="long_term")
        db.add(row)
        db.commit()
        return {"orgs": orgs, "row": row.id}


def _ledger(client, admin, query: str) -> tuple[list[int], int]:
    resp = client.get(f"/api/staffing/secondments?{query}", headers=admin)
    assert resp.status_code == 200, (query, resp.text)
    return [r["id"] for r in resp.json()], int(resp.headers["X-Total-Count"])


def test_台账needs_level按year取上一年度_与统计同一批(client, admin, world):
    with freeze_business_date(NEXT_JANUARY):
        stats = client.get("/api/staffing/dispatch-stats?year=2026", headers=admin).json()
        flagged, count = _ledger(client, admin, "needs_level=true&year=2026")
        rest, _ = _ledger(client, admin, "needs_level=false&year=2026")
    assert stats["year"] == 2026 and stats["unknown_title_level"] == 1
    assert [(o["org_name"], o["long_term_6m"], o["long_term_6m_senior"]) for o in stats["orgs"]] == [(TOWN, 1, 0)]
    assert flagged == [world["row"]] and count == stats["unknown_title_level"]   # 修前 year 被忽略：0 条
    assert world["row"] not in rest


def test_不带year的台账照旧取今年(client, admin, world):
    for today, flagged_expected in ((NEXT_JANUARY, []), (MID_YEAR, [world["row"]])):
        with freeze_business_date(today):
            stats = client.get("/api/staffing/dispatch-stats", headers=admin).json()
            flagged, count = _ledger(client, admin, "needs_level=true")
            same_year, _ = _ledger(client, admin, f"needs_level=true&year={today.year}")
            rest, _ = _ledger(client, admin, "needs_level=false")
        assert stats["year"] == today.year
        assert flagged == same_year == flagged_expected and count == stats["unknown_title_level"], today
        assert (world["row"] in rest) is (not flagged_expected), today


def test_year只作用于needs_level_台账本身不按年度筛(client, admin, world):
    with freeze_business_date(NEXT_JANUARY):
        assert _ledger(client, admin, "year=2025") == _ledger(client, admin, "") == ([world["row"]], 1)


@pytest.mark.parametrize("year", [10000, -1, 10 ** 20])   # 最后一个超出 C long：date() 抛 OverflowError
def test_统计年度越界两条路都422(client, admin, world, year):
    stats = client.get(f"/api/staffing/dispatch-stats?year={year}", headers=admin)
    assert stats.status_code == 422, stats.text   # 修前统计一侧 500（年初日期构造不出来）
    ledger = client.get(f"/api/staffing/secondments?needs_level=true&year={year}", headers=admin)
    assert ledger.status_code == 422, ledger.text


# ---------- 页面：年度选择，统计与台账两次请求带同一个年度 ----------


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def _section(source: str, head: str, next_head: str) -> str:
    start = source.index(head)
    return source[start:source.index(next_head, start)]


#: 页面取数换成桩：`$()` 按选择器给一个记 innerHTML 的假元素；`FormData` 读假表单的 `fields`；`api()` 经标准输出把地址交给
#: 测试进程、从标准输入读回真接口的响应；`route()` 照真路由重画这一页（记下这次重画，脚本等它画完再往下）
_HARNESS = r"""
const els = {};
const mk = () => ({ textContent: "", innerHTML: "", value: "", className: "", dataset: {},
  classList: { add() {}, remove() {} } });
globalThis.document = { addEventListener() {}, cookie: "", querySelector(sel) { return (els[sel] ||= mk()); } };
globalThis.FormData = class { constructor(form) { this.fields = form.fields || {}; }
  get(k) { return k in this.fields ? this.fields[k] : null; } };
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const requested = [];
async function api(path) {
  requested.push(path);
  process.stdout.write(JSON.stringify({ get: path }) + "\n");
  return JSON.parse((await lines.next()).value);
}
let redraw = Promise.resolve();
function route() { redraw = renderStaffing(); return redraw; }
function postAction() {}
function formJson() { return {}; }
function setMsg() {}
async function spdModal() { return null; }
const done = (result) => { process.stdout.write(JSON.stringify({ result }) + "\n"); rl.close(); };
"""

#: 缺省渲染一遍；在年度框里填 2026 提交、等重画；再点提示条上的「台账只看这 N 人次」、等重画。每一步记下页面与请求过的地址
_SCRIPT = r"""
(async () => {
  const steps = [];
  const snap = () => steps.push({ body: els["#page-body"].innerHTML, requested: requested.splice(0) });
  await renderStaffing();
  snap();
  els["#st-year"].onsubmit({ preventDefault() {}, target: { fields: { year: " 2026 " } } });
  await redraw;
  snap();
  await els["#page-body"].onclick({ target: { dataset: { stneeds: "1" } } });
  snap();
  done(steps);
})();
"""


def _run_page(client, headers) -> list[dict]:
    core, mgmt = _read("core.js"), _read("pages-mgmt.js")
    script = (_HARNESS + _read("shared.js") + _top_level(core, "function table(") + _top_level(core, "function panel(")
              + _section(mgmt, "const ASSIGN_TYPES = ", "/* ---------------- 专病管理") + _SCRIPT)
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


def _table(page: str, marker: str) -> list[list[str]]:
    """`marker` 之后第一张表的各行（每行的各格，去掉首尾空白）。"""
    start = page.index(marker)
    table = page[page.index("<table>", start):page.index("</table>", start)]
    rows = [re.findall(r"<td>(.*?)</td>", row, re.S) for row in re.findall(r"<tr>(.*?)</tr>", table, re.S)]
    return [[cell.strip() for cell in cells] for cells in rows if cells]


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_页面选2026_统计与待补职称等级都取到2026那条派驻(client, admin, world):
    with freeze_business_date(NEXT_JANUARY):
        default, picked, needs = _run_page(client, admin)
    # 缺省当年：两次请求照旧不带年度，2026 年那条派驻不在下沉指标里
    assert default["requested"] == ["/api/staffing/secondments?limit=100", "/api/staffing/dispatch-stats",
                                    "/api/organizations"]
    assert "<h3>下沉指标（2027 年度）</h3>" in default["body"] and "人次满足长期派驻满半年" not in default["body"]
    assert '<form class="inline" id="st-year">' in default["body"] and 'name="year"' in default["body"]   # 修前没有年度选择
    # 选 2026：统计与台账两次请求带同一个年度，下沉指标与提示条都是 2026 年的
    assert picked["requested"] == ["/api/staffing/secondments?limit=100&year=2026", "/api/staffing/dispatch-stats?year=2026",
                                   "/api/organizations"]
    assert "<h3>下沉指标（2026 年度）</h3>" in picked["body"] and 'value="2026"' in picked["body"]
    assert _table(picked["body"], "<h3>下沉指标（2026 年度）</h3>") == [[TOWN, "0", "1", "1", "<b>0</b>"]]
    assert "有 1 人次满足长期派驻满半年" in picked["body"] and 'data-stneeds="1"' in picked["body"]
    # 点提示条：台账按同一个年度筛「待补职称等级」，筛出的正是提示条报的那 1 人次
    assert needs["requested"] == ["/api/staffing/secondments?limit=100&needs_level=true&year=2026",
                                  "/api/staffing/dispatch-stats?year=2026", "/api/organizations"]
    ledger = _table(needs["body"], "<h3>派驻台账</h3>")
    assert [cells[0] for cells in ledger] == [DOCTOR]
