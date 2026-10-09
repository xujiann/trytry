"""危急值处置轨迹每一步、危急值清单每一行都出时刻，两端页面显示（P2-1364，第四十批扫描 AD3-4）。

模型 `CriticalAction` 的注释是「通知→确认→处置反馈全程记录」，每一步都落了 `created_at`，`schemas.CriticalActionOut` 却只有
id / report_id / action / actor；危急值清单的出参 `ExamReportOut` 也没有报告时刻。管理端的轨迹表（`pages-clinical.js`）只有
「动作 / 操作人」，医生移动端（`m/doctor.js`）只拼「操作人：动作」——何时通知、何时确认、何时处置都查不到，从通知到确认、
到处置各用了多久（危急值管理的核心指标，也是纠纷时的证据）只能查库。同模块的修订史早就出 `at`。修前实测：轨迹 4 条都没有
时间（库里 4 条的 created_at 俱全），清单行的键是 conclusion / critical / critical_status / finding / id / reported_by / request_id。

修法：`CriticalActionOut` 末尾只增 `at`（取 `created_at`），`ExamReportOut` 末尾只增 `reported_at`（报告表现成的出具时刻）；
口径与同模块已有的时刻（修订史的 `at`、超时未确认的 `reported_at`）相同——落库的 naive UTC 原样出 ISO 串，按本地时间显示
随 P1-105 一起定；两端页面截到分钟显示。原有的键与次序不动。
"""
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import CriticalAction, ExamReport

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
#: 危急值清单行与出报告、确认接收、处置反馈回执的键：原有的七个原样在前，`reported_at` 只加在末尾
REPORT_KEYS = ["finding", "conclusion", "critical", "reported_by", "id", "request_id", "critical_status", "reported_at"]
ACTION_KEYS = ["id", "report_id", "action", "actor", "at"]


@pytest.fixture
def east_eight(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        yield
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.fixture(scope="module")
def loop(client, admin):
    """一份危急值报告走完 出具→确认接收→处置反馈，留下三步轨迹；回执一并带回来。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21364 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21364 患者", "id_card": "330106197211133017", "gender": "女"})
    assert patient.status_code in (200, 201), patient.text
    req = client.post("/api/exams", headers=admin, json={
        "patient_id": patient.json()["id"], "from_org_id": org, "center_type": "lab", "item_code": "K",
        "item_name": "血钾"}).json()
    assert client.post(f"/api/exams/{req['id']}/claim", headers=admin).status_code == 200
    reported = client.post(f"/api/exams/{req['id']}/report", headers=admin, json={
        "conclusion": "血钾 7.1 mmol/L", "finding": "K 7.1（复测 7.0）", "critical": True, "reported_by": "检验科"})
    assert reported.status_code == 201, reported.text
    report_id = reported.json()["id"]
    acked = client.post(f"/api/exams/reports/{report_id}/acknowledge", headers=admin)
    resolved = client.post(f"/api/exams/reports/{report_id}/resolve", headers=admin, json={"note": "已通知患者急诊复查"})
    assert acked.status_code == resolved.status_code == 200, (acked.text, resolved.text)
    return {"id": report_id, "receipts": [reported.json(), acked.json(), resolved.json()]}


def _db_times(report_id):
    with SessionLocal() as db:
        actions = [a.created_at for a in db.query(CriticalAction).filter(CriticalAction.report_id == report_id)
                   .order_by(CriticalAction.id)]
        return db.get(ExamReport, report_id).reported_at, actions


def test_处置轨迹每一步带时刻_与库一致_键只加在末尾(client, admin, loop):
    actions = client.get(f"/api/exams/reports/{loop['id']}/critical-actions", headers=admin).json()
    _, created = _db_times(loop["id"])
    assert len(actions) == len(created) == 3   # 通知、确认接收、处置反馈
    assert [list(a) for a in actions] == [ACTION_KEYS] * 3   # 修前没有 at
    assert [a["at"] for a in actions] == [at.isoformat() for at in created]   # 同修订史的 at：落库时刻原样 ISO


def test_危急值清单与出报告确认处置的回执带出具时刻(client, admin, loop):
    reported_at, _ = _db_times(loop["id"])
    row = next(r for r in client.get("/api/exams/critical", headers=admin).json() if r["id"] == loop["id"])
    assert list(row) == REPORT_KEYS   # 修前没有 reported_at
    assert row["reported_at"] == reported_at.isoformat()
    for receipt in loop["receipts"]:   # 同一个出参：出报告 201、确认接收、处置反馈
        assert list(receipt) == REPORT_KEYS and receipt["reported_at"] == reported_at.isoformat()


def test_时刻与部署时区无关_与同页超时未确认一栏同一口径(client, admin, loop, east_eight):
    """落库是 naive UTC，出参原样给（不另起一套本地时刻口径）：同一页「超时未确认」一栏的 reported_at 也是这样出的，
    两栏并排不会差 8 小时；按本地时间显示随 P1-105 一起定。"""
    reported_at, created = _db_times(loop["id"])
    actions = client.get(f"/api/exams/reports/{loop['id']}/critical-actions", headers=admin).json()
    assert [a["at"] for a in actions] == [at.isoformat() for at in created]
    row = next(r for r in client.get("/api/exams/critical", headers=admin).json() if r["id"] == loop["id"])
    assert row["reported_at"] == reported_at.isoformat()


# ---------- 两端页面显示时刻 ----------


def _esc(text: str) -> str:
    """与 shared.js 的 `esc()` 同一张表。"""
    return "".join({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}.get(c, c) for c in text)


def _minute(moment: str) -> str:
    return moment[:16].replace("T", " ")


def _top_level(source: str, head: str, end: str = "\n}\n") -> str:
    start = source.index(head)
    return source[start:source.index(end, start) + len(end)]


#: 页面取数换成桩：`$()` 按选择器给一个记 innerHTML、登记监听的假元素，`api()` 按路径回给定的数据，`alert()` 记下文字
_HARNESS = """
const els = {};
let alerted = null;
globalThis.alert = (text) => { alerted = text; };
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { textContent: "", innerHTML: "", className: "", listeners: {},
    classList: { add() {}, remove() {} }, addEventListener(type, fn) { this.listeners[type] = fn; } }); } };
const DATA = JSON.parse(process.argv[1]);
async function api(path) { return DATA[path]; }
"""


def _run_node(script: str, data) -> dict:
    out = subprocess.run(["node", "-e", script, json.dumps(data, ensure_ascii=False)], capture_output=True, text=True,
                         timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.fixture(scope="module")
def page_data(client, admin, loop):
    return {
        "/api/exams/critical": client.get("/api/exams/critical", headers=admin).json(),
        # 两个危急值页把未处置的续页取全（P2-1711）：页面按 fetchAllPages 拼出来的地址取
        "/api/exams/critical?open=true&limit=500&offset=0":
            client.get("/api/exams/critical?open=true&limit=500&offset=0", headers=admin).json(),
        "/api/exams/critical/unacknowledged": [],
        f"/api/exams/reports/{loop['id']}/critical-actions":
            client.get(f"/api/exams/reports/{loop['id']}/critical-actions", headers=admin).json(),
    }


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_管理端危急值操作台_清单出报告时间_留痕轨迹出每一步时间(loop, page_data):
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    page = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    script = (_HARNESS + (STATIC / "shared.js").read_text(encoding="utf-8")
              + _top_level(core, "function table(") + _top_level(core, "function panel(")
              + _top_level(core, "function actionableFirst(")   # 未处置的排最前、按 id 去重（P2-1711）
              + _top_level(page, "const CRIT_STATUS = ")   # 状态表连同其后的 renderCritical
              + "(async () => { await renderCritical(); const listed = els['#page-body'].innerHTML;\n"
              f"  await els['#page-body'].onclick({{ target: {{ dataset: {{ trail: '{loop['id']}' }} }} }});\n"
              "  process.stdout.write(JSON.stringify({ listed, trail: els['#crit-trail'].innerHTML,"
              " msg: $('#crit-msg').textContent })); })();\n")
    out = _run_node(script, page_data)
    row = next(r for r in page_data["/api/exams/critical"] if r["id"] == loop["id"])
    assert "<th>报告时间</th>" in out["listed"]
    assert f"<td>{_esc(_minute(row['reported_at']))}</td>" in out["listed"]   # 修前清单没有时间
    assert "<th>时间</th><th>动作</th><th>操作人</th>" in out["trail"], out["msg"]   # 修前只有动作 / 操作人
    for a in page_data[f"/api/exams/reports/{loop['id']}/critical-actions"]:
        assert f"<tr><td>{_esc(_minute(a['at']))}</td><td>{_esc(a['action'])}</td><td>{_esc(a['actor'])}</td></tr>" \
            in out["trail"]


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_医生移动端危急值卡片出报告时间_处置轨迹每一步前面写时间(loop, page_data):
    doctor = (STATIC / "m" / "doctor.js").read_text(encoding="utf-8")
    script = (_HARNESS + (STATIC / "shared.js").read_text(encoding="utf-8")
              + _top_level(doctor, "function setMsg(") + _top_level(doctor, "function kv(")
              + _top_level(doctor, "function card(")
              + _top_level(doctor, "const CRITICAL_TAGS = ", "\n};\n") + _top_level(doctor, "async function loadCritical(")
              + _top_level(doctor, '$("#critical-list").addEventListener("click"', "\n});\n")
              + "(async () => { await loadCritical(); const listed = els['#critical-list'].innerHTML;\n"
              f"  await els['#critical-list'].listeners.click({{ target: {{ dataset: {{ trace: '{loop['id']}' }} }} }});\n"
              "  process.stdout.write(JSON.stringify({ listed, alerted, msg: $('#critical-msg').textContent })); })();\n")
    out = _run_node(script, page_data)
    row = next(r for r in page_data["/api/exams/critical"] if r["id"] == loop["id"])
    assert f'<span class="k">报告时间</span><span>{_esc(_minute(row["reported_at"]))}</span>' in out["listed"]
    actions = page_data[f"/api/exams/reports/{loop['id']}/critical-actions"]
    assert out["alerted"] == "\n".join(f"{_minute(a['at'])} {a['actor']}：{a['action']}" for a in actions), out["msg"]
