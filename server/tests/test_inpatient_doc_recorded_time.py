"""病程、住院护理的表单没有「记录时间」：补记一律记成点按钮那一刻（P2-1767，第五十二批扫描 AP3-3）。

`clinical_docs.ProgressNoteIn` / `NursingIn` 一直收 `recorded_at`（不填取此刻的本地时间，P2-455），模型注释写着「与创建时刻
分开：补记时二者不同」；桌面「住院临床文书」的病程、护理两张表单与医生移动端查房的病程表单却都只送类型 / 级别与内容。修前实测：
照页面不送 `recorded_at`，写一条「02:10 患者血压下降…」的抢救记录 → 201，`"recorded_at":"2026-10-09 18:02"`——抢救记录与夜班
事后补的护理，记录时间都成了录入那一刻；P2-1324 待裁定的「落库晚于记录时刻 N 小时标补记」对界面写的记录永远不会触发。兄弟表单
早有「留空按此刻」的时刻框：门急诊处置（P2-1633）、医嘱执行（P2-1694）。

修法：三张表单加「记录时间（留空按此刻）」日期时间框。桌面经 `formJson` 交（空值跳过、`T` 换成空格）；移动端照同页体征测量时刻
的写法换 `T`、留空不送，提交成功连同内容一起清空。记录时间的上下界随 P2-1135 / P2-1324 定，不在本条。

页面函数原样拿到 node 里跑：桌面见 `clinical_docs_page.py`（只交表单里真有的栏）；移动端取 `m/doctor.js` 里病程表单那段提交监听
的原文。请求都转给真接口，再按接口读回。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import clinical_docs_page
from jssrc import strip_comments

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
MGMT = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
DOCTOR_JS = (STATIC / "m" / "doctor.js").read_text(encoding="utf-8")
DOCTOR_HTML = (STATIC / "m" / "doctor.html").read_text(encoding="utf-8")

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")

#: 时刻框的写法与门急诊处置（P2-1633）同一句
RECORDED_AT_FIELD = '<label style="font-size:13px">记录时间（留空按此刻） <input name="recorded_at" type="datetime-local"></label>'


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21767 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P21767 内科病区"}).json()["id"]
    admissions = []
    for i in range(2):
        bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": f"R{i}"}).json()["id"]
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P21767 患者{i}", "id_card": f"33010619760707{1767 + i:04d}"}).json()["id"]
        created = client.post("/api/inpatient/admissions", headers=admin, json={
            "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "感染性休克"})
        assert created.status_code == 201, created.text
        admissions.append(created.json()["id"])
    return {"desktop": admissions[0], "mobile": admissions[1]}


def _form(page_source: str, form_id: str) -> str:
    start = page_source.index(f'<form class="inline" id="{form_id}">')
    return page_source[start:page_source.index("</form>", start)]


def _notes(client, admin, aid):
    return client.get(f"/api/inpatient/admissions/{aid}/progress-notes", headers=admin).json()


def _nursing(client, admin, aid):
    return client.get(f"/api/inpatient/admissions/{aid}/nursing-records", headers=admin).json()


# ---------------------------------------------------------------- 表单形状


def test_桌面病程与护理表单都有记录时间框_可以留空():
    for form_id in ("note-form", "nursing-form"):
        form = _form(MGMT, form_id)
        assert RECORDED_AT_FIELD in form, form_id   # 修前没有
        assert "required" not in form[form.index('name="recorded_at"'):].split(">")[0], form_id   # 留空按此刻


def test_移动端查房病程表单有记录时间框_留空不送_交完清空():
    form = DOCTOR_HTML[DOCTOR_HTML.index('<form id="round-note"'):]
    form = form[:form.index("</form>")]
    assert re.search(r'<input id="round-at" type="datetime-local">', form), form   # 修前没有，也不是必填
    code = strip_comments(DOCTOR_JS)
    handler = code[code.index('$("#round-note").addEventListener("submit"'):]
    handler = handler[:handler.index("\n});\n")]
    assert 'const recordedAt = $("#round-at").value.trim().replace("T", " ");' in handler
    assert "if (recordedAt) body.recorded_at = recordedAt;" in handler   # 留空不送
    assert handler.index("JSON.stringify(body)") > handler.index("body.recorded_at")
    assert '$("#round-at").value = "";' in handler   # 交完连同内容清空


# ---------------------------------------------------------------- 桌面：照页面交，按接口读回


@needs_node
def test_桌面病程与护理填0210_读回0210(client, admin, world):
    aid = world["desktop"]
    steps = """
      await renderClinicalDocs();
      await submitForm("note-form", { note_type: "rescue", content: "P21767 患者血压下降，予多巴胺静滴",
                                      recorded_at: "2026-10-09T02:10" });
      await submitForm("nursing-form", { content: "P21767 夜班巡视，血压回升", recorded_at: "2026-10-09T03:40" });
      return { posts, routed: ROUTED, msg: msgOf("#doc-msg") };
    """
    result, _ = clinical_docs_page.run(client, admin, steps, storage={"medplat_doc_adm": str(aid)})
    assert result["msg"] == "" and result["routed"] == 2, result
    bodies = {path.rsplit("/", 1)[1]: body for _, path, body in result["posts"]}
    assert bodies["progress-notes"]["recorded_at"] == "2026-10-09 02:10"   # 控件的 `T` 换成空格再送（P1-100）
    assert bodies["nursing-records"]["recorded_at"] == "2026-10-09 03:40"
    note = next(n for n in _notes(client, admin, aid) if n["content"].startswith("P21767 患者血压下降"))
    assert note["recorded_at"] == "2026-10-09 02:10"   # 修前是点按钮那一刻
    nursing = next(r for r in _nursing(client, admin, aid) if r["content"] == "P21767 夜班巡视，血压回升")
    assert nursing["recorded_at"] == "2026-10-09 03:40"


@needs_node
def test_桌面留空不送_按此刻落(client, admin, world):
    aid = world["desktop"]
    steps = """
      await renderClinicalDocs();
      await submitForm("note-form", { content: "P21767 当场写的日常病程" });
      return { posts };
    """
    result, _ = clinical_docs_page.run(client, admin, steps, storage={"medplat_doc_adm": str(aid)})
    (_, _, body), = result["posts"]
    assert "recorded_at" not in body, body   # 留空不送：空串会落成空的记录时间
    note = next(n for n in _notes(client, admin, aid) if n["content"] == "P21767 当场写的日常病程")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", note["recorded_at"]) and note["recorded_at"] != "2026-10-09 02:10"


# ---------------------------------------------------------------- 移动端：原样跑提交监听


_MOBILE_HARNESS = r"""
const ARGS = JSON.parse(process.argv[1]);
const elements = {};
const posts = [];
let REFRESHED = 0;
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (elements[sel] ||= { value: "", textContent: "", className: "", listeners: {},
    addEventListener(type, fn) { this.listeners[type] = fn; } }); } };
async function api(path, options = {}) {
  const body = options.body ? JSON.parse(options.body) : null;
  posts.push([options.method || "GET", path, body]);
  process.stdout.write(JSON.stringify({ method: options.method || "GET", path, body }) + "\n");
  const reply = JSON.parse((await lines.next()).value);
  if (reply.status >= 400) throw new Error(errorText(reply.body.detail, `请求失败(${reply.status})`));
  return reply.body;
}
async function refreshRoundDetail() { REFRESHED += 1; }
let roundAdmissionId = ARGS.aid;
let roundAdmissions = [];   // 在院清单这里不取：回执按住院号写明是谁（P2-1798）
"""


def _mobile_submit(client, admin, aid: int, values: dict) -> dict:
    """把 `m/doctor.js` 里病程表单的提交监听原样跑一次：`values` 是 {选择器: 值}，请求转给真接口。"""
    start = DOCTOR_JS.index('$("#round-note").addEventListener("submit"')
    listener = DOCTOR_JS[start:DOCTOR_JS.index("\n});\n", start) + 5]
    set_msg = DOCTOR_JS[DOCTOR_JS.index("function setMsg("):]
    set_msg = set_msg[:set_msg.index("\n}\n") + 3]
    who = DOCTOR_JS[DOCTOR_JS.index("function roundWho("):]
    who = who[:who.index("\n}\n") + 3]
    run = ("(async () => {\n"
           "  for (const [sel, value] of Object.entries(ARGS.values)) $(sel).value = value;\n"
           '  await elements["#round-note"].listeners.submit({ preventDefault() {} });\n'
           '  return { posts, at: $("#round-at").value, content: $("#round-content").value,\n'
           '           msg: $("#round-msg").textContent, refreshed: REFRESHED };\n'
           "})().then((r) => { process.stdout.write(JSON.stringify({ result: r }) + '\\n'); rl.close(); },\n"
           "          (e) => { console.error(e); process.exit(1); });\n")
    script = _MOBILE_HARNESS + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n" + set_msg + who + listener + run
    proc = subprocess.Popen(["node", "-e", script, json.dumps({"aid": aid, "values": values}, ensure_ascii=False)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "result" in message:
                return message["result"]
            resp = client.request(message["method"], message["path"], json=message["body"], headers=admin)
            proc.stdin.write(json.dumps({"status": resp.status_code, "body": resp.json()}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


@needs_node
def test_移动端查房病程填0210_读回0210_交完清空(client, admin, world):
    aid = world["mobile"]
    result = _mobile_submit(client, admin, aid, {
        "#round-note-type": "rescue", "#round-content": "P21767 查房补记：患者血压下降", "#round-at": "2026-10-09T02:10"})
    assert result["msg"] == f"病程已记录（住院号 {aid}）" and result["refreshed"] == 1, result   # 回执写明是谁（P2-1798）
    (_, _, body), = result["posts"]
    assert body == {"note_type": "rescue", "content": "P21767 查房补记：患者血压下降", "recorded_at": "2026-10-09 02:10"}
    assert result["at"] == "" and result["content"] == ""   # 记录时间连同内容清空，不留给下一条
    note = next(n for n in _notes(client, admin, aid) if n["content"] == "P21767 查房补记：患者血压下降")
    assert note["recorded_at"] == "2026-10-09 02:10"   # 修前是提交那一刻


@needs_node
def test_移动端留空不送(client, admin, world):
    aid = world["mobile"]
    result = _mobile_submit(client, admin, aid, {
        "#round-note-type": "daily", "#round-content": "P21767 查房当场写", "#round-at": ""})
    (_, _, body), = result["posts"]
    assert body == {"note_type": "daily", "content": "P21767 查房当场写"}   # 不带 recorded_at：后端按此刻
