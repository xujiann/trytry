"""住院记录表里已出院的行也摆「病案首页」：HIS 推来的出院没有首页，页面上补录得了（P2-1534，第四十五批扫描 AI4-3 的 clear 那半）。

修前：住院页（`pages-clinical.js::renderInpatient`）「住院记录」表的「病案首页」按钮只给在院行，已出院行的操作列是「—」。
后端建首页不看住院状态（`inpatient.create_case_summary`，出院后照收 201、照样入组），可平台出院要先有首页，经页面出院的
行都有；没有首页的出院只来自 HIS 推来的 ADT^A03（A03 是既成事实的镜像，不设首页门禁，docstring 写着「质量门禁由 HIS 端与
病案补录流程承担」）与存量导入——这些行页面上永远补录不了。修前实测（node 渲染页面）：A03 出院的那一行操作列只有「—」、
医嘱单与打印按钮，没有 `data-summary` 按钮。

修法：已出院的行也摆「病案首页」，复用在院行那一套处理：先取，取到了给只读首页，404「未填写」才弹填写表单（P2-617）。
住院行出参没有「有没有首页」的标志，为一个按钮文案多发请求不值，故已出院的与在院的统一写「病案首页」。DRG 统计的「出院
病例」分母算不算没首页的出院要业务拍板，不在本条。

页面函数原样拿到 node 里跑（写法照 test_org_group_page_edit 的 `_HARNESS`），`api` 经管道转给真接口；`spdModal` 换成桩：
记下框里的字段，按用例给的值交回（与 spdModal 一样去首尾空白），给了 `submit` 的照 spdModal 由框自己提交。
"""
import json
import os
import re
import shutil
import subprocess

import pytest

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")


def _read(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as fh:
        return fh.read()


def _top_level(source: str, head: str) -> str:
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


#: 页面取数换成桩：`$()` 按选择器给一个记 innerHTML 的假元素；`api()` 经标准输出把请求交给测试进程、从标准输入读回真接口的
#: 状态码与响应（失败照 core.js 的 `api` 抛 `errorText` 的话、带上 `status`）；`spdModal()` 记下标题、说明与字段，按 `args.answer`
#: 交回（None 即点了取消），给了 `opts.submit` 的由框自己提交、提交失败记下报错；`setMsg()` / `route()` 记下调用
_HARNESS = r"""
const args = JSON.parse(process.argv[1]);
const els = {};
const mk = () => ({ textContent: "", innerHTML: "", value: "", className: "", dataset: {},
  classList: { add() {}, remove() {} } });
globalThis.document = { addEventListener() {}, cookie: "", querySelector(sel) { return (els[sel] ||= mk()); } };
globalThis.localStorage = { getItem() { return null; }, setItem() {} };
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const calls = [];
async function api(path, opts = {}) {
  const method = opts.method || "GET";
  const body = opts.body ? JSON.parse(opts.body) : null;
  calls.push([method, path, body]);
  process.stdout.write(JSON.stringify({ req: { method, path, body } }) + "\n");
  const resp = JSON.parse((await lines.next()).value);
  if (resp.status >= 400) {
    const err = new Error(errorText(resp.body.detail, `请求失败(${resp.status})`));
    err.status = resp.status;
    throw err;
  }
  return resp.body;
}
let routed = 0;
function route() { routed += 1; }
const msgs = [];
function setMsg(sel, text, ok = true) { msgs.push([sel, text, ok]); }
function formJson() { return {}; }
async function postAction() {}
async function openPrintPage() {}
let modal = null;
async function spdModal(title, fields, opts = {}) {
  modal = { title, intro: opts.intro || "", fields: fields.map((f) => ({ ...f })), submit: !!opts.submit, error: "" };
  if (args.answer === null) return null;
  const out = {};
  for (const f of fields) {
    const raw = String(f.name in args.answer ? args.answer[f.name] : (f.value ?? "")).trim();
    out[f.name] = f.type === "number" ? Number(raw || 0) : raw;
  }
  if (!opts.submit) return out;
  try {
    const result = await opts.submit(out);
    return result === undefined ? true : result;
  } catch (err) {
    modal.error = err.message;   // 真框留着、报错写在框里；桩记下后当作点了取消
    return null;
  }
}
"""

_RUN = r"""
(async () => {
  await renderInpatient();
  const body = els["#page-body"].innerHTML;
  // 只点页面上真摆出来的按钮：修前已出院行没有这个按钮，不能绕过页面直接调处理函数
  const button = `data-summary="${args.aid}"`;
  if (args.click && body.includes(button)) {
    await els["#page-body"].onclick({ target: { dataset: { summary: String(args.aid) } } });
  }
  process.stdout.write(JSON.stringify({ result: { body, modal, calls, msgs, routed } }) + "\n");
  rl.close();
})().catch((err) => {
  process.stdout.write(JSON.stringify({ error: String((err && err.stack) || err) }) + "\n");
  rl.close();
});
"""


def _run_page(client, headers, aid: int, click: bool = False, answer: dict | None = None) -> dict:
    """渲染住院页；`click` 时点住院 `aid` 那一行的「病案首页」（页面上摆了才点），框里按 `answer` 填（None 即点了取消）。"""
    core, page = _read("core.js"), _read("pages-clinical.js")
    script = (_HARNESS + _read("shared.js")
              + "".join(_top_level(core, head) for head in ("function table(", "function panel("))
              + _top_level(page, "async function renderInpatient(") + _RUN)
    proc = subprocess.Popen(["node", "-e", script, json.dumps({"aid": aid, "click": click, "answer": answer},
                                                             ensure_ascii=False)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        while True:
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "result" in message:
                return message["result"]
            assert "error" not in message, message["error"]
            req = message["req"]
            resp = client.request(req["method"], req["path"], headers=headers, json=req["body"])
            proc.stdin.write(json.dumps({"status": resp.status_code, "body": resp.json()}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def _row(body: str, aid: int) -> str:
    """住院记录表里住院 `aid` 那一行（按「医嘱单」按钮认：在院、出院的行都有它）。"""
    at = body.index(f'data-orders="{aid}"')
    return body[body.rindex("<tr>", 0, at):body.index("</tr>", at)]


def _writes(out: dict) -> list:
    return [call for call in out["calls"] if call[0] != "GET"]


def _adt(event: str, control_id: str, id_card: str) -> str:
    return "\r".join([f"MSH|^~\\&|HIS|XZYY|MEDPLAT|COUNTY|20261005090000||{event}|{control_id}|P|2.4",
                      f"PID|1||{id_card}^^^CN^ID||P21534 患者||19660606|M"])


@pytest.fixture(scope="module")
def world(client, admin):
    """在院一例（没填首页）；HIS 推 A03 出院一例（没有首页）；平台出院一例（填过首页才出得了院）。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21534 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P21534 内科病区"}).json()["id"]
    out = {}
    for i, key in enumerate(("in_stay", "his", "platform")):
        bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": f"0{i + 1}"}).json()["id"]
        id_card = f"33010619660606{1534 + i:04d}"
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P21534 患者{key}", "id_card": id_card, "gender": "男", "birth_date": "1966-06-06"}).json()["id"]
        adm = client.post("/api/inpatient/admissions", headers=admin, json={
            "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "胸痛待查"})
        assert adm.status_code == 201, adm.text
        out[key] = adm.json()["id"]
        if key == "his":   # HIS 端办的出院：A03 镜像同步，不设首页门禁
            resp = client.post("/api/integration/hl7v2/adt", headers=admin,
                               json={"message": _adt("ADT^A03", "P21534A03", id_card)})
            assert resp.status_code == 201, resp.text
        if key == "platform":
            summary = client.post(f"/api/inpatient/admissions/{out[key]}/case-summary", headers=admin, json={
                "discharge_diagnosis": "社区获得性肺炎", "total_cost": 5000, "drug_cost": 1200, "outcome": "治愈"})
            assert summary.status_code == 201, summary.text
            assert client.post(f"/api/inpatient/admissions/{out[key]}/discharge", headers=admin).status_code == 200
    return out


def test_前提_HIS出院的没有首页_后端出院后照收首页(client, admin, world):
    rows = {a["id"]: a for a in client.get("/api/inpatient/admissions", headers=admin).json()}
    assert rows[world["his"]]["status"] == "discharged" and rows[world["platform"]]["status"] == "discharged"
    assert rows[world["in_stay"]]["status"] == "admitted"
    resp = client.get(f"/api/inpatient/admissions/{world['his']}/case-summary", headers=admin)
    assert (resp.status_code, resp.json()["detail"]) == (404, "病案首页未填写")


def test_已出院的行摆病案首页_在院行的按钮照旧(client, admin, world):
    out = _run_page(client, admin, world["his"])
    for key in ("his", "platform"):
        row = _row(out["body"], world[key])
        assert "已出院" in row, row
        # 修前已出院行操作列是「—」：没有这个按钮，HIS 推来的出院补录不了首页
        assert f'<button class="btn secondary" data-summary="{world[key]}">病案首页</button>' in row, row
        for gone in ("data-transfer", "data-order=", "data-discharge"):   # 转床、开医嘱、出院照旧只给在院行
            assert gone not in row, (gone, row)
    row = _row(out["body"], world["in_stay"])
    for button in (f'data-transfer="{world["in_stay"]}"', f'data-order="{world["in_stay"]}"',
                   f'data-summary="{world["in_stay"]}"', f'data-discharge="{world["in_stay"]}"'):
        assert button in row, (button, row)


def test_HIS出院没首页的_点病案首页弹填写表单_提交201入组(client, admin, world):
    aid = world["his"]
    out = _run_page(client, admin, aid, click=True, answer={
        "discharge_diagnosis": "急性心肌梗死", "total_cost": "12000", "drug_cost": "3000.5", "outcome": "好转",
        "note": "P21534 病案补录"})
    assert f'data-summary="{aid}"' in _row(out["body"], aid)   # 修前没有按钮，下面的框也就打不开
    assert out["modal"] is not None, "点「病案首页」没有打开填写表单"
    assert out["modal"]["title"] == f"病案首页（住院 {aid}）" and out["modal"]["submit"] is True
    assert [f["name"] for f in out["modal"]["fields"]] == [
        "discharge_diagnosis", "operation", "total_cost", "drug_cost", "outcome", "note"]
    assert out["modal"]["error"] == ""
    # 先取（404「未填写」），再由框提交
    assert [c[:2] for c in out["calls"] if c[1].endswith("/case-summary")] == [
        ["GET", f"/api/inpatient/admissions/{aid}/case-summary"],
        ["POST", f"/api/inpatient/admissions/{aid}/case-summary"]]
    assert _writes(out) == [["POST", f"/api/inpatient/admissions/{aid}/case-summary", {
        "discharge_diagnosis": "急性心肌梗死", "operation": "", "total_cost": 12000, "drug_cost": 3000.5,
        "outcome": "好转", "note": "P21534 病案补录"}]]
    assert out["routed"] == 1
    saved = client.get(f"/api/inpatient/admissions/{aid}/case-summary", headers=admin)
    assert saved.status_code == 200, saved.text   # 落库了：出院后补录照收（后端不看住院状态）
    saved = saved.json()
    assert (saved["discharge_diagnosis"], saved["total_cost"], saved["outcome"]) == ("急性心肌梗死", 12000, "好转")
    top = client.post("/api/drgs/pre-check", headers=admin, json={"diagnosis": "急性心肌梗死"}).json()["candidates"][0]
    assert (saved["drg_code"], saved["drg_weight"]) == (top["code"], top["base_weight"])   # 入了正式分组，不是 QY 兜底
    assert saved["drg_code"] == "FM19"


def test_已出院且已填首页的_点了给只读首页_不弹填写表单(client, admin, world):
    aid = world["platform"]
    out = _run_page(client, admin, aid, click=True, answer={})
    assert f'data-summary="{aid}"' in _row(out["body"], aid)
    assert out["modal"] is not None, "点「病案首页」什么也没打开"
    assert "已填写" in out["modal"]["title"] and out["modal"]["fields"] == []   # 只读：没有可填的字段
    assert re.search(r"出院诊断：社区获得性肺炎", out["modal"]["intro"]), out["modal"]["intro"]
    assert _writes(out) == [] and out["routed"] == 0
