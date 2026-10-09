"""门急诊文书、门诊病历按手输的就诊号定位，查得到这个号的接诊页认不出是谁的就诊，载入后也不回显——敲错一位，处置记录就写到
别人名下（P2-1631，第四十八批扫描 AL4-1）。

修前（实测）：60 次就诊时接诊页只列最新 50 行（`?limit=50`，X-Total-Count 60、页面不提示截断），列只有「ID / 患者ID /
机构ID / 类型 / 诊断 / 医师」——没有就诊时间与姓名，也没有按患者查的入口，张三那次就诊 #1 不在页上；照着相邻的号载入 #2
（李四的就诊），文书页拿得到的只有 `{encounter_id: 2, patient_id: 2}`，给张三做的青霉素皮试照样 201 记到李四名下。门诊病历
表单同样只有一个就诊号输入框。住院文书的选择框早按 P2-1335 改成「病区 床号 姓名」，理由同。

修法（不加迁移、不动授权，出参只加不改）：
- `EncounterOut` 末尾加 `created_at`（与 360 视图就诊段同一写法）与 `patient_name`（按一批取，`encounters._encounters_out`）；
  登记回执与清单同形（同住院行 P2-1335）；
- 门急诊完整性出参末尾加 `patient_name` / `encounter_created_at` / `org_name`；
- 接诊页加「按患者查」、就诊时间与姓名两列，读 X-Total-Count，列不全时标题写「已列 N / 共 total」（同 P2-1547）；
- 门急诊文书页载入后、门诊病历表单填好就诊号后回显「姓名 · 就诊时间 · 机构」（core.js `encounterWho`），一律 esc()。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import login
from sqlalchemy import event

from app.database import SessionLocal, engine
from app.models import Encounter

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

#: 修前 `EncounterOut` 的键与次序——新键只许接在后面
OLD_ENCOUNTER_KEYS = ["patient_id", "org_id", "doctor_name", "encounter_type", "diagnosis_code", "diagnosis_name",
                      "summary", "id"]
#: 修前门急诊完整性的键与次序
OLD_COMPLETENESS_KEYS = ["encounter_id", "patient_id", "treatment_records", "nursing_records", "consents_total",
                         "consents_pending", "consents_refused", "note"]


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21631 甲&乙卫生院", "org_type": "township", "level": "township"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p21631_doc", "password": "passw0rd1", "role": "doctor", "org_id": org, "full_name": "王医生"})
    assert created.status_code in (200, 201), created.text
    zhang = client.post("/api/patients", headers=admin, json={
        "name": "P21631 张三", "id_card": "330106197001011631", "gender": "男"}).json()["id"]
    li = client.post("/api/patients", headers=admin, json={
        "name": "P21631 李<四>", "id_card": "330106198001011631", "gender": "男"}).json()["id"]
    return {"org": org, "zhang": zhang, "li": li, "doctor": login(client, "p21631_doc", "passw0rd1")}


def _encounter(client, world, patient: int) -> dict:
    r = client.post("/api/encounters", headers=world["doctor"], json={"patient_id": patient, "org_id": world["org"]})
    assert r.status_code == 201, r.text
    return r.json()


def test_登记回执末尾带就诊时刻与姓名_原有键与次序不变(client, world):
    enc = _encounter(client, world, world["zhang"])
    assert list(enc) == OLD_ENCOUNTER_KEYS + ["created_at", "patient_name"]   # 修前只有前 8 个
    with SessionLocal() as db:
        row = db.get(Encounter, enc["id"])
        assert enc["created_at"] == row.created_at.isoformat()
    assert enc["patient_name"] == "P21631 张三"
    assert (enc["patient_id"], enc["org_id"], enc["encounter_type"], enc["doctor_name"]) == (
        world["zhang"], world["org"], "outpatient", "")


def test_就诊清单行带就诊时刻与姓名_按患者查只收这一位(client, world):
    mine = _encounter(client, world, world["zhang"])
    other = _encounter(client, world, world["li"])
    rows = client.get("/api/encounters", headers=world["doctor"]).json()
    by_id = {r["id"]: r for r in rows}
    assert list(by_id[mine["id"]]) == OLD_ENCOUNTER_KEYS + ["created_at", "patient_name"]
    assert by_id[mine["id"]]["patient_name"] == "P21631 张三"
    assert by_id[other["id"]]["patient_name"] == "P21631 李<四>"   # 出参原样，转义是页面的事
    assert by_id[other["id"]]["created_at"] == other["created_at"]
    zhang = client.get("/api/encounters", headers=world["doctor"], params={"patient_id": world["zhang"]})
    assert zhang.status_code == 200, zhang.text
    assert {r["patient_name"] for r in zhang.json()} == {"P21631 张三"}
    assert mine["id"] in [r["id"] for r in zhang.json()]
    assert int(zhang.headers["x-total-count"]) == len(zhang.json())


def test_就诊清单的姓名按一批取_不逐行查库(client, world):
    for _ in range(3):
        _encounter(client, world, world["li"])
    statements: list[str] = []

    def capture(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        rows = client.get("/api/encounters", headers=world["doctor"]).json()
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(rows) >= 5
    name_queries = [s for s in statements if "patients.name" in s]
    assert len(name_queries) == 1, name_queries


def test_接诊页按患者查_取得到被挤出最新一页的那次就诊(client, world):
    """修前接诊页只取 `?limit=50`、没有按患者查：张三那次就诊被后来的 59 人次挤出这一页，页面上找不到。"""
    first = _encounter(client, world, world["zhang"])
    for _ in range(55):
        _encounter(client, world, world["li"])
    page = client.get("/api/encounters?limit=50", headers=world["doctor"])
    assert first["id"] not in [r["id"] for r in page.json()]
    assert int(page.headers["x-total-count"]) > 50                  # 页面据此写「已列 50 / 共 N」
    found = client.get(f"/api/encounters?limit=50&patient_id={world['zhang']}", headers=world["doctor"]).json()
    assert first["id"] in [r["id"] for r in found]


def test_门急诊完整性末尾带姓名_就诊时刻与机构_原有键不变(client, world):
    enc = _encounter(client, world, world["li"])
    r = client.get(f"/api/outpatient/encounters/{enc['id']}/completeness", headers=world["doctor"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert list(body)[:len(OLD_COMPLETENESS_KEYS)] == OLD_COMPLETENESS_KEYS
    assert (body["encounter_id"], body["patient_id"]) == (enc["id"], world["li"])
    assert {k: body[k] for k in ("patient_name", "encounter_created_at", "org_name")} == {
        "patient_name": "P21631 李<四>", "encounter_created_at": enc["created_at"], "org_name": "P21631 甲&乙卫生院"}


# ---------- 页面 ----------


def _top_level(source: str, head: str, end: str = "\n}\n") -> str:
    start = source.index(head)
    return source[start:source.index(end, start) + len(end)]


#: `$()` 按选择器给一个假元素（带 `encounter_id` 子元素，门诊病历表单按它挂回显）；`api()` 按路径回 DATA.responses 里的
#: 数据（`DATA.errors` 里的路径抛错），带 `withTotal` 的回 `{ rows, total }`；记下每次调用的路径
_HARNESS = """
const els = {};
const calls = [];
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { textContent: "", innerHTML: "", className: "", value: "",
    encounter_id: { value: "" }, classList: { add() {}, remove() {} }, addEventListener() {} }); } };
const DATA = JSON.parse(process.argv[1]);
const STORE = DATA.storage || {};
globalThis.localStorage = { getItem: (k) => (k in STORE ? STORE[k] : null), setItem() {}, removeItem() {} };
async function api(path, opts = {}) {
  calls.push(path);
  if (path in (DATA.errors || {})) throw new Error(DATA.errors[path]);
  if (!(path in DATA.responses)) throw new Error(`夹具里没有 ${path}`);
  const data = DATA.responses[path];
  return opts.withTotal ? { rows: data, total: DATA.total ?? null } : data;
}
async function route() {}
"""


def _run(script: str, data: dict) -> dict:
    out = subprocess.run(["node", "-e", script, json.dumps(data, ensure_ascii=False)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _base() -> str:
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    return (_HARNESS + (STATIC / "shared.js").read_text(encoding="utf-8")
            + _top_level(core, "function table(") + _top_level(core, "function panel(")
            + _top_level(core, "function setMsg(") + _top_level(core, "function actionableFirst(")
            + _top_level(core, "function currentRole(", "\n") + _top_level(core, "function encounterWho("))


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")

#: 一行就诊（接口真实的形状，见上面的用例）：姓名里带尖括号，钉住转义
ROW = {"patient_id": 2, "org_id": 1, "doctor_name": "", "encounter_type": "outpatient", "diagnosis_code": "",
       "diagnosis_name": "", "summary": "", "id": 9, "created_at": "2026-10-09T04:52:31.123456",
       "patient_name": "李<四>"}


def _render_archive(data: dict, patient_filter: str = "") -> dict:
    page = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    script = (_base() + _top_level(page, "const ENC_FILTER", "\n") + _top_level(page, "async function renderArchive(")
              + f"ENC_FILTER.patient_id = {json.dumps(patient_filter)};\n"
              "(async () => { await renderArchive();\n"
              "  process.stdout.write(JSON.stringify({ html: els['#page-body'].innerHTML, calls })); })();\n")
    return _run(script, data)


@needs_node
def test_页面_接诊页列不全时标题写已列N共total_行上有就诊时间与姓名_一律转义():
    out = _render_archive({"responses": {"/api/encounters?limit=50": [ROW, {**ROW, "id": 8}]}, "total": 60})
    assert out["calls"] == ["/api/encounters?limit=50"]
    assert "<h3 style=\"margin-top:12px\">就诊记录（已列 2 / 共 60）</h3>" in out["html"]   # 修前不提示截断
    assert "<th>就诊时间</th><th>患者</th>" in out["html"]
    assert "<td>2026-10-09 04:52</td>" in out["html"]
    assert "<td>李&lt;四&gt;（2）</td>" in out["html"]
    assert 'id="enc-filter"' in out["html"] and "按患者查" in out["html"]
    assert "李<四>" not in out["html"]


@needs_node
def test_页面_接诊页按患者查_取数带上患者号_列全了只写条数():
    out = _render_archive({"responses": {"/api/encounters?limit=50&patient_id=2": [ROW]}, "total": 1},
                          patient_filter="2")
    assert out["calls"] == ["/api/encounters?limit=50&patient_id=2"]
    assert "按患者查的就诊记录（1）" in out["html"]
    assert 'name="patient_id" type="number" value="2"' in out["html"]


@needs_node
def test_页面_接诊页按患者查被拒_只在表那一段报错_登记表单照画():
    path = "/api/encounters?limit=50&patient_id=5"
    out = _render_archive({"responses": {}, "errors": {path: "无权调阅该患者档案：<本机构>无关系"}}, patient_filter="5")
    assert '<p class="msg err">无权调阅该患者档案：&lt;本机构&gt;无关系</p>' in out["html"]
    assert 'id="enc-form"' in out["html"]
    assert "就诊记录" in out["html"] and "（0）" in out["html"]


COMPLETENESS = {"encounter_id": 2, "patient_id": 2, "treatment_records": 0, "nursing_records": 0, "consents_total": 0,
                "consents_pending": 0, "consents_refused": 0, "note": "只列事实", "patient_name": "李<四>",
                "encounter_created_at": "2026-10-09T04:52:31.123456", "org_name": "甲&乙卫生院"}
WHO = "<b>李&lt;四&gt;</b> · 2026-10-09 04:52 · 甲&amp;乙卫生院"


@needs_node
def test_页面_门急诊文书载入后回显姓名_就诊时间_机构_一律转义():
    mgmt = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    public = (STATIC / "pages-public.js").read_text(encoding="utf-8")
    script = (_base() + _top_level(public, "const NURSING_LEVELS", "\n") + _top_level(mgmt, "const TPL_STATUS", "\n")
              + _top_level(mgmt, "const CONSENT_TYPES", "};\n") + _top_level(mgmt, "const CONSENT_RELATIONS", "\n")
              + _top_level(mgmt, "async function renderOutpatientDocs(")
              + "(async () => { await renderOutpatientDocs();\n"
              "  process.stdout.write(JSON.stringify({ html: els['#page-body'].innerHTML, calls })); })();\n")
    out = _run(script, {"storage": {"medplat_od_encounter": "2"}, "responses": {
        "/api/outpatient/consent-templates": [], "/api/outpatient/consents?limit=50": [],
        "/api/outpatient/consents?status=pending&limit=500": [],
        "/api/outpatient/encounters/2/treatments": [], "/api/outpatient/encounters/2/nursing-records": [],
        "/api/outpatient/encounters/2/completeness": COMPLETENESS}})
    assert f'<p class="desc" id="od-who">已载入就诊 #2：{WHO}' in out["html"]   # 修前载入后只有患者号
    assert "李<四>" not in out["html"]


def _render_quality(data: dict, typed: str) -> dict:
    page = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    script = (_base() + _top_level(page, "const MR_FIELDS", "];\n") + _top_level(page, "const MR_GRADE_COLOR", "\n")
              + _top_level(page, "async function renderQuality(")
              + "(async () => { await renderQuality();\n"
              f"  const input = {{ value: {json.dumps(typed)} }};\n"
              "  await els['#mr-form'].encounter_id.onchange({ target: input });\n"
              "  process.stdout.write(JSON.stringify({ who: els['#mr-who'].innerHTML,\n"
              "    calls: calls.filter((p) => p.includes('/encounters/')) })); })();\n")
    responses = {
        "/api/quality/adverse-events": [], "/api/quality/adverse-events-stats": {"total": 0, "closed_loop_pct": 0},
        "/api/quality/record-qc-stats": {"total": 0, "avg_score": 0, "grade_a_pct": 0},
        "/api/quality/infection-reports": [],
        "/api/quality/records/qc-summary": {"grade_distribution": {"甲": 0, "乙": 0, "丙": 0}, "avg_score": 0,
                                            "by_org": [], "by_doctor": []},
        "/api/quality/records": [], "/api/quality/record-qc-rules": [],
        "/api/quality/infection-stats": {"confirmed": 0, "pending_verify": 0, "by_site": {}},
        **data.get("responses", {})}
    return _run(script, {**data, "responses": responses})


@needs_node
def test_页面_门诊病历填好就诊号即回显姓名_就诊时间_机构_一律转义():
    out = _render_quality({"responses": {"/api/outpatient/encounters/2/completeness": COMPLETENESS}}, "2")
    assert out["calls"] == ["/api/outpatient/encounters/2/completeness"]
    assert out["who"] == f"就诊 #2：{WHO}"   # 修前表单上没有任何回显


@needs_node
def test_页面_门诊病历就诊号取不到写原因():
    path = "/api/outpatient/encounters/7/completeness"
    out = _render_quality({"errors": {path: "就诊记录<不存在>"}}, "7")
    assert out["who"] == '<span class="msg err">就诊 #7：就诊记录&lt;不存在&gt;</span>'
