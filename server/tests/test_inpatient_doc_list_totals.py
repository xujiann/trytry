"""文书页、医生移动端查房的标题把第一页的条数当总数印（P2-1771，第五十二批扫描 AP3-9）。

病程、护理两张清单一页 100 条、体温单一页 500 次，都从最近的往前翻（P2-362 / P2-451 / P1-81），总数在 X-Total-Count 里；
桌面住院临床文书页（`pages-mgmt.js::renderClinicalDocs`）的三块标题与医生移动端查房（`m/doctor.js::refreshRoundDetail`）的
「病程记录 N 条 / 体征记录 N 条」「病程记录（N）」原先都印这一页的条数。修前实测：101 条护理、101 条病程时，清单 100 条、
X-Total-Count 101、完整性 `nursing_records = 101`，标题却印「（100）」；体征过 500 次恒为「（500）」。同形已修 P2-1550 / P2-1693。

修法：三处与移动端改用 `withTotal` 读总数（移动端的 `api` 照管理端 core.js P2-1547、居民端 m.js P2-1674 补上这个选项），
列不全时写「已列 N / 共 M」，列全时与原先一字不差。

页面函数原样拿到 node 里跑：桌面见 `clinical_docs_page.py`；移动端取 `m/doctor.js` 的 `api`（垫一个经管道转给真接口的
`fetch`，响应头只垫 X-Total-Count）、`kv`、`card` 与 `refreshRoundDetail` 原文。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import clinical_docs_page

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")


@pytest.fixture(scope="module")
def world(client, admin):
    """两次在院：`many` 有 101 条病程、101 条护理、501 次体征（各多出一页一条）；`few` 各 3 条。"""
    from app.database import SessionLocal
    from app.models import NursingRecord, ProgressNote, User, VitalSignRecord

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21771 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P21771 病区"}).json()["id"]
    ids = {}
    for i, (key, notes, vitals) in enumerate((("many", 101, 501), ("few", 3, 3))):
        bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": f"T{i}"}).json()["id"]
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P21771 患者{key}", "id_card": f"33010619800101{1771 + i:04d}"}).json()["id"]
        created = client.post("/api/inpatient/admissions", headers=admin, json={
            "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "脑梗死恢复期"})
        assert created.status_code == 201, created.text
        aid = ids[key] = created.json()["id"]
        with SessionLocal() as db:   # 逐条走接口太慢，直接落库
            operator = db.query(User).filter(User.username == "admin").one().id
            db.add_all([ProgressNote(admission_id=aid, note_type="daily", content=f"日常病程 {n}", doctor_name="P21771",
                                     recorded_at="2026-10-01 08:00", created_by=operator) for n in range(notes)])
            db.add_all([NursingRecord(admission_id=aid, content=f"巡视 {n}", nurse_name="P21771",
                                      recorded_at="2026-10-01 08:00", created_by=operator) for n in range(notes)])
            db.add_all([VitalSignRecord(admission_id=aid, measured_at="2026-10-01 08:00", temperature=36.5,
                                        recorder="P21771", created_by=operator) for _ in range(vitals)])
            db.commit()
    return ids


def test_接口前提_一页列不全_总数在响应头里(client, admin, world):
    base = f"/api/inpatient/admissions/{world['many']}"
    for path, page, total in (("progress-notes", 100, 101), ("nursing-records", 100, 101), ("vitals", 500, 501)):
        resp = client.get(f"{base}/{path}", headers=admin)
        assert (len(resp.json()), resp.headers["x-total-count"]) == (page, str(total)), path


def _desktop(client, admin, aid):
    html, _ = clinical_docs_page.run(client, admin, "await renderClinicalDocs(); return pageHtml();",
                                     storage={"medplat_doc_adm": str(aid)})
    return html


def test_桌面三块标题_列不全写已列与共几条(client, admin, world):
    html = _desktop(client, admin, world["many"])
    assert "<h3>病程记录（已列 100 / 共 101）</h3>" in html   # 修前「病程记录（100）」
    assert "<h3>护理记录（已列 100 / 共 101）</h3>" in html
    assert "<h3>体温单（已列 500 / 共 501）</h3>" in html   # 修前恒为「（500）」


def test_桌面三块标题_列全时一字不变(client, admin, world):
    html = _desktop(client, admin, world["few"])
    for title in ("病程记录（3）", "护理记录（3）", "体温单（3）"):
        assert f"<h3>{title}</h3>" in html, title


_MOBILE = r"""
const ARGS = JSON.parse(process.argv[1]);
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const elements = {};
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (elements[sel] ||= { value: "", innerHTML: "", textContent: "", className: "" }); } };
/* 浏览器的 fetch：经管道转给真接口，响应头只垫 X-Total-Count（大小写不敏感，照 Headers.get） */
globalThis.fetch = async (path) => {
  process.stdout.write(JSON.stringify({ path }) + "\n");
  const reply = JSON.parse((await lines.next()).value);
  return { status: reply.status, ok: reply.status < 400, json: async () => reply.body,
    headers: { get: (name) => (name.toLowerCase() === "x-total-count" ? reply.total : null) } };
};
function token() { return ""; }
function csrfToken() { return ""; }
function logout() {}
let roundAdmissionId = ARGS.aid;
"""


def _mobile(client, admin, aid) -> dict:
    source = (STATIC / "m" / "doctor.js").read_text(encoding="utf-8")

    def top(head):
        start = source.index(head)
        if head.startswith(("function ", "async function ")):
            return source[start:source.index("\n}\n", start) + 3]
        return source[start:source.index(";\n", start) + 2]

    script = (_MOBILE + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n"
              + "".join(top(head) for head in ("async function api(", "function kv(", "function card(",
                                               "const NOTE_TYPE_NAMES = ", "let roundSeq = ",
                                               "async function refreshRoundDetail("))
              + "\n(async () => { await refreshRoundDetail();\n"
              + '  return { status: $("#round-status").innerHTML, notes: $("#round-notes").innerHTML }; })()'
              + ".then((r) => { process.stdout.write(JSON.stringify({ result: r }) + '\\n'); rl.close(); },"
              + " (e) => { console.error(e); process.exit(1); });\n")
    proc = subprocess.Popen(["node", "-e", script, json.dumps({"aid": aid})], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "result" in message:
                return message["result"]
            resp = client.get(message["path"], headers=admin)
            reply = {"status": resp.status_code, "body": resp.json(), "total": resp.headers.get("x-total-count")}
            proc.stdin.write(json.dumps(reply, ensure_ascii=False) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=60)


def test_移动端查房_列不全写已列与共几条(client, admin, world):
    out = _mobile(client, admin, world["many"])
    assert "已列 100 / 共 101 条" in out["status"] and "已列 500 / 共 501 条" in out["status"], out["status"]
    assert '<div class="sec-title">病程记录（已列 100 / 共 101）</div>' in out["notes"]   # 修前「病程记录（100）」


def test_移动端查房_列全时一字不变(client, admin, world):
    out = _mobile(client, admin, world["few"])
    assert "<span>3 条</span>" in out["status"] and "已列" not in out["status"], out["status"]
    assert '<div class="sec-title">病程记录（3）</div>' in out["notes"]
