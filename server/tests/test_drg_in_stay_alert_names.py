"""DRG 事中预警表的「患者」「机构」两列印姓名与机构名，不再只印编号（P2-1537，第四十五批扫描 AI4-8）。

修前：`GET /api/drgs/in-stay-alerts` 的预警行（`DrgInStayAlertOut`）只有 `patient_id` / `org_id`，DRG 页（`pages-public.js::
renderDrgs`）的事中预警表照印——「患者 17、机构 3」。预警是要人去处置的（住院日已明显超出同组均值），管理层看多家机构时更
认不出是谁；住院页的同一个形状 P2-1335 早已补上姓名、病区与床号。

修法：预警行末尾只增 `patient_name` / `org_name`（原有八键与次序不动，契约网 test_drgs_contract.py 同步追加），按这一批预警的
id 各取一次（与住院清单 `inpatient._admissions_out`、随访清单 `followups._name_maps` 同一写法），不逐行查库；页面 `esc()` 显示，
取不到的回显编号。
"""
import json
import os
import shutil
import subprocess
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import event

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")

#: 预警行原有八键（test_drgs_contract.py 的 ALERT_ROW_KEYS），新键只许追加在末尾
ALERT_ROW_KEYS = [
    "admission_id", "patient_id", "org_id", "drg_code",
    "stayed_days", "baseline_avg_days", "baseline_cases", "over_ratio",
]

_N = [0]


def _admit(client, admin, org: int, patient_name: str, diagnosis: str = "社区获得性肺炎") -> dict:
    _N[0] += 1
    n = _N[0]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": f"P21537 病区{n}"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": f"A{n}"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": patient_name, "id_card": f"33010619680808{n:04d}"}).json()["id"]
    adm = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": diagnosis})
    assert adm.status_code == 201, adm.text
    summary = client.post(f"/api/inpatient/admissions/{adm.json()['id']}/case-summary", headers=admin, json={
        "discharge_diagnosis": diagnosis, "total_cost": 5000})
    assert summary.status_code == 201, summary.text
    return {"admission": adm.json()["id"], "patient": patient}


def _org(client, admin, name: str) -> int:
    resp = client.post("/api/organizations", headers=admin, json={
        "name": name, "org_type": "lead_hospital", "level": "county"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _backdate(admission_id: int, admitted_days_ago: int, discharged_days_ago: int | None = None) -> None:
    """入出院时刻由服务端落笔、HTTP 种不出跨天住院：改到「今天往前若干天」（页面取预警不带 today，按业务日现算）。"""
    from app.database import SessionLocal
    from app.models import Admission, utcnow

    now = utcnow()
    with SessionLocal() as db:
        row = db.get(Admission, admission_id)
        row.admitted_at = now - timedelta(days=admitted_days_ago)
        if discharged_days_ago is not None:
            row.discharged_at = now - timedelta(days=discharged_days_ago)
        db.commit()


@pytest.fixture(scope="module")
def world(client, admin):
    """ES31 出院 5 例（住院日 10 天，做基线）；在院 ES31 两例都已住 25 天（超同组均值 1.5 倍），分属两家机构。"""
    base_org = _org(client, admin, "P21537 基线县医院")
    for i in range(5):
        case = _admit(client, admin, base_org, f"P21537 基线患者{i}")
        assert client.post(f"/api/inpatient/admissions/{case['admission']}/discharge", headers=admin).status_code == 200
        _backdate(case["admission"], 40, 30)
    org_a = _org(client, admin, 'P21537 县医院<东院>&"甲"')     # 带 HTML 特殊字符：页面要 esc()
    org_b = _org(client, admin, "P21537 中心卫生院")
    a = _admit(client, admin, org_a, 'P21537 王<五>&"六"')
    b = _admit(client, admin, org_b, "P21537 赵七")
    for case in (a, b):
        _backdate(case["admission"], 25)
    return {"a": {**a, "org": org_a}, "b": {**b, "org": org_b}}


def _alerts(client, admin) -> dict:
    resp = client.get("/api/drgs/in-stay-alerts", headers=admin)
    assert resp.status_code == 200, resp.text
    return {r["admission_id"]: r for r in resp.json()["alerts"]}


def test_预警行带患者姓名与机构名_末尾追加_原有八键不动(client, admin, world):
    alerts = _alerts(client, admin)
    a, b = alerts[world["a"]["admission"]], alerts[world["b"]["admission"]]
    assert list(a) == ALERT_ROW_KEYS + ["patient_name", "org_name"], list(a)   # 修前只有八键：没有名字
    assert (a["patient_id"], a["org_id"], a["drg_code"]) == (world["a"]["patient"], world["a"]["org"], "ES31")
    assert (a["patient_name"], a["org_name"]) == ('P21537 王<五>&"六"', 'P21537 县医院<东院>&"甲"')
    assert (b["patient_name"], b["org_name"]) == ("P21537 赵七", "P21537 中心卫生院")
    assert a["stayed_days"] >= 25 and a["baseline_avg_days"] == 10.0


@contextmanager
def _count_sql():
    from app.database import engine

    counter = {"n": 0}

    def _tick(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _tick)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", _tick)


def test_名字按这一批取_查询数不随预警行数涨(client, admin, world):
    """姓名、机构名按这一批预警各取一次：多出 6 条预警、分属 6 家机构，SQL 条数不变（逐行查就是每行两次往返）。"""
    from app.visibility import clear_visibility_cache

    def counted() -> tuple[int, int]:
        clear_visibility_cache()
        with _count_sql() as counter:
            alerts = _alerts(client, admin)
        assert all(r["patient_name"] and r["org_name"] for r in alerts.values()), alerts
        return counter["n"], len(alerts)

    counted()   # 预热：首个请求可能多几条一次性的查询
    before, rows_before = counted()
    for i in range(6):
        case = _admit(client, admin, _org(client, admin, f"P21537 加测机构{i}"), f"P21537 加测患者{i}")
        _backdate(case["admission"], 25)
    after, rows_after = counted()
    assert rows_after == rows_before + 6
    assert after == before, f"查询数随预警行数增长：{before} -> {after}"


# ---------------------------------------------------------------- 页面：预警表印姓名与机构名

pytest_node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


#: 页面取数换成桩（写法照 test_drg_group_catalog_edit 的 `_HARNESS`）：`localStorage` 只回 `args.role`，`api()` 经管道转给真接口
_HARNESS = r"""
const args = JSON.parse(process.argv[1]);
const els = {};
const mk = () => ({ textContent: "", innerHTML: "", value: "", className: "", dataset: {},
  classList: { add() {}, remove() {} } });
globalThis.document = { addEventListener() {}, cookie: "", querySelector(sel) { return (els[sel] ||= mk()); } };
globalThis.localStorage = { getItem(key) { return key === "medplat_role" ? args.role : null; }, setItem() {} };
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
async function api(path, opts = {}) {
  const method = opts.method || "GET";
  const body = opts.body ? JSON.parse(opts.body) : null;
  process.stdout.write(JSON.stringify({ req: { method, path, body } }) + "\n");
  const resp = JSON.parse((await lines.next()).value);
  if (resp.status >= 400) {
    const err = new Error(errorText(resp.body.detail, `请求失败(${resp.status})`));
    err.status = resp.status;
    throw err;
  }
  return resp.body;
}
function route() {}
function setMsg() {}
function formJson() { return {}; }
async function postAction() {}
async function spdModal() { return null; }
"""

_RUN = r"""
(async () => {
  await renderDrgs();
  process.stdout.write(JSON.stringify({ result: { alerts: els["#drg-alerts"].innerHTML } }) + "\n");
  rl.close();
})().catch((err) => {
  process.stdout.write(JSON.stringify({ error: String((err && err.stack) || err) }) + "\n");
  rl.close();
});
"""


def _alerts_html(client, headers, role: str = "admin", rewrite=None) -> str:
    """渲染 DRG 页，回事中预警那一块的 HTML；`rewrite` 可改写预警接口的响应体（造「取不到名字」）。"""
    core, page = _read("core.js"), _read("pages-public.js")
    script = (_HARNESS + _read("shared.js")
              + "".join(_top_level(core, head) for head in (
                  "function table(", "function panel(", "function currentRole(", "function barChart("))
              + _top_level(page, "async function renderDrgs(") + _RUN)
    proc = subprocess.Popen(["node", "-e", script, json.dumps({"role": role})],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "result" in message:
                return message["result"]["alerts"]
            assert "error" not in message, message["error"]
            req = message["req"]
            resp = client.request(req["method"], req["path"], headers=headers, json=req["body"])
            body = resp.json()
            if rewrite and req["path"].startswith("/api/drgs/in-stay-alerts"):
                body = rewrite(body)
            proc.stdin.write(json.dumps({"status": resp.status_code, "body": body}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def _alert_row(html: str, admission_id: int) -> str:
    at = html.index(f"<tr><td>{admission_id}</td>")
    return html[at:html.index("</tr>", at)]


@pytest_node
def test_预警表印姓名与机构名_经esc_不再只印编号(client, admin, world):
    html = _alerts_html(client, admin)
    row = _alert_row(html, world["a"]["admission"])
    # 修前「<td>17</td><td>3</td>」：患者 ID 与机构 ID
    assert (f"<td>{world['a']['admission']}</td><td>P21537 王&lt;五&gt;&amp;&quot;六&quot;</td>"
            "<td>P21537 县医院&lt;东院&gt;&amp;&quot;甲&quot;</td>") in row, row
    row = _alert_row(html, world["b"]["admission"])
    assert f"<td>{world['b']['admission']}</td><td>P21537 赵七</td><td>P21537 中心卫生院</td>" in row, row


@pytest_node
def test_取不到名字的回显编号(client, admin, world):
    def drop_names(body):
        for r in body["alerts"]:
            r["patient_name"], r["org_name"] = "", ""
        return body

    row = _alert_row(_alerts_html(client, admin, rewrite=drop_names), world["b"]["admission"])
    assert f"<td>{world['b']['admission']}</td><td>{world['b']['patient']}</td><td>{world['b']['org']}</td>" in row, row
