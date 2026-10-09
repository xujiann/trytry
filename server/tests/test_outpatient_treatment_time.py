"""门急诊处置记录没有执行时间缺省，页面「时间」列印的是落库的 UTC 时刻（P2-1633，第四十八批扫描 AL4-3）。

修前（实测，`TZ=Asia/Shanghai`）：页面那张处置表单不送 `performed_at`，接口缺省空串原样写入（雾化 `performed_at=''`）；同页
护理记录的 `recorded_at` 缺省取 `clock.now_local()`（P2-455 / P2-171 口径）。处置的两张表（就诊下的处置记录、按患者查的处置史）
「时间」列都是 `created_at.slice(0, 16)`——落库的 UTC 时刻：同一时刻记的雾化显示 04:52、护理显示 12:52；经接口补记执行于
08:00 的清创，页面照样显示 04:52（录入时刻），`performed_at` 全页不读。处置记录是纠纷举证用的（模型注释），执行时间看不到。

修法：`create_treatment` 不填执行时间就落此刻的本地时间（与同文件护理那一句同一写法）；处置出参末尾追加 `performed_at_shown`
（填了的原样，修前落库、没有执行时间的存量按落库时刻换本地时间——复用住院文书的 `clinical_docs._shown_time`，不另抄一份；
`performed_at` 本身原样，出参只加不改）；页面处置表单加执行时间（留空按此刻），两张处置表显示执行时间、护理表表头写「记录时间」。
执行时间早于就诊 / 写到将来不在本条（并入 P2-1135 / P2-1324 的门急诊侧）。
"""
import json
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import pytest
from conftest import login

from app.database import SessionLocal

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
FIXED = datetime(2026, 10, 9, 12, 52)


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21633 甲镇卫生院", "org_type": "township", "level": "township"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p21633_doc", "password": "passw0rd1", "role": "doctor", "org_id": org, "full_name": "王医生"})
    assert created.status_code in (200, 201), created.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21633 张三", "id_card": "330106197001011633", "gender": "男"}).json()["id"]
    doctor = login(client, "p21633_doc", "passw0rd1")
    enc = client.post("/api/encounters", headers=doctor, json={"patient_id": patient, "org_id": org})
    assert enc.status_code == 201, enc.text
    return {"org": org, "patient": patient, "doctor": doctor, "encounter": enc.json()["id"]}


def _rows(client, world, name: str) -> list[dict]:
    by_encounter = client.get(f"/api/outpatient/encounters/{world['encounter']}/treatments", headers=world["doctor"])
    by_patient = client.get("/api/outpatient/treatments", headers=world["doctor"], params={"patient_id": world["patient"]})
    assert (by_encounter.status_code, by_patient.status_code) == (200, 200), (by_encounter.text, by_patient.text)
    return [t for t in by_encounter.json() + by_patient.json() if t["treatment_name"] == name]


def test_不填执行时间_落此刻的本地时间(client, world, monkeypatch):
    from app.routers import outpatient_docs

    monkeypatch.setattr(outpatient_docs, "now_local", lambda: FIXED)
    r = client.post(f"/api/outpatient/encounters/{world['encounter']}/treatments", headers=world["doctor"],
                    json={"treatment_name": "P21633 雾化吸入", "site": "", "dose": "", "executor_name": "", "reaction": ""})
    assert r.status_code == 201, r.text
    assert r.json()["performed_at"] == "2026-10-09 12:52"   # 修前原样写入空串
    rows = _rows(client, world, "P21633 雾化吸入")
    assert len(rows) == 2   # 就诊下一条、处置史一条
    assert {(t["performed_at"], t["performed_at_shown"]) for t in rows} == {("2026-10-09 12:52", "2026-10-09 12:52")}


def test_手填的执行时间原样保存(client, world):
    r = client.post(f"/api/outpatient/encounters/{world['encounter']}/treatments", headers=world["doctor"],
                    json={"treatment_name": "P21633 清创换药", "performed_at": "2026-10-09 08:00"})
    assert r.status_code == 201, r.text
    assert (r.json()["performed_at"], r.json()["performed_at_shown"]) == ("2026-10-09 08:00", "2026-10-09 08:00")
    assert {t["performed_at_shown"] for t in _rows(client, world, "P21633 清创换药")} == {"2026-10-09 08:00"}


def test_存量没有执行时间的_按落库时刻换本地时间显示_执行时间本身原样(client, world, monkeypatch):
    from app.models import TreatmentRecord, User

    with SessionLocal() as db:
        author = db.query(User).filter(User.username == "p21633_doc").one().id
        db.add(TreatmentRecord(encounter_id=world["encounter"], patient_id=world["patient"], org_id=world["org"],
                               treatment_name="P21633 存量", performed_at="", created_by=author,
                               created_at=datetime(2026, 10, 9, 4, 52)))   # naive UTC
        db.commit()
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    time.tzset()
    try:
        rows = _rows(client, world, "P21633 存量")
    finally:
        monkeypatch.undo()
        time.tzset()
    assert len(rows) == 2
    # 修前页面印 created_at 的 04:52（UTC）；执行时间列原样是空串（未记录），只加一个给人看的键
    assert {(t["performed_at"], t["performed_at_shown"]) for t in rows} == {("", "2026-10-09 12:52")}


def test_出参新键接在末尾_原有键与次序不变(client, world):
    r = client.post(f"/api/outpatient/encounters/{world['encounter']}/treatments", headers=world["doctor"],
                    json={"treatment_name": "P21633 键序"})
    assert list(r.json()) == ["id", "encounter_id", "patient_id", "org_id", "treatment_name", "treatment_code", "site",
                              "dose", "executor_name", "performed_at", "reaction", "note", "created_at",
                              "performed_at_shown"]


# ---------- 页面 ----------


def _render_body() -> str:
    src = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    start = src.index("async function renderOutpatientDocs(")
    return src[start:src.index("\n}\n", start)]


def test_页面_处置表单有执行时间_两张处置表显示执行时间_护理表写记录时间():
    body = _render_body()
    assert '<label style="font-size:13px">执行时间（留空按此刻） <input name="performed_at" type="datetime-local"></label>' in body
    # 两张处置表（就诊下的处置记录、按患者查的处置史）都按执行时间显示，不再印落库的 created_at
    assert body.count('table(["处置", "部位", "剂量", "执行人", "反应", "执行时间"]') == 1
    assert body.count('table(["就诊", "处置", "部位", "剂量", "执行人", "反应", "执行时间"]') == 1
    assert body.count("${esc(t.performed_at_shown)}") == 2
    assert "t.created_at" not in body                                  # 修前两张表都是 created_at.slice(0, 16)
    assert 'table(["级别", "内容", "护士", "记录时间"]' in body


def _top_level(source: str, head: str, end: str = "\n}\n") -> str:
    start = source.index(head)
    return source[start:source.index(end, start) + len(end)]


_HARNESS = """
const els = {};
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { textContent: "", innerHTML: "", className: "", value: "",
    classList: { add() {}, remove() {} }, addEventListener() {} }); } };
const DATA = JSON.parse(process.argv[1]);
globalThis.localStorage = { getItem: (k) => (k === "medplat_od_encounter" ? "2" : null), setItem() {}, removeItem() {} };
async function api(path) { return path in DATA ? DATA[path] : []; }
async function route() {}
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_页面_处置表印执行时间_不印落库时刻():
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    mgmt = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    public = (STATIC / "pages-public.js").read_text(encoding="utf-8")
    script = (_HARNESS + (STATIC / "shared.js").read_text(encoding="utf-8")
              + _top_level(core, "function table(") + _top_level(core, "function panel(")
              + _top_level(core, "function actionableFirst(") + _top_level(core, "function currentRole(", "\n")
              + _top_level(core, "function encounterWho(")
              + _top_level(public, "const NURSING_LEVELS", "\n") + _top_level(mgmt, "const TPL_STATUS", "\n")
              + _top_level(mgmt, "const CONSENT_TYPES", "};\n") + _top_level(mgmt, "const CONSENT_RELATIONS", "\n")
              + _top_level(mgmt, "async function renderOutpatientDocs(")
              + "(async () => { await renderOutpatientDocs();\n"
              "  process.stdout.write(JSON.stringify(els['#page-body'].innerHTML)); })();\n")
    treatment = {"id": 1, "encounter_id": 2, "patient_id": 2, "org_id": 1, "treatment_name": "雾化吸入",
                 "treatment_code": "", "site": "", "dose": "", "executor_name": "", "performed_at": "",
                 "reaction": "", "note": "", "created_at": "2026-10-09T04:52:31",
                 "performed_at_shown": "2026-10-09 12:52"}
    completeness = {"encounter_id": 2, "patient_id": 2, "treatment_records": 1, "nursing_records": 1, "consents_total": 0,
                    "consents_pending": 0, "consents_refused": 0, "note": "", "patient_name": "张三",
                    "encounter_created_at": "2026-10-09T04:50:00", "org_name": "甲镇卫生院", "medical_record": 0,
                    "medical_record_grade": "", "has_diagnosis": False}
    data = {"/api/outpatient/encounters/2/completeness": completeness,
            "/api/outpatient/encounters/2/treatments": [treatment],
            "/api/outpatient/encounters/2/nursing-records": [{"id": 1, "nursing_level": "level3", "content": "输液",
                                                              "nurse_name": "", "recorded_at": "2026-10-09 12:53"}]}
    out = subprocess.run(["node", "-e", script, json.dumps(data, ensure_ascii=False)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    html = json.loads(out.stdout)
    assert "<td>2026-10-09 12:52</td>" in html     # 修前印 created_at 的 04:52（UTC），与护理那条差 8 小时
    assert "04:52" not in html
    assert "<td>2026-10-09 12:53</td>" in html
